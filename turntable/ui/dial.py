"""
The turntable dial — a pie chart with one wedge per position.

ONE HUE, THREE STATES. The wedges encode progress by intensity rather than
by colour, so the ring simply fills as the revolution goes round:

    not yet shot   a cool slate, barely above the panel — present, not loud
    being shot     the accent green at half strength
    finished       the accent green, solid

It used to be two competing hues (amber for done, green for current), which
meant remembering which was which and gave the whole widget a traffic-light
loudness it did not earn. A position indicator should be readable in
peripheral vision and otherwise quiet.

Uniform parallel gaps separate the wedges. The centre holds the top-down
camera art (`camera.png`, via assets.asset_path) rotated to face the current
wedge; a crude drawn camera is the fallback if the file is missing.

Outside a run the dial is a live preview of the positions/deg-per-move
pair; during a run the worker owns it.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFontMetrics, QImage, QPainter, QPen, QPolygonF,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..assets import asset_path
from .theme import ACCENT_AMBER, ACCENT_GREEN, PANEL_BG


class TurntableWidget(QWidget):
    """
    Rotation indicator, pie-chart style.

    One wedge per turntable position, coloured by state (see the module
    docstring), with a uniform-width gap between wedges — parallel edges, not
    a converging notch. A little top-down camera sits in the centre hole and
    rotates to face the current wedge. The angle readout lives in the card
    header (set by the window), not here.
    """

    angle_changed = Signal(float)      # so the header label can follow it

    #: Wedges not yet shot. Cool and low-contrast on purpose: it is the
    #: BACKGROUND state, and thirty-six loud wedges would drown the two or
    #: three that actually carry information.
    PENDING = QColor("#39445c")

    #: Opacity of the in-progress wedge, as a fraction of the finished one.
    #: Enough to read as "this one, now" without competing with the solid
    #: block of finished positions beside it.
    IN_PROGRESS_ALPHA = 0.5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.positions = 36
        self.completed = 0
        self.current = -1
        self.angle_deg = 0.0
        # Cached camera sprite: camera.png at 320x320 as shipped, or the
        # 48 px `_draw_fallback_camera` when that file is missing. Held at
        # source size — paintEvent fits its diagonal to the hole per paint.
        self._cam_img = None
        # Kept small so it yields space to the live view rather than the other
        # way round: this is a position indicator, not something to study.
        self.setMinimumSize(112, 112)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_state(self, positions, completed, current, angle):
        self.positions = max(int(positions), 1)
        self.completed = completed
        self.current = current
        if angle != self.angle_deg:
            self.angle_deg = angle
            self.angle_changed.emit(angle)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h)
        cx, cy = w / 2.0, h / 2.0
        r_out = side * 0.47
        r_in = side * 0.25
        n = self.positions
        cur = self.current % n if self.current >= 0 else -1
        slot = 360.0 / n
        rect = QRectF(cx - r_out, cy - r_out, 2 * r_out, 2 * r_out)

        # Full-span pie wedges (no angular gap yet). Finished first, so a
        # position that is both finished and current reads as finished — the
        # run has moved on and the dial should say so.
        done = QColor(ACCENT_GREEN)
        doing = QColor(ACCENT_GREEN)
        doing.setAlphaF(self.IN_PROGRESS_ALPHA)

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            start_qt = 90.0 - i * slot - slot / 2.0        # Qt: 0°=3 o'clock, CCW+
            if i < self.completed:
                p.setBrush(QBrush(done))
            elif i == cur:
                p.setBrush(QBrush(doing))
            else:
                p.setBrush(QBrush(self.PENDING))
            p.drawPie(rect, int(round(start_qt * 16)), int(round(slot * 16)))

        # Uniform-width gaps: paint a constant-width rectangle (in the card
        # background colour) along each wedge boundary, from the hole out to
        # the rim. Because the rectangle has parallel sides, the gap is the
        # same thickness all the way out instead of converging to a point.
        bg = QColor(PANEL_BG)
        gw = max(2.0, side * 0.016)
        p.setBrush(QBrush(bg))
        for i in range(n):
            boundary = (i + 0.5) * slot                    # clockwise from top
            p.save()
            p.translate(cx, cy)
            p.rotate(boundary)                             # local -y = the radial
            p.drawRect(QRectF(-gw / 2.0, -(r_out + 1.0),
                              gw, (r_out - r_in) + 2.0))
            p.restore()

        # Donut hole for the camera.
        p.setBrush(QBrush(bg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), r_in, r_in)

        # Top-down camera, scaled to sit inside the hole, rotated so its lens
        # (which points up in the artwork) faces the current wedge.
        if self._cam_img is None:
            self._cam_img = self._load_camera_image()
        if self._cam_img is not None and not self._cam_img.isNull():
            iw, ih = self._cam_img.width(), self._cam_img.height()
            # Fit the artwork's DIAGONAL inside the hole so no rotation clips.
            diag = math.hypot(iw, ih)
            scale = (r_in * 1.86) / diag
            tw, th = iw * scale, ih * scale
            p.save()
            p.translate(cx, cy)
            p.rotate(self.angle_deg)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(QRectF(-tw / 2.0, -th / 2.0, tw, th), self._cam_img)
            p.restore()
        p.end()

    @staticmethod
    def _load_camera_image():
        """
        Load the top-down camera artwork that sits in the middle of the pie.
        Lives in the project root as camera.png; falls back to a plain
        drawn camera if the file is missing.
        """
        path = asset_path("camera.png")
        if path.is_file():
            img = QImage(str(path))
            if not img.isNull():
                return img.convertToFormat(QImage.Format.Format_ARGB32)
        return TurntableWidget._draw_fallback_camera()

    @staticmethod
    def _draw_fallback_camera():
        S = 48
        img = QImage(S, S, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        q = QPainter(img)
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(QBrush(QColor("#33363f")))
        q.drawRoundedRect(QRectF(6, 18, 36, 24), 4, 4)
        q.setBrush(QBrush(QColor("#5e6270")))
        q.drawEllipse(QPointF(24, 12), 11, 11)
        q.setBrush(QBrush(QColor("#0d0e12")))
        q.drawEllipse(QPointF(24, 12), 6, 6)
        q.setBrush(QBrush(QColor("#9cbd74")))
        q.drawRect(QRectF(28, 26, 11, 8))
        q.end()
        return img


class DirectionToggle(QWidget):
    """
    Which way the table turns, as a circle you click rather than a dropdown.

    Replaces a two-item QComboBox. The value was never a list to be browsed —
    there are exactly two states and picking one is a toggle, so a control
    that opens a menu to change a boolean was the wrong shape. Clicking
    anywhere on the circle flips it.

    IT IS ALSO WHY THE CARD STOPPED LOOKING HALF-EMPTY. Direction sits in the
    Capture grid's third column, where Positions and Degrees fill two rows in
    the columns beside it — so a one-row combo left an obvious hole under
    itself. This spans both rows and uses the space the hole was wasting.

    THE ARROW POINTS THE WAY THE TABLE GOES, which is worth stating because
    screen-space and table-space do not have to agree and here they do: the
    dial directly below in the Turntable position panel draws its rotation the
    same way round, so the two graphics can be read together without anyone
    having to hold a mental mirror.

    `currentIndex()` is deliberately the QComboBox spelling, and 0 is still CW
    and 1 still CCW — main_window reads this straight into the LiveSettings
    box as "direction", and keeping the name and the encoding meant the
    replacement touched no behaviour at all.
    """

    #: Emitted on every user toggle, never on a programmatic `set_index`.
    #: Matches how the combo behaved: the settings box is told a value
    #: changed only when a person changed it.
    changed = Signal(int)

    #: Big enough to read the arrowhead at a glance, small enough to sit in a
    #: grid cell beside two spin boxes without setting the row height.
    SIDE = 68

    def __init__(self, parent=None):
        super().__init__(parent)
        self._index = 0                        # 0 = CW, 1 = CCW
        self.setFixedSize(self.SIDE, self.SIDE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_tooltip()

    # ── value ────────────────────────────────────────────────────────────
    def currentIndex(self) -> int:              # noqa: N802  (QComboBox name)
        return self._index

    def set_index(self, index: int) -> None:
        """Set without emitting — for restoring a saved value."""
        self._index = 1 if int(index) else 0
        self._sync_tooltip()
        self.update()

    def _sync_tooltip(self) -> None:
        way = "counter-clockwise" if self._index else "clockwise"
        self.setToolTip(f"The table turns {way}. Click to reverse it.\n"
                        f"Takes effect on the next revolution.")

    # ── interaction ──────────────────────────────────────────────────────
    def mousePressEvent(self, event):                         # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton:
            self._index ^= 1
            self._sync_tooltip()
            self.update()
            self.changed.emit(self._index)
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):                           # noqa: D102
        # Space and Enter, so the control is reachable without a mouse — a
        # combo box gave that for free and a bare QWidget does not.
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return,
                           Qt.Key.Key_Enter):
            self._index ^= 1
            self._sync_tooltip()
            self.update()
            self.changed.emit(self._index)
        else:
            super().keyPressEvent(event)

    # ── painting ─────────────────────────────────────────────────────────
    def paintEvent(self, _event):                             # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        pen_w = max(2.0, side * 0.075)
        # Inset by the pen's own half-width plus room for the arrowhead, or
        # both get clipped by the widget edge at small sizes.
        margin = pen_w * 0.5 + side * 0.16
        box = QRectF(margin, margin, side - 2 * margin, side - 2 * margin)

        colour = QColor(ACCENT_AMBER)
        cx, cy = box.center().x(), box.center().y()
        r = box.width() / 2.0

        # An arc with a GAP, not a closed ring: a full circle has no start and
        # no end, so it cannot say which way anything is going. The gap is
        # where the arrowhead goes, and the eye reads the head as the leading
        # end of the stroke.
        #
        # Qt angles run counter-clockwise from 3 o'clock, in 1/16ths of a
        # degree. The sweep starts at the top in both states and runs the way
        # the table will turn, so CW and CCW come out as exact mirror images.
        span = 300
        start = 90
        sweep = span if self._index else -span

        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(colour, pen_w)
        # FLAT cap, not round. The head is a triangle butted onto the end of
        # the stroke; a round cap puts a bulge behind the barbs that reads as
        # a blob with a spike on it at this size.
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        # The arc stops one head-length short so the triangle completes it
        # rather than overlapping it — overlapping stroke and head is what
        # made the first version look like two shapes rather than one arrow.
        head = side * 0.19
        head_deg = math.degrees(head / r) if r else 0.0
        drawn = sweep - math.copysign(head_deg, sweep)
        p.drawArc(box, int(start * 16), int(drawn * 16))

        # Arrowhead butted onto the leading end, pointing along the tangent.
        end_rad = math.radians(start + drawn)
        # Qt's y axis points down, so every sine is negated to keep the maths
        # in ordinary screen coordinates rather than mirrored about the middle.
        base = QPointF(cx + r * math.cos(end_rad), cy - r * math.sin(end_rad))
        tangent = end_rad + (math.pi / 2 if self._index else -math.pi / 2)
        tip = QPointF(base.x() + head * math.cos(tangent),
                      base.y() - head * math.sin(tangent))
        # Barbs square across the stroke, so the head is exactly as wide as
        # the arc is thick plus its own flare and sits flush on the end.
        half = pen_w * 1.25
        across = tangent + math.pi / 2
        barbs = [QPointF(base.x() + s * half * math.cos(across),
                         base.y() - s * half * math.sin(across))
                 for s in (1, -1)]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(colour))
        p.drawPolygon(QPolygonF([tip, barbs[0], barbs[1]]))

        # The label inside the ring, SIZED TO FIT rather than set to a fixed
        # fraction of the widget. "CCW" is half again as wide as "CW", so one
        # size cannot serve both: at the fraction that suited CW, the three
        # letters ran under the stroke and out through the arrowhead. The
        # width available is the ring's inner diameter less a small inset, and
        # the size is stepped down until the string measures inside it — which
        # keeps working if the widget is resized or the labels ever change.
        text = "CCW" if self._index else "CW"
        inner = 2.0 * (r - pen_w * 0.5) - side * 0.10
        f = p.font()
        f.setBold(True)
        size = int(side * 0.26)
        while size > 6:
            f.setPixelSize(size)
            if QFontMetrics(f).horizontalAdvance(text) <= inner:
                break
            size -= 1
        p.setPen(QPen(colour))
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
        p.end()
