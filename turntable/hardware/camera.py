"""
The camera client the app talks to — raw PTP over USB.

TWO THINGS WORTH KNOWING BEFORE CHANGING ANYTHING HERE
------------------------------------------------------
1. **We own the filenames.** PTP has no notion of a session folder or a
   naming template, so `begin_stack` / `set_filename_template` record that
   state here and `capture()` renders the name and writes the bytes. This is
   not optional bookkeeping: a frame captured to the camera's buffer reports a
   PLACEHOLDER name — every one comes back `DSC_0000.NEF` — so without local
   naming each frame silently overwrites the last. Measured, not theorised:
   five frames of visibly different sizes left one file on disk.

2. **close() is survivable.** Recovery calls it to force a fresh connection,
   so anything that goes through `_ensure` re-opens the transport rather than
   failing. FOUR METHODS DO NOT: `hide_liveview` answers "OK",
   `get_liveview_jpeg` answers None, `zoom_levels` answers [] and `get_zoom`
   answers None the moment `_cam is None`, without reconnecting. For
   `hide_liveview` that is honest — a closed transport has no live view to
   hide. For the other three it is not: all three need the transport (both
   zoom calls read property 0xD1BD), and an empty answer is indistinguishable
   from a real one. A cache is what usually hides that: main_window records
   `zoom_levels()` once at connect in `self._zoom_levels`, and `_step_zoom`
   reads `self._zoom_levels or self._camera.zoom_levels()` — so after a
   recovery close the populated cache short-circuits the closed client.
   Where the connect-time query itself came back empty there is nothing to
   short-circuit, the closed client answers [] again, and the user is told
   "This camera offers no live view magnifier." The cache is the only thing
   standing between this and a visible misdiagnosis.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable, Optional

from .constants import CAMERA_UNKNOWN, SESSION_COUNTER_START
from .errors import CameraError
from . import ptp

#: `[Counter 4 digit]`. The token spelling is load-bearing in one direction
#: only: `set_filename_template` callers write it and this substitutes it, so a
#: template that misspells it silently produces the literal text in every
#: filename. It is NOT parsed back anywhere. This note used to claim "the resume
#: and audit code both key off the names this produces", which would make the
#: whole naming scheme unchangeable — and it is not true. Resume works from
#: `RunCheckpoint`, which carries the revolution and position as NUMBERS;
#: `count_images_in` counts by extension; and `stacking._renumber` swaps names
#: between files without reading them. Nothing in the app reads a counter, a
#: revolution or a position back out of a filename, which is what made the
#: naming free to change.
_COUNTER_TOKEN = re.compile(r"\[Counter\s+(\d+)\s+digit\]", re.IGNORECASE)


class PTPCameraClient:
    """
    The camera, as the run and UI code sees it — backed by raw PTP over USB.

    Raises `CameraError` rather than letting `ptp.PTPError` escape on the
    calls it wraps, so most callers have one exception type to catch. THE
    PROMISE IS NOT WHOLE, and the gap is worth knowing before writing a bare
    `except CameraError`. The transport is fetched one line ABOVE the try in
    `set_zoom`, `drive_units`, `drive_to_stop` and `measure_travel`, and that
    fetch can start LiveView, which raises a raw `ptp.PTPError` — so a body
    that refuses StartLiveView leaks that type out of all four. The routing is
    no longer uniform: `set_zoom` calls `_ensure_liveview` directly and always
    can leak this way, while the focus three call `_ensure_for_focus`, which is
    `_ensure_liveview` unless a run has set `hold_liveview_closed`. Under that
    hold they take the plain `_ensure()` path, never issue StartLiveView, and
    so cannot leak here at all. That is a narrowing, not a fix: the hold is off
    everywhere except inside a run, and everything below happens outside one.
    `get_liveview_jpeg`, `zoom_levels` and `get_zoom` have no try at all.
    And every conversion below is an `except ptp.PTPError`, so the raw
    `usb.core.USBError` a stalled or timed-out transfer produces passes
    through all of them untouched.

    That has teeth. `focus_cal.measure_travel_by_stops` catches only
    `CameraError`, both around the measurement and around the park after it,
    so a LiveView failure walks straight past both. During
    `camera.measure_travel` it skips the best-effort `park_near()` in that
    function's `except CameraError` and leaves the lens wherever the sweep
    stopped. At the closing park — the one that function calls "NOT
    best-effort" — `_ensure_liveview()` raises before `drive_to_stop` is
    issued (reached through `_ensure_for_focus`: calibration is driven from
    the focus panel at connect, never inside a run, so the hold is off and
    LiveView is started as it always was), so the lens is never driven back
    off the FAR stop the measurement ends against, and the user gets a bare
    PTPError string instead of that function's "could not park the lens back
    against the near stop ... Press Home before setting A". In both cases only
    `focus_panel._calibrate`'s catch-all `except Exception` keeps it from
    being a crash: it lands as a calibration FAILURE, so `_on_cal_done`
    never runs and the panel's position counter is left holding a
    pre-calibration value the lens no longer matches.
    """

    #: Per-transfer budget for ordinary commands, in seconds. NOT the time a
    #: whole operation may take — `wait_for_object` and `wait_ready` have their
    #: own deadlines. It only bounds how long the camera may go completely
    #: silent, so it wants to be short: at 30 s a single wedged transfer cost
    #: 30 s to notice, another 30 s in close(), and 30 s more per health probe,
    #: which is why a hiccuped capture took 90 seconds to be reported.
    COMMAND_TIMEOUT_S = 6.0

    def __init__(self, timeout_s: float = 30.0):
        self._cam: Optional[ptp.NikonCamera] = None
        self._timeout_s = timeout_s
        # Serialises open. Two threads (the run worker and the live-view feed)
        # both finding `_cam is None` would each build a transport and each
        # claim the interface; the loser gets ERROR_ACCESS and the winner's
        # handle is orphaned, which is unrecoverable without a device reset.
        self._open_lock = threading.Lock()
        # Set by a run that has hidden LiveView; see
        # hold_liveview_closed. Off means focus commands re-open it.
        self._lv_hold_closed = False
        self._trace_fn: Optional[Callable[[str], None]] = None

        # Where captures land and what they are called. PTP has no notion of
        # either, so we keep it.
        self._folder: Optional[Path] = None
        self._template = "[Counter 4 digit]"
        self._counter = SESSION_COUNTER_START
        self._keep_on_camera = False

    # ── connection ───────────────────────────────────────────────────────
    def _ensure(self) -> ptp.NikonCamera:
        cam = self._cam
        if cam is not None:
            return cam
        with self._open_lock:
            if self._cam is not None:           # another thread won the race
                return self._cam
            cam = ptp.NikonCamera(
                ptp.PTPTransport(int(self.COMMAND_TIMEOUT_S * 1000)))
            try:
                cam.connect()
            except ptp.PTPError as e:
                raise CameraError(str(e)) from e
            if self._trace_fn:
                cam.set_trace(self._trace_fn)
            self._cam = cam
            return cam

    def close(self):
        cam, self._cam = self._cam, None
        if cam is None:
            return
        try:
            cam.close()
        except Exception:                                    # noqa: BLE001
            pass

    def set_trace(self, fn: Optional[Callable[[str], None]]):
        self._trace_fn = fn
        if self._cam is not None:
            self._cam.set_trace(fn)

    def verify_connection(self) -> str:
        try:
            cam = self._ensure()
        except CameraError:
            raise
        model = cam.model
        return model if model and model != "unknown" else CAMERA_UNKNOWN

    def camera_status(self) -> str:
        """'connected' or 'nocamera'."""
        try:
            cam = self._ensure()
            cam.wait_ready(timeout_s=5.0)
            return "connected"
        except Exception:                                    # noqa: BLE001
            pass

        # Not answering does not mean it is gone. The overwhelmingly common
        # cause is a transfer that timed out and left the pipe mid-container,
        # which no amount of waiting fixes but a stream reset does in
        # milliseconds. Try that before telling the user to check their cable.
        cam = self._cam
        if cam is not None:
            try:
                cam.recover()
                cam.wait_ready(timeout_s=5.0)
                return "connected"
            except Exception:                                # noqa: BLE001
                pass

        self.close()            # genuinely gone — next attempt re-enumerates
        return "nocamera"

    # ── session state ────────────────────────────────────────────────────
    def set_filename_template(self, template: str) -> str:
        self._template = template or "[Counter 4 digit]"
        return self._template

    def begin_stack(self, folder: Path,
                    counter: int = SESSION_COUNTER_START) -> None:
        """Point captures at `folder` and restart the frame counter."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self._folder = folder
        self._counter = int(counter)

    def _next_filename(self) -> str:
        """
        Render the template. The counter is bumped BEFORE use, so a stack
        begun at 0 produces 0001 as its first frame.
        """
        self._counter += 1
        counter = self._counter

        def sub(m: "re.Match") -> str:
            return str(counter).zfill(int(m.group(1)))

        name = _COUNTER_TOKEN.sub(sub, self._template)
        return name or f"{counter:04d}"

    # ── transfer mode ────────────────────────────────────────────────────
    def set_transfer_mode(self, keep_on_camera: bool) -> str:
        """
        Card-only versus straight to the PC.

        The PC path goes through the camera's buffer and touches no card at
        all, so a run with no card in the body still works.
        """
        self._keep_on_camera = bool(keep_on_camera)
        return "Save to camera only" if keep_on_camera else "Save to PC only"

    def get_transfer_mode(self) -> str:
        return "Save to camera only" if self._keep_on_camera else "Save to PC only"

    # ── capture ──────────────────────────────────────────────────────────
    def capture(self, autofocus: bool = False) -> str:
        """
        Fire one frame and, unless keeping it on the card, write it to the
        current session folder under the rendered filename.

        `autofocus` must stay False: refocusing before the shutter would walk
        the very focus plane the stack just positioned. It is a parameter
        rather than an assumption so that a caller asking for it gets a clear
        refusal instead of a silently ruined stack.
        """
        if autofocus:
            raise CameraError(
                "autofocus capture is not supported — it would move the focus "
                "plane the stack just set")
        cam = self._ensure()
        try:
            if self._keep_on_camera:
                cam.shoot(keep_on_camera=True, timeout_s=self._timeout_s)
                return "OK"

            if self._folder is None:
                raise CameraError("no session folder set — call begin_stack first")
            path = cam.shoot(dest_dir=self._folder,
                             filename=self._next_filename(),
                             timeout_s=self._timeout_s)
            return str(path) if path else "OK"
        except ptp.PTPError as e:
            raise CameraError(f"Capture failed: {e}") from e

    def capture_to(self, folder: Path, filename: str) -> str:
        """
        Fire ONE frame straight into `folder` under `filename`, and return the
        path written. For the single-shot button, not for a run.

        TOUCHES NO SESSION STATE, deliberately. `capture()` renders its name
        from `_folder`/`_template`/`_counter`, so reaching the same effect
        through it would mean `begin_stack` + `set_filename_template` — which
        re-points where captures land and resets the frame counter. A run
        re-establishes both per stack (`worker.run` calls `begin_stack` for
        every position), so that would probably survive; "probably" is not
        worth it for a convenience button, and a single shot taken between two
        Recover presses would be repointing state a resumed run is mid-way
        through using. Nothing here writes to any field.

        ALWAYS PULLS TO THE PC, ignoring `set_transfer_mode`. The button's
        whole contract is a file in the user's output folder, and card-only
        mode would leave nothing there — so the mode is bypassed rather than
        obeyed, and the caller says so in the log. That costs no lingering
        state: `ptp.set_recording_media` is written per capture and put back
        both ways, so the next run's first frame sets it to whatever that run
        asked for.

        The name is a stem; `ptp.shoot` adds the body's own extension and, if
        something of that name is already there, a numeric suffix — so the
        returned path is the one actually written, which is not necessarily
        the one requested.
        """
        cam = self._ensure()
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            path = cam.shoot(dest_dir=folder, filename=filename,
                             timeout_s=self._timeout_s)
        except ptp.PTPError as e:
            raise CameraError(f"Capture failed: {e}") from e
        return str(path) if path else "OK"

    # ── live view ────────────────────────────────────────────────────────
    def show_liveview(self) -> str:
        cam = self._ensure()
        try:
            cam.start_liveview()
        except ptp.PTPError as e:
            raise CameraError(f"Could not start LiveView: {e}") from e
        return "OK"

    def hide_liveview(self) -> str:
        if self._cam is None:
            return "OK"
        try:
            self._cam.stop_liveview()
        except ptp.PTPError:
            pass            # already down, or the body refused — harmless
        return "OK"

    def get_liveview_jpeg(self) -> Optional[bytes]:
        if self._cam is None:
            return None
        frame = self._cam.liveview_frame()
        return frame[384:] if frame else None       # strip the header

    # ── live view magnifier ──────────────────────────────────────────────
    #
    # MEASURED ON THE Z 6_2 — by a development probe that is not shipped —
    # because the name misleads and the obvious candidate does not exist:
    #
    #   0xD1A3 LiveViewImageZoomRatio — the name you would reach for — is NOT
    #   advertised by this body at all. 0xD1BD LiveViewZoomArea is, and it is
    #   writable with exactly [0, 512, 1024, 2048].
    #
    #   The value is the SIZE OF THE MAGNIFIED REGION, not the magnification,
    #   which is what "Area" means and why the list runs BACKWARDS: 512 is the
    #   deepest zoom and 2048 the shallowest, with 0 for off. Stepping up the
    #   enum would zoom out. Confirmed by eye, one saved frame per level.
    #
    #   The stream stays 640x424 at every level, so this is a sensor crop and
    #   not a resize — which is exactly what makes it worth having for judging
    #   a focus plane.
    LIVEVIEW_ZOOM = 0xD1BD

    def zoom_levels(self) -> list:
        """
        The magnifier's steps, ordered least to most magnified.

        Read from the camera rather than hardcoded — the enum is the body's
        own answer and another body may offer different steps — but REORDERED
        here, because the raw enum is ascending region size and therefore
        descending zoom. Off first, then smaller and smaller regions.
        """
        if self._cam is None:
            return []
        desc = self._cam.prop_desc(self.LIVEVIEW_ZOOM)
        if not desc:
            return []
        enum = desc.get("enum") or []
        if not enum:
            return []
        return [0] + sorted((v for v in enum if v), reverse=True)

    def get_zoom(self) -> Optional[int]:
        if self._cam is None:
            return None
        return self._cam.get_prop(self.LIVEVIEW_ZOOM)

    def set_zoom(self, value: int) -> None:
        """
        Set the magnifier. Needs LiveView up — it is a LiveView property and
        there is nothing to magnify without the stream.
        """
        cam = self._ensure_liveview()
        try:
            cam.set_prop(self.LIVEVIEW_ZOOM, int(value))
        except ptp.PTPError as e:
            raise CameraError(f"Could not set the live view zoom: {e}") from e

    # ── focus ────────────────────────────────────────────────────────────
    def _ensure_liveview(self) -> ptp.NikonCamera:
        """
        Raise a LiveView session if one is not already up.

        THE REASON THIS DOCSTRING GAVE FOR ITSELF WAS WRONG, AND IT WAS
        MEASURED WRONG — Z 6_2, 2026-08-03
        ---------------------------------------------------------------
        Until that date this said the focus motor will not accept a command
        without a LiveView session, and it went on to forbid ever adding a flag
        that skips the restart. The argument ran: MfDrive answers
        NIKON_NotLiveView (0xA00B) with LiveView down — ptp.py's module
        docstring lists it as one of the four ways a focus move can end, and
        `ptp.NikonCamera.drive` raises on it explicitly — so suppressing the
        restart would turn every focus move of a run into a `CameraError` and
        abort the run on its first gap move or anchor sweep. It was written
        confidently, days ago, during the audit, and it was never tested.

        It has now been tested on the body this rig drives, with no shutter
        actuations and no frames:

            LiveView OFF (`stop_liveview` called, `_liveview_on` False):
                drive_to_stop(NEAR)   parked the lens, 600 units travelled
                drive(+500)           ACCEPTED, rc = 0x2001 RC_OK. NOT refused.
                drive_to_stop(NEAR)   400 units travelled recovering it —
                                      so the lens really moved.

            LiveView ON, as a control:
                drive_to_stop(NEAR)   0 units, already parked
                drive(+500)           ACCEPTED, rc = 0x2001 RC_OK
                drive_to_stop(NEAR)   400 units travelled recovering it

        Identical, which is the whole point: LiveView made no difference to
        any of it. (400 back for a commanded 500 is `drive_to_stop`'s own
        documented undercount — the chunk that reports the stop has already
        arrived and is not counted, and STOP_SWEEP_CHUNK is 200 — and it came
        out the same figure on both sides.) An earlier one-shot check drove
        +10 with LiveView down and got 0xA00C MfDriveStepEnd: accepted, and
        already against the stop.

        So on the Z 6_2, MfDrive is accepted and moves the lens with LiveView
        down exactly as it does with LiveView up. This restart is not what
        makes focus work here.

        THE OTHER HALF, AND WHAT IT LICENCED
        ------------------------------------
        The trial above cleared focus and only focus. This docstring then said
        CAPTURE was still unmeasured, that a run-mode suppression could not be
        called safe end to end, and that no flag should be added. All three of
        those are spent. Capture was measured the same day on the same body,
        and it cost three shutter actuations:

            LiveView DOWN, two consecutive frames:
                both landed as real NEFs, 31.6 MB, ~1.1 s each
            LiveView UP, one control frame:
                real NEF, 31.7 MB, 1.1 s

        Two in a row deliberately, because the InvalidObjectHandle failure this
        repo spent so long chasing surfaces on the frame AFTER the bad one — a
        single frame would have proved nothing. LiveViewProhibitCondition
        (0xD1A4) was read after every frame and stayed 0x00000000 throughout,
        so bit 12, "pending unretrieved SDRAM image", was never set and no
        buffer was left stuck for the next frame to trip over. Capture is as
        indifferent to LiveView as focus is. Nothing a run does needs LiveView.

        So the flag this docstring once forbade now exists, and both halves of
        it are immediately below: `hold_liveview_closed` sets it and
        `_ensure_for_focus` reads it. Read those two before changing anything
        here — they carry the routing rules, including why `set_zoom` is
        deliberately left out of the gating.

        What the two trials do NOT settle is any other body. One camera was
        measured, so all of this is a fact about the Z 6_2 and not about
        Nikons.

        The 0xA00B handling in `ptp.NikonCamera.drive` therefore stays exactly
        as it is, and so does `RC_NIKON_NOT_LIVEVIEW`. A body that does refuse
        should say so out loud instead of accepting and ignoring the move, and
        for such a body the old warning above is still the right shape: nothing
        on the run's focus path catches a `CameraError` — `run/stacking.py`'s
        `_do_gap_move`, `_anchor_to` and `_goto_start` all let one straight out
        — so it really would end the run. Keep the mechanism; it was only the
        claim that it is REQUIRED that failed.

        One thing that is not about focus at all: `set_zoom` comes through here
        too, and the magnifier genuinely does need the stream — LIVEVIEW_ZOOM
        (0xD1BD) is a LiveView property and there is nothing to magnify without
        it. That is why the suppression was scoped to the focus callers rather
        than bolted onto this method wholesale. This method still starts
        LiveView unconditionally for anyone who calls it directly, and
        `set_zoom` is the reason it must go on doing so.
        """
        cam = self._ensure()
        if not cam._liveview_on:
            cam.start_liveview()
        return cam

    def hold_liveview_closed(self, on: bool) -> None:
        """
        Stop focus commands re-opening LiveView. Used by a run.

        Off by default, and only a run turns it on: outside a run, re-opening
        is what the user wants, because touching a focus control is normally a
        prelude to looking through the lens.

        SCOPED TO FOCUS, deliberately. `set_zoom` also comes through
        `_ensure_liveview` and genuinely does need the stream — the magnifier
        IS a LiveView property (LIVEVIEW_ZOOM, 0xD1BD) and there is nothing to
        magnify without one — so this flag must never reach that path. Focus
        and capture were both measured safe with LiveView down; the magnifier
        is the one thing that provably is not, and `capture` never asks for a
        session in the first place. So focus is the only thing left to gate:
        `_ensure_for_focus` reads this flag and nothing else does.
        """
        self._lv_hold_closed = bool(on)

    def _ensure_for_focus(self) -> ptp.NikonCamera:
        """
        Transport for a focus command, raising LiveView only if it is wanted.

        The one difference from `_ensure_liveview` is that a run can ask it not
        to bother. That is legal because it was MEASURED on 2026-08-03 on this
        rig's Z 6_2: with the session stopped and `_liveview_on` False,
        drive(+500) was accepted with RC_OK (0x2001) and the lens really moved
        — the drive_to_stop(NEAR) recovering it travelled 400 units, and a
        LiveView-up control gave the identical 400. Capture was measured the
        same day and is equally indifferent: two consecutive frames with
        LiveView down landed as real 31.6 MB NEFs, matching a LiveView-up
        control, with LiveViewProhibitCondition staying 0x00000000 throughout
        so no unretrieved SDRAM image was left stuck.

        The three focus entry points — `drive_units`, `drive_to_stop` and
        `measure_travel` — route through here. `set_zoom` does not, and must
        not.

        For a body that DOES refuse focus without LiveView this is where the
        damage would show: `ptp.NikonCamera.drive` raises on 0xA00B, nothing on
        the run's focus path catches a CameraError, and the run would abort
        rather than degrade. That is why the flag defaults off and only a run
        sets it — see `hold_liveview_closed`.
        """
        if self._lv_hold_closed:
            return self._ensure()
        return self._ensure_liveview()

    def drive_units(self, units: int) -> str:
        """
        Move an arbitrary number of driver units in ONE command, positive
        toward far/infinity.

        Does not return until the camera confirms the lens has stopped, and
        reports "AT_LIMIT" if a mechanical stop got in the way — so a focus
        move here cannot silently do nothing.
        """
        cam = self._ensure_for_focus()
        try:
            rc = cam.drive(units)
        except ptp.PTPError as e:
            raise CameraError(f"Focus command failed: {e}") from e
        return "AT_LIMIT" if rc == ptp.RC_NIKON_MFDRIVE_STEP_END else "OK"

    def drive_to_stop(self, toward_far: bool = False) -> int:
        """Sweep to a mechanical stop; returns units travelled."""
        cam = self._ensure_for_focus()
        try:
            return cam.drive_to_stop(ptp.FAR if toward_far else ptp.NEAR)
        except ptp.PTPError as e:
            raise CameraError(f"Homing failed: {e}") from e

    def measure_travel(self, chunk: int = 100,
                       check_stop: Optional[Callable[[], bool]] = None) -> int:
        """
        Total focus travel in DRIVER UNITS, measured between the two
        mechanical stops. No frames and no shutter actuations.

        TWO THINGS THE CALLER MUST HANDLE, because they are not what the name
        suggests:

        1. This leaves the lens against the FAR stop, not the near one — the
           measurement ends by creeping into it. Anything that keeps a
           position counter has to re-park before trusting the counter.
        2. A `check_stop` that fires mid-measurement makes the underlying
           routine RETURN EARLY with the distance covered so far, which is a
           smaller number that looks exactly like a valid answer. Callers must
           re-check `check_stop` afterwards and discard the figure, rather than
           reporting a partial sweep as the travel.

        `focus_cal.measure_travel_by_stops` does both and returns app steps;
        prefer it unless you specifically want driver units.
        """
        cam = self._ensure_for_focus()
        try:
            return cam.measure_travel(chunk=chunk, check_stop=check_stop)
        except ptp.PTPError as e:
            raise CameraError(f"Travel measurement failed: {e}") from e
