"""
Where the app's on-disk assets live.

`camera.png` and the chime/alarm .wav files sit in the project root next
to the launcher. Every module that wants one has to resolve it from HERE
rather than from its own ``__file__`` — a module two directories deep
would otherwise look inside ``turntable/ui/``, silently miss
`camera.png` (falling back to the crude drawn camera) and re-synthesise
the chimes in the wrong place.

Frozen builds need a second rule. PyInstaller's one-file mode unpacks the
bundle into a temp directory and points ``sys._MEIPASS`` at it, so that
is where the bundled assets are — but it is wiped on exit, so anything
the app *generates* has to go next to the .exe instead. Hence two roots:

    ASSET_DIR     read from here   (bundle when frozen, project root otherwise)
    WRITABLE_DIR  write to here    (next to the .exe when frozen, same otherwise)

`asset_path()` picks the right one: an existing bundled file wins,
otherwise you get a writable path to create it at.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _frozen() -> bool:
    """True when running from a PyInstaller (or similar) bundle."""
    return getattr(sys, "frozen", False)


def _asset_dir() -> Path:
    if _frozen():
        # _MEIPASS is the one-file unpack dir; in one-dir mode it is absent
        # and the assets sit next to the executable.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable_dir() -> Path:
    if _frozen():
        # Never write into _MEIPASS — it is deleted when the app exits.
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


#: Where bundled/shipped assets are read from.
ASSET_DIR = _asset_dir()

#: Where generated files (the synthesised fallback chimes) are written.
WRITABLE_DIR = _writable_dir()


def asset_path(name: str) -> Path:
    """
    Path to an asset file.

    Returns the shipped copy if it exists; otherwise a writable path in
    the same directory as the executable, so a caller that generates a
    missing asset puts it somewhere that survives.
    """
    shipped = ASSET_DIR / name
    if shipped.is_file():
        return shipped
    return WRITABLE_DIR / name
