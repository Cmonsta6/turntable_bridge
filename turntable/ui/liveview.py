"""
In-app LiveView preview.

Reads `GetLiveViewImg` off the camera and paints it, so the preview you judge
focus from lives in this window rather than a floating one.

It spans all three grid columns on a row of its own, and the focus controls
are in the same card: the A/B track floats over the image and the single
control row (Go to A · jog near · Set A · Set B · jog far · Go to B) sits
hard under it — see `layout._build_liveview_card`, which explains why
judging a plane and stepping to it belong in one panel. This note used to
say the preview sat in the RIGHT column, away from the focus controls, as
the only place it fit at a useful size; that layout is gone.

THREADING
---------
Every frame is a USB transaction of roughly 20 ms, so polling on the GUI thread
would make the whole window stutter. The feed runs in a QThread and hands
finished QImages over by signal; JPEG decoding happens there too, so the GUI
thread only ever blits.

The transport serialises itself with a lock, so a preview poll can never
interleave with a focus command or a capture — the worst it can do is wait. It
is still paused during a run: the run is the thing that must not be slowed
down, and hours of streaming would compete with it for the one USB link.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .theme import (
    ACCENT_AMBER, ACCENT_GREEN, BORDER, PANEL_BG_SOFT, TEXT_SECONDARY,
)


class LiveViewFeed(QThread):
    """
    Pulls LiveView frames from the camera until told to stop.

    Failure is expected rather than exceptional here — the camera goes busy
    during a capture, LiveView gets torn down between stacks, the USB link
    drops. So a failed poll backs off and reports rather than killing the
    thread; the preview simply resumes when frames come back.
    """

    frame = Signal(QImage)
    state = Signal(str)                     # short human-readable status

    #: Target rate. The body will not necessarily keep up, and that is fine —
    #: the loop measures what it actually achieved rather than assuming.
    DEFAULT_FPS = 15.0

    #: Wait this long after a failed poll before trying again, so a camera that
    #: has gone away does not spin the USB link.
    RETRY_S = 0.5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cam = None
        self._stop = False
        self._paused = True
        self._fps_target = self.DEFAULT_FPS
        self._measured_fps = 0.0

    # ── control ──────────────────────────────────────────────────────────
    def set_camera(self, cam) -> None:
        self._cam = cam

    def pause(self, reason: str = "paused") -> None:
        if not self._paused:
            self._paused = True
            self.state.emit(reason)

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def measured_fps(self) -> float:
        return self._measured_fps

    def stop(self) -> None:
        self._stop = True
        self._paused = False                # let the loop wake and exit
        self.wait(2000)

    # ── the loop ─────────────────────────────────────────────────────────
    def run(self) -> None:                                    # noqa: D102
        last = 0.0
        while not self._stop:
            if self._paused or self._cam is None:
                self.msleep(80)
                continue

            interval = 1.0 / max(self._fps_target, 1.0)
            now = time.monotonic()
            if now - last < interval:
                self.msleep(max(int((interval - (now - last)) * 1000), 1))
                continue

            try:
                jpeg = self._cam.get_liveview_jpeg()
            except Exception as e:                            # noqa: BLE001
                self.state.emit(f"live view unavailable: {e}")
                self.msleep(int(self.RETRY_S * 1000))
                continue

            if not jpeg:
                # Normal during a capture: the body stops serving frames while
                # it is busy. Not worth reporting as an error.
                self.msleep(int(self.RETRY_S * 1000))
                continue

            image = QImage()
            if not image.loadFromData(jpeg, "JPG"):
                self.state.emit("frame did not decode")
                self.msleep(int(self.RETRY_S * 1000))
                continue

            elapsed = time.monotonic() - last if last else 0.0
            if elapsed > 0:
                # Smooth it, or the readout flickers between adjacent values.
                instant = 1.0 / elapsed
                self._measured_fps = (instant if not self._measured_fps
                                      else self._measured_fps * 0.8 + instant * 0.2)
            last = time.monotonic()
            self.frame.emit(image)


class LiveViewPanel(QWidget):
    """
    Shows the feed, or explains why it is not showing anything.

    An empty black rectangle is indistinguishable from a broken preview, so
    every non-streaming state says what it is instead.
    """

    #: The camera's LiveView frame, in pixels. The body sends 750x500 (3:2) on
    #: the Z 6_2; the panel is shaped to match so a frame fills it edge to edge
    #: instead of sitting in letterbox bars.
    FEED_W, FEED_H = 750, 500

    #: Never smaller than the feed itself. Below this the preview is a
    #: DOWNSCALE of what the camera sent — you would be judging focus from
    #: fewer pixels than the camera was willing to give you, which is the one
    #: thing this panel exists not to do. Growing is fine: upscaling costs
    #: sharpness but hides nothing.
    MIN_W, MIN_H = FEED_W, FEED_H

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self._message = "not connected"
        self._cam = None

        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._overlay: Optional[QWidget] = None

        # Built HERE, once, and never anywhere else. These four lines used to
        # sit at the end of resizeEvent — close enough to __init__ to read like
        # part of it, but a comment block below `_place_overlay` had absorbed
        # them into the method body. The panel then had no `_feed` until its
        # first resize (so set_camera and shutdown raised AttributeError), and
        # every later resize started ANOTHER thread without stopping the last,
        # leaving the preview attached to a camera-less feed while `streaming`
        # still reported True. Keep construction in __init__.
        self._feed = LiveViewFeed(self)
        self._feed.frame.connect(self._on_frame)
        self._feed.state.connect(self._on_state)
        self._feed.start()

    #: Inset for the floating overlay, so it does not touch the panel edges.
    OVERLAY_MARGIN = 10

    def set_overlay(self, widget: "QWidget") -> None:
        """
        Float a widget over the bottom of the preview.

        Used for the focus track. It goes ON the image rather than under it
        because the image is the expensive pixel here — every row the track
        occupies below the preview is a row the preview does not get, and the
        track is a thin rail that costs nothing to read through.
        """
        widget.setParent(self)
        widget.raise_()
        self._overlay = widget
        self._place_overlay()

    def _place_overlay(self) -> None:
        if self._overlay is None:
            return
        m = self.OVERLAY_MARGIN
        h = self._overlay.height()
        self._overlay.setGeometry(m, self.height() - h - m,
                                  max(self.width() - 2 * m, 1), h)

    def resizeEvent(self, event):                              # noqa: D102
        super().resizeEvent(event)
        self._place_overlay()

    # ── wiring ───────────────────────────────────────────────────────────
    def set_camera(self, cam) -> None:
        """Attach (or detach, with None) the camera the feed reads from."""
        self._cam = cam
        self._feed.set_camera(cam)
        if cam is None:
            self._feed.pause("not connected")
            self._image = None
            self._message = "not connected"
            self.update()

    def start(self) -> bool:
        """
        Begin streaming. Returns False if there is no camera to stream from.

        This used to say that starting LiveView on the body is what MfDrive
        needs anyway, so the stream costs nothing jogging focus would not
        already have paid. The conclusion survives; the reason does not.
        Measured on the Z 6_2 on 2026-08-03, MfDrive is accepted and moves the
        lens with LiveView down exactly as it does with LiveView up, so the
        body needs nothing of the sort. What actually makes the stream free is
        `PTPCameraClient._ensure_liveview`: every focus command goes through
        it and it raises LiveView if one is not already up, so a jog opens the
        stream on the body whether or not this panel asked for it.
        """
        if self._cam is None:
            self._message = "not connected"
            self.update()
            return False
        try:
            self._cam.show_liveview()
        except Exception as e:                                # noqa: BLE001
            self._message = f"could not start live view: {e}"
            self.update()
            return False
        self._message = "starting…"
        self._feed.resume()
        self.update()
        return True

    def pause(self, reason: str = "paused") -> None:
        self._feed.pause(reason)

    def resume(self) -> None:
        if self._cam is not None:
            self._feed.resume()

    @property
    def streaming(self) -> bool:
        return not self._feed.paused and self._cam is not None

    def shutdown(self) -> None:
        """Stop the thread. Must be called before the window closes."""
        self._feed.stop()

    # ── slots ────────────────────────────────────────────────────────────
    def _on_frame(self, image: QImage) -> None:
        self._image = image
        self._message = ""
        self.update()

    def _on_state(self, text: str) -> None:
        self._message = text
        self.update()

    # ── painting ─────────────────────────────────────────────────────────
    def sizeHint(self) -> QSize:                              # noqa: D102
        # NO heightForWidth anywhere in this class, deliberately. Tying height
        # to width sounds right — the box would always be exactly 3:2 and never
        # letterbox — but it makes the panel FIGHT the window: widening to
        # 990 px demanded 660 px of height, which pushed the total past the
        # screen, which shrank the preview, which is the opposite of what
        # widening was for. Aspect is handled where it belongs instead, in
        # paintEvent, which fits the frame inside whatever box it is given.
        # MIN_W/MIN_H are what actually matter: never fewer pixels than the
        # camera sent.
        #
        # Deliberately larger than the floor. The window opens at its content's
        # sizeHint capped to the screen, so asking for exactly the feed size
        # makes it open a few pixels short of what the screen could give — and
        # those few pixels are the difference between a frame that fills the
        # panel and one sitting in a 40 px letterbox. Ask generously; the cap
        # and the floor between them decide what actually happens.
        return QSize(round(self.FEED_W * 1.35), round(self.FEED_H * 1.35))

    def paintEvent(self, _event) -> None:                     # noqa: D102
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()

        painter.fillRect(rect, QColor(PANEL_BG_SOFT))

        if self._image is not None and not self._image.isNull():
            # Aspect-fit, letterboxed. Stretching a live view is worse than
            # useless when you are judging focus from it.
            scaled = self._image.size().scaled(
                rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(
                rect.x() + (rect.width() - scaled.width()) // 2,
                rect.y() + (rect.height() - scaled.height()) // 2,
                scaled.width(), scaled.height())
            painter.drawImage(target, self._image)

        if self._message:
            painter.setPen(QPen(QColor(TEXT_SECONDARY)))
            font = QFont()
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._message)

        # A frame rate that has quietly collapsed is worth seeing, so it is
        # always on screen rather than hidden behind a debug toggle.
        if self._image is not None and self.streaming:
            fps = self._feed.measured_fps
            painter.setPen(QPen(QColor(ACCENT_GREEN if fps >= 5 else ACCENT_AMBER)))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(rect.adjusted(8, 6, -8, -6),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                             f"{fps:.0f} fps")

        painter.setPen(QPen(QColor(BORDER)))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()
