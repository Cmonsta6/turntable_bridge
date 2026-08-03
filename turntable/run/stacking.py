"""
Shooting one focus stack, and the repositioning around it.

Split out of BridgeWorker so the outer session loop stays readable. Mixed
into BridgeWorker, so `self` is the worker: these methods use its client
handles, LiveSettings box and stop/pause gate.

The serpentine detail worth remembering: a REVERSED stack must home to B
(pos_a + span), not A — homing to A and then stepping Minus captures
planes below A, outside the requested range.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from ..hardware.focus import anchored_drive, drive_focus
from ..hardware import lensdata
from ..hardware.images import list_image_names, wait_for_new_image
from ..hardware.settings import interruptible_sleep
from ..ui.theme import ACCENT_AMBER, ACCENT_CYAN, ACCENT_RED, TEXT_MUTED

#: Steps of drift beyond which the READING stops being credible. Not a cap on
#: anything any more, and never a correction limit: nothing here steers by the
#: measurement. Its only consumer is `_report_drift`, which adds a sentence to
#: the log when a reading passes it (see its docstring).
#:
#: It used to be applied to the figure itself — `_measure_drift` returned
#: min(err, DRIFT_CAP) — which is a bound earned by a closed correction loop
#: that was later deleted, left behind quietly shrinking the only number the
#: user is shown. A genuine 60-step fault reached them as "+25 step(s) from
#: where the plan says", with the real figure appearing once, muted. The
#: reasoning survives the cap: real losses are a command or two, so a much
#: bigger reading is more likely a bad decrypt than a lens that jumped. That
#: is now said in words rather than subtracted from the measurement.
DRIFT_CAP = 25.0

#: Plausible range for the ruler's counts-per-step scale. Samples outside
#: it are rejected before the estimator runs: a pair of frames spanning a
#: mechanical stop, or a dropped command, produces a ratio that is not a
#: scale measurement at all. The band is this wide because 10.0 and 11.67
#: had both been recorded on this body, which looked like the scale not
#: being a constant. That finding is RETIRED: hardware/focus_cal.py:31-35
#: measured the two rulers side by side on 2026-07-31 and every 25-step
#: chunk of a full sweep moved exactly 250 counts, i.e. a scale of 10.00 —
#: STEP_UNITS["Small"] to the digit. The old scatter was the pre-PTP
#: transport quietly losing commands, which measures LOW. The band is kept
#: wide only to stay tolerant of a body nobody has measured.
SCALE_MIN, SCALE_MAX = 4.0, 30.0


class StackShooterMixin:
    def _shoot_stack(self, folder: Path, photo_count: int,
                     intervals: List[int], dir_now: str) -> Tuple[List[Path], int]:
        """
        Capture one focus stack.

        Returns (files_seen, shots_fired). In the normal PC-transfer mode the
        files list is what we watched arrive on disk. In keep-on-camera mode
        nothing lands on the PC, so the files list is empty and shots_fired is
        the count of frames the shutter took.
        """
        live = self.live
        stop_cb = self._stop.is_set
        keep_on_camera = self._keep_on_camera
        captured: List[Path] = []
        shots_fired = 0
        sign = 1 if dir_now == "Plus" else -1

        for img_idx in range(photo_count):
            self._gate()

            # Re-read every time: this is what makes a mid-run edit land on
            # the very next frame instead of the next session.
            wait_for_file = live.get_bool("wait_for_file", True) \
                and not keep_on_camera
            capture_wait = live.get_float("capture_wait_s", 2.0)
            retries = max(live.get_int("capture_retries", 3), 1)

            if keep_on_camera:
                # Card-only: nothing reaches the PC, so there is no folder to
                # watch and no path handed back. camera.capture(autofocus=
                # False) goes to NikonCamera.shoot(keep_on_camera=True), which
                # waits out the body's BUSY (the whole exposure and commit)
                # and then wait_for_object() until the camera announces the
                # new object — so a clean return means the shot is done.
                fired = False
                attempt = 0
                drop_refires = 0
                while attempt < retries:
                    attempt += 1
                    try:
                        self.camera.capture(autofocus=False)
                        fired = True
                        break
                    except Exception as e:                  # noqa: BLE001
                        self._log(f"  Capture attempt {attempt} hiccuped: "
                                  f"{e} — recovering.", ACCENT_AMBER)
                        self._last_recover_was_drop = False
                        if not self._recover_camera(attempt, e):
                            self._check_stop()
                        if self._last_recover_was_drop:
                            # A DEVICE went away (camera unplugged/off/asleep)
                            # and has now been restored. On the card we cannot
                            # check whether this frame made it, and a MISSING
                            # frame leaves a real hole in the focus stack while
                            # a duplicate at the same plane is harmless — so we
                            # always re-fire. The drop didn't consume a retry
                            # (it wasn't a camera fault), but cap the re-fires
                            # so a flapping cable can't loop forever.
                            attempt -= 1
                            drop_refires += 1
                            if drop_refires > 5:
                                self._log("  Device kept dropping — giving up "
                                          "on this frame.", ACCENT_RED)
                                break
                            self._log("  Re-firing this shot after the "
                                      "reconnect so the stack has no gap "
                                      "(a duplicate frame is possible).",
                                      ACCENT_CYAN)
                            continue
                        # A stall with both devices present. The capture call
                        # blocks until the card write finishes, so a call that
                        # hung has usually already tripped the shutter, and
                        # re-firing would duplicate it. Count it and move on.
                        fired = True
                        break
                if fired:
                    shots_fired += 1
                else:
                    self._log(f"  Frame {img_idx + 1}/{photo_count} could not "
                              f"be captured — this stack will have a gap.",
                              ACCENT_RED)
                self.signals.images_changed.emit(img_idx + 1, photo_count)
                if img_idx < len(intervals) and intervals[img_idx] > 0:
                    # No correction here: nothing lands on the PC in
                    # card-only mode, so there is no frame to read back
                    # and this path stays open loop by necessity.
                    self._do_gap_move(intervals, img_idx, dir_now, sign)
                continue

            known = list_image_names(folder) if wait_for_file else set()

            got = None
            attempt = 0
            drop_refires = 0
            while attempt < retries:
                attempt += 1
                wrote = None
                try:
                    wrote = self.camera.capture(autofocus=False)
                except Exception as e:                      # noqa: BLE001
                    # A camera.capture() that times out has USUALLY already
                    # fired the shutter, so we must never blindly re-send it —
                    # ptp.capture will not re-send one that errored or timed
                    # out either, for the same reason (its only re-send is the
                    # DeviceBusy case, where the trigger was refused and the
                    # shutter never fired).
                    # Recover the pipeline, then let the wait below answer the
                    # one question that can be answered here: did a FILE land?
                    # Not the same question as whether the shutter fired. Only
                    # re-fire if a full wait finds nothing.
                    self._log(f"  Capture attempt {attempt} hiccuped: {e} — "
                              f"recovering.", ACCENT_AMBER)
                    self._last_recover_was_drop = False
                    if not self._recover_camera(attempt, e):
                        self._check_stop()
                    if self._last_recover_was_drop and wait_for_file:
                        # A device went away and is now back. The shot was lost
                        # to the drop, not to a camera fault, so it must not
                        # eat the retry budget — otherwise a few drops in a row
                        # exhaust it and the frame is silently skipped, leaving
                        # a hole in the stack. Re-fire this same frame.
                        attempt -= 1
                        drop_refires += 1
                        if drop_refires > 5:
                            self._log("  Device kept dropping — giving up on "
                                      "this frame.", ACCENT_RED)
                            break
                        self._log("  This frame never landed before the drop — "
                                  "re-firing it so the stack has no gap.",
                                  ACCENT_CYAN)
                        continue

                if not wait_for_file:
                    if not interruptible_sleep(capture_wait, stop_cb):
                        self._check_stop()
                    break

                # THE ORDINARY PATH NEEDS NO POLLING. capture() hands back the
                # path it has already written: ptp.shoot() does
                # `path.write_bytes(payload); return path`, synchronously, so
                # the file is closed on disk before this line runs. Watching the
                # folder for it cost a scandir plus the full file_stable_ms
                # settle (~0.36 s) on EVERY frame of every stack — around four
                # minutes a revolution at 37 stacks x 20 frames, spent waiting
                # for something that had already happened. Taking the path
                # directly also drops images.py's `max(paths, key=mtime)` guess,
                # which could pick the wrong file when two landed together.
                if wrote and wrote != "OK":
                    landed = Path(wrote)
                    if landed.exists():
                        got = landed
                        self._check_stop()
                        break

                # Only reached when capture() raised, or came back with no path
                # or one that is not on disk. Nothing writes into this folder
                # but ptp.shoot's own write_bytes — recovery writes no image —
                # so after a raised capture there is nothing here to find and
                # this wait runs out. That is what it is for now: an empty wait
                # is the evidence that no frame landed, which is what the drop
                # check and the re-fire below stand on.
                got = wait_for_new_image(
                    folder, known,
                    timeout_s=max(capture_wait * 3.0, 8.0),
                    poll_s=0.04,
                    stable_ms=live.get_int("file_stable_ms", 250),
                    stop=stop_cb,
                )
                self._check_stop()
                if got is not None:
                    break                       # the frame landed — done

                # Nothing landed. Is a device gone? (A silent drop yields an
                # OK-looking capture but never a file.) Pause/alarm/wait if so.
                # Here we CAN verify — the file either arrived or it didn't —
                # so after the device is back we re-fire this same frame rather
                # than moving on and leaving a hole in the stack. A drop is not
                # the camera's fault, so it doesn't consume a retry; the
                # re-fire count is capped so a flapping cable can't loop.
                if self._ensure_device_or_wait():
                    attempt -= 1
                    drop_refires += 1
                    if drop_refires > 5:
                        self._log("  Device kept dropping — giving up on this "
                                  "frame.", ACCENT_RED)
                        break
                    self._log("  This frame never landed before the drop — "
                              "re-firing it so the stack has no gap.",
                              ACCENT_CYAN)
                    continue
                if attempt < retries:
                    self._log("  No frame landed after a full wait — the shot "
                              "did not fire, re-firing.", ACCENT_AMBER)

            if wait_for_file:
                if got is None:
                    self._file_wait_misses += 1
                    self._log("  Image did not appear in time — falling back "
                              "to the fixed wait.", ACCENT_AMBER)
                    if self._file_wait_misses == 3:
                        self._log(
                            "  Three images in a row were not seen on disk. "
                            "Check the save folder is writable, or turn off "
                            "'Continue as soon as the file lands'.",
                            ACCENT_RED)
                    if not interruptible_sleep(capture_wait, stop_cb):
                        self._check_stop()
                else:
                    self._file_wait_misses = 0
                    captured.append(got)
                    self._report_drift(got)

            shots_fired += 1
            self.signals.images_changed.emit(img_idx + 1, photo_count)

            if img_idx < len(intervals) and intervals[img_idx] > 0:
                self._do_gap_move(intervals, img_idx, dir_now, sign)

        return captured, shots_fired

    #: Report a discrepancy only once it exceeds this many steps, so ordinary
    #: rounding and the ruler's own scale uncertainty stay quiet.
    DRIFT_REPORT_STEPS = 3.0

    def _report_drift(self, frame: Path) -> None:
        """
        Check the frame's own record against the counter, and SAY so — but do
        not steer by it.

        Deliberately a monitor, not a correction. Every move is confirmed by
        the camera, so there should be nothing to correct. The second reason
        this used to give — that the NEF ruler's counts-per-step was not a
        constant (10.0 on one run, 11.67 on another, tracking focal length),
        so acting on a mis-scaled reading would inject error into a plan that
        was right — is RETIRED: hardware/focus_cal.py:31-35 measured 10.00
        with no scatter on 2026-07-31 and traced the old spread to the pre-PTP
        transport losing commands. The monitor stands on the first reason.

        Kept because a focus fault that nothing watches for is invisible until
        the stacks come out wrong. This reports it on the very next frame.

        THE NUMBER IS THE MEASUREMENT. Nothing bounds it on the way out —
        DRIFT_CAP records what happened while something did — but a reading
        past DRIFT_CAP earns a sentence saying it is bigger than a lost
        command can account for, because at that size the likelier fault is
        in the reading. Uncapping does not make this any louder: it is one
        line per frame over DRIFT_REPORT_STEPS either way, and a fault large
        enough to have been capped was already over that threshold and
        already writing a line on every frame.
        """
        err = self._measure_drift(frame)
        if not err or abs(err) < self.DRIFT_REPORT_STEPS:
            return
        self._log(
            f"  Lens is {err:+.0f} step(s) from where the plan says. Moves are "
            f"confirmed by the camera, so this should not happen — read back "
            f"the 'off plan' lines in this log: a figure that shows up once "
            f"and clears came from one bad frame, while one that holds or "
            f"grows across frames is a genuine fault and the stack is worth "
            f"shooting again."
            + ("" if abs(err) <= DRIFT_CAP else
               " A reading this large is more likely a misread frame than a "
               "lens that moved that far, so weigh it accordingly."),
            ACCENT_AMBER)

    def _measure_drift(self, frame: Path) -> float:
        """
        How far the lens really is from where the counter says, in steps.

        The camera never reports focus position over the wire, but every NEF
        carries it: the frame just captured says exactly where the lens was
        when the shutter opened. Comparing that with the plan is the only
        independent check this rig has on its own step counter.

        Returns steps of error, positive meaning the lens sits further toward
        FAR than intended. Returns 0.0 when the position cannot be read, so an
        unreadable frame simply leaves the counter unchallenged rather than
        throwing the run off.

        The figure is raw. Nothing is clamped, rounded toward zero or
        otherwise made to look better on the way out — DRIFT_CAP is a
        plausibility mark `_report_drift` puts into words, not a limit
        applied here.
        """
        try:
            got = lensdata.read_focus(frame)
        except Exception:                                   # noqa: BLE001
            got = None
        measured, stop_flag = got if got else (None, 0)

        # The body flags it when the lens is jammed against a limit, so a
        # range that runs off the end of the lens can be reported instead of
        # silently photographing the same plane over and over. Correcting
        # drift cannot help here: the hardware is refusing the move, not
        # losing it, and every frame past the stop is a duplicate.
        if stop_flag == 2 and not self._warned_far_stop:
            self._warned_far_stop = True
            self._log("  The lens has hit its FAR limit — the rest of this "
                      "stack is photographing the same plane. B is set past "
                      "the end of the lens; reduce it or re-run Calibrate.",
                      ACCENT_RED)
        elif stop_flag == 1 and not self._warned_near_stop:
            self._warned_near_stop = True
            self._log("  The lens is against its NEAR limit — frames here are "
                      "duplicates. A is set past the end of the lens.",
                      ACCENT_RED)

        if measured is None:
            if not self._drift_unreadable:
                self._drift_unreadable = True
                self._log("  Cannot read the lens position from the frames, "
                          "so focus stays open loop. This needs Nikon RAW "
                          "(NEF) saved to the PC.", ACCENT_AMBER)
            return 0.0

        # ── learn how many ruler counts make one app step ────────────────
        # On this body it IS a constant — focus_cal.py:31-35 measured 10.00
        # with no scatter and retired the old 10.0-vs-11.67 reading as the
        # pre-PTP transport losing commands — so the fallback below is
        # already the right answer here, and this estimate earns its keep
        # only on a body nobody has measured. Every consecutive pair of
        # frames commanded to different places is a sample; the scale is the
        # MAX of them, for the reason spelled out below, and three samples
        # are required before it displaces POSITION_COUNTS_PER_STEP.
        prev = self._scale_prev
        self._scale_prev = (measured, self.current_pos)
        if prev is not None and prev[1] != self.current_pos:
            s = (measured - prev[0]) / float(self.current_pos - prev[1])
            if SCALE_MIN <= s <= SCALE_MAX:
                self._scale_samples.append(s)
        if len(self._scale_samples) >= 3:
            # MAX, not median, for the same reason calibration uses it: a
            # lost command makes a pair move LESS than commanded and so
            # measure a scale that is too small, never too large. The median
            # is dragged down by every lossy pair, and a scale that is too
            # small makes every measured drift look larger than it is, so the
            # report cries fault where there is none. The largest sample is
            # the pair that lost the least.
            scale = max(self._scale_samples)
        else:
            scale = float(lensdata.POSITION_COUNTS_PER_STEP)

        # The ruler's zero is wherever the encoder's origin happens to be, so
        # it is only ever used as a DIFFERENCE. The reference is kept as the
        # raw (counts, step) PAIR rather than a derived offset, which means a
        # later refinement of the scale does not invalidate it.
        #
        # Pin it once per session, not once per stack: per stack would make
        # each stack internally consistent while leaving them offset from one
        # another, which is precisely the fault being fixed.
        if self._focus_origin is None:
            self._focus_origin = (measured, self.current_pos)
            return 0.0

        ref_counts, ref_pos = self._focus_origin
        err = ((measured - ref_counts) / scale) - (self.current_pos - ref_pos)

        # Do not fight a wall. If the lens is against a mechanical stop and
        # the correction would drive it further into that stop, the commands
        # cannot achieve anything: the hardware is refusing the move, not
        # losing it. Reporting a correction here also reads as a focus fault
        # when the real fault is that the range extends past the end of the
        # lens, which is a different thing to fix.
        if (stop_flag == 2 and err < 0) or (stop_flag == 1 and err > 0):
            return 0.0
        if abs(err) < 0.5:
            return 0.0
        # THE MEASUREMENT GOES BACK, not a bounded version of it. Clamping to
        # DRIFT_CAP here is what made a genuine 60-step fault reach the user
        # as "+25", and there was nothing left for the clamp to protect: the
        # loop that once steered by this number is gone, so all the bound did
        # was make the fault look smaller than it is. A wild reading is still
        # worth flagging as wild — `_report_drift` does that in words, which
        # costs the user nothing and costs the number nothing either.
        self._log(f"    lens is {err:+.0f} step(s) off plan", TEXT_MUTED)
        return err

    def _do_gap_move(self, intervals, img_idx, dir_now, sign):
        """
        Advance the lens by the planned gap between two shots in a stack.

        One confirmed command, of exactly the planned size — nothing is
        subtracted for measured drift. `_report_drift` watches rather than
        corrects; see its docstring for why.

        `on_limit` is the one thing read back off the move: it fires when the
        lens ran into a mechanical stop, which means it may have gone less far
        than commanded — while `self.current_pos` below still advances by the
        whole planned gap. That is left alone on purpose (see `drive_focus`:
        the distance actually covered is not knowable, and the next
        `_anchor_to` re-measures the position off the stop anyway). What is
        new is that the user is told, which is the part that used to be
        missing.
        """
        planned = intervals[img_idx]
        want = sign * planned                       # signed, toward FAR
        steps = int(round(abs(want)))
        direction = "Plus" if want > 0 else "Minus"

        if self._verbose_moves:
            # Narrate the first stack so a run that looks static can be told
            # apart from one that never issues a command.
            self._log(
                f"    gap {img_idx + 1}: {planned} step(s) {dir_now}",
                TEXT_MUTED)

        if steps:
            drive_focus(self.camera, direction, steps,
                        check_stop=self._stop.is_set,
                        on_limit=lambda: self._gap_hit_stop(want > 0))
        self.current_pos += sign * planned
        self.signals.focus_pos.emit(self.current_pos)
        self._check_stop()

    def _gap_hit_stop(self, toward_far: bool) -> None:
        """
        The camera reported a mechanical stop partway through a gap move.

        Once per run, not once per gap: every later gap in the same direction
        runs into the same stop, so an unguarded version would write a line
        per remaining frame of every remaining stack.

        The flags are SHARED with `_measure_drift` rather than two new ones,
        because the two are the same fact from different evidence — the
        drive's own return code here, byte 0x56 of the NEF there — and the
        user wants telling once, not twice.

        Which of the two speaks first depends on the mode, and in one mode
        only this one can speak at all: keep-on-camera puts no frame on the
        PC, so `_measure_drift` never runs and this is the ONLY warning that
        the focus range runs off the end of the lens.

        HEDGED ON PURPOSE, both clauses. All AT_LIMIT proves is that the lens
        is against the stop now — never how far into the move it got there
        (see `drive_focus`), and never that it was cut short at all. A gap
        that lands EXACTLY on the stop reports the same thing, and the one
        setup where that happens routinely is a reversed serpentine stack
        with A sitting on the near stop: its last gap arrives dead on 0 and
        nothing at all is wrong. So "at or past the end", not "past", and
        "any remaining gap WOULD be duplicates" rather than a flat claim that
        some are — on the last gap of a stack there are none.
        """
        if toward_far and not self._warned_far_stop:
            self._warned_far_stop = True
            self._log("  The lens hit its FAR stop during a focus move — it is "
                      "against the stop now, so any remaining gap in this "
                      "stack moves nothing and those frames would be "
                      "duplicates. B is at or past the end of the lens; reduce "
                      "it or re-run Calibrate.", ACCENT_RED)
        elif not toward_far and not self._warned_near_stop:
            self._warned_near_stop = True
            self._log("  The lens hit its NEAR stop during a focus move — it is "
                      "against the stop now, so any remaining gap in this "
                      "stack moves nothing and those frames would be "
                      "duplicates. A is at or past the end of the lens; move "
                      "it or re-run Calibrate.", ACCENT_RED)

    def _anchor_to(self, target: int) -> bool:
        """
        Re-establish the lens at `target` by way of the near stop.

        Returns False if there is no known stop to anchor on, in which case
        the caller falls back to a relative move.

        The point is independence: a reposition measured from the step counter
        inherits whatever error that counter has already accumulated, so any
        one bad stack quietly shifts every stack after it. Anchoring measures
        from the mechanical stop instead, which cannot drift, so each stack
        starts in the same place regardless of what came before.
        """
        if not self.live.get_bool("focus_homed", False):
            return False
        limit_lo = self.live.get_int("focus_limit_lo", 0)
        self._log(f"  Re-anchoring on the near stop, then out to "
                  f"{target}…", ACCENT_CYAN)
        # `expected_from` makes the sweep audit the datum as well as use it.
        # Worth having most here of anywhere: this runs between every stack,
        # so a focus ring nudged mid-session is caught on the next reposition
        # rather than discovered in the files afterwards.
        self.current_pos = anchored_drive(
            self.camera, target, limit_lo, check_stop=self._stop.is_set,
            expected_from=self.current_pos,
            warn=lambda m: self._log(f"  ⚠  {m}", ACCENT_AMBER))
        self.signals.focus_pos.emit(self.current_pos)
        self._check_stop()
        return True

    def _goto_start(self, pos_a: int):
        """
        Drive from wherever the lens is to A.

        Anchored on the near stop when one is known (see `_anchor_to`),
        because the alternative trusts a step counter that drifts. Without a
        stop it falls back to a plain direct move.

        The fallback is deliberately DIRECT — no overshoot-and-return leg to
        take up backlash. This lens shows none worth compensating for, and the
        extra legs would nearly quadruple the commands a reposition costs.
        """
        if self._anchor_to(pos_a):
            return
        diff = pos_a - self.current_pos
        if diff == 0:
            return
        direction = "Plus" if diff > 0 else "Minus"
        self._log(f"  Moving to A ({abs(diff)} steps)…", ACCENT_CYAN)
        moved = drive_focus(self.camera, direction, abs(diff),
                            check_stop=self._stop.is_set)
        self.current_pos += (1 if direction == "Plus" else -1) * moved
        self.signals.focus_pos.emit(self.current_pos)

    def _reposition(self, reset_mode: str, pos_a: int):
        """
        Put the lens back on the plan for the next stack.

        Two paths, and neither is a plain move back to A. Serpentine — the
        default — deliberately does NOT return: the next stack runs the other
        way from where this one ended, so the target is `self.current_pos`,
        and `_anchor_to` re-establishes it by way of the near stop. Every
        other reset mode goes through `_goto_start(pos_a)`, which anchors on
        the stop as well when one is known and only falls back to a direct
        move when it is not.

        "One confirmed command whatever the distance" — what this said until
        the audit — describes only that un-homed fallback. The anchored path
        is a sweep into the stop in STOP_SWEEP_CHUNK-sized pieces plus the
        drive back out, which is what a datum that cannot drift costs.
        """
        if reset_mode == "serpentine":
            # No repositioning MOVE is needed: the next stack simply runs the
            # other way from where this one ended. But that continuity is
            # exactly what lets drift compound here — serpentine never issues
            # a corrective move, so nothing ever resets the error, and by
            # stack 5 the range being photographed has quietly slid. So the
            # lens is put back on the plan without changing where it should
            # be: anchor on the stop, then come out to the position the
            # counter says this stack should start from.
            self._log("  Serpentine — shooting back the other way.", ACCENT_CYAN)
            self._anchor_to(self.current_pos)
            return

        # Anything else: back to A, via the stop when one is known.
        self._goto_start(pos_a)

    def _renumber(self, captured: List[Path], photo_count: int):
        """
        Serpentine stacks are shot far->near, so renumber them to match the
        near->far ordering of the others.  Only runs when we positively
        identified every file; otherwise the files are left alone.
        """
        if len(captured) != photo_count or photo_count < 2:
            self._log("  Reversed stack left in capture order "
                      "(could not match every file).", ACCENT_AMBER)
            return
        names = [p.name for p in captured]
        try:
            temps = []
            for p in captured:
                t = p.with_name(p.name + ".reorder")
                p.rename(t)
                temps.append(t)
            for i, t in enumerate(temps):
                t.rename(t.parent / names[photo_count - 1 - i])
            self._log("  Reversed stack renumbered to near→far order.",
                      TEXT_MUTED)
        except OSError as e:
            self._log(f"  Could not renumber reversed stack: {e}", ACCENT_AMBER)

