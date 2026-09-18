"""
Hardware layer — everything that talks to a device, and nothing that
knows about Qt.

Import graph inside this package, read off the modules rather than assumed —
the arrow points at the importer::

    constants ──> camera, images, table
    errors ─────> camera, table, focus_cal
    ptp ────────> camera, focus
    focus ──────> focus_cal
    lensdata ───> focus_cal
    settings ───> no other module here, only this __init__

That is module to module. This ``__init__`` on top of it imports constants,
errors, camera, focus, images and settings to build the package facade, plus
table behind the guard, table still being the only module that needs
pyserial. It does NOT import lensdata or focus_cal:
``run/`` and ``ui/`` reach those two by their own module paths, which is why
neither appears in ``__all__`` below.

``ptp`` is the transport (raw PTP over USB, needing pyusb) and ``camera``
is the client the app talks to. ``ptp`` is also the BOTTOM of the package —
it imports nothing from it — which is what makes ``from .ptp import
STOP_SWEEP_CHUNK`` in ``focus`` safe to do for real. The one edge that would
cycle, ``focus`` needing ``camera``, is annotation-only and stays behind
``if TYPE_CHECKING``: it is not a runtime edge and must not become one.
``constants`` and ``errors`` import nothing at all, from here or anywhere.

Nothing in this package imports Qt — verified, not hoped for — which is what
lets the camera and the table be driven headlessly, from a REPL or a plain
script, with no GUI running at all. That is how device behaviour gets
reproduced away from the app, and it is why the Qt boundary sits exactly
here: ``run/`` above this package does import Qt, and this package must not.

pyserial is optional at import time: without it the GUI still starts,
reports why, and leaves the run controls disabled — which is more useful
than a traceback on launch. Check ``HARDWARE_AVAILABLE`` before
constructing a ``ComximClient``.
"""

from __future__ import annotations

from .constants import (
    CAMERA_UNKNOWN, EVENT_DONE_TOKEN, IMAGE_EXTS, MSG_ERR, MSG_OK,
    SESSION_COUNTER_START,
)
from .camera import PTPCameraClient
from .errors import (
    CameraError, RotateDoneTimeout, TurntableError, TurntablePortClosed,
    TurntableWriteTimeout,
)
from .focus import (
    STEP_UNITS, anchored_drive, compute_step_angle, drive_focus,
    plan_focus_intervals, step_ratios,
)
from .images import count_images_in, list_image_names, wait_for_new_image
from .settings import LiveSettings, interruptible_sleep

HARDWARE_AVAILABLE = True
HARDWARE_IMPORT_ERROR = ""

try:
    from .table import ComximClient
except ImportError as _e:                                # pragma: no cover
    HARDWARE_AVAILABLE = False
    HARDWARE_IMPORT_ERROR = str(_e)
    ComximClient = None                                  # type: ignore[assignment]

__all__ = [
    "HARDWARE_AVAILABLE", "HARDWARE_IMPORT_ERROR",
    # constants
    "CAMERA_UNKNOWN", "EVENT_DONE_TOKEN", "IMAGE_EXTS", "MSG_ERR", "MSG_OK",
    "SESSION_COUNTER_START",
    # errors
    "CameraError", "RotateDoneTimeout", "TurntableError", "TurntablePortClosed",
    "TurntableWriteTimeout",
    # clients
    "ComximClient", "PTPCameraClient",
    # focus
    "STEP_UNITS", "anchored_drive", "compute_step_angle",
    "drive_focus", "plan_focus_intervals", "step_ratios",
    # misc
    "LiveSettings", "count_images_in", "interruptible_sleep",
    "list_image_names", "wait_for_new_image",
]
