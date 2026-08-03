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


class RotateDoneTimeout(TurntableError):
    """The turntable acknowledged the rotate (we saw OK) but its terminal
    'done' event never arrived. The move has almost certainly completed and
    only the token was lost, so the rotation MUST NOT be re-sent (that would
    double-rotate). Distinct from a plain timeout, where no acknowledgement
    was seen at all and re-sending is safe."""
    pass
