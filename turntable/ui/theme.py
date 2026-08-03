"""
Colour palette, global stylesheet and small style helpers.

Note `rgba()`: Qt reads an 8-digit hex colour as #AARRGGBB — alpha FIRST —
so writing `{ACCENT_BLUE}44` for "blue at 27%" silently produced a
different colour. Going through rgba() removes the ambiguity.

The combo-box and spin-box arrows are painted here at startup and written
into `turntable_bridge_ui` under `tempfile.gettempdir()` as five PNGs,
alongside a sixth for the checkbox tick, then all six are referenced by
`image: url(...)` from the generated rules — see
`_make_arrow_stylesheet` for why Qt leaves no better option. No image
asset ships with the app, but the theme does write to disk; when that
write fails it returns "" and Qt falls back to its own native arrows.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter


DARK_BG        = "#0d0f16"
PANEL_BG       = "#151925"
PANEL_BG_SOFT  = "#1a1f2e"
FIELD_BG       = "#1e2334"
BORDER         = "#272d3f"
BORDER_SOFT    = "#1f2534"
TEXT_PRIMARY   = "#e2e7f2"
TEXT_SECONDARY = "#8a94ab"
TEXT_MUTED     = "#616b82"
ACCENT_GREEN   = "#2ecc71"
ACCENT_AMBER   = "#f5a623"
ACCENT_BLUE    = "#5aa9fa"
ACCENT_PURPLE  = "#a78bfa"
ACCENT_RED     = "#ef4d4d"
ACCENT_CYAN    = "#22d3ee"


def rgba(hex_color: str, alpha: float) -> str:
    """
    Build a Qt-stylesheet rgba() string.

    Worth knowing: Qt parses an 8-digit hex colour as #AARRGGBB — alpha
    FIRST.  The old GUI wrote things like `{ACCENT_BLUE}44` expecting
    "blue at 27% alpha" and actually got a completely different, mostly
    transparent colour.  Going through rgba() removes the ambiguity.
    """
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha:.3f})"


STYLESHEET = f"""
QMainWindow, QDialog {{ background-color: {DARK_BG}; }}
QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI Variable", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

QFrame#card {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#subpanel {{
    background-color: {DARK_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
}}
QFrame#hairline {{ background-color: {BORDER}; border: none; }}

QLabel {{ color: {TEXT_SECONDARY}; font-size: 12px; background: transparent; }}
QLabel#cardTitle {{
    color: {TEXT_SECONDARY}; font-size: 11px; font-weight: 700;
    letter-spacing: 1.2px;
}}
QLabel#hint {{ color: {TEXT_MUTED}; font-size: 10px; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {FIELD_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    color: {TEXT_PRIMARY};
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
    min-height: 20px;
    selection-background-color: {rgba(ACCENT_BLUE, 0.35)};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border: 1px solid {rgba(ACCENT_BLUE, 0.45)};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT_BLUE};
    background-color: #222840;
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled {{
    background-color: {DARK_BG}; color: {TEXT_MUTED};
    border-color: {BORDER_SOFT};
}}
/* Arrow sub-controls get their glyph from `_make_arrow_stylesheet()`,
   which `full_stylesheet()` appends to this sheet at startup. Qt's
   stylesheet engine does not honour the CSS
   border-triangle idiom for sub-controls — it draws the border box
   literally, which is why hand-rolled arrows show up as grey blocks. */
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    background-color: transparent; border: none;
    width: 16px; height: 12px; margin: 2px 3px 0 0;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    background-color: transparent; border: none;
    width: 16px; height: 12px; margin: 0 3px 2px 0;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background-color: {PANEL_BG_SOFT}; border: 1px solid {BORDER};
    selection-background-color: {rgba(ACCENT_BLUE, 0.30)};
    color: {TEXT_PRIMARY}; padding: 4px; outline: none;
}}

QPushButton {{
    border: 1px solid {BORDER}; border-radius: 7px;
    padding: 7px 14px; font-weight: 600; font-size: 12px;
    min-height: 20px; background-color: {PANEL_BG_SOFT};
    color: {TEXT_PRIMARY};
}}
QPushButton:hover {{ background-color: {FIELD_BG}; border-color: {rgba(ACCENT_BLUE, 0.4)}; }}
QPushButton:pressed {{ background-color: {DARK_BG}; }}
QPushButton:disabled {{
    background-color: {DARK_BG}; color: {TEXT_MUTED};
    border-color: {BORDER_SOFT};
}}

QCheckBox {{ color: {TEXT_SECONDARY}; font-size: 12px; spacing: 7px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid {BORDER}; background-color: {FIELD_BG};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT_GREEN}; border-color: {ACCENT_GREEN};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT_BLUE}; }}

QProgressBar {{
    background-color: {DARK_BG}; border: 1px solid {BORDER_SOFT};
    border-radius: 5px; height: 14px; text-align: center;
    font-size: 10px; font-weight: 600; color: {TEXT_SECONDARY};
}}
QProgressBar::chunk {{ border-radius: 4px; margin: 0px; }}

QTextEdit {{
    background-color: {DARK_BG}; border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    font-family: "Cascadia Mono", "Consolas", monospace; font-size: 11px;
    color: {TEXT_SECONDARY}; padding: 8px;
    selection-background-color: {rgba(ACCENT_BLUE, 0.35)};
}}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QToolTip {{
    background-color: {PANEL_BG_SOFT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; padding: 5px 7px; border-radius: 5px;
}}
"""


def _make_arrow_stylesheet() -> str:
    """
    Draw the spin/combo arrow glyphs to little PNGs and return the rules
    that point at them.

    Qt's stylesheet engine renders `border-*` on an arrow sub-control as a
    literal box, so the usual CSS triangle trick produces a grey rectangle
    (which is exactly what the old GUI showed).  Painting the glyph and
    handing Qt an `image:` is the approach that actually works.
    """
    import tempfile
    from PySide6.QtGui import QPolygonF, QPen as _QPen
    from PySide6.QtCore import QPointF

    try:
        out = Path(tempfile.gettempdir()) / "turntable_bridge_ui"
        out.mkdir(parents=True, exist_ok=True)

        def draw_check(name: str, colour: str) -> str:
            # A checkmark, so a ticked box reads as ticked by SHAPE as well
            # as colour — the colour-blind cue the fill alone cannot give.
            path = out / f"{name}.png"
            s = 15 * 3
            img = QImage(s, s, QImage.Format.Format_ARGB32)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = _QPen(QColor(colour))
            pen.setWidth(int(s * 0.13))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF([
                QPointF(s * 0.24, s * 0.52),
                QPointF(s * 0.42, s * 0.70),
                QPointF(s * 0.78, s * 0.28)]))
            p.end()
            img.save(str(path))
            return path.as_posix()

        def draw(name: str, up: bool, colour: str) -> str:
            path = out / f"{name}.png"
            w, h, scale = 11, 7, 3          # oversampled, Qt scales it down
            img = QImage(w * scale, h * scale, QImage.Format.Format_ARGB32)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(colour)))
            if up:
                pts = [QPointF(0.5, h - 0.8), QPointF(w - 0.5, h - 0.8),
                       QPointF(w / 2.0, 0.8)]
            else:
                pts = [QPointF(0.5, 0.8), QPointF(w - 0.5, 0.8),
                       QPointF(w / 2.0, h - 0.8)]
            p.drawPolygon(QPolygonF([QPointF(x * scale, y * scale)
                                     for x, y in ((q.x(), q.y()) for q in pts)]))
            p.end()
            if not img.save(str(path)):
                raise OSError(f"could not write {path}")
            return path.as_posix()

        up_m = draw("up_muted", True, TEXT_MUTED)
        dn_m = draw("down_muted", False, TEXT_MUTED)
        up_a = draw("up_accent", True, ACCENT_BLUE)
        dn_a = draw("down_accent", False, ACCENT_BLUE)
        dn_s = draw("down_sec", False, TEXT_SECONDARY)
        check = draw_check("check_black", "#0b0e14")   # near-black tick
    except Exception:                                       # noqa: BLE001
        return ""                       # fall back to the native arrows

    return f"""
QCheckBox::indicator:checked {{ image: url({check}); }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({up_m}); width: 9px; height: 6px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({dn_m}); width: 9px; height: 6px;
}}
QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {{
    image: url({up_a});
}}
QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {{
    image: url({dn_a});
}}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{
    image: none;
}}
QComboBox::down-arrow {{
    image: url({dn_s}); width: 10px; height: 7px; margin-right: 6px;
}}
QComboBox::down-arrow:disabled {{ image: none; }}
"""


def full_stylesheet() -> str:
    """The base sheet plus the generated arrow glyphs. Needs a QApplication."""
    return STYLESHEET + _make_arrow_stylesheet()


def btn_style(bg: str, fg: str, border: str, hover: str) -> str:
    return f"""
        QPushButton {{ background-color: {bg}; color: {fg};
                       border: 1px solid {border}; }}
        QPushButton:hover {{ background-color: {hover}; }}
        QPushButton:disabled {{ background-color: {DARK_BG};
                                color: {TEXT_MUTED};
                                border-color: {BORDER_SOFT}; }}
    """
