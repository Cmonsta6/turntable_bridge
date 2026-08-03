"""
Reading the lens's real position back out of a captured frame.

No absolute focus position comes over the wire — checked against all 280 of
the camera's device properties and the LiveView header — so the app otherwise
counts the steps it asked for and has no independent way to check them.

Nikon bodies do record the lens state, in the MakerNote's LensData block.
It is encrypted with Nikon's own cipher, keyed on the body serial number
and the shutter count. Because the shutter count changes on every frame,
the ciphertext changes on every frame even when nothing about the lens
did, so raw bytes cannot be compared: it has to be decrypted properly.

TWO LAYERS, and this docstring used to admit only the first. The lower one
is DELIBERATELY LENS-AGNOSTIC: `decrypt`, `read_lens_state`, `same_position`
and `diff_bytes` never interpret a byte. Two frames taken at the same focus
decrypt to identical plaintext, so the only question that layer ever asks is
"did this change?". That works across LensData versions, lenses and bodies
without a per-model table, and it is what made travel calibration possible
on hardware nobody had tested this against.

The layer above it does know exactly which byte, and it is the one the app
actually reads. POSITION_OFFSET (0x5A), POSITION_COUNTS_PER_STEP (10) and
LIMIT_FLAG_OFFSET (0x56) are a field map derived from 176 frames across four
runs — see the comment above those constants — and they are consumed by
`read_position`, `at_mechanical_stop` and `read_focus`, the last of which is
what focus_cal.py's frame-based calibration and run/stacking.py's drift
check call on live frames. So the agnostic path is no longer the module's
whole approach; it is the portable fallback, and the offsets are the
measured fast path that would have to be re-derived on a body this has never
been run against.

Nikon-only, and gracefully absent otherwise: every entry point returns None
rather than raising if the frame is not a Nikon RAW, the block is missing,
or the keys cannot be read.
"""

from __future__ import annotations

from typing import Optional

# Nikon's two substitution tables, as published in exiftool's Nikon.pm.
# Written as integer rows rather than one long hex literal so they survive
# being copied between files intact.
_XLAT0 = bytes([
    0xc1, 0xbf, 0x6d, 0x0d, 0x59, 0xc5, 0x13, 0x9d,
    0x83, 0x61, 0x6b, 0x4f, 0xc7, 0x7f, 0x3d, 0x3d,
    0x53, 0x59, 0xe3, 0xc7, 0xe9, 0x2f, 0x95, 0xa7,
    0x95, 0x1f, 0xdf, 0x7f, 0x2b, 0x29, 0xc7, 0x0d,
    0xdf, 0x07, 0xef, 0x71, 0x89, 0x3d, 0x13, 0x3d,
    0x3b, 0x13, 0xfb, 0x0d, 0x89, 0xc1, 0x65, 0x1f,
    0xb3, 0x0d, 0x6b, 0x29, 0xe3, 0xfb, 0xef, 0xa3,
    0x6b, 0x47, 0x7f, 0x95, 0x35, 0xa7, 0x47, 0x4f,
    0xc7, 0xf1, 0x59, 0x95, 0x35, 0x11, 0x29, 0x61,
    0xf1, 0x3d, 0xb3, 0x2b, 0x0d, 0x43, 0x89, 0xc1,
    0x9d, 0x9d, 0x89, 0x65, 0xf1, 0xe9, 0xdf, 0xbf,
    0x3d, 0x7f, 0x53, 0x97, 0xe5, 0xe9, 0x95, 0x17,
    0x1d, 0x3d, 0x8b, 0xfb, 0xc7, 0xe3, 0x67, 0xa7,
    0x07, 0xf1, 0x71, 0xa7, 0x53, 0xb5, 0x29, 0x89,
    0xe5, 0x2b, 0xa7, 0x17, 0x29, 0xe9, 0x4f, 0xc5,
    0x65, 0x6d, 0x6b, 0xef, 0x0d, 0x89, 0x49, 0x2f,
    0xb3, 0x43, 0x53, 0x65, 0x1d, 0x49, 0xa3, 0x13,
    0x89, 0x59, 0xef, 0x6b, 0xef, 0x65, 0x1d, 0x0b,
    0x59, 0x13, 0xe3, 0x4f, 0x9d, 0xb3, 0x29, 0x43,
    0x2b, 0x07, 0x1d, 0x95, 0x59, 0x59, 0x47, 0xfb,
    0xe5, 0xe9, 0x61, 0x47, 0x2f, 0x35, 0x7f, 0x17,
    0x7f, 0xef, 0x7f, 0x95, 0x95, 0x71, 0xd3, 0xa3,
    0x0b, 0x71, 0xa3, 0xad, 0x0b, 0x3b, 0xb5, 0xfb,
    0xa3, 0xbf, 0x4f, 0x83, 0x1d, 0xad, 0xe9, 0x2f,
    0x71, 0x65, 0xa3, 0xe5, 0x07, 0x35, 0x3d, 0x0d,
    0xb5, 0xe9, 0xe5, 0x47, 0x3b, 0x9d, 0xef, 0x35,
    0xa3, 0xbf, 0xb3, 0xdf, 0x53, 0xd3, 0x97, 0x53,
    0x49, 0x71, 0x07, 0x35, 0x61, 0x71, 0x2f, 0x43,
    0x2f, 0x11, 0xdf, 0x17, 0x97, 0xfb, 0x95, 0x3b,
    0x7f, 0x6b, 0xd3, 0x25, 0xbf, 0xad, 0xc7, 0xc5,
    0xc5, 0xb5, 0x8b, 0xef, 0x2f, 0xd3, 0x07, 0x6b,
    0x25, 0x49, 0x95, 0x25, 0x49, 0x6d, 0x71, 0xc7,
])
_XLAT1 = bytes([
    0xa7, 0xbc, 0xc9, 0xad, 0x91, 0xdf, 0x85, 0xe5,
    0xd4, 0x78, 0xd5, 0x17, 0x46, 0x7c, 0x29, 0x4c,
    0x4d, 0x03, 0xe9, 0x25, 0x68, 0x11, 0x86, 0xb3,
    0xbd, 0xf7, 0x6f, 0x61, 0x22, 0xa2, 0x26, 0x34,
    0x2a, 0xbe, 0x1e, 0x46, 0x14, 0x68, 0x9d, 0x44,
    0x18, 0xc2, 0x40, 0xf4, 0x7e, 0x5f, 0x1b, 0xad,
    0x0b, 0x94, 0xb6, 0x67, 0xb4, 0x0b, 0xe1, 0xea,
    0x95, 0x9c, 0x66, 0xdc, 0xe7, 0x5d, 0x6c, 0x05,
    0xda, 0xd5, 0xdf, 0x7a, 0xef, 0xf6, 0xdb, 0x1f,
    0x82, 0x4c, 0xc0, 0x68, 0x47, 0xa1, 0xbd, 0xee,
    0x39, 0x50, 0x56, 0x4a, 0xdd, 0xdf, 0xa5, 0xf8,
    0xc6, 0xda, 0xca, 0x90, 0xca, 0x01, 0x42, 0x9d,
    0x8b, 0x0c, 0x73, 0x43, 0x75, 0x05, 0x94, 0xde,
    0x24, 0xb3, 0x80, 0x34, 0xe5, 0x2c, 0xdc, 0x9b,
    0x3f, 0xca, 0x33, 0x45, 0xd0, 0xdb, 0x5f, 0xf5,
    0x52, 0xc3, 0x21, 0xda, 0xe2, 0x22, 0x72, 0x6b,
    0x3e, 0xd0, 0x5b, 0xa8, 0x87, 0x8c, 0x06, 0x5d,
    0x0f, 0xdd, 0x09, 0x19, 0x93, 0xd0, 0xb9, 0xfc,
    0x8b, 0x0f, 0x84, 0x60, 0x33, 0x1c, 0x9b, 0x45,
    0xf1, 0xf0, 0xa3, 0x94, 0x3a, 0x12, 0x77, 0x33,
    0x4d, 0x44, 0x78, 0x28, 0x3c, 0x9e, 0xfd, 0x65,
    0x57, 0x16, 0x94, 0x6b, 0xfb, 0x59, 0xd0, 0xc8,
    0x22, 0x36, 0xdb, 0xd2, 0x63, 0x98, 0x43, 0xa1,
    0x04, 0x87, 0x86, 0xf7, 0xa6, 0x26, 0xbb, 0xd6,
    0x59, 0x4d, 0xbf, 0x6a, 0x2e, 0xaa, 0x2b, 0xef,
    0xe6, 0x78, 0xb6, 0x4e, 0xe0, 0x2f, 0xdc, 0x7c,
    0xbe, 0x57, 0x19, 0x32, 0x7e, 0x2a, 0xd0, 0xb8,
    0xba, 0x29, 0x00, 0x3c, 0x52, 0x7d, 0xa8, 0x49,
    0x3b, 0x2d, 0xeb, 0x25, 0x49, 0xfa, 0xa3, 0xaa,
    0x39, 0xa7, 0xc5, 0xa7, 0x50, 0x11, 0x36, 0xfb,
    0xc6, 0x67, 0x4a, 0xf5, 0xa5, 0x12, 0x65, 0x7e,
    0xb0, 0xdf, 0xaf, 0x4e, 0xb3, 0x61, 0x7f, 0x2f,
])

#: Frames whose plaintext differs by no more than this many bytes count as
#: "the lens did not move". Zero would be correct in theory and is what the
#: measurements showed, but a body that stamps something incidental into the
#: block would then read as movement forever and calibration would never
#: terminate. One byte of slack costs nothing and cannot mask a real move,
#: which changes several bytes at once.
SAME_TOLERANCE = 1


def decrypt(data: bytes, serial: int, count: int, start: int = 4) -> bytes:
    """
    Nikon's LensData cipher. The first `start` bytes are the plaintext
    version header ("0800", "0801", …) and are left alone.
    """
    key = 0
    for i in range(4):
        key ^= (count >> (i * 8)) & 0xFF
    ci = _XLAT0[serial & 0xFF]
    cj = _XLAT1[key]
    ck = 0x60
    out = bytearray(data)
    for i in range(start, len(out)):
        cj = (cj + ci * ck) & 0xFF
        ck = (ck + 1) & 0xFF
        out[i] ^= cj
    return bytes(out)


def available() -> bool:
    """Is the metadata reader usable at all? (exifread is an optional dep.)"""
    import importlib.util
    return importlib.util.find_spec("exifread") is not None


def read_lens_state(path) -> Optional[bytes]:
    """
    Decrypted LensData for one captured frame, or None if unavailable.

    Opaque to the lens-agnostic callers: `same_position` and `diff_bytes`
    compare it to another frame's and never interpret a byte, which is what
    keeps that path working on lenses and bodies it has never seen. The
    layer above is not agnostic — `read_position`, `at_mechanical_stop` and
    `read_focus` index the measured field map (POSITION_OFFSET,
    LIMIT_FLAG_OFFSET) straight into these bytes, and that is the path a
    live run takes.
    """
    try:
        import exifread
    except ImportError:
        return None
    try:
        with open(str(path), "rb") as fh:
            tags = exifread.process_file(fh, details=True)
    except Exception:                                    # noqa: BLE001
        return None

    block = tags.get("MakerNote LensData")
    if block is None:
        return None

    def _int(name):
        v = tags.get(name)
        if v is None:
            return None
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    serial = _int("MakerNote SerialNumber")
    # The cipher is keyed on the TOTAL shutter release count. Nikon also
    # reports a mechanical-only count, which is a different number and
    # decrypts to noise; measured 0/104 bytes agreeing when used.
    count = _int("MakerNote TotalShutterReleases")
    if serial is None or count is None:
        return None

    try:
        raw = bytes(list(block.values))
    except Exception:                                    # noqa: BLE001
        return None
    if len(raw) < 8:
        return None
    return decrypt(raw, serial, count)


def same_position(a: Optional[bytes], b: Optional[bytes]) -> Optional[bool]:
    """
    Did the lens stay put between these two frames?

    None means "cannot tell" (metadata missing, or the two frames are not
    comparable), which callers must treat as inconclusive rather than as a
    no-move — reading a stop where there is none would end calibration early.
    """
    if a is None or b is None or len(a) != len(b):
        return None
    if a[:4] != b[:4]:                    # different LensData version
        return None
    differing = sum(1 for x, y in zip(a[4:], b[4:]) if x != y)
    return differing <= SAME_TOLERANCE


#: Offset of the focus-position field inside the decrypted LensData block,
#: and how many of its counts make one app step.
#:
#: This is the only absolute position this rig can observe: nothing comes
#: over the wire, but the camera writes it into every frame.
#:
#: Derived from 176 frames across four runs: a signed 32-bit little-endian
#: integer that DECREASES as focus moves toward FAR, at exactly 10 counts per
#: app step, with a repeat scatter of ZERO (frames at one position come out
#: byte-identical). Byte 0x56 is a companion flag: 1 at the near mechanical
#: stop, 2 at the far one, 0 in between.
#:
#: An earlier attempt used bytes 0x44/0x46. Those saturate near the far end
#: and cannot resolve better than four steps, so they read as agreement where
#: there was none.
POSITION_OFFSET = 0x5A
POSITION_COUNTS_PER_STEP = 10
LIMIT_FLAG_OFFSET = 0x56


def read_position(path) -> Optional[float]:
    """
    The lens position this frame was taken at, in app steps, or None.

    Increases toward FAR so it runs the same way as the app's own counter.
    The zero is arbitrary (it is wherever the encoder's origin happens to
    be), so only DIFFERENCES between frames mean anything.
    """
    blk = read_lens_state(path)
    if blk is None or len(blk) < POSITION_OFFSET + 4:
        return None
    raw = int.from_bytes(blk[POSITION_OFFSET:POSITION_OFFSET + 4],
                         "little", signed=True)
    return -raw / float(POSITION_COUNTS_PER_STEP)


def at_mechanical_stop(path) -> Optional[int]:
    """0 if the lens is free, 1 at the near stop, 2 at the far stop."""
    blk = read_lens_state(path)
    if blk is None or len(blk) <= LIMIT_FLAG_OFFSET:
        return None
    return blk[LIMIT_FLAG_OFFSET]


def read_focus(path):
    """
    (position in RAW COUNTS, mechanical-stop flag) for one frame, or None.

    Counts increase toward FAR, so they run the same way as the app's own
    step counter, but they are NOT steps: the caller must divide by a
    counts-per-step scale.

    RETIRED FINDING, written out rather than deleted because the shape of
    the mistake is worth keeping. This used to say the scale had to be
    MEASURED and never assumed, on the grounds that it came out at exactly
    10.0 on one run and 11.67 on another — so not a constant of the camera,
    and plausibly tracking focal length, since the same block records the
    lens breathing from 63.97 to 69.66 mm as focus travels. focus_cal.py's
    module docstring retires that from a side-by-side run on 2026-07-31:
    1556 counts across seven 25-step chunks, and every chunk that did not
    run into the far stop moved exactly 250 — a scale of 10.00, which is
    STEP_UNITS["Small"] to the digit, i.e. one NEF encoder count per driver
    unit. (Not all seven: seven whole chunks would be 1750 counts, 194 more
    than the lens has, so the last one hit the stop partway and measured
    low.) Those older readings predate the current transport, which
    confirms every move; a chunk that quietly lost a command measures LOW,
    which is exactly the shape the scatter had. It was the transport, not the
    ruler, and the focal-length story was fitted to an artefact.

    So POSITION_COUNTS_PER_STEP (= 10, defined with the field map above) is
    the honest figure on this body, and the per-run estimator that survives
    in focus_cal.measure_travel (`scale = max(samples)`, biased high on
    purpose because lost commands bias low) is tolerance for a body nobody
    has measured — not evidence that this one wanders.

    Both fields come out of one decrypted block, so they are read together:
    decrypting means parsing the whole MakerNote, and this runs once per
    captured frame during a live run.
    """
    blk = read_lens_state(path)
    if blk is None or len(blk) < POSITION_OFFSET + 4:
        return None
    raw = int.from_bytes(blk[POSITION_OFFSET:POSITION_OFFSET + 4],
                         "little", signed=True)
    flag = blk[LIMIT_FLAG_OFFSET] if len(blk) > LIMIT_FLAG_OFFSET else 0
    return float(-raw), flag


def diff_bytes(a: Optional[bytes], b: Optional[bytes]):
    """
    Every byte that changed between two frames, as (index, before, after).

    `same_position` answers only "did it move", and it does that by COUNTING
    changed bytes against a tolerance of one. That is the right call for
    calibration, which needs a yes/no and must not stop early on noise, but
    it hides magnitude — and worse, a move small enough to disturb a single
    byte reads as "same". Anything trying to measure HOW FAR the lens went,
    or to compare two routes to the same nominal position, needs this
    instead. Returns None when the frames are not comparable at all.
    """
    if a is None or b is None or len(a) != len(b) or a[:4] != b[:4]:
        return None
    return [(i + 4, x, y)
            for i, (x, y) in enumerate(zip(a[4:], b[4:])) if x != y]
