"""
Small reusable widgets: the titled Card panel and the StatusPill badge.

Card replaces QGroupBox to get tighter control of the look; StatusPill is
the coloured connection/state badge used across the header.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QVBoxLayout,
    QWidget,
)

from .theme import ACCENT_BLUE, ACCENT_RED, rgba


class ScalingHost(QGraphicsView):
    """
    Holds the whole window's content and shrinks it to fit. No scrollbars.

    TWO STAGES, in this order, because they answer different problems:

      1. RELAYOUT. While the window is at least as big as the content's own
         minimum, the content is simply given the window and the ordinary Qt
         layout does the work — columns narrow, the preview gives back its
         letterbox, panels tighten. Nothing is scaled and nothing is blurred.
      2. SCALE. Past that point there is no honest layout left to do: the
         preview will not draw below the camera's 750x500 and the focus row
         needs its width. So the content is laid out at the size it still
         wants and the VIEW is scaled down to fit the window. Everything gets
         smaller together — type, buttons, the live image — instead of the
         window refusing to shrink, growing scrollbars, or letting widgets
         overlap each other.

    The three rejected alternatives, so nobody re-litigates them:
    a hard window minimum forbids the small window outright; a QScrollArea
    keeps everything full-size and hides half of it behind bars; and letting
    Qt undershoot the minimum makes over-minimum widgets OVERLAP, which is
    what once drew the jog buttons across the live image.

    A QGraphicsView is what makes stage 2 possible at all — it is the only
    thing in Qt that will apply a transform to a live, interactive widget
    tree. Clicks, focus, wheel and tooltips are mapped through the transform
    by Qt, so the content does not know it is being scaled.
    """

    def __init__(self, content: QWidget, parent=None):
        super().__init__(parent)
        self._content = content
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._proxy = self._scene.addWidget(content)

        # The whole point is that there are none. Without this the view adds
        # bars the moment the scene is bigger than the viewport, which is
        # precisely the state stage 2 exists to handle by scaling instead.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Smooth, or the scaled preview and the arrow PNGs alias badly. Text
        # is re-laid out by Qt at the transformed size rather than resampled,
        # so it stays sharp all the way down.
        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform
                            | QPainter.RenderHint.TextAntialiasing)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        # Nothing here is a document to scroll, and a stray wheel event that
        # panned the scene would look like the window had come loose.
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    def resizeEvent(self, event):                             # noqa: D102
        super().resizeEvent(event)
        self._rescale()

    def showEvent(self, event):                               # noqa: D102
        super().showEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        vp = self.viewport().size()
        vw, vh = max(vp.width(), 1), max(vp.height(), 1)
        floor = self._content.minimumSizeHint()
        fw, fh = max(floor.width(), 1), max(floor.height(), 1)

        # ONE factor for both axes. Scaling x and y independently would fit
        # the window exactly and distort every circle in the UI doing it —
        # the dial and the direction toggle would go oval.
        scale = min(1.0, vw / fw, vh / fh)

        # Lay the content out at viewport/scale, so that after the transform
        # it covers the viewport precisely. On the axis that forced the scale
        # this comes back to the floor; on the other it hands the layout the
        # surplus, which is what keeps the columns stretching normally
        # instead of leaving a dead margin down one side.
        cw, ch = round(vw / scale), round(vh / scale)
        # Resize the PROXY, not the widget. A widget owned by a
        # QGraphicsProxyWidget takes its geometry from the proxy, so calling
        # resize() on the widget itself is overwritten on the next relayout —
        # the visible symptom was the content settling at its sizeHint height
        # and leaving a dead band under the bottom row in a tall window.
        self._proxy.resize(cw, ch)
        self._scene.setSceneRect(0, 0, cw, ch)
        self.resetTransform()
        if scale < 1.0:
            self.scale(scale, scale)

    @property
    def scale_factor(self) -> float:
        """Current transform scale — 1.0 whenever the window is big enough."""
        return self.transform().m11()


class Card(QFrame):
    """
    A titled panel. Replaces QGroupBox for tighter control of the look.

    `expand=False` (the default) pins the content to the TOP of the card.
    Without it a QVBoxLayout shares spare height evenly between the header
    and the body, so a short card in a tall row floats its own title down the
    middle of itself — which reads as a mysterious gap above the text.

    `expand=True` is for the three panels whose content should soak up the
    extra instead: the live view, the dial and the event log.
    """

    def __init__(self, title: str, accent: str = ACCENT_BLUE, parent=None,
                 expand: bool = False):
        super().__init__(parent)
        self.setObjectName("card")
        self.accent = accent

        # Tight vertical margins on purpose. Four rows of cards stack down the
        # window, so every pixel of padding here is spent four times — and the
        # live view is competing for the same height.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(13, 7, 13, 9)
        outer.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(7)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {accent}; font-size: 9px;")
        head.addWidget(dot)
        self.title_label = QLabel(title.upper())
        self.title_label.setObjectName("cardTitle")
        head.addWidget(self.title_label)
        head.addStretch()
        self.head = head
        outer.addLayout(head)

        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body)
        if not expand:
            outer.addStretch(1)
        self._outer = outer

    def add_header_widget(self, w: QWidget):
        self.head.addWidget(w)

    def center_header_group(self, middle: list, right: list) -> None:
        """
        Rebuild the header as three groups, with `middle` truly centred.

        EQUAL STRETCH IS NOT ENOUGH, which is the whole reason this exists.
        The obvious header is `title … middle … right` with a stretch either
        side of the middle, and it looks centred right up until the two ends
        differ in width. Work it through: with both stretches at factor 1 the
        slack splits evenly, so the middle's centre lands at
        `left + slack/2 + middle/2`, and setting that equal to half the total
        reduces to `left == right`. The stretches were never the problem — the
        ENDS have to match. On the live-view card they do not come close: the
        title is one short word and the right end carries the magnifier group
        and Start live view, so the middle sat visibly left of centre.

        So both ends are wrapped in containers and given the SAME minimum
        width, taken from whichever of them asks for more. That number is read
        from the widgets' own sizeHint at build time rather than written down
        here, so it follows the font and the button text instead of going
        stale the first time either changes.
        """
        # Everything already in the header, in order, is the left group —
        # for every card that is the accent dot and the title.
        left = []
        while self.head.count():
            item = self.head.takeAt(0)
            if item.widget() is not None:
                left.append(item.widget())

        def group(widgets, align) -> QWidget:
            box = QWidget()
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(7)
            if align == "right":
                lay.addStretch(1)
            for w in widgets:
                w.setParent(box)
                lay.addWidget(w)
            if align == "left":
                lay.addStretch(1)
            return box

        left_box = group(left, "left")
        mid_box = group(middle, "center")
        right_box = group(right, "right")

        ends = max(left_box.sizeHint().width(), right_box.sizeHint().width())
        left_box.setMinimumWidth(ends)
        right_box.setMinimumWidth(ends)

        # Factor 1 on the ends and 0 on the middle: the ends absorb the slack
        # equally — which is now correct, because they start equal — and the
        # middle keeps its natural width instead of stretching its buttons.
        self.head.addWidget(left_box, 1)
        self.head.addWidget(mid_box, 0)
        self.head.addWidget(right_box, 1)

    def add(self, widget_or_layout):
        if isinstance(widget_or_layout, QWidget):
            self.body.addWidget(widget_or_layout)
        else:
            self.body.addLayout(widget_or_layout)


class StatusPill(QLabel):
    """Compact connection-state chip for the header bar."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = ACCENT_RED
        self.set_state("offline", ACCENT_RED)

    def set_state(self, text: str, color: str):
        self._color = color
        self.setText(f"  {self._label}  ·  {text}  ")
        self.setStyleSheet(
            f"background-color: {rgba(color, 0.13)};"
            f"color: {color};"
            f"border: 1px solid {rgba(color, 0.40)};"
            f"border-radius: 10px; padding: 3px 4px;"
            f"font-size: 11px; font-weight: 700;"
        )
        self.setToolTip(f"{self._label}: {text}")

    def set_text_only(self, text: str):
        """Update the value without rebuilding the stylesheet (cheap, for a
        once-a-second clock)."""
        self.setText(f"  {self._label}  ·  {text}  ")
        self.setToolTip(f"{self._label}: {text}")
