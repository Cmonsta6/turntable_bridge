"""
The capture run: a QThread worker plus the two mixins it is built from.

``worker``      the session/round/rotation loop, stop/pause gating, alerts
``stacking``    shooting one focus stack and repositioning around it
``recovery``    drop-out detection and the wait-for-device ladders
``checkpoint``  RunCheckpoint (resume point) and the Qt signal bundle
"""

from __future__ import annotations

from .checkpoint import BridgeSignals, RunCheckpoint
from .recovery import DeviceRecoveryMixin
from .stacking import StackShooterMixin
from .worker import BridgeWorker

__all__ = [
    "BridgeSignals", "BridgeWorker", "DeviceRecoveryMixin", "RunCheckpoint",
    "StackShooterMixin",
]
