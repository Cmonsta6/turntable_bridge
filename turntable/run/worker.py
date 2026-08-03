"""
The capture worker thread — the session/round/rotation state machine.

Reads every timing from the LiveSettings box at the moment it is needed,
so edits made mid-run take effect without a restart. Stack shooting lives
in StackShooterMixin and device recovery in DeviceRecoveryMixin; this
module owns the outer loop, stop/pause gating, checkpoints and alerts.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

from PySide6.QtCore import QThread

from ..hardware.camera import PTPCameraClient
from ..hardware.focus import compute_step_angle, plan_focus_intervals
from ..hardware.images import count_images_in
from ..hardware.settings import LiveSettings, interruptible_sleep
from ..ui.sound import play_jingle, start_alarm, stop_alarm
from ..ui.theme import (
    ACCENT_AMBER, ACCENT_BLUE, ACCENT_CYAN, ACCENT_GREEN, ACCENT_PURPLE,
    ACCENT_RED, TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY,
)
from .checkpoint import BridgeSignals, RunCheckpoint
from .recovery import DeviceRecoveryMixin
from .stacking import StackShooterMixin

if TYPE_CHECKING:                       # pyserial is optional at import time
    from ..hardware.table import ComximClient


class BridgeWorker(StackShooterMixin, DeviceRecoveryMixin, QThread):
    """
    Runs the capture session.

    Holds a LiveSettings box rather than a frozen config, and re-reads MOST
    values from it at the moment they are used. That is what makes "change
    the delay mid-run" land on the very next frame: pos_a, focus_range,
    focus_dir, photo_count, base_folder and reset_mode are read per stack,
    rotate_timeout_s and settle_after_rotate_s per rotation, and
    wait_for_file, capture_wait_s and capture_retries per FRAME in
    `_shoot_stack`.

    Six values are not live, because reading them late would be incoherent
    rather than responsive:

      * `keep_on_camera` (into `self._keep_on_camera`) and `close_lv_run`
        (into `self._lv_closed`) are read from the box once at the top of
        run(), before the loop, and never consulted again. The first decides
        whether session folders are made at all and whether anything waits
        for files; the second arms a hide AND A HOLD that lasts the whole
        run — `run` closes live view and then calls
        `PTPCameraClient.hold_liveview_closed(True)`, which stops focus
        commands re-opening it, and releases the hold in run()'s finally.
        Re-reading the setting mid-run would mean taking or dropping that
        hold halfway through, which nothing does. (It was described here as
        a one-shot action rather than a state, "and a short-lived one" —
        true until the hold existed, because the run's first focus command
        re-opened live view and nothing stopped it. The block above the hide
        in `run` has the whole story.)
        Toggling "Keep photos on the card" mid-run therefore does nothing
        until the next run — which is the scope the UI already reports for
        it: the `chk_keep_on_cam` handler in ui/layout.py reports the change
        as applying "next run".
      * `steps_per_rev`, `degrees_per_step`, `direction` and `stack_name`
        are frozen at the top of each revolution; changing steps/rev halfway
        would desynchronise the angle from the stack count.
    """

    def __init__(self, camera: "PTPCameraClient", tt: "ComximClient",
                 live: "LiveSettings", start_pos: int, parent=None,
                 resume_from: "Optional[RunCheckpoint]" = None,
                 recapture_first: bool = False):
        super().__init__(parent)
        self.camera = camera
        self.tt = tt                       # owned by the window, not by us
        self.live = live
        self.current_pos = start_pos
        # Closed-loop focus state. The origin ties the camera's own
        # position readings to the app's step counter, and is pinned
        # ONCE per session so every stack is corrected into the same
        # frame rather than each into its own.
        self._focus_origin = None
        self._drift_unreadable = False
        self._warned_far_stop = False
        self._warned_near_stop = False
        # Ruler calibration, learned from the frames as they arrive.
        self._scale_samples = []
        self._scale_prev = None
        self.signals = BridgeSignals()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._file_wait_misses = 0
        self._verbose_moves = True      # narrate the first stack only
        self._did_complete_beep = False
        self._lv_closed = False         # live view shut for the run?
        # Did we actually take the hold? Separate from _lv_closed,
        # which is only the user's preference: hide_liveview can
        # fail, and releasing a hold never taken would be a lie in
        # the log.
        self._lv_held = False
        self._keep_on_camera = False    # card-only mode for this run?
        self._transfer_set = False      # did we switch the transfer mode?
        self._alarm_active = False      # camera-drop alarm currently sounding?
        self._last_recover_was_drop = False   # did the last recovery wait on
        #                                        a physically absent camera?
        # Resume support: where to pick up, and whether the first stack we
        # shoot should be re-captured (marked _recaptured) rather than shot
        # fresh. checkpoint is updated at the top of every stack and mirrored
        # to the window so an interrupted run can always be resumed.
        self._resume_from = resume_from
        self._recapture_first = recapture_first
        self.checkpoint: "Optional[RunCheckpoint]" = resume_from

    # ── control ──────────────────────────────────────────────────────
    def request_stop(self):
        self._stop.set()
        self._pause.clear()

    def toggle_pause(self):
        if self._pause.is_set():
            self._pause.clear()
            self.signals.status_changed.emit("running")
            self.signals.log.emit("Resumed.", ACCENT_GREEN)
        else:
            self._pause.set()
            self.signals.status_changed.emit("paused")
            self.signals.log.emit("Paused.", ACCENT_AMBER)

    def _wait_if_paused(self):
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.05)

    def _check_stop(self):
        if self._stop.is_set():
            raise InterruptedError("Stopped")

    def _gate(self):
        self._wait_if_paused()
        self._check_stop()

    def _log(self, text, color=TEXT_SECONDARY):
        self.signals.log.emit(text, color)

    # ── main ─────────────────────────────────────────────────────────
    def run(self):
        live = self.live
        self.signals.status_changed.emit("running")
        stop_cb = self._stop.is_set

        # Card-only mode is decided once up-front: it changes whether we make
        # session folders at all and whether we wait for files to land.
        self._keep_on_camera = bool(self.live.get_bool("keep_on_camera", False))

        # Skip this in card-only mode: nothing reaches the PC, so there is no
        # name for us to render.
        if not self._keep_on_camera:
            try:
                self.camera.set_filename_template("[Counter 4 digit]")
            except Exception as e:                          # noqa: BLE001
                self._log(f"Could not set filename template: {e}", ACCENT_AMBER)

        # Read now, applied after the lens is at A — see the block below.
        self._lv_closed = bool(self.live.get_bool("close_lv_run", True))

        # Keep-on-camera: save each shot to the card only and skip the (slow)
        # USB download. Nothing then lands on the PC, so file-waiting is
        # switched off further down. Set once here; the finally block restores
        # normal PC transfer when the run ends.
        if self._keep_on_camera:
            try:
                self.camera.set_transfer_mode(True)
                self._transfer_set = True
                mode = self.camera.get_transfer_mode()
                self._log(
                    f"Keep-on-camera ON — photos stay on the card ({mode or '…'}); "
                    f"no files will appear on the PC. This is much faster. "
                    f"Offload the card yourself afterwards.", ACCENT_CYAN)
            except Exception as e:                          # noqa: BLE001
                self._keep_on_camera = False
                self._log(f"Could not enable keep-on-camera ({e}); falling "
                          f"back to normal PC transfer.", ACCENT_AMBER)

        # Seed the loop — either fresh, or from a resume checkpoint.
        cp0 = self._resume_from
        round_idx = (cp0.round_idx - 1) if cp0 else 0
        cumulative_angle = cp0.cumulative_angle if cp0 else 0.0
        reverse_stack = cp0.reverse_stack if cp0 else False
        first_stack = True          # first stack of ANY run returns the lens to A
        resume_stack_start = cp0.stack_in_round if cp0 else 0
        recapture_pending = bool(cp0 and self._recapture_first)
        self._current_tmpl: Optional[str] = None    # last filename template set
        last_plan: Optional[Tuple[int, int]] = None

        if cp0:
            self.signals.angle_changed.emit(cumulative_angle)
            self._log(
                f"Resuming from revolution {cp0.round_idx}, position "
                f"{cp0.stack_in_round + 1} — returning the lens to A, "
                f"re-capturing this stack (marked _recaptured), then "
                f"continuing with the stacks that were left.", ACCENT_CYAN)

        # ── position the lens at A BEFORE closing live view ──────────────
        # NOT because the camera needs a live-view session to focus — measured
        # on this rig's Z 6_2 on 2026-08-03, it does not; see the block below
        # the move. The ordering used to be forced by our own client:
        # `PTPCameraClient._ensure_liveview` raised a session before every
        # focus move, needed or not, so driving to A after the close would
        # just have re-opened what we closed and close_lv_run would have done
        # nothing for the whole run. That constraint is gone — focus now goes
        # through `PTPCameraClient._ensure_for_focus`, and with the hold on it
        # leaves live view alone, so this move would behave identically on the
        # far side of the close. The order is kept because it is free and it
        # reads in the right sequence: put the lens where the run starts, then
        # shut the sensor down for the run.
        try:
            pos_a0 = live.get_int("pos_a", 0)
            if reverse_stack:
                rng0 = max(live.get_int("focus_range", 0), 0)
                dir0 = live.get("focus_dir", "Plus") or "Plus"
                pos_a0 += rng0 if dir0 == "Plus" else -rng0
            # No "already there, skip it" shortcut. When a stop is known this
            # move re-anchors, and the counter reading A is precisely the
            # state not to trust — a dropped command during jogging leaves it
            # saying A while the lens sits elsewhere, and skipping the move is
            # what would carry that into the whole session.
            #
            # "The only reposition that happens with LiveView still up, so the
            # cheapest place in the run to be certain" — what this said until
            # the audit — was false when it was written and is false again for
            # a different reason. It was false then because every later
            # reposition re-opened LiveView on its way through
            # `_ensure_liveview`, so this was never the only one. It is the
            # only one NOW, because the hold below keeps LiveView down for
            # every focus move that follows — but that buys this move nothing.
            # Focus is indifferent to LiveView on this body (measured
            # 2026-08-03), so this reposition costs and confirms exactly what
            # every later one does. What makes it worth doing unconditionally
            # is the untrustworthy counter, not the stream.
            self._log("Placing the lens at the start of the range before "
                      "the run begins…", ACCENT_CYAN)
            self._goto_start(pos_a0)
        except Exception as e:                              # noqa: BLE001
            self._log(f"Could not pre-position the lens: {e}", ACCENT_AMBER)

        # Close live view now and HOLD it closed for the rest of the run.
        # `camera.hide_liveview` goes through stop_liveview(), which waits on
        # DeviceReady, so there is nothing to settle for afterwards. What the
        # option is actually worth is at the bottom of this block — it is not
        # the USB link, whatever the old "(faster)" label suggested.
        #
        # THIS COMMENT HAS BEEN WRONG TWICE, IN OPPOSITE DIRECTIONS, and the
        # record is kept rather than replaced: the defensive branch that both
        # wrong versions leaned on is still in the code, and whoever finds it
        # needs to know why it is there.
        #
        # WHAT WAS BELIEVED. Until the audit this said the hide lasted "for the
        # duration of the run". It did not. drive_units and drive_to_stop both
        # began with `PTPCameraClient._ensure_liveview()`, which re-opens the
        # session, so the first gap move of the first stack undid the hide in
        # every configuration — hardware/focus_cal.py's module docstring had
        # recorded exactly that (verified on the Z 6_2, `_liveview_on` True
        # both before and after a full run) and this module was never corrected
        # to match. The obvious repair — a run mode that suppresses the restart
        # — was then examined and REJECTED, on the grounds that MfDrive answers
        # NIKON_NotLiveView (0xA00B) with live view down and
        # `ptp.NikonCamera.drive` raises on that code: every focus move of the
        # run would come back a CameraError, and neither `_do_gap_move` nor
        # `_goto_start` nor `_anchor_to` catches one, so the run would die on
        # its first gap move or anchor sweep. That rejection was written down
        # confidently, was never tested, and survived long enough to talk a
        # later reviewer out of a correct fix. That is the expensive part of
        # this episode, and the reason the story is told at length here.
        #
        # WHAT WAS MEASURED, on this rig's Z 6_2, on 2026-08-03. First focus,
        # with no shutter actuations: with live view stopped and `_liveview_on`
        # False, drive(+500) was ACCEPTED and answered RC_OK (0x2001), and the
        # lens really moved — the drive_to_stop(NEAR) recovering it travelled
        # 400 units, and the identical sequence with live view UP, run as a
        # control, gave the identical 400. (400 rather than 500 is
        # drive_to_stop's own documented chunked undercount — the chunk that
        # reports the stop has already arrived and is not counted, and
        # STOP_SWEEP_CHUNK is 200 — and it came out the same both ways, which
        # is the point: live view made no difference at all.) An earlier
        # one-shot drive(+10) with live view down answered 0xA00C
        # (MfDriveStepEnd, i.e. already against a stop) — accepted, not
        # refused.
        #
        # Then CAPTURE, the half that was missing, same body, three shutter
        # actuations. Two consecutive frames with live view DOWN both landed as
        # real NEFs, 31.6 MB, ~1.1 s; a control frame with live view UP gave a
        # real NEF, 31.7 MB, 1.1 s. Two in a row deliberately, because the
        # InvalidObjectHandle failure this repo chased surfaces on the frame
        # AFTER the bad one, not on the bad one. LiveViewProhibitCondition
        # (0xD1A4, DPC_NIKON_LIVEVIEW_PROHIBIT) was read after every frame and
        # was 0x00000000 throughout, so bit 12 — pending unretrieved SDRAM
        # image — was never set and no buffer was left stuck. Capture is as
        # indifferent to live view as focus is. NOTHING A RUN DOES NEEDS LIVE
        # VIEW ON THIS BODY.
        #
        # WHAT WAS CONSEQUENTLY BUILT, and is what the code below now does.
        # `PTPCameraClient.hold_liveview_closed(on)` sets a flag;
        # `PTPCameraClient._ensure_for_focus` reads it and returns the plain
        # session instead of raising live view while it is set. The three focus
        # entry points — drive_units, drive_to_stop and measure_travel — route
        # through `_ensure_for_focus`. `set_zoom` deliberately does NOT and
        # must not: the magnifier is a live view property (LIVEVIEW_ZOOM,
        # 0xD1BD) and genuinely needs the stream. The hold is taken immediately
        # after the hide below and released in the single try/finally around
        # the whole run loop, so Stop, an exception and a clean finish all
        # release it.
        #
        # WHAT DID NOT CHANGE, and must not. 0xA00B is a real Nikon code and
        # `ptp.NikonCamera.drive` is right to keep raising on it: for a body
        # that does refuse focus without live view, the abort described above
        # is exactly what would happen, and a body that refuses should say so
        # out loud rather than accept the move and quietly not make it. And one
        # body was measured — this is a fact about the Z 6_2, not about Nikons.
        #
        # WHAT THE OPTION IS WORTH. Less than "(faster)" ever claimed, and any
        # surviving speed framing is simply wrong. What puts live-view traffic
        # on the link is the preview's poll thread, and
        # `main_window._pause_liveview` stops that when a run starts whether
        # this option is ticked or not; a session nobody polls serves no frames
        # and costs no transfers, so this checkbox never protected the USB link
        # and still does not. What the hold buys is the sensor and the live
        # view pipeline staying off the body for the WHOLE run rather than for
        # the first few seconds — a heat and power argument, not a speed one.
        if self._lv_closed:
            try:
                self.camera.hide_liveview()
                # AND HOLD IT CLOSED — the half that makes this option do what
                # its label says, rather than lasting until the run's first
                # focus command. The flag is cleared unconditionally in the
                # finally at the end of run(), so a Stop, an exception, or a
                # device that never comes back cannot leave the preview
                # suppressed afterwards — that would reach the user as "the
                # preview is broken now", which is worse than the wasted
                # option ever was.
                self.camera.hold_liveview_closed(True)
                self._lv_held = True
                self._log("Live view closed for the run.", ACCENT_CYAN)
            except Exception as e:                          # noqa: BLE001
                self._log(f"Could not close live view: {e}", ACCENT_AMBER)

        try:
            while True:
                self._gate()

                rounds = live.get_int("rounds", 1)
                rounds_target = None if rounds <= 0 else rounds
                if rounds_target is not None and round_idx >= rounds_target:
                    break
                round_idx += 1

                # Rotation geometry is fixed for the duration of a round —
                # changing steps/rev halfway through would desynchronise the
                # angle from the stack count.
                steps_per_rev = max(live.get_int("steps_per_rev", 36), 1)
                stacks_per_round = steps_per_rev + 1
                deg = live.get("degrees_per_step")
                step_angle = compute_step_angle(steps_per_rev, deg)
                direction = live.get_int("direction", 0)
                name = str(live.get("stack_name", "subject")) or "subject"

                # On the first resumed revolution, start at the stack we were
                # parked on; every later revolution starts from the top. The
                # filename template is (re)set per stack below, only when it
                # actually changes, so the recapture suffix can be toggled
                # without paying the per-stack set cost every time.
                start_j = max(0, min(resume_stack_start, stacks_per_round - 1))
                if resume_stack_start and resume_stack_start != start_j:
                    self._log(
                        f"  (Resume point {resume_stack_start + 1} is past this "
                        f"revolution's {stacks_per_round} stacks — starting at "
                        f"{start_j + 1}. Did steps/rev change?)", ACCENT_AMBER)
                resume_stack_start = 0      # only the first resumed round offsets

                self.signals.round_changed.emit(round_idx)
                self._log(
                    f"─── Revolution {round_idx} — {stacks_per_round} stacks "
                    f"· {step_angle:.2f}° per move ───",
                    TEXT_PRIMARY)

                for stack_in_round in range(start_j, stacks_per_round):
                    self._gate()
                    t_stack = time.monotonic()
                    # Record where we are physically parked BEFORE shooting, so
                    # a failure anywhere in or after this stack resumes here.
                    self._set_checkpoint(round_idx, stack_in_round,
                                         stacks_per_round, cumulative_angle,
                                         reverse_stack, name)
                    recapture_this = recapture_pending
                    recapture_pending = False       # only the first resumed stack

                    photo_count = max(live.get_int("photo_count", 2), 1)
                    focus_range = max(live.get_int("focus_range", 0), 0)
                    pos_a = live.get_int("pos_a", 0)
                    focus_dir = live.get("focus_dir", "Plus") or "Plus"
                    base_folder = Path(str(live.get("base_folder", "."))).expanduser()
                    reset_mode = live.get("reset_mode", "serpentine")

                    self.signals.stack_changed.emit(stack_in_round, stacks_per_round)
                    self.signals.images_changed.emit(0, photo_count)

                    # Three self-describing levels:
                    #   base/unicorn/unicorn_rev1/unicorn_rev1_pos001/
                    rev_name = f"{name}_rev{round_idx}"
                    pos_name = f"{rev_name}_pos{stack_in_round + 1:03d}"
                    stack_folder = base_folder / name / rev_name / pos_name

                    self._log(
                        f"Revolution {round_idx}, position {stack_in_round + 1}"
                        f"/{stacks_per_round}  →  {name}/{rev_name}/{pos_name}",
                        ACCENT_AMBER)

                    if self._keep_on_camera:
                        # Card-only: no folder is made and no name is rendered,
                        # because nothing reaches the PC. The frame lands on
                        # the card under the camera's own name.
                        if recapture_this:
                            self._log(
                                "  Re-capturing this stack to the card. In "
                                "card-only mode the _recaptured tag can't reach "
                                "the camera-named files, but these are the "
                                "frames to keep for this position.", ACCENT_CYAN)
                    else:
                        stack_folder.mkdir(parents=True, exist_ok=True)
                        # Filename template for this stack. A re-captured stack
                        # gets a _recaptured tag so its frames never collide with
                        # (or get confused for) whatever the interrupted attempt
                        # left behind.
                        suffix = "_recaptured" if recapture_this else ""
                        desired_tmpl = f"{rev_name}{suffix}_[Counter 4 digit]"
                        if desired_tmpl != self._current_tmpl:
                            try:
                                self.camera.set_filename_template(desired_tmpl)
                                self._current_tmpl = desired_tmpl
                            except Exception as e:          # noqa: BLE001
                                self._log(f"Could not set filename template: "
                                          f"{e}", ACCENT_AMBER)

                        stale = count_images_in(stack_folder)
                        if recapture_this:
                            note = ("  Re-capturing this stack; frames are "
                                    "tagged _recaptured")
                            note += (f". The {stale} earlier frame(s) here are "
                                     f"left untouched." if stale else ".")
                            self._log(note, ACCENT_CYAN)
                        elif stale:
                            # Left-over frames from an earlier run get counted as
                            # part of this stack and handed to the stacker as if
                            # they belonged — say so rather than silently mixing
                            # two sessions together.
                            self._log(
                                f"  {stack_folder.name} already holds {stale} "
                                f"image(s) from an earlier run — they will be "
                                f"mixed in with this stack.", ACCENT_RED)

                        self.camera.begin_stack(stack_folder)   # counter 0 → 0001

                    # ── put the lens back at the start of the range ──
                    shooting_reversed = reverse_stack
                    if first_stack:
                        # A reversed stack shoots far→near (dir_now=Minus), so
                        # it must START at B, not A — homing to A and driving
                        # Minus would capture the planes BELOW A. Homing to B
                        # also leaves the lens at A when the stack ends, which
                        # keeps serpentine continuity for the stacks that
                        # follow. (Only reachable on a serpentine resume; a
                        # fresh run's first stack is never reversed.)
                        if shooting_reversed:
                            span = (focus_range if focus_dir == "Plus"
                                    else -focus_range)
                            self._goto_start(pos_a + span)
                        else:
                            self._goto_start(pos_a)
                    else:
                        self._reposition(reset_mode, pos_a)
                    first_stack = False

                    # The throwaway anchor-check frame that used to be shot
                    # here is gone. It bought back the one frame the closed
                    # loop could not protect -- a command lost during the
                    # anchor, landing on the first frame with nothing yet
                    # measured. Anchoring is confirmed by the camera now, so
                    # it was paying an actuation per stack for a risk that no
                    # longer exists.

                    # ── shoot ────────────────────────────────────────
                    intervals = plan_focus_intervals(focus_range, photo_count)

                    # Announce the plan whenever it changes. If the gaps come
                    # out as zeros the lens will visibly stop moving partway
                    # through the stack, and this is where you can see why.
                    plan_key = (focus_range, photo_count)
                    if plan_key != last_plan:
                        last_plan = plan_key
                        moving = sum(1 for g in intervals if g > 0)
                        gap = focus_range / max(photo_count - 1, 1)
                        # Report the physical size too. "1 step per gap"
                        # sounds fine but is only 10 focus units on a Nikon,
                        # which is a nudge you may not be able to see.
                        unit = max(live.get_int("small_units", 0), 0)
                        phys = (f" = {int(round(gap * unit))} focus units"
                                if unit else "")
                        self._log(
                            f"  Focus plan: {photo_count} photos over "
                            f"{focus_range} steps → {gap:.2f} steps per gap"
                            f"{phys}  ({moving}/{max(photo_count - 1, 0)} "
                            f"gaps move).",
                            ACCENT_AMBER if moving < max(photo_count - 1, 1)
                            else TEXT_MUTED)
                        if moving < max(photo_count - 1, 1):
                            self._log(
                                "  The range is too small for this many "
                                "photos — some shots will repeat the same "
                                "focus position.", ACCENT_RED)
                        elif unit and gap * unit < 30:
                            self._log(
                                "  That is a very small move per shot. If "
                                "the lens looks static, widen A→B or lower "
                                "the photo count.", ACCENT_AMBER)

                    dir_now = focus_dir
                    if shooting_reversed:
                        dir_now = "Minus" if focus_dir == "Plus" else "Plus"
                        intervals = list(reversed(intervals))

                    captured, shots_fired = self._shoot_stack(
                        stack_folder, photo_count, intervals, dir_now)

                    # Serpentine renumbering reorders files on the PC, so it
                    # only applies when files actually landed (normal mode).
                    if (not self._keep_on_camera and shooting_reversed
                            and live.get_bool("renumber_reversed", True)):
                        self._renumber(captured, photo_count)

                    self._verbose_moves = False
                    if self._keep_on_camera:
                        # Nothing on disk to count — report the frames shot,
                        # which are now on the camera's card.
                        final_count = shots_fired
                        ok_color = (ACCENT_GREEN if final_count >= photo_count
                                    else ACCENT_AMBER)
                        self._log(f"  Stack complete — {final_count} shot(s) "
                                  f"saved to the camera card.", ok_color)
                    else:
                        final_count = count_images_in(stack_folder)
                        ok_color = (ACCENT_GREEN if final_count >= photo_count
                                    else ACCENT_AMBER)
                        self._log(f"  Stack complete — {final_count} images "
                                  f"in {stack_folder.name}.", ok_color)
                    self.signals.stack_done.emit(time.monotonic() - t_stack)

                    if reset_mode == "serpentine":
                        reverse_stack = not reverse_stack

                    # ── rotate ───────────────────────────────────────
                    if stack_in_round < stacks_per_round - 1:
                        self._check_stop()
                        rotate_timeout = live.get_float("rotate_timeout_s", 60.0)
                        settle = live.get_float("settle_after_rotate_s", 0.2)
                        self._log(f"  Rotating {step_angle:.2f}°…", ACCENT_BLUE)
                        self._rotate_with_recovery(direction, step_angle,
                                                   rotate_timeout, stop_cb)
                        interruptible_sleep(settle, stop_cb)
                        cumulative_angle = (cumulative_angle + step_angle) % 360
                        self.signals.angle_changed.emit(cumulative_angle)
                        # The table is now physically parked at the NEXT stack.
                        # Record that immediately so a Stop in this window (or a
                        # failure before the next iteration's top-of-loop
                        # checkpoint) can't leave the checkpoint one stack behind
                        # the real position — which would resume at the wrong
                        # angle. reverse_stack is already toggled above, so it
                        # matches the next stack's shooting direction.
                        self._set_checkpoint(round_idx, stack_in_round + 1,
                                             stacks_per_round, cumulative_angle,
                                             reverse_stack, name)

                self._log(f"Revolution {round_idx} complete.", ACCENT_PURPLE)
                self.signals.revolution_done.emit(round_idx)

                rounds = live.get_int("rounds", 1)
                rounds_target = None if rounds <= 0 else rounds
                if rounds_target is None or round_idx < rounds_target:
                    if live.get_bool("hold_between_rounds", True):
                        self._pause.set()
                        self.signals.status_changed.emit("paused")
                        self._log(
                            "Revolution complete — holding. Adjust the scene "
                            "if you like, then press Resume for the next "
                            "revolution.", ACCENT_AMBER)
                        # Three quick beeps, once, to call you back to the rig.
                        self._alert()
                        self._wait_if_paused()
                    else:
                        self._log("Revolution complete — continuing straight "
                                  "into the next one.", ACCENT_PURPLE)

            self.signals.status_changed.emit("complete")
            self._log("All revolutions complete.", ACCENT_GREEN)
            self._beep()

        except InterruptedError:
            self._log("Stopped by user.", ACCENT_RED)
            self.signals.status_changed.emit("idle")
        except Exception as exc:                            # noqa: BLE001
            self._log(f"ERROR: {exc}", ACCENT_RED)
            self.signals.status_changed.emit("error")
        finally:
            # Never leave the alarm ringing, the camera in card-only mode, or
            # live view held closed.
            self._stop_alarm()
            if self._lv_held:
                # Unconditional, and deliberately not inside a `if finished
                # cleanly` branch: the whole hazard of the hold is that an
                # abandoned run leaves focus commands unable to raise the
                # preview, which the user would read as the app having broken
                # its own live view. Releasing costs nothing when the run
                # ended normally.
                try:
                    self.camera.hold_liveview_closed(False)
                except Exception as e:                      # noqa: BLE001
                    self._log(f"Could not release the live-view hold: {e}",
                              ACCENT_AMBER)
                self._lv_held = False
            if self._transfer_set:
                try:
                    self.camera.set_transfer_mode(False)
                    self._log("Restored normal PC transfer.", TEXT_MUTED)
                except Exception as e:                      # noqa: BLE001
                    self._log(f"Could not restore PC transfer mode: {e}",
                              ACCENT_AMBER)
                self._transfer_set = False
            self.signals.finished.emit()

    # ── stack helpers ────────────────────────────────────────────────

    def _set_checkpoint(self, round_idx: int, stack_in_round: int,
                        stacks_per_round: int, angle: float,
                        reverse_stack: bool, name: str):
        cp = RunCheckpoint(round_idx=round_idx, stack_in_round=stack_in_round,
                           stacks_per_round=stacks_per_round,
                           cumulative_angle=angle, reverse_stack=reverse_stack,
                           name=name)
        self.checkpoint = cp
        self.signals.checkpoint_changed.emit(cp)

    def _start_alarm(self):
        if self._alarm_active:
            return
        self._alarm_active = True
        if self.live.get_bool("beep_enabled", True):
            start_alarm()

    def _stop_alarm(self):
        if not self._alarm_active:
            return
        self._alarm_active = False
        stop_alarm()

    def _beep(self):
        """The finish jingle (a real synthesised WAV) — fires once per run."""
        if self._did_complete_beep:
            return
        self._did_complete_beep = True
        if self.live.get_bool("beep_enabled", True):
            play_jingle("done")

    def _alert(self):
        """The 'come and Resume' jingle, once per hold between revolutions."""
        if self.live.get_bool("beep_enabled", True):
            play_jingle("hold")
