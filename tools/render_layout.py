#!/usr/bin/env python
"""
Render the main window OFFSCREEN and report how big the live-view frame draws.

    python tools/render_layout.py [--empty] [tag] [WxH ...]
    python tools/render_layout.py --measure

Writes `<tag>_<W>x<H>_r<deg>.png` for each window size at 0° and 90°, and
prints one line per render: the window, the ScalingHost's scale factor, the
preview panel's size and the frame's on-screen size. A development tool for
layout work — `ui/layout.py`'s module docstring says which numbers it was
last used to justify. Not shipped.

WHY OFFSCREEN. `QT_QPA_PLATFORM=offscreen` means no window flashes on
screen and nothing touches the real settings, but the offscreen platform
has NO system fonts, which makes every label wider and every floor wrong.
So the Windows fonts the theme asks for are loaded by hand from the fonts
folder first. Metrics come out within a few percent of the real app — good
enough to see a layout fail, not good enough to quote a floor to the pixel;
for that, run `measure()` with the platform left at its default and no
window shown (see the `--measure` flag).

WHY THE FAKE FRAME. There is no camera on a build box. A 750x500 gradient
with the size written across it and a "TOP" marker is enough to see whether
the frame fills the panel and which way up it is.

SETTINGS ARE REDIRECTED to a temp folder so the run neither reads the
user's saved geometry (which would defeat the point of asking for a size)
nor overwrites it. Done by replacing `MainWindow._settings`, NOT with
`QSettings.setDefaultFormat`/`setPath`: the window builds its QSettings
with the two-argument (organization, application) constructor, and that
constructor uses the NATIVE format regardless of the default — measured on
Qt 6.9, `fileName()` still answered the registry key after both calls. A
harness that believes it is sandboxed and is not will quietly overwrite
the user's rotation and window geometry, which is exactly what happened
once.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_SIZES = ((2560, 1400), (1920, 1040), (1600, 900))
FONTS = ("segoeui.ttf", "segoeuib.ttf", "SegUIVar.ttf", "consola.ttf",
         "consolab.ttf", "CascadiaMono.ttf")


def _app(offscreen: bool):
    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    scratch = tempfile.mkdtemp(prefix="turntable-render-")
    ini = str(Path(scratch) / "settings.ini")
    from turntable.ui.main_window import MainWindow
    MainWindow._settings = lambda self: QSettings(ini, QSettings.Format.IniFormat)
    app = QApplication(sys.argv[:1])
    if offscreen:
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in FONTS:
            if (fonts / name).is_file():
                QFontDatabase.addApplicationFont(str(fonts / name))
        app.setFont(QFont("Segoe UI", 9))
    app.setStyle("Fusion")
    from turntable.ui.theme import full_stylesheet
    app.setStyleSheet(full_stylesheet())
    return app


def fake_frame(w: int = 750, h: int = 500):
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import (
        QColor, QFont, QImage, QLinearGradient, QPainter, QPen,
    )
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#7a4a2a"))
    g.setColorAt(1, QColor("#2a4a7a"))
    p.fillRect(0, 0, w, h, g)
    p.setPen(QPen(QColor("#ffcc33"), 6))
    p.drawRect(3, 3, w - 6, h - 6)
    p.setPen(QPen(QColor("#ffffff"), 4))
    p.drawEllipse(w // 2 - 120, h // 2 - 120, 240, 240)
    p.setPen(QColor("#ffffff"))
    p.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
    p.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter,
               f"{w}x{h}\nTOP ▲")
    p.end()
    return img


def render(tag: str, sizes, out_dir: Path, empty: bool = False) -> None:
    from PySide6.QtCore import Qt
    app = _app(offscreen=True)
    from turntable.ui.main_window import MainWindow

    w = MainWindow()
    if not empty:
        w.liveview._on_frame(fake_frame())
    try:
        for W, H in sizes:
            for deg in (0, 90):
                w._apply_rotation(deg, persist=False)
                w.show()
                # The window sizes ITSELF once, on a zero-timer after the
                # first show (`MainWindow._apply_startup_geometry`), and with
                # no saved geometry that means "fit the screen" — which, on
                # the offscreen platform, is a tiny virtual one. Let that
                # fire first, then impose the size being tested.
                for _ in range(4):
                    app.processEvents()
                w.resize(W, H)
                # Floors ripple up through nested layouts one event-loop
                # turn at a time, and both the width cap and the ScalingHost
                # re-fit on zero-timers behind them. Give it several turns.
                for _ in range(8):
                    app.processEvents()
                out = out_dir / f"{tag}_{W}x{H}_r{deg}.png"
                w.grab().save(str(out))
                lv = w.liveview
                fitted = lv.frame_shape().scaled(
                    lv.size(), Qt.AspectRatioMode.KeepAspectRatio)
                k = w.scaling_host.scale_factor
                print(f"{tag} {W}x{H} rot{deg}: scale={k:.3f} "
                      f"panel={lv.width()}x{lv.height()} "
                      f"frame={fitted.width()}x{fitted.height()} "
                      f"on screen={round(fitted.width() * k)}x"
                      f"{round(fitted.height() * k)}  -> {out.name}")
    finally:
        w.liveview.shutdown()


def measure() -> None:
    """Print every card's minimum and preferred size, with real fonts."""
    _app(offscreen=False)
    from turntable.ui.main_window import MainWindow
    from turntable.ui.widgets import Card

    w = MainWindow()
    try:
        w.content_widget.layout().activate()
        for card in w.content_widget.findChildren(Card):
            m, s = card.minimumSizeHint(), card.sizeHint()
            print(f"{card.title_label.text():22s} min={m.width()}x{m.height()}"
                  f"  hint={s.width()}x{s.height()}")
        row = w.focus_panel.controls_widget
        print(f"{'focus row':22s} min={row.minimumSizeHint().width()}"
              f"  hint={row.sizeHint().width()}")
    finally:
        w.liveview.shutdown()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--measure":
        measure()
    else:
        # `--empty` renders the no-camera state: the panel's placeholder
        # instead of the synthetic frame, which is what a user sees before
        # connecting and the state in which the rotate button once looked
        # like it did nothing.
        empty = "--empty" in args
        args = [a for a in args if a != "--empty"]
        tag = args[0] if args else "layout"
        sizes = [tuple(int(v) for v in a.lower().split("x")) for a in args[1:]]
        render(tag, sizes or DEFAULT_SIZES, Path.cwd(), empty=empty)
