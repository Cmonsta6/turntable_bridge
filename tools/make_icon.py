"""
Regenerate app_icon.ico from camera.png.

    python tools/make_icon.py

The artwork is a black top-down camera, which vanishes against a dark
Windows taskbar at 16-24 px. So the icon is not the raw art: it is the
art composited on a light rounded-square plate, which reads on a dark
taskbar and still reads on a light one because the camera itself stays
dark.

Pass --plain to build the icon straight from the artwork with no plate.

Requires Pillow (dev-time only — the app itself never imports it):
    pip install pillow
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "camera.png"
OUT = ROOT / "app_icon.ico"

# Rendered at 4x then downsampled, so the rounded corners stay smooth.
CANVAS = 1024
SUPER = 4

PLATE_FILL = (238, 241, 247, 255)      # light neutral, close to the UI's text colour
PLATE_EDGE = (188, 196, 212, 255)      # a touch darker so it has an edge on white
CORNER = 0.20                          # radius as a fraction of the canvas
INSET = 0.10                           # plate margin
ART_SCALE = 0.74                       # camera size within the plate

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48),
             (64, 64), (128, 128), (256, 256)]


def build_plated(src: Image.Image) -> Image.Image:
    size = CANVAS * SUPER
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    inset = int(size * INSET)
    radius = int(size * CORNER)
    draw.rounded_rectangle(
        [inset, inset, size - inset - 1, size - inset - 1],
        radius=radius, fill=PLATE_FILL,
        outline=PLATE_EDGE, width=max(2, size // 160),
    )

    # resize(), not thumbnail(): thumbnail only ever shrinks, so the small
    # source artwork would sit as a speck in the middle of the big canvas.
    art_w = int(size * ART_SCALE)
    ratio = min(art_w / src.width, art_w / src.height)
    art = src.resize((max(1, round(src.width * ratio)),
                      max(1, round(src.height * ratio))), Image.LANCZOS)
    img.alpha_composite(art, ((size - art.width) // 2,
                              (size - art.height) // 2))

    return img.resize((CANVAS, CANVAS), Image.LANCZOS)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plain", action="store_true",
                    help="no plate — the raw artwork as the icon")
    args = ap.parse_args()

    if not SRC.is_file():
        raise SystemExit(f"missing {SRC}")

    src = Image.open(SRC).convert("RGBA")
    icon = src.copy() if args.plain else build_plated(src)
    icon.save(OUT, sizes=ICO_SIZES)

    kind = "plain artwork" if args.plain else "artwork on a light plate"
    print(f"wrote {OUT.relative_to(ROOT)}  ({kind})")
    print("sizes: " + ", ".join(f"{w}x{h}" for w, h in ICO_SIZES))


if __name__ == "__main__":
    main()
