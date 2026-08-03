"""
The worker's value types: a resume checkpoint and the Qt signal bundle.

RunCheckpoint is set at the top of every stack AND again after each
successful rotate — a Stop inside the settle window would otherwise leave
the checkpoint one stack behind where the table physically is.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal


@dataclass
class RunCheckpoint:
    """
    Where a run is physically parked, captured at the top of every stack so a
    run can be resumed after ANY interruption (turntable glitch, camera
    unplugged, card full, flat battery, …). The turntable is parked at
    `stack_in_round` of `round_idx` with the dial at `cumulative_angle`;
    `reverse_stack` is the serpentine state for that stack.
    """
    round_idx: int              # 1-based revolution
    stack_in_round: int         # 0-based stack index we're parked at
    stacks_per_round: int
    cumulative_angle: float
    reverse_stack: bool
    name: str                   # subject name, for the log/UI


class BridgeSignals(QObject):
    log = Signal(str, str)
    round_changed = Signal(int)
    stack_changed = Signal(int, int)          # index in round, stacks per round
    images_changed = Signal(int, int)         # done, total
    angle_changed = Signal(float)
    focus_pos = Signal(int)
    stack_done = Signal(float)                # seconds the stack took
    revolution_done = Signal(int)             # revolution number just finished
    status_changed = Signal(str)
    checkpoint_changed = Signal(object)       # RunCheckpoint — resume point
    finished = Signal()
