"""
Focus movement planning and execution.

Focus position is RELATIVE — the camera reports no absolute position over PTP,
confirmed against all 280 of its device properties and the LiveView header —
so everything here is bookkeeping against a zero set at A.

Two properties of the transport shape this whole module, and anything added
here should respect them: every move returns a status rather than an
acknowledgement, and the mechanical stop announces itself (`MfDriveStepEnd`).
So there is nothing to pace, nothing to settle for, and no need to compose a
distance out of fixed step sizes — a gap of any size is ONE confirmed
command.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

# Safe to import for real: ptp is the bottom of this package and pulls in
# nothing from it. The `camera` guard below is NOT breaking a cycle — there
# is none. camera.py's entire import list is __future__, re, threading,
# pathlib, typing, .constants, .errors and .ptp, and not one of those three
# siblings reaches back here, so a plain runtime `from .camera import
# PTPCameraClient` would resolve cleanly in any order. The guard stays for
# two smaller reasons: the name is wanted for an annotation and nothing
# else, and camera is the layer directly above this one — it is the module
# that WOULD close the loop if it ever grew a use for STEP_UNITS or
# plan_focus_intervals, which costs nothing to defend against in advance.
# Just do not read it as evidence of a cycle.
from .ptp import STOP_SWEEP_CHUNK

if TYPE_CHECKING:                       # annotation only — no cycle to break
    from .camera import PTPCameraClient


def plan_focus_intervals(total_range: int, n_photos: int) -> List[int]:
    """
    Split `total_range` steps into n_photos-1 gaps as evenly as possible.
    intervals[i] = steps to move AFTER photo i.
    """
    if n_photos < 2 or total_range <= 0:
        return []
    gaps = n_photos - 1
    base = total_range // gaps
    remainder = total_range % gaps
    return [base + (1 if i < remainder else 0) for i in range(gaps)]


def drive_focus(camera: "PTPCameraClient",
                direction: str,
                distance: int,
                check_stop: Optional[Callable[[], bool]] = None,
                progress: Optional[Callable[[int], None]] = None,
                on_limit: Optional[Callable[[], None]] = None) -> int:
    """
    Move `distance` steps in `direction` ("Plus" = far, "Minus" = near).

    ONE COMMAND, whatever the distance, and it does not return until the
    camera confirms the lens has stopped. What comes back is the COMMANDED
    distance, not a measured one — `steps` on both paths, including the one
    where the camera reports AT_LIMIT; the branch below records why no
    shorter figure is guessed there, and that decision is the reason this
    docstring cannot promise "steps actually moved". Zero comes back only in
    the two do-nothing cases: a non-positive `distance`, or `check_stop`
    already true before the command goes out.

    The camera DOES say when a stop got in the way — `camera.drive_units`
    turns RC_NIKON_MFDRIVE_STEP_END into "AT_LIMIT" — and that fact now
    leaves here, by way of `on_limit`: called once, with no arguments, before
    the return. A SEPARATE CHANNEL on purpose. The return value stays the
    commanded distance, so every caller that adds it to a position counter
    keeps counting exactly as it did and none of them changes meaning; only a
    caller that asks gets told.

    A caller that does not ask is where all of them used to be. It adds the
    full commanded distance and so over-counts by however much travel the
    stop ate, and the error only ever runs one way, since the lens cannot
    travel further than commanded. `on_limit` does not repair that — nothing
    here can, the distance actually covered is not knowable — it only makes
    the loss sayable. Re-anchoring is what puts the counter right:
    `anchored_drive` measures the position off the near stop instead of
    accumulating it.
    """
    steps = int(distance)
    if steps <= 0:
        return 0
    if check_stop and check_stop():
        return 0

    units = steps * STEP_UNITS["Small"]
    result = camera.drive_units(units if direction == "Plus" else -units)
    if progress:
        progress(steps)
    if result == "AT_LIMIT":
        # The lens ran out of travel partway. How far it actually got is not
        # knowable from here, and pretending otherwise is what used to put B
        # past the end of the lens — so no shorter figure is invented and
        # `steps` is still what comes back. What this branch used to do was
        # nothing whatsoever: it returned the same value as the fall-through,
        # so the stop was unmentionable as well as uncounted, and a stack
        # could end up with several frames on the same plane against a clean
        # log. `on_limit` is the one channel that fact has.
        if on_limit:
            on_limit()
    return steps


def anchored_drive(camera: "PTPCameraClient",
                   target: int,
                   limit_lo: int = 0,
                   check_stop: Optional[Callable[[], bool]] = None,
                   say: Optional[Callable[[str], None]] = None,
                   expected_from: Optional[int] = None,
                   warn: Optional[Callable[[str], None]] = None) -> int:
    """
    Land on `target` by way of the near mechanical stop. Returns the position
    reached, which the caller stores as its new counter value.

    The value of going via the stop is that it does not depend on knowing
    where the lens started, so a counter that has drifted for any reason is
    corrected rather than inherited. The sweep stops the moment the camera
    reports the stop, and the drive out is one confirmed command, so it is
    cheap enough to do between stacks.

    ANCHOR CHECK. Pass `expected_from` (the caller's current position) and the
    sweep doubles as an audit of the caller's own bookkeeping, for free — no
    extra commands, no frames.

    This closes a failure that was otherwise silent. Re-anchoring corrects a
    drifted COUNTER, but it cannot correct a drifted `limit_lo`, and `limit_lo`
    is exactly what Set A rewrites using the counter's value. So if the lens
    moved without the app issuing the command — the focus ring is the only way,
    since it is fly-by-wire and the body takes no notice of us — then Set A
    banks that error into the datum, every anchored move afterwards lands the
    same distance off, and nothing anywhere says so. The frames come back
    focused on the wrong planes and the log is clean.

    The sweep measures the truth: from `expected_from` the lens is genuinely
    `(expected_from - limit_lo) * STEP_UNITS["Small"]` DRIVER UNITS off the
    stop, if the datum is honest — expected_from and limit_lo are app steps,
    what the sweep returns is driver units, and the comparison must be made in
    one currency. What comes back is a bracket, not a number (see
    STOP_SWEEP_CHUNK), so only a disagreement WIDER than that bracket is
    reported — this must not cry wolf on the ordinary case or it will be
    ignored on the real one.

    WHAT IT CATCHES, measured rather than claimed. An error larger than one
    STOP_SWEEP_CHUNK (200 units, 20 app steps) is caught every time: the true
    distance is always within a chunk of what the sweep reports, so anything
    further out cannot fit the bracket. Below that it depends where the true
    position happens to fall inside its chunk — a 15-step error was caught in
    testing, a 10-step one was not. So this is a guard against the ring having
    been TURNED, which is a gross movement, and not a precision audit. Making
    it finer means sweeping in smaller chunks, which costs proportionally more
    commands on a path that runs between every stack; the trade was taken
    deliberately and this is the number it bought.

    Reported, not repaired. The sweep knows the true distance to within a
    chunk, which is enough to prove the datum wrong but not enough to replace
    it; quietly substituting a less-wrong number would leave the same class of
    error in place with the evidence thrown away. Homing is the actual fix and
    the message says so.
    """
    if say:
        say("anchoring on the near stop")
    travelled = camera.drive_to_stop(toward_far=False)
    if check_stop and check_stop():
        return int(limit_lo)

    if warn is not None and expected_from is not None:
        # DRIVER UNITS on both sides of the comparison. `travelled` comes back
        # from drive_to_stop in driver units; `expected_from` and `limit_lo`
        # are APP STEPS, the same currency line 145 hands to drive_focus, which
        # multiplies by STEP_UNITS["Small"] itself. Comparing the two raw was a
        # silent factor-of-10 error that inverted this guard: it went quiet
        # inside ~20 steps of the stop (the hand-turned-ring case it exists to
        # catch) and fired on every honest anchored move beyond that — on a
        # path stacking.py runs between every stack.
        expected = (int(expected_from) - int(limit_lo)) * STEP_UNITS["Small"]
        # Upper bound INCLUSIVE. A lens sitting an exact multiple of the chunk
        # from the stop reports the stop on the chunk that lands on it, without
        # counting that chunk — so the true distance is travelled + CHUNK on the
        # nose, and a strict `<` calls the honest case a fault. Verified against
        # FakeLens: true=200/400/1000 all give gap == CHUNK exactly.
        if not (travelled <= expected <= travelled + STOP_SWEEP_CHUNK):
            off = expected - travelled
            warn(
                f"Focus bookkeeping is wrong: the near stop was "
                f"{travelled}-{travelled + STOP_SWEEP_CHUNK} units away, but "
                f"the app expected {expected} — out by about "
                f"{abs(off) / STEP_UNITS['Small']:.0f} steps. The usual cause "
                f"is the focus ring being turned by hand; the camera does not "
                f"report that, so A, B and every anchored move are now "
                f"measured from the wrong place. Press Home, then set A and B "
                f"again.")

    out = int(target) - int(limit_lo)
    if out <= 0:
        return int(limit_lo)
    if say:
        say(f"out {out} step(s) to {target}")
    moved = drive_focus(camera, "Plus", out, check_stop=check_stop)
    return int(limit_lo) + moved


def compute_step_angle(steps_per_rev: int,
                       degrees_per_step: Optional[float]) -> float:
    if degrees_per_step is not None and degrees_per_step > 0:
        return float(degrees_per_step)
    if steps_per_rev <= 0:
        # Named --steps and --deg until this was audited; there has never
        # been a command line in this app to carry those flags, so if it ever
        # fired it would send a user hunting for something that does not
        # exist. The two things that DO exist are the Positions and Degrees
        # spinboxes.
        # Unreachable as it stands: the only production caller, run/worker.py,
        # clamps with max(live.get_int("steps_per_rev", 36), 1) just before
        # the call, and ui/main_window._rotation_geometry() returns at
        # least 1 on every path (falling back to 36, 10.0 when both fields
        # read "auto"). Kept anyway — it is the guard that makes those two
        # facts safe to rely on, and it costs one comparison per revolution.
        raise ValueError("Set Positions or Degrees to a positive value.")
    return 360.0 / float(steps_per_rev)


# ═══════════════════════════════════════════════════════════════════════════
# Focus step sizes
# ═══════════════════════════════════════════════════════════════════════════
#: Driver units moved by one Small / Medium / Large step.
#:
#: MfDrive takes an arbitrary uint32, so these are a UI convenience rather
#: than a protocol limit. "Small" is the app's step unit and the only one the
#: run path uses; Medium and Large are just the two larger jog buttons.
STEP_UNITS: Dict[str, int] = {"Small": 10, "Medium": 100, "Large": 500}


def step_ratios() -> Tuple[float, float]:
    """(medium, large) expressed as multiples of a Small step."""
    small = max(STEP_UNITS["Small"], 1)
    return (round(STEP_UNITS["Medium"] / small, 2),
            round(STEP_UNITS["Large"] / small, 2))
