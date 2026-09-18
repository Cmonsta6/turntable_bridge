"""
In-app LiveView preview.

Reads `GetLiveViewImg` off the camera and paints it, so the preview you judge
focus from lives in this window rather than a floating one.

It owns the CENTRE COLUMN of the window, top to bottom under the Set up
bar, with every other panel stacked in the two side columns — see the map
in `layout.py`. The focus controls are in the same card: the A/B track
floats over the image and the single control row (Go to A · jog near ·
Set A · Set B · jog far · Go to B) sits hard under it — see
`layout._build_liveview_card`, which explains why judging a plane and
stepping to it belong in one panel.

HOW BIG THE FRAME DRAWS is decided by two things in this class and one in
the layout. `_apply_floor` keeps the panel at least the size of the frame AS
DISPLAYED, so the floor turns with the rotation; `ideal_width` says how wide
the panel would need to be for a frame of its current height to fill it
edge to edge; and `fit_changed` tells the layout when either answer moved,
so it can cap the card's width there and hand the rest of the window to the
side columns instead of drawing it as bars. Two earlier arrangements are
gone: a full-width band across the window (the preview drew at a third of
its box on a wide monitor, and a seventh when rotated) and, before that, a
right-column slot away from the focus controls.

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

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .theme import (
    ACCENT_AMBER, ACCENT_GREEN, BORDER, DARK_BG, PANEL_BG_SOFT, TEXT_MUTED,
    TEXT_SECONDARY,
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
    every non-streaming state says what it is instead — and draws the
    FOOTPRINT of the frame it is waiting for, a dashed outline in the frame's
    displayed shape. Without that, the rotate button did nothing visible
    until a camera was connected: the label changed, the floor and the width
    cap changed, but a grey box is the same grey box either way up. The
    outline also shows, before a single frame arrives, how much of the panel
    the picture will fill at this window size and rotation.
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
    #:
    #: These are the LANDSCAPE numbers. The floor actually applied is the
    #: frame's size as it appears on screen, so at 90° or 270° the two swap
    #: (`_apply_floor`) — a portrait frame in a landscape-shaped floor was
    #: guaranteed to draw downscaled, which the floor exists to prevent.
    MIN_W, MIN_H = FEED_W, FEED_H

    #: Emitted whenever the width the panel would like changed: on every
    #: change of height (the height it is fitting to moved) and on every
    #: rotation (the aspect it is fitting moved). The layout listens and
    #: re-caps the card — see `layout._fit_viewer_width`.
    fit_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self._message = "not connected"
        self._cam = None
        self._rotation = 0                  # degrees clockwise; see set_rotation

        self._apply_floor()
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
        if event.size().height() != event.oldSize().height():
            # Only height feeds `ideal_width`, so a width-only resize — which
            # is what the cap itself causes — must not re-emit, or the layout
            # and this panel would trade events until one of them gave up.
            self.fit_changed.emit()

    # ── how big the frame can draw ────────────────────────────────────────
    def _quarter_turn(self) -> bool:
        return self._rotation in (90, 270)

    def frame_shape(self) -> QSize:
        """
        The frame's size AS DISPLAYED — the camera's own pixels, turned.

        The real frame's dimensions when one has arrived, the Z 6_2's 750x500
        until then, and swapped at 90° or 270°. Everything that reasons about
        aspect goes through this so the rotation is applied in one place.
        """
        if self._image is not None and not self._image.isNull():
            shape = self._image.size()
        else:
            shape = QSize(self.FEED_W, self.FEED_H)
        if self._quarter_turn():
            shape = QSize(shape.height(), shape.width())
        return shape

    def ideal_width(self, height: int) -> int:
        """
        How wide the panel must be for a frame `height` tall to fill it.

        THE SCALING RULE, in one line: width follows height at the frame's
        displayed aspect. The panel lives in a column whose height is fixed
        by the window, so height is the given and width is the free variable.
        Any width beyond this number is a bar beside the picture; the layout
        caps the card here and the side columns take what is left.

        A number, not a `heightForWidth`. Tying HEIGHT to width would make the
        panel fight the window — widening demands more height, which pushes
        the total past the screen, which shrinks everything. Tying width to
        height cannot: nothing else in the row depends on this panel's width.
        """
        shape = self.frame_shape()
        return max(1, round(height * shape.width() / max(shape.height(), 1)))

    def _apply_floor(self) -> None:
        """Set the minimum to the frame's displayed shape — see MIN_W/MIN_H."""
        if self._quarter_turn():
            self.setMinimumSize(self.MIN_H, self.MIN_W)
        else:
            self.setMinimumSize(self.MIN_W, self.MIN_H)

    # ── display rotation ─────────────────────────────────────────────────
    #: The four settings the button cycles through, in order.
    ROTATIONS = (0, 90, 180, 270)

    def set_rotation(self, degrees: int) -> None:
        """
        Turn the displayed frame by 0, 90, 180 or 270 degrees clockwise.

        FOR A CAMERA MOUNTED IN PORTRAIT. The body streams LiveView in its own
        sensor orientation and says nothing about which way up it is bolted, so
        a camera turned on its side sends a perfectly ordinary landscape frame
        with the subject lying down in it. There is nothing to detect and
        nothing to ask: which way up the picture should be is a fact about the
        rig, so it is a setting.

        A DISPLAY TRANSFORM AND NOTHING ELSE — this is the important part. It
        lives in `paintEvent`, and no captured frame passes through this class
        at all: `ptp.shoot` reads the object off the camera and writes those
        exact bytes to disk. So this cannot rotate, re-encode, or otherwise
        touch a single saved image, and it cannot be blamed for one either.
        Nikon writes an orientation tag into the file from the body's own
        sensor, which is what makes the saved frames come out upright in an
        editor regardless of what is set here.

        Not stored on the camera and not sent to it. It costs one painter
        transform per frame rather than an image copy, so it is free at any
        frame rate.

        THE PANEL RESHAPES ITSELF TO MATCH. At 90 or 270 the picture is
        portrait, so the floor swaps to 500 wide by 750 tall (`_apply_floor`)
        and `ideal_width` starts answering for a tall frame; `fit_changed`
        then has the layout narrow the card to that width and give the rest
        of the row to the side columns. Both orientations fill their panel
        edge to edge. This used to letterbox — a portrait frame inside a
        landscape-shaped floor in a full-width band drew at a fraction of the
        box — and that is the arrangement the column layout replaced.
        """
        rotation = int(degrees) % 360
        if rotation not in self.ROTATIONS:
            raise ValueError(f"rotation must be one of {self.ROTATIONS}, "
                             f"not {degrees}")
        if rotation != self._rotation:
            self._rotation = rotation
            self._apply_floor()
            self.fit_changed.emit()
            self.update()

    @property
    def rotation(self) -> int:
        return self._rotation

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
        # makes it open a few pixels short of what the screen could give. Now
        # that the preview is the centre column and the whole point of the
        # layout is to give it every pixel the screen has, ask for twice the
        # feed: on a monitor that fits, the cap decides; on one that does not,
        # the window simply opens at the screen's size.
        return QSize(self.FEED_W * 2, self.FEED_H * 2)

    def paintEvent(self, _event) -> None:                     # noqa: D102
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()

        painter.fillRect(rect, QColor(PANEL_BG_SOFT))

        if self._image is None or self._image.isNull():
            # THE FOOTPRINT. Where the frame will go and what shape it will
            # be, fitted exactly as a real frame is below (`frame_shape`
            # already answers for the rotation), so this turns with the
            # rotate button and narrows with the window like the picture
            # would. A slightly darker fill and a dashed border: present
            # enough to read as "the picture goes here", quiet enough not to
            # be mistaken for one.
            shape = self.frame_shape()
            fitted = shape.scaled(rect.size(),
                                  Qt.AspectRatioMode.KeepAspectRatio)
            footprint = QRect(
                rect.x() + (rect.width() - fitted.width()) // 2,
                rect.y() + (rect.height() - fitted.height()) // 2,
                fitted.width(), fitted.height())
            painter.fillRect(footprint, QColor(DARK_BG))
            pen = QPen(QColor(TEXT_MUTED))
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawRect(footprint.adjusted(1, 1, -2, -2))
            # The frame's native size and the way it is turned, in the
            # corner: the one fact the outline cannot carry on its own.
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(footprint.adjusted(8, 6, -8, -6),
                             Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignTop,
                             f"{shape.width()}×{shape.height()}"
                             + (f"  ·  {self._rotation}°" if self._rotation
                                else ""))
        else:
            # Aspect-fit, letterboxed. Stretching a live view is worse than
            # useless when you are judging focus from it.
            #
            # FIT THE ROTATED SHAPE, not the frame's own. At 90 or 270 the
            # picture on screen is as tall as the frame is wide, so fitting the
            # unrotated size would size the box for a landscape image and then
            # draw a portrait one across it — the ends would hang outside the
            # panel. Swapping first is what keeps the whole frame inside the box
            # at every setting.
            quarter_turn = self._quarter_turn()
            shape = self.frame_shape()
            scaled = shape.scaled(rect.size(),
                                  Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(
                rect.x() + (rect.width() - scaled.width()) // 2,
                rect.y() + (rect.height() - scaled.height()) // 2,
                scaled.width(), scaled.height())

            if self._rotation:
                # Rotate the PAINTER, never the image. `QImage.transformed()`
                # would copy every frame — 750x500 at 15 fps, for a result the
                # GPU gives away in the transform. Costs nothing here.
                #
                # The draw rect is expressed in the ROTATED frame, so its width
                # and height go back the other way for a quarter turn: `target`
                # is what the result must measure on screen, and after a 90°
                # turn a rect w wide draws h tall. Centred on the origin because
                # the translate below puts the origin at the middle of `target`,
                # which is the one point a rotation must leave alone.
                w, h = ((scaled.height(), scaled.width()) if quarter_turn
                        else (scaled.width(), scaled.height()))
                painter.save()
                painter.translate(target.center())
                painter.rotate(self._rotation)
                # QRectF, not QRect: an odd width would put the centre half a
                # pixel out and the whole frame would sit fractionally off.
                painter.drawImage(QRectF(-w / 2.0, -h / 2.0, w, h), self._image)
                painter.restore()
            else:
                painter.drawImage(target, self._image)

        # EVERYTHING FROM HERE IS OUTSIDE THE ROTATION, by virtue of the
        # save/restore above rather than by accident. The status line, the frame
        # rate and the border belong to the panel, not to the picture — a
        # sideways "live view unavailable" would be a worse bug than the one the
        # rotation fixes. The focus track is safe for a different reason: it is
        # a real child widget (`set_overlay`), so no painter here touches it.
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
