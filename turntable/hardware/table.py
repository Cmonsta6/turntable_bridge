"""
Comxim turntable client (serial).

The controller answers asynchronously, so a background reader thread feeds
a queue and callers wait on predicates. `reconnect()` reopens the port IN
PLACE, keeping object identity, so references held by the worker and the
window stay valid across a recovery.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, List, Optional

import serial

from .constants import EVENT_DONE_TOKEN, MSG_ERR, MSG_OK
from .errors import RotateDoneTimeout, TurntableError, TurntablePortClosed


class ComximClient:
    def __init__(self, port: str, baudrate: int = 115200,
                 read_timeout: float = 0.1):
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self._tx_lock = threading.Lock()
        self._closed = True                 # set False once the reader is up
        self.ser = None
        self._stop = threading.Event()
        self._rx_queue: "queue.Queue[str]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        try:
            self.ser = serial.Serial(port=port, baudrate=baudrate,
                                     timeout=read_timeout)
        except serial.SerialException as e:
            raise TurntableError(
                f"Cannot open {port} at {baudrate} baud: {e}"
            ) from e
        self._closed = False
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    # -- lifecycle --------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return not self._closed and bool(getattr(self.ser, "is_open", False))

    def _teardown(self):
        """Stop the reader thread and close the port, leaving the object
        marked closed. Idempotent — safe to call before a reconnect."""
        self._closed = True
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2)
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass

    def close(self):
        if self._closed and (self.ser is None or not getattr(self.ser, "is_open", False)):
            return
        self._teardown()

    def reconnect(self, retries: int = 0, backoff_s: float = 0.4,
                  handshake: bool = True) -> bool:
        """
        Close and reopen the serial port IN PLACE, keeping this object's
        identity so every reference (the worker's and the window's) stays
        valid. Used to auto-recover a turntable that glitched, was unplugged,
        or wedged mid-run.

        Returns True only when the port reopened AND (if handshake) the
        controller actually answered the CT+STARTUP handshake — so a True
        result means the table is genuinely back and ready to rotate. A port
        that opens but stays mute returns False, so callers keep waiting.
        """
        self._teardown()
        for i in range(max(retries, 0) + 1):
            try:
                ser = serial.Serial(port=self.port, baudrate=self.baudrate,
                                    timeout=self.read_timeout)
            except Exception:
                if i < retries:
                    time.sleep(backoff_s)
                continue
            self.ser = ser
            self._closed = False
            self._stop = threading.Event()
            self._rx_queue = queue.Queue()
            self._thread = threading.Thread(target=self._reader_loop,
                                            daemon=True)
            self._thread.start()
            if handshake:
                try:
                    self.init_session()
                except Exception:
                    # Port is open but the controller isn't answering yet.
                    # Keep the port open (a later call can retry) but report
                    # not-ready so a wait loop keeps polling.
                    if i < retries:
                        time.sleep(backoff_s)
                        continue
                    return False
            return True
        return False

    def _reader_loop(self):
        # Bind this thread's port, stop-flag and queue as locals. reconnect()
        # replaces self.ser / self._stop / self._rx_queue with fresh objects for
        # the NEXT reader; capturing them here means an old reader can never read
        # the new port or watch the new stop-flag — it sees its own closed port
        # (read raises) and its own set stop-flag, and exits cleanly. No zombie.
        ser = self.ser
        stop = self._stop
        rxq = self._rx_queue
        buf = ""
        while not stop.is_set():
            try:
                data = ser.read(256)
                if not data:
                    continue
                chunk = data.decode(errors="ignore")
            except Exception:
                # Port yanked out mid-run: stop reading rather than spin.
                if stop.is_set():
                    break
                time.sleep(0.2)
                continue
            chunk = chunk.replace("#", "")
            buf += chunk
            while ";" in buf:
                msg, buf = buf.split(";", 1)
                msg = msg.strip()
                if msg:
                    rxq.put(msg)

    # -- io ---------------------------------------------------------------
    def send(self, cmd: str):
        if self._closed:
            raise TurntablePortClosed("Turntable port is closed.")
        if not cmd.endswith(";"):
            cmd += ";"
        with self._tx_lock:
            self.ser.write(cmd.encode("ascii", errors="ignore"))

    def drain_rx(self):
        while True:
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break

    def wait_for(self, predicate, timeout_s: float,
                 abort: Optional[Callable[[], bool]] = None) -> List[str]:
        """
        Wait for a matching message.

        `abort` lets a Stop request break out of a long rotation wait
        instead of blocking for the full rotate timeout.
        """
        deadline = time.monotonic() + timeout_s
        seen: List[str] = []
        while time.monotonic() < deadline:
            if abort and abort():
                raise InterruptedError("Aborted while waiting for turntable")
            try:
                msg = self._rx_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            seen.append(msg)
            if predicate(msg):
                return seen
        raise TimeoutError(
            f"Timed out waiting for turntable response. Seen: {seen[-10:]}")

    # -- protocol ---------------------------------------------------------
    def init_session(self):
        self.drain_rx()
        self.send("CT+STARTUP()")
        self.send("CT+ACK(1)")
        self.send("CT+EVENT(1)")
        self.wait_for(
            lambda m: m.startswith(MSG_OK) or m.startswith(MSG_ERR),
            timeout_s=3,
        )

    # -- free running -----------------------------------------------------
    #
    # From the ComXim manual's CT Command List (6.3), not from guesswork — an
    # empty-argument probe of 78 candidate verbs found SETSPEED/SETDIR/SETMODE/
    # SETZERO but missed both of the ones that matter here, because they do not
    # follow the naming anyone would guess: stopping is SETSTOP, not STOP, and
    # starting is RUN.
    #
    # Continuous rotation is a MODE plus a trigger, not a command:
    #     SETMODE(0) -> SETDIR(d) -> SETSPEED(n) -> RUN()   ... SETSTOP()
    # The manual is explicit that "mode setting alone won't start rotation".
    #
    # None of this is used by a run. It exists so the subject can be turned
    # while the focus planes are being set, because an asymmetric subject
    # needs looking at from several angles before A and B are right.

    #: `CT+SETMODE` values.
    MODE_CONTINUOUS, MODE_STEP, MODE_SWING = 0, 1, 2

    #: `CT+SETDIR` values. Note the manual's sense: 0 is Left/CW.
    DIR_CW, DIR_CCW = 0, 1

    def _command(self, cmd: str, timeout_s: float = 3.0) -> List[str]:
        """Send one command and wait for its CR+OK / CR+ERR acknowledgement."""
        self.drain_rx()
        self.send(cmd)
        msgs = self.wait_for(
            lambda m: m.startswith(MSG_OK) or m.startswith(MSG_ERR),
            timeout_s=timeout_s)
        if any(m.startswith(MSG_ERR) for m in msgs):
            raise TurntableError(f"{cmd} refused: {msgs}")
        return msgs

    def mute(self, on: bool = True) -> None:
        """
        Silence the controller's beeper (`CT+SPKMUTE`).

        It chirps on every command it rejects, which during normal operation
        is never — but a run issues thousands of commands over hours beside
        someone trying to work, and one glitched link turns that into an
        alarm nobody asked for. Best effort: an older firmware without the
        verb answers CR+ERR and that is not worth failing a connection over.
        """
        try:
            self._command(f"CT+SPKMUTE({1 if on else 0})")
        except (TurntableError, TimeoutError):
            pass

    def set_speed(self, grade: int) -> None:
        """
        Speed level, 1-127, and HIGHER IS SLOWER — the grade behaves like a
        period, not a rate.

        0 is refused (CR+ERR=81), which `_command` turns into a TurntableError
        rather than a silently ignored setting, so 1 is the fastest this
        controller has. Above 32 it clamps: 64 and 127 measure identical to
        32. Both facts were timed on this table, not read from the manual —
        the timing script was a development tool and is not shipped, but see
        main_window.SPIN_GRADES, which carries the whole measured deg/s curve
        and is the one place to change if the grades are ever retuned.
        """
        self._command(f"CT+SETSPEED({int(grade)})")

    def spin(self, direction: int, speed: int) -> None:
        """Start turning and keep turning until `stop()`."""
        self._command(f"CT+SETMODE({self.MODE_CONTINUOUS})")
        self._command(f"CT+SETDIR({int(direction)})")
        self.set_speed(speed)
        self._command("CT+RUN()")

    def stop(self) -> None:
        """Halt a free run. Harmless on a table that is already still."""
        self._command("CT+SETSTOP()")

    def get_offset_angle(self) -> Optional[float]:
        """
        Signed degrees from the zero point, or None if this firmware does not
        answer (the manual dates it to V2R03C01).

        This is the readback the CAMERA has no equivalent of, and it is what
        makes a free spin safe to offer: after turning the table by hand the
        app does not have to infer where it ended up, it can ask. Without it a
        Stop button would leave the dial confidently wrong.
        """
        self.drain_rx()
        self.send("CT+GETOFFSETANGLE()")
        try:
            msgs = self.wait_for(
                lambda m: "OffsetAngle" in m or m.startswith(MSG_ERR),
                timeout_s=2.0)
        except TimeoutError:
            return None
        for m in msgs:
            if "OffsetAngle" in m:
                try:
                    return float(m.split("=", 1)[1].split(";")[0].strip())
                except (IndexError, ValueError):
                    return None
        return None

    def rotate_single(self, direction: int, angle_deg: float,
                      timeout_s: float,
                      abort: Optional[Callable[[], bool]] = None):
        # Drop anything left over (e.g. a late TB_END from a previous move)
        # so we cannot mistake it for this rotation finishing.
        self.drain_rx()
        self.send(f"CT+TURNSINGLE({direction},{angle_deg})")
        # Phase 1 — acknowledgement. A timeout here means NO reply at all: the
        # command almost certainly never landed (dead/glitched link), so it's
        # a plain TimeoutError the caller may safely reconnect-and-resend.
        msgs = self.wait_for(
            lambda m: m.startswith(MSG_OK) or m.startswith(MSG_ERR),
            timeout_s=3, abort=abort,
        )
        if any(m.startswith(MSG_ERR) for m in msgs):
            raise TurntableError(f"Turntable returned error: {msgs}")
        # Phase 2 — completion. We already saw OK, so the move was accepted and
        # is under way. A timeout waiting for the terminal 'done' token means
        # the move almost certainly finished (a single step can't exceed the
        # rotate timeout) and only the token was dropped. Re-sending would
        # double-rotate, so signal this distinctly.
        try:
            self.wait_for(lambda m: EVENT_DONE_TOKEN in m,
                          timeout_s=timeout_s, abort=abort)
        except TimeoutError as e:
            raise RotateDoneTimeout(
                f"Rotate acknowledged but no '{EVENT_DONE_TOKEN}' within "
                f"{timeout_s}s — treating the move as complete.") from e
