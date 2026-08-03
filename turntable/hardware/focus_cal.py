"""
Measuring a lens's usable focus travel.

TWO METHODS LIVE HERE. `measure_travel_by_stops` is what the Calibrate button
uses; `measure_travel` is the older frame-based one, kept working as a
fallback. Both obey the rule below. What separates them is the UNITS they
measure in:

  * by_stops works in DRIVER UNITS — the currency MfDrive takes and the same
    currency `focus.drive_focus` spends (`steps * STEP_UNITS["Small"]`). An app
    step IS 10 driver units by definition, so there is no scale to estimate and
    nothing for an estimate to get wrong. It costs no frames at all.
  * measure_travel works in NEF ENCODER COUNTS, read out of a captured RAW, and
    must derive a counts-per-step scale per run to convert them.

RUN SIDE BY SIDE ON THE Z 6_2, 2026-07-31, same lens, same session:

    by_stops    1556 driver units -> 155 steps.  3.3-8.8 s, no shutter.
                7 runs across chunk sizes 50/100/200 returned 1556 EVERY time —
                not "within tolerance", the identical integer.
    frame       1556 NEF counts   -> 152 steps.  24.9 s, 8 frames, 276 MB.
                Near stop -1428, far stop +128. Derived scale: exactly 10.00.

So the two rulers turn out to BE THE SAME RULER: one NEF encoder count is one
driver unit on this body, and the frame-based path's own estimate of "counts
per step" landed on 10.00, which is STEP_UNITS["Small"] to the digit. They
agree on the measurement to the unit. The whole 155-vs-152 gap is the
frame-based path's deliberate max(3.0, 2%) pad: 1556/10.00 = 155.6, less 3.11,
rounds to 152.

That also retires the old worry that counts-per-step was not constant (10.0 and
11.67 had both been recorded on this body). Those readings predate the current
transport, which confirms every move; a chunk that quietly lost a command
measures low, which is exactly the shape of that scatter. All seven 25-step
chunks above moved exactly 250 counts. It was the transport, not the ruler.

The stop-based path is therefore not preferred because the NEF ruler is
untrustworthy — it is not. It is preferred because it is ~8x faster, fires no
shutter, needs neither exifread nor RAW-reaching-the-PC, and gives back the 3
steps of range the pad was costing.

A LiveView sharpness sweep over those 1556 units found the lens still
responding optically at 1500, so the travel is real end to end rather than a
driver counting units the optics no longer answer.

THE RULE BOTH ARE BUILT ON: never report the travel by counting the commands
you sent. Read it off the hardware. For by_stops that is MfDriveStepEnd; for
the frame-based one it is two fields in the decrypted Nikon LensData block:

  * byte 0x56 flags a mechanical limit: 1 at the near stop, 2 at the far one.
    So "has it stopped" is answered by the body itself rather than inferred
    from two frames looking similar.
  * the int32 at 0x5A is the position. The travel is the DIFFERENCE between
    the two stops in those counts.

Counting commands instead is biased in exactly one direction — the lens can
never travel FURTHER than commanded, only less — so a command-counted travel
is always too big. That is what puts B past the end of the lens while the
app's own display shows it comfortably inside the stops, after which the last
frames of every stack are duplicates and nothing notices.

Only the counts-to-steps SCALE still has to be estimated from commanded
motion, and the estimator is chosen to lean the safe way (see `scale` below).

LIVEVIEW: neither routine has to arrange one. Focus calls go through
`PTPCameraClient._ensure_for_focus`, which raises a session before every one of
them unless a run has asked it not to via
`PTPCameraClient.hold_liveview_closed`. Calibration is driven from the focus
panel — unprompted at connect, or from the Calibrate button — and never from
inside a run, so that hold is off here and a session is up whether the caller
thought about it or not.

This paragraph used to say the caller must establish the session first and that
it was "not optional — focus commands are inert without one". That cause is
false, measured on the Z 6_2 on 2026-08-03: with LiveView stopped, MfDrive was
ACCEPTED and the lens moved — a commanded +500 units answered 0x2001 RC_OK, and
the sweep that recovered it reported the same 400 units as the identical trial
run with LiveView up. Focus is not inert without LiveView on this body. The
0xA00B NIKON_NotLiveView handling in `ptp.NikonCamera.drive` stays regardless,
because a body that DOES refuse should say so rather than pretend the move
happened — it is just not this one, and this one is the only one measured.

The correction then left CAPTURE open, and said the frame-based path below
claimed nothing either way. That is closed now: same body, same day, three
shutter actuations. Two consecutive frames with LiveView DOWN landed as real
NEFs, 31.6 MB, ~1.1 s each, against a LiveView-UP control of 31.7 MB in 1.1 s,
and LiveViewProhibitCondition (0xD1A4) read 0x00000000 after every one of them
— bit 12, "pending unretrieved SDRAM image", never set, nothing left stuck in
the buffer. Two in a row on purpose, because the InvalidObjectHandle failure
this repo chased surfaces on the frame AFTER the bad one. So the frame-based
path's probe frames do not depend on the session either, and on this body the
LiveView question is settled in both halves: nothing calibration does, and
nothing a run does, needs LiveView. Suppressing it during a run is implemented
rather than debated — see `PTPCameraClient.hold_liveview_closed`. The Z 6_2 is
still the only body measured, so none of this generalises to other Nikons.

The consequence the old paragraph warned about is worth keeping, because it
never depended on LiveView: a routine that cannot move the lens "measures" a
travel of about zero, and a travel of zero comes back looking exactly like a
number. A
lens left in MF, or a focus motor that is not answering, gets you there just as
well as a refused command would. Both paths refuse to report it rather than
return it — by_stops raises on a gap between the stops of less than one step and
names the AF switch, and the frame-based one raises both on never reaching the
far stop and on having no usable scale samples. Those checks are the guard; do
not quietly relax one into a fallback figure.

The frame-based path calls `hide_liveview()` at the top and its docstring used
to claim live view then stays down for the rest of it. On the PTP transport it
does not: every focus command goes through
`PTPCameraClient._ensure_for_focus()`, which re-opens it before the very next
move. Verified on the Z 6_2 — `_liveview_on` is True both before and after a
full frame-based run. Harmless (the measurement works either way), but do not
rely on the hide. A hide only sticks
when something holds it closed, and the only thing that does is a run setting
`PTPCameraClient.hold_liveview_closed`; nothing here sets it, so nothing here
gets the hide it looks like it is asking for.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from . import lensdata
from .errors import CameraError
from .focus import STEP_UNITS, drive_focus

#: Which method the Calibrate button uses. Flip to False and restart to go back
#: to the frame-based one; nothing else needs changing, both are live and both
#: return app steps.
#:
#: Kept as a source-level switch rather than a UI control on purpose. The two
#: are not a user preference — one of them is right and the other is a fallback
#: for a body where MfDriveStepEnd turns out not to behave as it does on the
#: Z 6_2. If that body ever turns up, this is the line to change.
USE_STOP_BASED = True

#: Driver units per chunk on the first (bracketing) pass of the stop-based
#: measurement. Pure time/accuracy-free tradeoff: pass 1 costs travel/chunk
#: commands and pass 2 costs up to `chunk` single-unit ones, so the total is
#: worst around both extremes and best near sqrt(travel) ~= 39 here.
#:
#: Measured on the Z 6_2 at 1556 units — 50: 3.5 s, 100: 4.3-4.9 s, 200: 8.8 s,
#: and all three returned 1556. So this buys speed and cannot buy error.
STOP_CHUNK = 50

#: Steps per probe while walking out to the far stop. Every probe is a
#: shutter actuation, so this trades frames against how far past the stop the
#: final chunk can overshoot. It does not affect ACCURACY: the answer comes
#: from the ruler, not from this number.
COARSE_STEP = 25

#: Give up if the far stop has not been reached by here. Guards a lens that
#: is not really moving.
COARSE_MAX = 600

#: Plausible counts-per-step scale. Readings outside this band are rejected
#: before the estimator runs: a chunk that ran into the stop measures near zero.
#:
#: This band was wide because 10.0 and 11.67 had both been recorded on the same
#: body, which looked like the scale not being a constant. On PTP it is one:
#: measured 2026-07-31, every 25-step chunk of a full sweep moved exactly 250
#: counts, for a scale of 10.00 — i.e. one NEF count per driver unit, and
#: STEP_UNITS["Small"] per step. The old scatter was lost commands, so the band
#: is kept wide only to stay tolerant of a body that has not been measured
#: rather than because this one wanders.
SCALE_MIN, SCALE_MAX = 4.0, 30.0

#: Seconds to let the lens settle before a probe frame.
#:
#: The move itself is already confirmed by the camera before this runs, so
#: this is only mechanical settling. It is almost certainly longer than it
#: needs to be — it costs ~12 s of the minute calibration takes — but it has
#: never been the reason a calibration failed, so it is left alone until
#: someone measures the real figure.
SETTLE_S = 1.2

NEAR_STOP, FAR_STOP = 1, 2


class CalibrationError(RuntimeError):
    pass


def measure_travel_by_stops(
        camera,
        chunk: int = STOP_CHUNK,
        progress: Optional[Callable[[str], None]] = None,
        check_stop: Optional[Callable[[], bool]] = None) -> int:
    """
    Return the usable travel in steps, measured between the two mechanical
    stops over PTP. No frames, no shutter actuations, no exifread, and no
    requirement that photos reach the PC.

    Leaves the lens parked at the NEAR stop — like the frame-based version, and
    unlike `camera.measure_travel`, which finishes against the FAR one. The
    caller resets its position counter to that, so this is load-bearing rather
    than tidiness.

    Raises CalibrationError rather than returning a figure that was not fully
    measured.
    """
    def say(msg):
        if progress:
            progress(msg)

    def stopped():
        return bool(check_stop and check_stop())

    def park_near():
        say("Parking against the near stop...")
        camera.drive_to_stop(toward_far=False)

    say("Measuring between the two mechanical stops. No photos needed — the "
        "camera announces each stop as the lens reaches it.")
    t0 = time.monotonic()
    try:
        units = camera.measure_travel(chunk=chunk, check_stop=check_stop)
    except CameraError as e:
        try:
            park_near()
        except Exception:                                # noqa: BLE001
            pass        # the measurement's error is the one worth reporting
        raise CalibrationError(str(e)) from e

    # A cancel makes `camera.measure_travel` RETURN the distance covered so
    # far, and a partial sweep is indistinguishable from a real answer by
    # looking at it. So the cancel is caught here rather than trusted there.
    if stopped():
        try:
            park_near()
        except Exception:                                # noqa: BLE001
            pass
        raise CalibrationError("Cancelled.")

    step_units = max(1, STEP_UNITS["Small"])
    # Minus one unit, THEN floor. Two separate reasons, and neither is padding:
    #
    #   floor, because a part-step at the end is a step the lens has no room
    #   for. Rounding up would put B against the stop by design.
    #
    #   the -1, because the measurement is exact to within exactly one driver
    #   unit and no more. The creep loop counts the command that answers
    #   MfDriveStepEnd as having moved; measured on the Z 6_2, the body answers
    #   MfDriveStepEnd for a command issued into a stop it is ALREADY sitting
    #   on, so that final unit may have covered anything between 0 and 1.
    #   `units - 1` is therefore the distance the lens is guaranteed to have.
    #
    # Together they cost at most one step. That is the entire safety margin,
    # and it replaces the frame-based path's max(3.0, 2%) — which was covering
    # an ESTIMATED counts-per-step scale that could come out low and inflate
    # the travel. There is no estimate here, so there is nothing to pad.
    travel = max(0, (units - 1) // step_units)

    if travel <= 0:
        raise CalibrationError(
            f"The two stops came out {units} driver unit(s) apart, which is "
            f"less than one step. Check the lens is in autofocus (AF) and that "
            f"jogging focus by hand still moves it.")

    say(f"Travel: {travel} steps — {units} driver units between the stops at "
        f"{step_units} units per step, in {time.monotonic() - t0:.0f}s with no "
        f"photos taken.")

    # NOT best-effort. The caller is about to treat the near stop as position
    # 0, so a lens that is not actually there makes the whole figure a lie.
    try:
        park_near()
    except CameraError as e:
        raise CalibrationError(
            f"Measured {travel} steps, but could not park the lens back "
            f"against the near stop ({e}), so the position counter cannot be "
            f"trusted. Press Home before setting A.") from e
    return travel


def measure_travel(camera,
                   folder: Path,
                   coarse: int = COARSE_STEP,
                   coarse_max: int = COARSE_MAX,
                   progress: Optional[Callable[[str], None]] = None,
                   check_stop: Optional[Callable[[], bool]] = None) -> int:
    """
    Return the usable travel in steps, measured between the two stops.

    Leaves the lens parked at the near stop, so the caller's position counter
    should be reset to that afterwards.

    Raises CalibrationError if the camera will not give a readable position,
    which is the honest outcome on a body this cannot read and far better
    than a number that was never measured.
    """
    def say(msg):
        if progress:
            progress(msg)

    def stopped():
        return bool(check_stop and check_stop())

    shots = [0]

    try:
        camera.hide_liveview()
    except Exception:                                    # noqa: BLE001
        pass
    time.sleep(1.2)

    def probe():
        """One frame. Returns (counts, limit flag)."""
        if stopped():
            raise CalibrationError("Cancelled.")
        time.sleep(SETTLE_S)
        before = {p.name for p in folder.glob("*") if p.is_file()}
        camera.capture(autofocus=False)
        deadline = time.monotonic() + 30.0
        newest = None
        while time.monotonic() < deadline:
            if stopped():
                raise CalibrationError("Cancelled.")
            fresh = {p.name for p in folder.glob("*") if p.is_file()} - before
            if fresh:
                newest = folder / sorted(fresh)[-1]
                size = -1
                while size != newest.stat().st_size:
                    size = newest.stat().st_size
                    time.sleep(0.25)
                break
            time.sleep(0.25)
        if newest is None:
            raise CalibrationError(
                "The camera did not deliver a frame. Calibration needs the "
                "photos to reach the PC, so it cannot run in keep-on-camera "
                "mode.")
        got = lensdata.read_focus(newest)
        if got is None:
            raise CalibrationError(
                f"No readable lens position in {newest.name}. This needs a "
                f"Nikon RAW (NEF); the camera records the lens position in "
                f"its own metadata and nothing else reports it.")
        shots[0] += 1
        return got

    def move(direction: str, steps: int):
        drive_focus(camera, direction, steps, check_stop=check_stop)

    # ── the near stop ────────────────────────────────────────────────
    # Driven until the CAMERA reports the stop, not for a fixed blind sweep.
    say("Parking against the near stop...")
    camera.drive_to_stop(toward_far=False)
    time.sleep(1.0)
    near_counts, flag = probe()
    if flag != NEAR_STOP:
        raise CalibrationError(
            "The lens did not reach its near stop, so there is nothing to "
            "measure from. Check that the lens is in autofocus (AF) and that "
            "jogging focus by hand still moves it.")
    say(f"  Near stop confirmed by the camera at {near_counts:+.0f}.")

    # ── walk out until the body reports the far stop ─────────────────
    samples = []
    prev = (near_counts, 0)
    cum = 0
    far_counts = None
    while cum < coarse_max:
        move("Plus", coarse)
        cum += coarse
        counts, flag = probe()

        # Every chunk is also a scale measurement. One that lost a command,
        # or that ran into the stop, comes out low; the median ignores those.
        s = (counts - prev[0]) / float(cum - prev[1])
        if SCALE_MIN <= s <= SCALE_MAX:
            samples.append(s)
        prev = (counts, cum)

        if flag == FAR_STOP:
            far_counts = counts
            say(f"  Far stop reported by the camera at {counts:+.0f}, after "
                f"{cum} commanded step(s).")
            break
        say(f"  {cum}: still moving ({counts:+.0f}).")

    if far_counts is None:
        raise CalibrationError(
            f"The lens never reported reaching its far stop within "
            f"{coarse_max} steps. Either the travel is larger than this "
            f"expects, or focus commands are not moving the lens.")

    if not samples:
        raise CalibrationError(
            "Could not work out how far one step moves the lens. Focus "
            "commands are being accepted but the lens is barely moving.")
    # MAX, not median. Dropped commands are one-directional: a chunk that
    # lost one moved LESS than it was told to, so its counts-per-step ratio
    # comes out too SMALL. Never too large, because the lens cannot move
    # further than commanded. So the median of the chunks is dragged down by
    # every lossy one, and since travel = counts / scale, a scale that is too
    # small makes the travel too BIG. That is the same one-directional
    # inflation this rewrite set out to remove, sneaked back in through the
    # estimator: it reported 190 steps for a lens with about 115.
    #
    # The largest ratio is the chunk that lost the least, which is the one
    # closest to the truth. The counts have a measured noise floor of zero,
    # so there is no upward scatter for a max to latch onto, and SCALE_MAX
    # still catches a bad decrypt.
    scale = max(samples)

    raw_travel = abs(far_counts - near_counts) / scale
    # Report slightly LESS than measured, on purpose. Even the best chunk may
    # have lost a command, so the scale can still come out a little small and
    # the travel a little large. The two errors are not equally bad: a travel
    # under-reported by a few steps costs a few steps of range nobody will
    # miss, while one over-reported puts B past the end of the lens, and then
    # the last frames of every stack are duplicates jammed against the far
    # stop and the app's own display shows B comfortably inside. So take the
    # safe side of the uncertainty.
    travel = int(max(0, round(raw_travel - max(3.0, raw_travel * 0.02))))
    say(f"Travel: {travel} steps, measured between the two stops the camera "
        f"itself reported ({abs(far_counts - near_counts):.0f} counts at "
        f"{scale:.2f} per step, from {shots[0]} frames).")
    if cum > travel:
        # Not lost commands — every move is confirmed. The excess is simply
        # the last chunk running into the stop partway: we command a whole
        # `coarse` step and the lens has less room left than that.
        say(f"  Crossing it took {cum} commanded steps; the last chunk ran "
            f"into the stop {cum - travel} step(s) early. The travel above "
            f"comes from the lens, not from counting commands.")

    say("Parking against the near stop again...")
    camera.drive_to_stop(toward_far=False)
    return travel
