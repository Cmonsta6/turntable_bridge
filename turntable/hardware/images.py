"""
Watching a capture folder for files to actually land.

Used in download-to-PC mode. `count_images_in` does the per-stack tally;
the folder watch is only the fallback after a hiccuped capture, because the
ordinary path takes the path `capture()` already wrote (see
`wait_for_new_image`). In keep-on-camera mode nothing reaches the PC at
all, so none of this runs.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional, Set

from .constants import IMAGE_EXTS


def count_images_in(folder: Path) -> int:
    """Count image files in folder (non-recursive + one level deep)."""
    count = 0
    try:
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                count += 1
            elif p.is_dir():
                for child in p.iterdir():
                    if child.is_file() and child.suffix.lower() in IMAGE_EXTS:
                        count += 1
    except OSError:
        pass
    return count


def list_image_names(folder: Path) -> Set[str]:
    """Names of image files directly inside `folder` (cheap, one scandir)."""
    out: Set[str] = set()
    try:
        import os
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_file():
                    name = entry.name
                    dot = name.rfind(".")
                    if dot >= 0 and name[dot:].lower() in IMAGE_EXTS:
                        out.add(name)
    except OSError:
        pass
    return out


def wait_for_new_image(folder: Path,
                       known: Set[str],
                       timeout_s: float,
                       poll_s: float = 0.05,
                       stable_ms: int = 250,
                       stop: Optional[Callable[[], bool]] = None
                       ) -> Optional[Path]:
    """
    Block until a new image file appears in `folder` and stops growing.

    THE POST-HICCUP FALLBACK, not the ordinary wait. This used to be the big
    timing win over v1 — v1 slept a flat capture_wait_s (default 2 s) after
    *every* frame whether the file took 0.4 s or 1.9 s, and watching the
    folder ended the wait as soon as the file was actually on disk. Nothing
    is asynchronous any more: `ptp.shoot` does `path.write_bytes(payload)`
    and returns that path, so on the ordinary path the file is closed on disk
    before `capture()` returns and run/stacking.py takes the path directly
    (see "THE ORDINARY PATH NEEDS NO POLLING" there). Polling for a file that
    already exists cost a scandir plus the full stable_ms settle on every
    frame of every stack and bought nothing.

    What is left is the case that path cannot answer: `capture()` raised, or
    came back with a path that is not on disk. Be clear about what the
    folder can settle there. The only writer into the capture folder
    anywhere in the app is `ptp.shoot`'s `path.write_bytes(payload)`, and
    recovery writes no image at all — it closes the transport, probes
    camera_status and sleeps — so a capture that raised never wrote the
    bytes and this wait will normally run out its timeout. That timeout is
    the useful answer: it is how run/stacking.py learns nothing landed,
    which is what licenses re-firing the frame instead of moving on with a
    hole in the stack. What the folder cannot say is whether the SHUTTER
    fired — the trigger and the transfer fail separately — which is why
    stacking never re-sends on the strength of an error alone. The
    stability wait only bites if a file does turn up; with nothing writing
    asynchronously any more it is insurance, not a mechanism.

    Returns the new file, or None if it never showed up / we were stopped.
    """
    deadline = time.monotonic() + max(timeout_s, 0.05)
    stable_s = max(stable_ms, 0) / 1000.0
    candidate: Optional[Path] = None
    last_size = -1
    last_change = 0.0

    while time.monotonic() < deadline:
        if stop and stop():
            return None

        if candidate is None:
            new_names = list_image_names(folder) - known
            if new_names:
                # If several land at once (RAW+JPEG), take the newest.
                paths = [folder / n for n in new_names]
                try:
                    candidate = max(paths, key=lambda p: p.stat().st_mtime)
                except OSError:
                    candidate = paths[0]
                last_size = -1
                last_change = time.monotonic()
        else:
            try:
                size = candidate.stat().st_size
            except OSError:
                size = -1
            now = time.monotonic()
            if size != last_size:
                last_size = size
                last_change = now
            elif size > 0 and (now - last_change) >= stable_s:
                return candidate

        time.sleep(poll_s)

    return None
