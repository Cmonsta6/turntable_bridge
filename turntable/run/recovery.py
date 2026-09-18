"""
Device drop-out detection and recovery. Mixed into BridgeWorker.

KEY PRINCIPLE: every check in here runs only AFTER a command has already
failed, so the safe default is "assume something is wrong". Continuing
requires positive proof of health; raising the alarm never requires
positive proof of failure.

`_device_health` confirms twice, 0.3 s apart — two probes that disagree
mean a hang, not a drop. A drop deliberately does NOT consume the
capture-retry budget: a gap in a stack is worse than a duplicate frame at
the same focus plane.

A rotate is the dangerous one to retry — see RotateDoneTimeout: if only
the terminal done-token was lost the move already happened, and re-sending
silently double-rotates and desyncs every later frame.
"""
from __future__ import annotations

from typing import Callable

from ..hardware.errors import (
    RotateDoneTimeout, TurntableError, TurntablePortClosed,
)
from ..hardware.settings import interruptible_sleep
from ..ui.theme import ACCENT_AMBER, ACCENT_GREEN, ACCENT_RED


class DeviceRecoveryMixin:
    def _recover_camera(self, attempt: int, error: Exception) -> bool:
        """
        Called when a camera command fails mid-run. Returns False if the user
        asked to stop.

        There is nothing to unwedge: a command either comes back with a status
        code or the link is gone. So recovery is just two moves — drop the
        transport so the next call re-opens a clean one, and if the camera
        itself has gone away, wait for the user to bring it back.
        """
        self._last_recover_was_drop = False
        if self._stop.is_set():
            return False

        # Dropped BEFORE probing: ours may be the failed handle, and a stale
        # one would make a perfectly healthy camera look absent.
        try:
            self.camera.close()
        except Exception:
            pass

        if self._device_health() == "nocamera":
            self._last_recover_was_drop = True
            if not self._wait_for_camera():
                return False                 # user pressed Stop while waiting
            return not self._stop.is_set()

        wait = min(1.0 + attempt, 4.0)
        self._log(f"  Camera command failed ({error}). Recovery attempt "
                  f"{attempt} — reopening the connection…", ACCENT_AMBER)
        if not interruptible_sleep(wait, self._stop.is_set):
            return False
        return not self._stop.is_set()

    # ── device drop-out (camera unplugged / powered off / asleep) ─────
    def _device_health(self) -> str:
        """
        After a command has failed, work out whether the camera is still there:

          'ok'       – the camera answers
          'nocamera' – it is gone (unplugged, powered off, asleep)

        Confirmed twice, a third of a second apart, so a single blip during a
        stall does not raise a false alarm. If the two probes disagree we
        report 'ok' and let the ordinary retry run, because a flapping probe is
        a stall symptom rather than a drop-out.
        """
        def probe() -> str:
            try:
                return ("ok" if self.camera.camera_status() == "connected"
                        else "nocamera")
            except Exception:                               # noqa: BLE001
                return "nocamera"

        first = probe()
        if first == "ok":
            return "ok"
        if not interruptible_sleep(0.3, self._stop.is_set):
            return "ok"                 # stopping — let the caller unwind
        return first if probe() == first else "ok"

    def _ensure_device_or_wait(self) -> bool:
        """
        If the camera has dropped out, pause + alarm + wait for it. Returns
        True if we actually waited (so the caller knows a shot may have been
        lost and should be re-checked / re-fired), False if all was well.
        """
        if self._stop.is_set():
            return False
        if self._device_health() == "nocamera":
            self._wait_for_camera()
            return True
        return False

    def _wait_for_camera(self) -> bool:
        """Camera dropped out — pause, alarm, wait for it, then resume."""
        def back() -> bool:
            # Drop the transport before every probe: a reconnected camera
            # enumerates as a fresh USB device, so a stale handle would keep
            # reporting it absent long after it returned.
            try:
                self.camera.close()
            except Exception:
                pass
            return self.camera.camera_status() == "connected"

        return self._wait_for_device(
            status="nocamera",
            warn=("⚠  Camera disconnected — the run is paused. Reconnect the "
                  "camera (check the USB cable and that it is powered on and "
                  "awake); capture resumes automatically once it is back."),
            ready=back,
            ready_msg="✓  Camera reconnected — resuming.")

    def _wait_for_device(self, status: str, warn: str,
                         ready: Callable[[], bool], ready_msg: str) -> bool:
        """
        A device (camera or turntable) went away mid-run. Pause the run, warn
        loudly, sound the repeating scale alarm, and poll `ready()` until the
        device is back, then resume. Returns True once ready, False if the
        user pressed Stop. Shared by the camera and turntable drop handlers so
        every "device lost" path behaves identically.
        """
        if self._stop.is_set():
            return False
        self.signals.status_changed.emit(status)
        self._log(warn, ACCENT_RED)
        self._start_alarm()
        try:
            while not self._stop.is_set():
                # Poll on a gentle cadence; the alarm keeps sounding meanwhile.
                if not interruptible_sleep(1.5, self._stop.is_set):
                    break
                try:
                    if ready():
                        self._log(ready_msg, ACCENT_GREEN)
                        self.signals.status_changed.emit("running")
                        return True
                except Exception:                           # noqa: BLE001
                    pass
        finally:
            self._stop_alarm()
        return False

    # ── turntable drop-out / glitch ──────────────────────────────────
    def _rotate_with_recovery(self, direction: int, angle: float,
                              timeout: float, stop_cb: Callable[[], bool]):
        """
        Rotate the table, transparently recovering from a serial glitch or a
        disconnected/wedged turntable: reconnect the port and retry, and if it
        stays unreachable, pause the run (alarm) until the user fixes it, then
        give the rotation one fresh set of retries.

        THREE ways out, not two. A Stop and a real controller error from an
        open port each break out at once — and so does running out of retries:
        `tt_retries` (default 3) quick attempts, then the user-wait, then
        `tt_retries` more, after which the last exception is re-raised. That
        bound is deliberate; see the comment above the final `raise`. Ending
        the run leaves a checkpoint the user can Recover from, which is worth
        more than spinning forever against a table that answers comms but
        will not turn.
        """
        max_retries = max(self.live.get_int("tt_retries", 3), 1)
        attempt = 0
        waited = False
        while True:
            try:
                self.tt.rotate_single(direction, angle, timeout, abort=stop_cb)
                return
            except InterruptedError:
                raise                       # Stop pressed mid-rotation
            except RotateDoneTimeout:
                # The move was acknowledged and has (almost certainly) finished;
                # only the terminal 'done' token was lost. Re-sending would
                # double-rotate the table and desync the angle, so DON'T. Just
                # clear any glitched RX state and treat the rotation as done.
                self._log("  Rotation acknowledged but its 'done' signal was "
                          "lost — treating the move as complete (not re-sending, "
                          "which would double-rotate).", ACCENT_AMBER)
                try:
                    self.tt.drain_rx()
                except Exception:
                    pass
                return
            except Exception as e:          # noqa: BLE001
                # A REAL controller error from an OPEN port is not retryable —
                # reconnecting won't change it. Everything else is recoverable:
                # a bare timeout; a CLOSED-port TurntablePortClosed (e.g.
                # Windows hasn't released the COM port yet after a reconnect);
                # and TurntableWriteTimeout, which is a TurntablePortClosed
                # subclass precisely so it lands here — an open port that will
                # not take bytes wants reopening, not a run abandoned. That last
                # one used to be a HANG rather than an exception, because the
                # port was opened with no write timeout at all; see
                # `ComximClient.WRITE_TIMEOUT_S`.
                if (isinstance(e, TurntableError)
                        and not isinstance(e, TurntablePortClosed)
                        and self.tt.is_open):
                    raise
                attempt += 1
                if attempt <= max_retries:
                    # Quick reconnect + retry for a transient serial glitch.
                    if not self._recover_turntable(attempt, max_retries, e):
                        self._check_stop()
                        raise
                    continue
                if not waited:
                    # Quick retries exhausted — the table is really gone
                    # (unplugged / powered off). Pause and wait for the user
                    # (alarm), then give the rotation one fresh set of retries.
                    waited = True
                    if not self._wait_for_turntable():
                        self._check_stop()
                        raise
                    attempt = 0
                    continue
                # We already waited for the user AND retried, and it STILL
                # fails — the table answers comms but won't turn (jammed?), or
                # it was resumed too early. Stop retrying so the run ends with a
                # checkpoint the user can Recover from, rather than spinning
                # forever. A Stop is reported as such.
                self._check_stop()
                raise

    def _recover_turntable(self, attempt: int, max_retries: int,
                           error: Exception) -> bool:
        """
        One quick reconnect+backoff cycle for a glitched turntable. Returns
        True to retry the rotation, False only if the user pressed Stop. The
        caller bounds how many times this runs and escalates to the user-wait.
        """
        if self._stop.is_set():
            return False
        self._log(f"  Turntable did not respond ({error}). Reconnecting the "
                  f"serial port (attempt {attempt}/{max_retries})…",
                  ACCENT_AMBER)
        if self.tt.reconnect(retries=1):
            self._log("  Turntable reconnected — retrying the rotation.",
                      ACCENT_GREEN)
            interruptible_sleep(0.3, self._stop.is_set)
        else:
            # Port still not back — brief backoff before the next quick try.
            interruptible_sleep(min(0.5 + attempt, 3.0), self._stop.is_set)
        return not self._stop.is_set()

    def _wait_for_turntable(self) -> bool:
        """Turntable unreachable — pause, alarm, wait for it, then resume."""
        return self._wait_for_device(
            status="notable",
            warn=("⚠  Turntable not responding — the run is paused. Check the "
                  "serial/USB cable and power; if it is wedged, power-cycle "
                  "it. Capture resumes automatically once it answers."),
            ready=lambda: self.tt.reconnect(retries=0),
            ready_msg="✓  Turntable reconnected — resuming.")
