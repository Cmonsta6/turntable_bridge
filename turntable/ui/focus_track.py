"""
Horizontal focus-travel track: near limit, A, B and the live position.

Replaces the inert "Position: 0" readout — focus is relative, so a raw
step count says nothing without seeing where it sits between the marks.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .theme import ACCENT_AMBER, ACCENT_BLUE, ACCENT_CYAN, DARK_BG, TEXT_MUTED


class FocusTrackWidget(QWidget):
    """
    Horizontal focus-travel track: near limit, A, B and the live position.

    This replaces v1's "Position: 0" text label, which showed a raw count in
    arbitrary units and gave no sense of where in the lens' range you were.
    """

    #: Height the track needs to draw its rail plus the labels above and
    #: below it. Fixed, so the widget never changes size when A or B appear —
    #: a track that grew by a few pixels the moment you pressed Set A would
    #: nudge everything under it.
    HEIGHT = 58

    def __init__(self, parent=None, overlay: bool = False):
        super().__init__(parent)
        self.position = 0
        self.a_set = False                     # A is always position 0
        self.pos_b: Optional[int] = None
        self.limit_lo: Optional[int] = None    # near mechanical stop
        self.limit_hi: Optional[int] = None    # far mechanical stop
        self._span_hi = 100                    # sticky auto-scale ceiling
        self._span_lo = 0
        # Overlay mode: floated over the live image rather than sitting in a
        # panel. It then has to carry its own scrim, because a 1px rail and
        # 7pt labels are invisible over a photograph.
        self._overlay = overlay
        if overlay:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, position, a_set, pos_b, limit_lo, limit_hi):
        self.position = position
        self.a_set = a_set
        self.pos_b = pos_b
        self.limit_lo = limit_lo
        self.limit_hi = limit_hi
        self.update()

    def _domain(self):
        """
        Pick the scale.

        Once the mechanical limits are known the scale is pinned to them and
        never moves again, so the markers stay put while you jog.  Before
        that it grows to fit but never shrinks — a scale that re-fitted on
        every click made the whole bar jump around, which is exactly what
        made this widget feel broken.
        """
        if self.limit_lo is not None and self.limit_hi is not None \
                and self.limit_hi > self.limit_lo:
            lo, hi = float(self.limit_lo), float(self.limit_hi)
        else:
            self._span_hi = max(self._span_hi, self.position,
                                self.pos_b if self.pos_b is not None else 0)
            self._span_lo = min(self._span_lo, self.position,
                                self.pos_b if self.pos_b is not None else 0)
            lo, hi = float(self._span_lo), float(self._span_hi)
        if hi - lo < 10:
            hi = lo + 10
        pad = (hi - lo) * 0.05
        return lo - pad, hi + pad

    #: Inset at each end of the rail, reserved for the NEAR / FAR captions.
    #: They used to be QLabels bracketing the jog row; that row now also
    #: carries Set A/B and Go to A/B, and two words of axis annotation are
    #: not worth ~90 px of a row twelve buttons are sharing. They belong on
    #: the axis they describe anyway.
    END_PAD_L, END_PAD_R = 54.0, 48.0

    #: Below this the captions would eat the rail, so they are dropped and
    #: the old plain padding comes back. Only reachable if the window is
    #: dragged narrower than the preview's own minimum.
    MIN_W_FOR_ENDS = 220

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        ends = w >= self.MIN_W_FOR_ENDS
        left = self.END_PAD_L if ends else 10.0
        right = w - (self.END_PAD_R if ends else 10.0)
        y = h * 0.52
        lo, hi = self._domain()
        span = max(hi - lo, 1e-6)

        def x_of(v):
            return left + (right - left) * ((v - lo) / span)

        # Scrim, only when floating over the image. Dark enough to read a
        # hairline rail against a bright frame, light enough that you can
        # still see what is behind it — you are judging focus through this.
        if self._overlay:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, 130)))
            p.drawRoundedRect(0, 0, w, h, 8, 8)

        # Base rail
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0x1e, 0x23, 0x34)))
        p.drawRoundedRect(int(left), int(y - 4), int(right - left), 8, 4, 4)

        # Reachable travel between the mechanical stops
        if self.limit_lo is not None and self.limit_hi is not None:
            p.setBrush(QBrush(QColor(0x25, 0x2c, 0x42)))
            p.drawRoundedRect(int(x_of(self.limit_lo)), int(y - 4),
                              max(int(x_of(self.limit_hi)
                                      - x_of(self.limit_lo)), 2), 8, 4, 4)

        # A .. B working range  (A is always 0)
        if self.a_set and self.pos_b is not None and self.pos_b != 0:
            xa, xb = x_of(0), x_of(self.pos_b)
            x0, x1 = min(xa, xb), max(xa, xb)
            grad = QLinearGradient(x0, 0, x1, 0)
            grad.setColorAt(0.0, QColor(ACCENT_BLUE))
            grad.setColorAt(1.0, QColor(ACCENT_AMBER))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(int(x0), int(y - 4), max(int(x1 - x0), 3), 8, 4, 4)

        # End captions, in the inset reserved for them above. Same colours as
        # the jog arrows they sit over — blue is near, amber is far, all the
        # way down the panel.
        if ends:
            cap = QFont("Segoe UI", 7)
            cap.setBold(True)
            cap.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
            p.setFont(cap)
            p.setPen(QColor(ACCENT_BLUE))
            p.drawText(8, int(y - 9), int(left - 16), 18,
                       Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter, "NEAR")
            p.setPen(QColor(ACCENT_AMBER))
            p.drawText(int(right + 8), int(y - 9), int(w - right - 16), 18,
                       Qt.AlignmentFlag.AlignRight
                       | Qt.AlignmentFlag.AlignVCenter, "FAR")

        font = QFont("Cascadia Mono", 7)
        p.setFont(font)

        def marker(v, color, text, above):
            if v is None:
                return
            x = x_of(v)
            p.setPen(QPen(QColor(color), 1.5))
            p.drawLine(int(x), int(y - 11), int(x), int(y + 11))
            p.setPen(QColor(color))
            ty = int(y - 26) if above else int(y + 13)
            p.drawText(int(x - 24), ty, 48, 13,
                       Qt.AlignmentFlag.AlignCenter, text)

        marker(self.limit_lo, TEXT_MUTED, "stop", False)
        marker(self.limit_hi, TEXT_MUTED, "stop", False)
        if self.a_set:
            marker(0, ACCENT_BLUE, "A = 0", True)
        marker(self.pos_b, ACCENT_AMBER, "B", True)

        # Live handle
        xp = x_of(self.position)
        p.setPen(QPen(QColor(DARK_BG), 2))
        p.setBrush(QBrush(QColor(ACCENT_CYAN)))
        p.drawEllipse(int(xp - 6), int(y - 6), 12, 12)
        p.end()
