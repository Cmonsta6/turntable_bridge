"""
Live settings box and the interruptible sleep built on top of it.

Together these are the fix for "changing a setting mid-run does nothing":
the worker reads each value at the moment it needs it, and every wait is
sliced so a Stop is noticed promptly.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional


class LiveSettings:
    """
    A tiny thread-safe key/value box shared between the GUI thread (writer)
    and the capture worker thread (reader).

    v1 froze every setting into a BridgeConfig at Start, so edits made while
    a run was in progress went nowhere.  Here the worker calls .get() at the
    exact moment it needs a value, which means a change lands on the very
    next use — no restart required.

    Widgets are *not* read directly from the worker thread: Qt objects are
    not thread-safe, so the GUI pushes values in here instead.
    """

    def __init__(self, **initial: Any):
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(initial)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self.get(key, default))

    def set(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if self._data.get(k, object()) != v:
                    self._data[k] = v

    def update(self, mapping: Dict[str, Any]) -> None:
        self.set(**mapping)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data


def interruptible_sleep(duration_s: float,
                        stop: Optional[Callable[[], bool]] = None,
                        slice_s: float = 0.05) -> bool:
    """
    Sleep in small slices so a Stop request is noticed quickly.

    Returns False if `stop()` went True during the wait, else True.
    v1 used bare time.sleep(cfg.capture_wait_s), which meant Stop could sit
    unnoticed for a couple of seconds on every single frame.
    """
    if duration_s <= 0:
        return not (stop and stop())
    deadline = time.monotonic() + duration_s
    while True:
        if stop and stop():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(slice_s, remaining))
