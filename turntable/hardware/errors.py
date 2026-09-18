"""
Exception types for the hardware layer.

Kept dependency-free (no pyserial, no http) so that every other module —
including the recovery mixin, whose `except` clauses need these at
runtime — can import them unconditionally.
"""


class CameraError(RuntimeError):
    """Error communicating with the camera."""
    pass

class TurntableError(RuntimeError):
    pass


class TurntablePortClosed(TurntableError):
    """Raised by send() when the port is closed — a RECOVERABLE state (e.g.
    Windows hasn't released the COM port yet right after a reconnect), not a
    controller error. Callers route it through the reconnect ladder."""
    pass


class TurntableWriteTimeout(TurntablePortClosed):
    """
    A write to the serial port did not complete in time — the port is open but
    the OS will not take the bytes.

    A SUBCLASS OF TurntablePortClosed ON PURPOSE, because an open-but-wedged
    port needs the same handling as a closed one and precisely NOT the handling
    a controller error gets: `recovery._rotate_with_recovery` re-raises a plain
    `TurntableError` from an open port as unrecoverable, and this must go down
    the reconnect ladder instead. Inheriting routes it correctly everywhere
    without a single `except` clause changing.

    It exists as a type at all because the alternative is letting pyserial's
    `SerialTimeoutException` escape into callers, and this module is
    deliberately dependency-free so that every `except` clause in the app can
    import it unconditionally.

    THE FAILURE IT REPLACES was a hang, not an error. `ComximClient` used to
    open the port with no write timeout, which on Windows means no OS write
    timeout at all, and pyserial then waits on the overlapped result forever.
    A run that hit it froze inside `send()` holding the transmit lock, with
    Stop unable to reach it — see `ComximClient.WRITE_TIMEOUT_S`.
    """
    pass


class RotateDoneTimeout(TurntableError):
    """The turntable acknowledged the rotate (we saw OK) but its terminal
    'done' event never arrived. The move has almost certainly completed and
    only the token was lost, so the rotation MUST NOT be re-sent (that would
    double-rotate). Distinct from a plain timeout, where no acknowledgement
    was seen at all and re-sending is safe."""
    pass
