"""
Widget construction for the main window. Mixed into MainWindow.

Everything here builds widgets and assigns them to `self`; the behaviour
that reads them lives in main_window.py. Splitting along that seam keeps
either half readable on its own.

ONE GRID, THREE COLUMNS, TWO ARRANGEMENTS — THE VIEWER IS THE MIDDLE COLUMN
----------------------------------------------------------------------------
The preview rotation decides which. 0° and 180° are a LANDSCAPE frame, 90°
and 270° a PORTRAIT one, and the two want different things from the window:
a landscape frame is starved of WIDTH, a portrait frame of HEIGHT. So there
are two placements of the same nine cards, and `_arrange` swaps between
them whenever the rotation crosses from one family to the other — without
rebuilding anything, so a run in progress, the live feed and every field's
value are untouched.

LANDSCAPE (0°, 180°) — the viewer gets the middle column under Set up:

    col 0  (stretch 0)    col 1  (stretch 1)    col 2  (stretch 0)
    ──────────────────    ──────────────────    ──────────────────
    ◄───────────── SET UP — devices and destination ─────────────►   row 0
    options               ┌────────────────┐    TURNTABLE DIAL       row 1
    progress              │   LIVE VIEW    │    (soaks up slack)
    timing                │  track on the  │    capture
    event log             │ image, controls│    run
    (soaks up slack)      └────── under ───┘

PORTRAIT (90°, 270°) — the viewer gets the middle column top to bottom, and
Set up moves into the left column so the frame can have the height Set up
was taking:

    col 0  (stretch 0)    col 1  (stretch 1)    col 2  (stretch 0)
    ──────────────────    ──────────────────    ──────────────────
    set up                ┌──────────┐          TURNTABLE DIAL       row 0
    options               │          │          (soaks up slack)
    progress              │ LIVE VIEW│          capture
    timing                │          │          run
    event log             │          │
    (soaks up slack)      └──────────┘

In both, the side columns are a QVBoxLayout each. Left is what you SET and
READ — Set up when it is not on top, Options, Progress, Timing, then the
Event log growing into whatever is left. Right is what you DO — the dial,
growing, then Capture and Run, so Start lands in the bottom-right corner
where a hand looking for it goes first. The left column takes Set up
rather than the right because the right is the taller stack (the dial's
floor is 381 px): with Set up and Progress both on the left the columns
come to 834 and 752 px, under the portrait viewer's own 847, so on a
1080p screen nothing has to scale.

WHY PORTRAIT MOVES SET UP AND LANDSCAPE DOES NOT. Measured on a 2560x1400
window: a landscape frame is WIDTH-limited (the side columns' minimums
leave it 1648 px, and it would need 1698 to be height-limited), so giving
it Set up's ~120 px of height gains nothing. A portrait frame is
HEIGHT-limited at 1132 px tall; with Set up out of the way it gets ~1270,
which is a quarter more picture. The side columns have the room: a
portrait frame is only ~850 px wide, so they get ~850 px each, and Set
up's 609 px minimum fits with margin.

WHY A COLUMN AND NOT A BAND. The preview was a full-width band across the
window on the only stretching row, and on any real monitor that starves it:
the band was ~2500 x 550 on a 2560-wide screen, a 3:2 frame in a 4.6:1 box
uses a third of it, and a rotated 2:3 frame a seventh. Height is what a
viewer needs and the side panels are content-height cards that do not — so
stack them and the viewer gets the window's full height instead of what
three rows of cards leave over. The band was kept for a long time because
it made the column seams line up down the window; they still do, and there
is only one seam per side now.

THE SCALING RULE. Width follows height at the frame's displayed aspect, and
the side columns take what is left. Concretely: only column 1 stretches, so
the viewer is handed every pixel of surplus width first; on each resize and
each rotation `_fit_viewer_width` caps the live-view card at
`LiveViewPanel.ideal_width(height)`, and a stretched column that has hit
its maximum makes Qt share the remainder between the other two columns
equally (measured, not assumed — see the note in `_fit_viewer_width`). So a
landscape frame is width-limited and takes all the width there is, a
portrait frame is height-limited and gives back the width it cannot use,
and neither draws bars beside itself beyond a few pixels of rounding. The
panel's floor turns with the rotation too (`LiveViewPanel._apply_floor`),
so a portrait frame is never downscaled just because its floor was shaped
for a landscape one. `LiveViewPanel.sizeHint` records why the rule is a
width-from-height cap and not a `heightForWidth`.

THE SIDE COLUMNS DO NOT SHARE A MINIMUM WIDTH. An earlier build set both to
the wider of the two natural minimums for symmetry — and never actually
did, because a QVBoxLayout measured inside `_build_ui` answers 0 before its
first layout pass. Re-placing the cards on rotation made the rule take
effect for real, and it cost the landscape frame 66 px of width on a
1080p screen (the left column's 387 held to the right's 453). The frame
wins: each side sits at its own natural minimum, the viewer takes the
difference, and when the width cap binds the surplus is still shared
equally, so the window is only ever off-centre by half the difference of
two card minimums.

Only row 1 stretches; row 0 is content height. Inside each side column the
last card is `Card(expand=True)` and takes the slack, which is what keeps
every other panel's title hard against its own top edge (a card that
neither expands nor is pinned floats its title down the middle of itself).

THE WINDOW HAS NO MINIMUM SIZE, and grows no scrollbars. Shrinking it does two
things in order: the layout below reflows and tightens as far as it honestly
can, and past that point `ScalingHost` (widgets.py) scales the entire content
down as one piece. This layout has a genuine floor — the preview will not draw
below the camera's own 750x500 — and Qt's response to a window smaller than its
content is to leave over-minimum widgets at their minimum and let them OVERLAP,
which is what once drew the jog buttons on top of the live image. Scaling is
what makes a smaller window legal without either forbidding it or overlapping.
Do not reintroduce a window minimum.

MEASURE `content_widget`, NOT `centralWidget()`. `centralWidget()` is the
ScalingHost now, so the widget being scaled — the one whose geometry is the
layout's real numbers — is `content_widget`, and `_build_ui` keeps a handle on
it for exactly that. `MainWindow._size_to_fit_panels` reads it the same way.
The script this was measured with was a development tool and is not shipped,
so measuring again means writing one. For the record, the minimums that
shaped the column layout (Windows, Segoe UI, 2026-09-18): Options 376,
Progress 324, Timing 388, Event log 161 wide on the left; Dial 415, Capture
453, Run 322 on the right; the live-view card 821 — the focus row's 793
(twelve buttons, the eight arrows held at the widest label's width; see
`FocusPanel._relabel_jog_buttons`) plus margins, above the panel's own 776
— once the header stopped demanding 949 (see `_HintWidthBox`). So the
whole layout's floor is about 1705 x 930, which fits a 1920 x 1080 screen
unscaled in landscape and scales by a few percent in portrait.
`tools/render_layout.py` is the script that measures this and renders the
window at any size to check it.

BEWARE STYLESHEET min-height. A Qt stylesheet's min-height/max-height BEAT
`setMinimumHeight`/`setFixedHeight` on the same widget, and theme.py's global
`QPushButton` rule carries `min-height: 20px`. Every button height in this file
therefore has to live in a stylesheet, not in a widget call — two separate bugs
here were buttons silently rendering at padding-plus-font size while the code
next to them asked for something taller and was ignored.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGraphicsDropShadowEffect,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QSizePolicy, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from .dial import DirectionToggle, TurntableWidget
from .liveview import LiveViewPanel
from .focus_panel import FocusPanel
from .theme import (
    ACCENT_AMBER, ACCENT_BLUE, ACCENT_CYAN, ACCENT_GREEN, ACCENT_PURPLE,
    ACCENT_RED, BORDER, BORDER_SOFT, DARK_BG, PANEL_BG_SOFT, TEXT_MUTED,
    TEXT_PRIMARY, TEXT_SECONDARY, btn_style, rgba,
)
from .widgets import Card, ScalingHost, StatusPill


class _Keep:
    """
    Looks like a layout to the `_build_*` methods, and keeps what it is given.

    They all end in `parent.addWidget(card)` — some with a stretch factor —
    and know nothing about where the card goes. That is the point: placement
    is `_arrange`'s job, done after everything is built and done AGAIN each
    time the rotation changes family, so a builder that placed its own card
    would have to be undone. This records the card and its stretch instead.
    """

    __slots__ = ("card", "stretch")

    def __init__(self):
        self.card, self.stretch = None, 0

    def addWidget(self, widget, stretch: int = 0, *_args, **_kwargs):  # noqa: N802
        self.card, self.stretch = widget, stretch


#: Which card goes where, per orientation — see the module docstring's map.
#: `top` spans all three columns; `left` and `right` are stacked in order.
#: Cards are the names `_build_ui` registers in `self._cards`.
ARRANGEMENTS = {
    "landscape": dict(
        top=["setup"],
        left=["options", "progress", "timing", "log"],
        right=["dial", "capture", "run"],
    ),
    "portrait": dict(
        top=[],
        left=["setup", "options", "progress", "timing", "log"],
        right=["dial", "capture", "run"],
    ),
}


class WindowLayoutMixin:
    #: One height for every control in the live-view card's header, so the
    #: zoom group and Start live view sit on a single line. They were 22 px
    #: buttons around a free-sizing label, which was enough to step the row.
    HEAD_BTN_H = 24

    #: Transport button height, as a STYLESHEET min-height in px — it has to
    #: go in every sheet applied to those buttons, and it is a number rather
    #: than a whole rule because each button also carries its own colours.
    #:
    #: In one place because `_update_button_states` in main_window.py REPLACES
    #: btn_pause's sheet outright every time the run state changes. Whatever
    #: this file sets on that button is gone the first time the window updates
    #: itself, which is why Pause alone stayed 36 px while Start and Stop went
    #: to 56. Both files read this constant so the row cannot go ragged again.
    #:
    #: 40 is the CONTENT height; theme.py's global rule adds 7 px of padding
    #: top and bottom and a 1 px border, so the buttons draw 56 px tall.
    TRANSPORT_BTN_MIN_H = 40

    #: Spin-button look, as a template so `_refresh_spin` can re-fill it to
    #: light whichever speed is running. Kept in one place because the lit
    #: and unlit variants must differ ONLY in the fill — a button that also
    #: changed size or weight when selected would shift the row.
    #: THE HEIGHT LIVES HERE, not in `setFixedHeight`. A Qt stylesheet's
    #: min-height/max-height beat the widget properties, so the `min-height: 0`
    #: this used to carry silently cancelled the `setFixedHeight` next to it —
    #: the buttons were whatever padding and font-size happened to add up to
    #: (16 px), not the 28 the constructor asked for. `_refresh_spin`
    #: re-applies this sheet on every toggle, so the sheet is the only thing
    #: that gets the last word. Change the size here.
    #: FONT SIZE IS SET BY THE WIDEST LABEL, which is the four-arrow button.
    #: Measured with QFontMetrics at the default window, where these buttons
    #: come out 69 px wide and so have 61 px of content width once padding and
    #: border are taken off: "►►►►" needs 60 px at 15 px, which is a 1 px
    #: margin and reads as clipped, and 48 px at 12 px, which does not. The
    #: buttons only get taller from here, not wordier — if a fifth speed is
    #: ever added, re-measure rather than assuming this still fits.
    SPIN_BTN_CSS = (
        "QPushButton {{ color: {accent}; font-size: 12px; font-weight: 700;"
        " padding: 2px 3px; min-height: 30px; background-color: {bg};"
        " border: 1px solid {border}; }}"
        "QPushButton:hover {{ background-color: {hover};"
        " border-color: {hover_border}; }}"
        "QPushButton:disabled {{ color: {muted}; border-color: {soft};"
        " background-color: {dark}; }}"
    )

    def _build_ui(self):
        # THE CONTENT RELAYOUTS, THEN SCALES — `ScalingHost` in widgets.py
        # owns the reasoning and the rejected alternatives. In short: the
        # window has no minimum and grows no scrollbars, so shrinking it
        # tightens the panels normally until the layout has nothing left to
        # give, and past that the whole UI scales down as one piece.
        content = QWidget()
        #: The real content widget. `centralWidget()` is the ScalingHost, so
        #: anything measuring the layout wants this instead — see
        #: `MainWindow._size_to_fit_panels`, which sizes the window off it.
        self.content_widget = content
        self.scaling_host = ScalingHost(content)
        self.setCentralWidget(self.scaling_host)
        root = QVBoxLayout(content)
        root.setContentsMargins(12, 8, 12, 9)
        root.setSpacing(9)

        root.addLayout(self._build_header())

        # ── THREE COLUMNS, TWO ROWS, ONE GRID — see the module docstring ──
        # The viewer is the middle column and the seven other panels stack
        # beside it. A QGridLayout still, rather than one QHBoxLayout, so the
        # Set up bar spans the three columns and their edges line up under it.
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        # 0 : 1 : 0 — ONLY THE VIEWER STRETCHES. Every surplus pixel of width
        # goes to the preview first, and the side columns get some only once
        # `_fit_viewer_width` has capped the card because the frame could not
        # use any more. Stretching the sides as well would hand the frame a
        # share of the window it can never draw.
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 0)
        root.addLayout(grid, 1)
        self._grid = grid

        # BUILT ONCE, PLACED BY `_arrange`. Each builder hands its card to a
        # `_Keep`, which records it and the stretch the builder asked for; the
        # builders neither know nor care which column they end up in, which
        # is what lets the arrangement change under them later.
        self._cards = {}
        self._card_stretch = {}
        for name, build in (
            ("setup", self._build_setup_card),
            ("options", self._build_options_card),
            ("progress", self._build_progress_card),
            ("timing", self._build_timing_card),
            ("log", self._build_log_card),
            ("liveview", self._build_liveview_card),
            ("dial", self._build_dial_card),
            ("capture", self._build_rotation_card),
            ("run", self._build_transport_card),
        ):
            keep = _Keep()
            build(keep)
            self._cards[name] = keep.card
            self._card_stretch[name] = keep.stretch
        self._arranged = None
        self._arrange("landscape")

    # ── placing the cards ────────────────────────────────────────────
    def _arrange(self, orientation: str) -> None:
        """
        Put the nine cards where `ARRANGEMENTS[orientation]` says.

        Runs at build and again whenever the rotation moves between the
        landscape family (0°, 180°) and the portrait one (90°, 270°). It is
        a RELAYOUT, not a rebuild: the cards are the same widgets before and
        after, still children of the content widget, so every field keeps
        its value, the worker thread keeps running, the live feed keeps
        painting, and the only thing that changes is which grid cell each
        card is measured into. Nothing is hidden or shown either — taking an
        item out of a layout leaves the widget where it is, visible, until
        the next layout claims it.

        The grid is emptied completely each time rather than patched. Nine
        cards and two throwaway QVBoxLayouts cost nothing to re-place, and
        "remove everything, add everything" cannot leave a card in two
        places or none — which patching, the first time a name moved
        columns, did.
        """
        if orientation == self._arranged:
            return
        self._arranged = orientation
        grid = self._grid
        plan = ARRANGEMENTS[orientation]
        # Set up is wide across the top and stacked in a column.
        self._arrange_setup("wide" if "setup" in plan["top"] else "stacked")

        # Empty the grid. Sub-layouts are QObjects owned by the grid, so they
        # are detached and deleted; their widgets stay put.
        while grid.count():
            item = grid.takeAt(0)
            box = item.layout()
            if box is not None:
                while box.count():
                    box.takeAt(0)
                box.setParent(None)
                box.deleteLater()

        # Landscape: Set up spans the top row and the columns fill row 1.
        # Portrait: no top row; the columns and the viewer own row 0 and the
        # empty row 1 gets no stretch, so it collapses to nothing.
        if plan["top"]:
            grid.addWidget(self._cards[plan["top"][0]], 0, 0, 1, 3)
            row = 1
        else:
            row = 0
        grid.setRowStretch(row, 1)
        grid.setRowStretch(1 - row, 0)

        columns = {}
        for col, names in ((0, plan["left"]), (2, plan["right"])):
            box = QVBoxLayout()
            box.setSpacing(8)
            for name in names:
                box.addWidget(self._cards[name], self._card_stretch[name])
            grid.addLayout(box, row, col)
            columns[col] = box
        grid.addWidget(self._cards["liveview"], row, 1)
        self._side_columns = columns

    @staticmethod
    def orientation_for(rotation: int) -> str:
        """Which arrangement a preview rotation calls for."""
        return "portrait" if rotation % 360 in (90, 270) else "landscape"

    # ── header ───────────────────────────────────────────────────────
    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(9)

        title = QLabel("COMXIM  +  NIKON  FOCUS  STACKING")
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 700;"
            f"letter-spacing: 2.4px;")
        bar.addWidget(title)

        sub = QLabel("Nikon · USB")
        sub.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        bar.addWidget(sub)
        bar.addStretch()

        # Connection state, not the camera model: model detection was
        # unreliable and the Connect button already shows the state.
        self.pill_camera = StatusPill("CAMERA")
        self.pill_tt = StatusPill("TURNTABLE")
        self.pill_state = StatusPill("SESSION")
        self.pill_state.set_state("idle", TEXT_MUTED)
        self.pill_elapsed = StatusPill("ELAPSED")
        self.pill_elapsed.set_state("00:00", ACCENT_GREEN)
        self.pill_elapsed.setToolTip('Time the session has been running. Pauses while held or paused.')
        bar.addWidget(self.pill_camera)
        bar.addWidget(self.pill_tt)
        bar.addWidget(self.pill_state)
        bar.addWidget(self.pill_elapsed)
        return bar

    # ── live view, with the focus controls inside it ─────────────────
    def _build_liveview_card(self, parent):
        """
        The preview and the focus controls are ONE panel.

        Judging a focus plane and stepping to it is a single action, and it
        was previously spread across two panels — look up here, click down
        there, look back. Inside one card the controls sit hard against the
        bottom edge of the image, so the thing you are watching and the thing
        you are pressing are a few pixels apart.

        ONE control row now, not two. Go to A · jog near · Set A · Set B ·
        jog far · Go to B, reading near→far across the width, under a track
        that floats on the image itself. The commits had a separate row and a
        hairline of their own; that cost ~50 px of preview to guard against a
        button that is not in either row. Calibrate is the click that destroys
        work, and Calibrate is in the header.
        """
        card = Card("Live view", ACCENT_CYAN, expand=True)
        card.setToolTip(
            'What the camera sees. Opens on connect and whenever you touch a focus control. Pauses during a run.')

        self.btn_liveview = QPushButton("Start live view")
        self.btn_liveview.setCheckable(True)
        self.btn_liveview.setFixedHeight(self.HEAD_BTN_H)
        self.btn_liveview.setStyleSheet(
            f"QPushButton {{ font-size: 10px; padding: 2px 10px;"
            f" color: {TEXT_MUTED}; background: transparent;"
            f" border: 1px solid {BORDER}; border-radius: 5px; }}"
            f"QPushButton:hover {{ color: {ACCENT_CYAN}; }}"
            f"QPushButton:checked {{ color: {ACCENT_CYAN};"
            f" border-color: {rgba(ACCENT_CYAN, 0.5)}; }}")
        self.btn_liveview.setToolTip(
            'Start or stop the preview by hand. It opens by itself on connect and when you jog focus.')
        # The focus panel is built here but never added as a whole: it hands
        # out two pieces and keeps ownership of every control in them.
        self._build_focus_controls()

        # Header, left to right: title … Calibrate · Home … zoom · rotate ·
        # Start live view · Single shot. The lens pair sits in the middle, away
        # from both the title and the button people actually reach for, because
        # Calibrate clears A and B and should never be a near-miss for
        # anything.
        #
        # Centred by `Card.center_header_group`, which is assembled at the end
        # of this method once the right-hand group exists — read its docstring
        # for why balancing the two stretches was not enough on its own.
        centre_buttons = list(self.focus_panel.cal_buttons)

        # Same quiet treatment: a diagnostic that belongs beside the lens
        # controls rather than in the Options list, where it read as a
        # preference. Checkable, so its state is visible at a glance.
        self.chk_debug = QPushButton("Trace")
        self.chk_debug.setCheckable(True)
        self.chk_debug.setStyleSheet(
            self.focus_panel.btn_home.styleSheet()
            + f"QPushButton:checked {{ color: {ACCENT_CYAN};"
              f" border-color: {rgba(ACCENT_CYAN, 0.5)}; }}")
        self.chk_debug.setToolTip(
            "Log every focus move, capture and connection event with its result and\ntiming, to the event log and camera_trace.log in the save folder.\n\nTurn on when the lens isn't moving or a shot isn't landing.")
        self.chk_debug.toggled.connect(self._on_debug_toggled)
        centre_buttons.append(self.chk_debug)

        # ── the magnifier ─────────────────────────────────────────────
        # Beside Start live view rather than with Calibrate/Home: this is a
        # VIEWING control, it changes nothing about the lens or the run, and
        # it belongs with the other button that only affects what you see.
        #
        # A readout as well as the two buttons, because the magnifier is
        # modal and invisible once you have looked away — a zoomed preview
        # and a badly-framed subject look identical until you zoom out.
        # HEAD_BTN_H is shared with Start live view below. The three zoom
        # widgets and that button are the header's right-hand group and have
        # to sit on one line; a label left free to size itself was enough to
        # nudge the row and leave them visibly stepped.
        self.lbl_zoom = QLabel("1×")
        self.lbl_zoom.setFixedWidth(34)
        self.lbl_zoom.setFixedHeight(self.HEAD_BTN_H)
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_zoom.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 12px; font-weight: 700;"
            f"font-family: 'Cascadia Mono', Consolas, monospace;")

        zoom_tip = (
            "Magnify the preview to judge focus, without moving the lens or\n"
            "affecting a single captured frame — the camera crops the sensor\n"
            "readout and the stream stays the same size, so the magnified\n"
            "view is real detail rather than an upscale.\n"
            "\n"
            "Three steps plus off. Live view has to be running.")
        # Carried in the card's own cyan rather than muted grey: this is a
        # control you reach for while judging focus, and at TEXT_MUTED on a
        # transparent field it read as decoration next to the buttons around
        # it. Filled, accented and a size up.
        self.btn_zoom_out = QPushButton("－")
        self.btn_zoom_in = QPushButton("＋")
        for b in (self.btn_zoom_out, self.btn_zoom_in):
            b.setFixedHeight(self.HEAD_BTN_H)
            b.setFixedWidth(30)
            b.setToolTip(zoom_tip)
            b.setStyleSheet(
                f"QPushButton {{ font-size: 15px; font-weight: 700;"
                f" padding: 0px; min-height: 0px; color: {ACCENT_CYAN};"
                f" background-color: {PANEL_BG_SOFT};"
                f" border: 1px solid {rgba(ACCENT_CYAN, 0.45)};"
                f" border-radius: 5px; }}"
                f"QPushButton:hover {{ color: #ffffff;"
                f" background-color: {rgba(ACCENT_CYAN, 0.22)};"
                f" border-color: {ACCENT_CYAN}; }}"
                f"QPushButton:disabled {{ color: {BORDER};"
                f" background-color: transparent;"
                f" border-color: {BORDER_SOFT}; }}")
        self.btn_zoom_out.clicked.connect(lambda: self._step_zoom(-1))
        self.btn_zoom_in.clicked.connect(lambda: self._step_zoom(+1))
        self.btn_liveview.clicked.connect(self._toggle_liveview)

        # ── the rotate ────────────────────────────────────────────────
        # For a camera bolted on its side. Beside the magnifier because it is
        # the same kind of control — it changes what you see and nothing else,
        # touches no saved frame, and (unlike the magnifier) needs no camera at
        # all, so it can be set before connecting.
        #
        # ONE BUTTON THAT CYCLES, with its own label as the readout, rather than
        # the magnifier's two-buttons-and-a-label. Four states means three
        # clicks back to upright at worst, and this is set once when the camera
        # is mounted and then left alone — spending three header widgets on it
        # would cost preview width every day to save one click a week. The label
        # carries the angle for the same reason the magnifier has a readout at
        # all: a rotated preview and a sideways subject look identical.
        self.btn_rotate = QPushButton("⟳ 0°")
        self.btn_rotate.setFixedHeight(self.HEAD_BTN_H)
        # Wide enough for the longest label it can hold ("⟳ 270°"), fixed so
        # cycling cannot change the button's width — the header's two ends are
        # given matching minimum widths at build time
        # (`Card.center_header_group`), and a button that grew mid-cycle would
        # shunt the centred lens buttons sideways.
        self.btn_rotate.setFixedWidth(62)
        self.btn_rotate.setStyleSheet(
            f"QPushButton {{ font-size: 11px; font-weight: 700;"
            f" padding: 0px; min-height: 0px; color: {ACCENT_CYAN};"
            f" background-color: {PANEL_BG_SOFT};"
            f" border: 1px solid {rgba(ACCENT_CYAN, 0.45)};"
            f" border-radius: 5px; }}"
            f"QPushButton:hover {{ color: #ffffff;"
            f" background-color: {rgba(ACCENT_CYAN, 0.22)};"
            f" border-color: {ACCENT_CYAN}; }}")
        self.btn_rotate.setToolTip(
            "Turn the preview 90° clockwise. Click again to keep going —\n"
            "0° → 90° → 180° → 270° → 0°.\n"
            "\n"
            "For a camera mounted in portrait. The body streams live view in\n"
            "its own sensor orientation and never says which way up it is\n"
            "bolted, so a camera on its side sends a landscape frame with the\n"
            "subject lying down in it.\n"
            "\n"
            "Affects the preview ONLY. Saved photos are the camera's own bytes\n"
            "and are not touched, re-encoded or rotated by this.\n"
            "\n"
            "Remembered between sessions, since the camera stays mounted.")
        self.btn_rotate.clicked.connect(self._step_rotation)

        # ── the single shot ───────────────────────────────────────────
        # One frame, no rotation, into <base>/<subject>_singleshots. A test
        # exposure: check the light, the framing and the focus plane you just
        # set, without committing to a run.
        #
        # LAST IN THE HEADER, behind a hairline, and the only filled AMBER
        # control in it — because it is the one button up here that fires the
        # shutter. Everything else in this group only changes what is on
        # screen, and a shutter release sitting flush against the zoom pair is
        # a mis-click that costs an actuation. `_single_shot` in
        # main_window.py owns the behaviour.
        shot_sep = QFrame()
        shot_sep.setObjectName("hairline")
        shot_sep.setFixedWidth(1)
        shot_sep.setFixedHeight(self.HEAD_BTN_H)

        self.btn_single_shot = QPushButton("◉  Single shot")
        self.btn_single_shot.setFixedHeight(self.HEAD_BTN_H)
        self.btn_single_shot.setStyleSheet(
            f"QPushButton {{ font-size: 10px; font-weight: 700;"
            f" padding: 2px 10px; min-height: 0px; color: {ACCENT_AMBER};"
            f" background-color: {rgba(ACCENT_AMBER, 0.16)};"
            f" border: 1px solid {rgba(ACCENT_AMBER, 0.5)};"
            f" border-radius: 5px; }}"
            f"QPushButton:hover {{ color: #ffffff;"
            f" background-color: {rgba(ACCENT_AMBER, 0.34)};"
            f" border-color: {ACCENT_AMBER}; }}"
            f"QPushButton:disabled {{ color: {TEXT_MUTED};"
            f" background-color: {DARK_BG};"
            f" border-color: {BORDER_SOFT}; }}")
        self.btn_single_shot.setToolTip(
            "Take one photo right now. The table does not turn and the focus\n"
            "does not move — it shoots exactly what the preview is showing.\n"
            "\n"
            "Lands in <save folder>/<subject>_singleshots, numbered, and never\n"
            "overwrites an earlier one. Always saved to the PC, even with\n"
            "'Keep photos on the camera card' ticked.\n"
            "\n"
            "For checking the light, the framing and the focus plane before\n"
            "committing to a run.")
        self.btn_single_shot.clicked.connect(self._single_shot)

        # Left = the dot and title already in the header, middle = the lens
        # buttons, right = the viewing controls plus the single shot. Done in
        # one call at the end because the balance needs the right-hand group to
        # exist first.
        card.center_header_group(
            centre_buttons,
            [self.btn_zoom_out, self.lbl_zoom, self.btn_zoom_in,
             self.btn_rotate, self.btn_liveview, shot_sep,
             self.btn_single_shot])

        self.liveview = LiveViewPanel()
        card.add(self.liveview)
        card.body.setStretch(0, 1)
        # Floated over the image, not stacked under it — see set_overlay.
        self.liveview.set_overlay(self.focus_panel.track)
        # ONE row under the preview, holding every focus control. The ~50 px
        # the old second row and its hairline took come straight back here as
        # preview: `card.body` gives all its slack to index 0 (the live view)
        # and the viewer row is the only row that stretches, so removing fixed
        # height below the image is the same thing as adding it to the image.
        card.add(self.focus_panel.controls_widget)
        # The row's floor is its PREFERRED width, not the sum of its buttons'
        # 34 px minimums. Now that the card is a column and gets capped at the
        # frame's width, it actually reaches its floor — a portrait frame on
        # a 1080p screen lands there — and at the buttons' bare minimum "◄ 500"
        # renders as "◄ 50" with the rest cut off. A size policy rather than a
        # number: `Minimum` tells every layout above that this widget's
        # sizeHint IS its minimum, and the hint follows the labels, which is
        # what a number read here could not do — two of the jog buttons are
        # labelled after the window is built (`FocusPanel.apply_step_sizes`),
        # so a width captured now would be ~50 px short and still clip.
        controls = self.focus_panel.controls_widget
        controls.setSizePolicy(QSizePolicy.Policy.Minimum,
                               QSizePolicy.Policy.Fixed)

        self._lv_card = card
        # The width rule. The panel says when its height or aspect moved and
        # the card is re-capped to what the frame can actually fill.
        self._viewer_fit_queued = False
        self.liveview.fit_changed.connect(self._queue_viewer_fit)
        parent.addWidget(card)

    def _queue_viewer_fit(self) -> None:
        """
        Run `_fit_viewer_width` on the next turn of the event loop.

        Deferred because the cap clamps to the card's minimum, and that
        minimum is STALE at the moment the panel emits — the panel has just
        changed its own floor, and Qt only recomputes the card's once the
        layout request it queued has been handled (measured on Qt 6.9; see
        `ScalingHost.eventFilter`, which exists for the same reason one level
        up). Coalesced, so a window drag costs one fit per paint rather than
        one per resize event.
        """
        if not self._viewer_fit_queued:
            self._viewer_fit_queued = True
            QTimer.singleShot(0, self._fit_viewer_width)

    def _fit_viewer_width(self) -> None:
        """
        Cap the live-view card at the width its frame can fill.

        THE SCALING RULE IN CODE. The panel's height is fixed by the window
        (it is the whole of the viewer row); `LiveViewPanel.ideal_width` turns
        that into the width a frame of that height needs at its displayed
        aspect; the card's maximum is set there, plus the card's own margins,
        so the panel inside comes out exactly frame-shaped. Column 1 is the
        only stretched column, and Qt's response to a stretched column that
        has hit its maximum is to give the remaining surplus to the other
        columns equally — measured on Qt 6.9 with a three-column grid, 0:1:0,
        middle capped: the sides came out identical. That hand-off is what
        turns "bars beside the picture" into "wider side panels".

        NEVER BELOW THE CARD'S OWN MINIMUM. Qt drags a widget's minimum down
        to meet a smaller maximum, silently — so a cap below the minimum
        would not be ignored, it would let the card be squeezed under the
        preview's floor and the focus row's width. The clamp is the whole
        reason this is a method and not one `setMaximumWidth` call.

        Cheap and convergent. Only the panel's height and rotation feed the
        cap, and neither depends on the card's width, so re-capping cannot
        change the numbers it was computed from; a width-only relayout does
        not re-emit `fit_changed` in the first place (see its resizeEvent).
        """
        self._viewer_fit_queued = False
        card = self._lv_card
        margins = card.layout().contentsMargins()
        chrome = margins.left() + margins.right()
        wanted = self.liveview.ideal_width(self.liveview.height()) + chrome
        floor = card.minimumSizeHint().width()
        card.setMaximumWidth(max(wanted, floor))

    def _build_focus_controls(self):
        """Create the focus panel and wire it. Its widgets are mounted by
        `_build_liveview_card`; this only exists to keep that method readable."""
        self.focus_panel = FocusPanel(self)
        self.focus_panel.log_message.connect(self._append_log)
        self.focus_panel.changed.connect(
            lambda: self._setting_changed("Focus range", "next stack"))
        self.focus_panel.ensure_liveview = self._ensure_fresh_lv_for_focus
        # Calibration shuts live view itself, so it needs a way to say so —
        # otherwise the window still believes one is up, skips reopening, and
        # the next focus jog runs under a dead preview. NOT because the jog
        # would fail, which is what this used to say: measured on this rig's
        # Z 6_2, the focus motor does not care whether live view is up (see the
        # chk_close_lv note in `_build_options_card`). The lens would move
        # perfectly well with nothing on screen to judge it by, which for a
        # control whose whole purpose is setting A and B by eye is worse.
        self.focus_panel.liveview_invalidated = self._invalidate_liveview

    # ── 1. set-up: devices and destination, one panel ────────────────
    def _build_setup_card(self, parent):
        """
        Everything you do once, before a session: attach the two devices and
        say where the files land.

        One panel rather than two because they are one step in practice — you
        never connect without also checking the subject name — and because two
        cards meant two headers and two sets of padding for five fields.
        """
        card = Card("Set up", ACCENT_GREEN)
        card.setToolTip(
            'Connect both devices, then set where images are saved and the subject name.')

        conn = QHBoxLayout()
        conn.setSpacing(6)

        conn.addWidget(self._tag("CAMERA"))
        self.btn_connect_camera = QPushButton("Connect")
        self.btn_connect_camera.setToolTip(
            "Find the camera over USB and open the preview.\n\nIf it fails, the body is probably on Windows' MTP driver — it needs\nWinUSB (via Zadig).\n\nStays available while a run is PAUSED, once the run has come to rest:\nit re-opens the link the run is holding, without disturbing the\nfocus range or the preview. Comes alive a moment after Pause —\nthat moment is the run finishing the frame it was on.")
        self.btn_connect_camera.setStyleSheet(
            btn_style("#15803d", "#ffffff", rgba(ACCENT_GREEN, 0.5), "#166534"))
        self.btn_connect_camera.clicked.connect(self._connect_camera)
        conn.addWidget(self.btn_connect_camera)

        sep = QFrame()
        sep.setObjectName("hairline")
        sep.setFixedWidth(1)
        conn.addSpacing(6)
        conn.addWidget(sep)
        conn.addSpacing(6)

        conn.addWidget(self._tag("TABLE"))
        self.field_com = QLineEdit("COM4")
        self.field_com.setFixedWidth(72)
        conn.addWidget(self.field_com)
        self.field_baud = QComboBox()
        self.field_baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.field_baud.setCurrentText("115200")
        self.field_baud.setFixedWidth(84)
        conn.addWidget(self.field_baud)
        self.btn_connect_tt = QPushButton("Connect")
        self.btn_connect_tt.setToolTip(
            "Open the serial port and shake hands with the controller.\n\nStays available while a run is PAUSED, once the run has come to rest:\nit re-opens the port the run is holding. The port and baud fields stay\nlocked — a mid-run reconnect can only reopen the device the run\nstarted on, so moving to a different COM port needs Stop and Recover.")
        self.btn_connect_tt.setStyleSheet(
            btn_style("#15803d", "#ffffff", rgba(ACCENT_GREEN, 0.5), "#166534"))
        self.btn_connect_tt.clicked.connect(self._connect_tt)
        conn.addWidget(self.btn_connect_tt)
        conn.addStretch()
        # No live-view button here: it lives in the Live view card's own
        # header, where the thing it toggles is. Two buttons for one piece of
        # state is a sync bug waiting to happen.

        # TWO SHAPES, because this card lives in two places. Across the top
        # of the window (landscape) the devices sit LEFT of the destination
        # pair: the pair has to stay stacked (a subject name belongs directly
        # under the folder it names), so stacking the devices too would make
        # the panel three rows tall in a window with no spare height. In a
        # side column (portrait) the same row would squeeze the folder field
        # to a few characters, so there the devices go ABOVE the pair and
        # the fields get the column's whole width. `_arrange_setup` builds
        # whichever shape `_arrange` asks for; the pieces are wrapped in
        # widgets here so they can be re-placed without being rebuilt.
        dest = QVBoxLayout()
        dest.setSpacing(6)
        dest.addLayout(self._make_folder_row())
        dest.addLayout(self._make_subject_row())

        self._setup_conn = QWidget()
        conn.setContentsMargins(0, 0, 0, 0)
        self._setup_conn.setLayout(conn)
        self._setup_dest = QWidget()
        dest.setContentsMargins(0, 0, 0, 0)
        self._setup_dest.setLayout(dest)
        self._setup_sep = QFrame()
        self._setup_sep.setObjectName("hairline")
        self._setup_body = QWidget()
        card.add(self._setup_body)
        self._setup_shape = None
        self._arrange_setup("wide")
        parent.addWidget(card)

    def _arrange_setup(self, shape: str) -> None:
        """
        Lay the Set up card's two halves out `wide` (side by side) or
        `stacked` (devices over destination). See `_build_setup_card`.

        A widget cannot be handed a second layout while it still owns one,
        and a layout cannot be deleted from Python while a widget owns it —
        so the old layout is emptied, handed to a throwaway widget, and goes
        when that does. The halves are widgets and survive; only the box
        around them is replaced.
        """
        if shape == self._setup_shape:
            return
        self._setup_shape = shape
        body = self._setup_body
        old = body.layout()
        if old is not None:
            while old.count():
                old.takeAt(0)
            QWidget().setLayout(old)
        sep = self._setup_sep
        if shape == "wide":
            lay = QHBoxLayout(body)
            lay.setSpacing(14)
            sep.setMinimumSize(0, 0)
            sep.setMaximumSize(1, 16777215)
            sep.setFixedWidth(1)
            lay.addWidget(self._setup_conn)
            lay.addWidget(sep)
            lay.addWidget(self._setup_dest, 1)
        else:
            lay = QVBoxLayout(body)
            lay.setSpacing(8)
            sep.setMinimumSize(0, 0)
            sep.setMaximumSize(16777215, 1)
            sep.setFixedHeight(1)
            lay.addWidget(self._setup_conn)
            lay.addWidget(sep)
            lay.addWidget(self._setup_dest)
        lay.setContentsMargins(0, 0, 0, 0)

    # ── 5. transport ─────────────────────────────────────────────────
    def _build_transport_card(self, parent):
        """
        The last step, and the bottom of the column for that reason.

        The run-size line rides in the header rather than beside the settings
        that produce it: the moment it matters is the second before you
        commit, and it is the only warning that catches a photo count too high
        for the focus range.
        """
        card = Card("Run", ACCENT_GREEN)
        # Air between the card title and the transport row. Start is the
        # biggest, most consequential button in the window and it was sitting
        # hard under the header; the gap gives it a moment of its own.
        #
        # The stretch below, paired with the one after the estimate line,
        # centres the block should the card ever be taller than its contents.
        # It is content-height at the bottom of the right column now (the dial
        # above it takes the slack), so the pair mostly sits at zero — kept
        # because it costs nothing and the card would look wrong without it
        # the day something hands it spare height again.
        card.body.addSpacing(14)
        card.body.addStretch(1)

        run = QHBoxLayout()
        run.setSpacing(10)

        self.btn_start = QPushButton("▶   START")
        # HEIGHT COMES FROM THE SHEET BELOW, not from setMinimumHeight.
        # theme.py's global QPushButton rule carries `min-height: 20px`,
        # and a stylesheet min-height overrides the widget property — so
        # the setMinimumHeight(46) that used to be here never did
        # anything and these buttons were 36 px, whatever padding and
        # font-size happened to add up to. Same trap as SPIN_BTN_CSS.
        self.btn_start.setStyleSheet(f"""
            QPushButton {{ background-color: #16a34a; color: #ffffff;
                           min-height: {self.TRANSPORT_BTN_MIN_H}px;
                           border: 1px solid {rgba(ACCENT_GREEN, 0.55)};
                           font-size: 15px; letter-spacing: 1.5px;
                           border-radius: 9px; }}
            QPushButton:hover {{ background-color: #15803d; }}
            QPushButton:disabled {{ background-color: {DARK_BG};
                                    color: {TEXT_MUTED};
                                    border-color: {BORDER_SOFT}; }}
        """)
        self.btn_start.clicked.connect(self._start_session)
        run.addWidget(self.btn_start, 3)

        self.btn_pause = QPushButton("⏸   PAUSE")
        # Its own sheet purely to carry the height, because the global
        # QPushButton rule in theme.py sets min-height and a stylesheet
        # min-height beats setMinimumHeight — the other three transport
        # buttons get theirs from the sheets they already have.
        #
        # SELECTED BY ID, not by type. A bare `QPushButton { min-height }`
        # here ties with theme.py's `QPushButton` rule on specificity and
        # loses, which left this the only transport button still 36 px while
        # its neighbours went to 56. An id selector outranks a type selector,
        # so this one wins outright.
        self.btn_pause.setStyleSheet(
            f"QPushButton {{ min-height: {self.TRANSPORT_BTN_MIN_H}px; }}")
        self.btn_pause.clicked.connect(self._toggle_pause)
        run.addWidget(self.btn_pause, 2)

        self.btn_stop = QPushButton("■   STOP")
        self.btn_stop.setStyleSheet(f"""
            QPushButton {{ background-color: #7f1d1d; color: #fca5a5;
                           min-height: {self.TRANSPORT_BTN_MIN_H}px;
                           border: 1px solid {rgba(ACCENT_RED, 0.35)};
                           border-radius: 9px; }}
            QPushButton:hover {{ background-color: #991b1b; }}
            QPushButton:disabled {{ background-color: {DARK_BG};
                                    color: {TEXT_MUTED};
                                    border-color: {BORDER_SOFT}; }}
        """)
        self.btn_stop.clicked.connect(self._stop_session)
        run.addWidget(self.btn_stop, 2)

        # Recover: only appears after a run is interrupted (error, Stop, or a
        # device that never came back). Picks up from the last checkpoint —
        # reconnects, re-captures the current stack (marked _recaptured), then
        # continues. Hidden during normal operation to avoid clutter.
        self.btn_resume_run = QPushButton("⤾   RECOVER")
        self.btn_resume_run.setToolTip(
            'Pick up an interrupted run where it stopped: reconnect, return focus to\nA, re-shoot the stack it was on, then continue.\n\nRe-shot files are tagged _recaptured. Leave the table where it stopped —\nit is not re-homed.')
        self.btn_resume_run.setStyleSheet(f"""
            QPushButton {{ background-color: #0e7490; color: #cffafe;
                           min-height: {self.TRANSPORT_BTN_MIN_H}px;
                           border: 1px solid {rgba(ACCENT_CYAN, 0.55)};
                           font-size: 14px; letter-spacing: 1px;
                           border-radius: 9px; }}
            QPushButton:hover {{ background-color: #155e75; }}
            QPushButton:disabled {{ background-color: {DARK_BG};
                                    color: {TEXT_MUTED};
                                    border-color: {BORDER_SOFT}; }}
        """)
        self.btn_resume_run.clicked.connect(self._resume_run)
        self.btn_resume_run.setVisible(False)
        run.addWidget(self.btn_resume_run, 2)
        card.add(run)

        # Under the buttons, not in the header: this card sits in a narrow
        # column now, and the header clipped the line mid-sentence — which is
        # useless for the one message that warns you the photo count is too
        # high for the focus range.
        self.lbl_estimate = QLabel()
        self.lbl_estimate.setWordWrap(True)
        # Two lines' worth, always. The text changes length as you set A and
        # B — and grows again for the too-many-photos warning — so a label
        # free to re-wrap would resize this card and nudge everything below it
        # at the exact moment you are clicking things.
        #
        # MEASURED, not guessed. This was 32 px, which was never two lines of
        # anything: at the default window the label is ~500 px wide, where
        # even the ordinary message wraps and the old 11 px sheet already
        # wanted 45. The second line was being clipped, and only a window
        # wide enough to keep the text on one line hid it. 51 px is what
        # `heightForWidth` asks for at this sheet with the longest warning
        # string; re-measure it if the font or the padding below changes.
        self.lbl_estimate.setFixedHeight(51)
        # CENTRED in its reserved box. The box has to stay tall enough for
        # the two-line warning, so with the usual one-line message something
        # has to absorb ~20 px: at the top it stranded the text against the
        # buttons, at the bottom it opened a hole in the middle of the panel.
        # Split between the two and neither gap reads as a mistake.
        self.lbl_estimate.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        # The units paragraph earns its place: the gap and the jog buttons
        # sit inches apart on screen, both show bare numbers, and they count
        # in different currencies — which read as the app planning a move
        # smaller than its smallest button. The label has no room to explain
        # that (481 px at the layout's floor), so it says it here.
        self.lbl_estimate.setToolTip(
            "The size of the run. Warns if the photo count is too high for\n"
            "the A→B range.\n\n"
            "The gap is given twice because the focus controls use two units.\n"
            "A, B and the range are counted in STEPS; the jog buttons are\n"
            "labelled in DRIVER UNITS, ten to the step — so the button marked\n"
            "100 moves ten steps, and the one marked 10 moves one.\n\n"
            "It is an average, and the only fraction in the focus UI. Nothing\n"
            "moves a part-step: the range is split into whole steps with the\n"
            "remainder spread across the gaps, so 50 steps over 4 photos goes\n"
            "out as 17, 17, 16.")
        self.lbl_estimate.setStyleSheet(self.estimate_style(TEXT_SECONDARY))
        card.add(self.lbl_estimate)
        # The other half of the pair opened before the transport row. Equal
        # weights, so whatever height this column has spare lands half above
        # the buttons and half below them and the block sits centred.
        card.body.addStretch(1)
        parent.addWidget(card)

        self.spin_photos.valueChanged.connect(
            lambda _v: self._setting_changed("Photos per stack", "next stack"))

    @staticmethod
    def estimate_style(colour: str) -> str:
        """
        The run-size line's look, in ONE place.

        `_update_estimate` re-applies a sheet to this label on every change,
        because the too-many-photos warning turns it red. A second copy of
        these rules over there would therefore WIN at the first keystroke and
        quietly undo whatever was set at build time — the two would drift and
        the symptom would be a label that looks right until you touch a spin
        box. Both call this instead, and only the colour differs.
        """
        return (f"color: {colour}; font-size: 13px; font-weight: 600;"
                f"font-family: 'Cascadia Mono', Consolas, monospace;"
                f"padding-top: 7px;")

    def _tag(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 9px; font-weight: 700;"
            f"letter-spacing: 1.2px;")
        return lbl

    # ── rotation card ────────────────────────────────────────────────
    def _build_rotation_card(self, parent):
        card = Card("Capture", ACCENT_PURPLE)
        card.setToolTip(
            'How many frames per focus stack, and how many positions the table stops at per revolution.')

        # Three rows, three columns, and the ROWS MEAN SOMETHING.
        #
        # Row 0 is the three independent numbers — how many frames, how many
        # revolutions, which way round. Row 1 is Positions and Degrees, which
        # are not independent at all: they are two views of one quantity and
        # typing in either flips the other to `auto`. Sitting alone together
        # on their own row is what makes that visible; scattered across row 0
        # beside Photos it was a fact you could only find in a tooltip.
        # Row 2 is what the five come out as.
        #
        # Photos-per-stack used to be a 27 px hero box of its own — it is the
        # headline number, but it was costing ~70 px of a panel that now
        # shares a row with two others.
        # Air under the card title before the first row of fields. Without it
        # the Photos box sits hard against the header and the whole card reads
        # as cramped — the same complaint the Run card's opening gap answers,
        # and the same fix.
        card.body.addSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        # 16, not 10. Two rows of boxes plus the summary line under them had
        # less space between rows than the boxes are tall, which is what made
        # six controls read as one dense block instead of two.
        grid.setVerticalSpacing(16)
        for c in (1, 3, 5):
            grid.setColumnStretch(c, 1)

        def cell(row, col, text, widget, tip=""):
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)
            # A gutter before every pair but the first. Horizontal spacing is
            # one number for the whole grid, so at 8 px the gap between one
            # pair's field and the next pair's label was the same as the gap
            # inside a pair — six controls in a row with nothing saying which
            # label owned which box. The margin is what groups them.
            if col:
                lbl.setContentsMargins(18, 0, 0, 0)
            if tip:
                lbl.setToolTip(tip)
            grid.addWidget(lbl, row, col)
            grid.addWidget(widget, row, col + 1)

        self.spin_photos = QSpinBox()
        self.spin_photos.setRange(1, 9999)
        self.spin_photos.setValue(10)
        self.spin_photos.setToolTip(
            'Frames per focus stack, spread evenly from A to B. Takes effect on the\nnext stack.')
        # Still the headline number, just carried by colour and weight now
        # rather than by 27 px of type.
        self.spin_photos.setStyleSheet(
            f"QSpinBox {{ color: {ACCENT_AMBER}; font-weight: 700; }}")
        cell(0, 0, "Photos:", self.spin_photos)

        # Positions and Degrees are two views of the SAME quantity, so each
        # can be left on "auto" and derived from the other — whichever one you
        # type into becomes the master and the other shows "auto". They get
        # row 1 to themselves so that reads off the layout.
        self.field_steps = QSpinBox()
        self.field_steps.setRange(0, 3600)
        self.field_steps.setSpecialValueText("auto")
        self.field_steps.setValue(36)
        self.field_steps.setToolTip(
            "Positions the table stops at per revolution. 36 = every 10°. One extra\nstack closes the loop.\n\nSet 'auto' to derive this from Degrees instead.")
        self.field_steps.valueChanged.connect(
            lambda _v: self._on_rotation_changed("steps"))
        cell(1, 0, "Positions:", self.field_steps)

        self.field_deg = QDoubleSpinBox()
        # Above 180° a "revolution" would be fewer than two positions, which
        # is not a turntable capture — cap it there.
        self.field_deg.setRange(0, 180)
        self.field_deg.setDecimals(2)
        self.field_deg.setSpecialValueText("auto")
        self.field_deg.setValue(0)
        self.field_deg.setToolTip(
            "Degrees between positions. Set a value and Positions switches to 'auto'.\n\nLeave on 'auto' to derive this from Positions instead.")
        self.field_deg.valueChanged.connect(
            lambda _v: self._on_rotation_changed("deg"))
        cell(1, 2, "Degrees:", self.field_deg)

        self.field_rounds = QSpinBox()
        self.field_rounds.setRange(-1, 9999)
        self.field_rounds.setValue(1)
        self.field_rounds.setSpecialValueText("infinite")
        self.field_rounds.setToolTip(
            "Full revolutions to shoot. 'infinite' keeps going until you stop.\nChangeable mid-run.")
        self.field_rounds.valueChanged.connect(
            lambda _v: self._setting_changed("Revolutions", "now"))
        cell(0, 2, "Revolutions:", self.field_rounds)

        # A clickable circle rather than a two-item dropdown — see
        # DirectionToggle for why, and note it still answers currentIndex()
        # with 0 = CW / 1 = CCW, so main_window's settings read is unchanged.
        #
        # Placed by hand instead of through `cell`, because it is the one
        # control here that spans both rows: `cell` puts a label and a widget
        # side by side on a single row, which is exactly what left a hole
        # under the old combo. The label keeps `cell`'s right-alignment and
        # its 18 px gutter so it still lines up with Photos and Revolutions
        # above it, and both label and toggle are pinned to the TOP of the
        # two-row span so the label sits level with the Direction row rather
        # than floating halfway down beside the circle.
        self.field_direction = DirectionToggle()
        self.field_direction.changed.connect(
            lambda _i: self._setting_changed("Direction", "next revolution"))
        dir_tip = ('Which way the table turns. Click to reverse it.\n'
                   'Takes effect on the next revolution.')
        self.field_direction.setToolTip(dir_tip)

        # Caption ABOVE the graphic, not beside it like the spin-box rows.
        # Every other control here is a labelled box on one row; this one is a
        # square that owns two, so a right-aligned label on its left would sit
        # against the circle's edge with the whole second row empty beneath.
        # Stacked, the caption reads as the group's heading and the pair
        # centres in the column as one block.
        dir_box = QVBoxLayout()
        dir_box.setSpacing(6)
        dir_box.setContentsMargins(18, 0, 0, 0)
        dir_lbl = QLabel("Direction:")
        dir_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dir_lbl.setToolTip(dir_tip)
        dir_box.addWidget(dir_lbl)
        dir_box.addWidget(self.field_direction,
                          0, Qt.AlignmentFlag.AlignHCenter)
        grid.addLayout(dir_box, 0, 4, 2, 2)

        # Row 2, spanning: what the five numbers above actually come out as.
        #
        # This is the card's answer, not a footnote to it — "46 stacks" is the
        # number you check before pressing Start, and it was set two sizes
        # below the fields that produce it and jammed against the bottom of
        # them. Bigger type, and padding of its own so the grid's row spacing
        # is not the only thing holding it off the Revolutions/Direction row.
        self.lbl_rotation_info = QLabel()
        self.lbl_rotation_info.setStyleSheet(
            f"color: {ACCENT_PURPLE}; font-size: 13px; font-weight: 700;"
            f"font-family: 'Cascadia Mono', Consolas, monospace;"
            f"padding-top: 14px;")
        self.lbl_rotation_info.setToolTip(
            'What the fields above work out to. One extra stack per revolution closes the loop, so this is positions + 1.')
        grid.addWidget(self.lbl_rotation_info, 2, 0, 1, 6)

        card.add(grid)
        parent.addWidget(card)
        self._on_rotation_changed()

    # ── timing card ──────────────────────────────────────────────────
    def _build_timing_card(self, parent):
        card = Card("Timing", ACCENT_AMBER)
        # Lives in the panel tooltip rather than a visible hint line: it
        # applies to every field here, and the left column has no ~20px of
        # height to spare (see the module docstring).
        card.setToolTip("Every value here takes effect immediately, "
                        "even mid-run.")

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        def cell(row, col, text, widget, span=1):
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(lbl, row, col)
            grid.addWidget(widget, row, col + 1, 1, span)

        self.field_capture_wait = QDoubleSpinBox()
        self.field_capture_wait.setRange(0.05, 60)
        self.field_capture_wait.setSingleStep(0.1)
        self.field_capture_wait.setValue(2.0)
        self.field_capture_wait.setSuffix(" s")
        self.field_capture_wait.setToolTip(
            'Delay between shots.\n\nWith file-arrival waiting on, only the fallback for when no file appears.\nUnused with card-only saving.')
        self.field_capture_wait.valueChanged.connect(
            lambda _v: self._setting_changed("Delay between shots", "now"))
        cell(0, 0, "Shot delay:", self.field_capture_wait)

        self.field_retries = QSpinBox()
        self.field_retries.setRange(0, 20)
        self.field_retries.setValue(6)
        self.field_retries.setToolTip(
            "Silent retries for a command that didn't answer. Camera: reconnects\nUSB. Turntable: reopens the port.\n\nIf they run out the run pauses with the alarm and waits — it doesn't\ndie. 0 = escalate immediately.")
        self.field_retries.valueChanged.connect(
            lambda _v: self._setting_changed("Auto-retry count", "now"))
        cell(0, 2, "Auto-retry:", self.field_retries)

        self.field_settle = QDoubleSpinBox()
        self.field_settle.setRange(0, 30)
        self.field_settle.setDecimals(2)
        self.field_settle.setValue(0.20)
        self.field_settle.setSuffix(" s")
        self.field_settle.setToolTip('Pause after the table stops moving, to let vibration die down.')
        self.field_settle.valueChanged.connect(
            lambda _v: self._setting_changed("Settle after rotate", "now"))
        cell(1, 0, "Table settle:", self.field_settle)

        self.field_rotate_timeout = QDoubleSpinBox()
        self.field_rotate_timeout.setRange(1, 600)
        self.field_rotate_timeout.setValue(60)
        self.field_rotate_timeout.setSuffix(" s")
        self.field_rotate_timeout.setToolTip(
            'How long to wait for the table to report it finished moving.')
        self.field_rotate_timeout.valueChanged.connect(
            lambda _v: self._setting_changed("Rotate timeout", "now"))
        cell(1, 2, "Turn timeout:", self.field_rotate_timeout)

        self.combo_reset = QComboBox()
        # Serpentine first, so it is what a new user gets. It shoots the next
        # stack back the other way instead of running the lens home, which
        # halves the focus travel over a session.
        # Short labels: this sits in the narrowest column, and a combo clips
        # its text rather than growing. The tooltip carries the detail.
        self.combo_reset.addItem("Serpentine — back the other way",
                                 "serpentine")
        self.combo_reset.addItem("Direct — one move back", "direct")
        self.combo_reset.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo_reset.setMinimumContentsLength(10)
        self.combo_reset.setToolTip(
            'How the lens gets back to A between stacks.\n\nSerpentine: shoots the next stack backwards, then renumbers near→far.\n   Half the focus travel.\nDirect: one move back to A, anchored on the near stop when calibrated.')
        self.combo_reset.currentIndexChanged.connect(self._on_reset_mode_changed)
        cell(2, 0, "Return to A:", self.combo_reset, span=3)

        card.add(grid)
        parent.addWidget(card)

    # ── options card ─────────────────────────────────────────────────
    def _build_options_card(self, parent):
        card = Card("Options", ACCENT_CYAN)

        self.chk_wait_file = QCheckBox("Continue as soon as the file lands")
        self.chk_wait_file.setChecked(True)
        self.chk_wait_file.setToolTip(
            'Move on the moment the image is written instead of always waiting the\nfull shot delay. Usually the biggest time saving in a run.\n\nIgnored with card-only saving.')
        self.chk_wait_file.toggled.connect(
            lambda _v: self._setting_changed("File-arrival waiting", "now"))
        card.add(self.chk_wait_file)

        self.chk_hold = QCheckBox("Hold between revolutions (to adjust the scene)")
        self.chk_hold.setChecked(True)
        self.chk_hold.setToolTip(
            'Pause after each revolution so you can change lighting or reposition.\nPress Resume to continue.')
        self.chk_hold.toggled.connect(
            lambda _v: self._setting_changed("Hold between revolutions", "now"))
        card.add(self.chk_hold)

        # STILL NOT "(faster)", which this said until the audit, and which must
        # not come back however good the option gets. What puts preview traffic
        # on the wire is the poll thread in ui/liveview.py, and
        # main_window._spawn_worker calls _pause_liveview UNCONDITIONALLY when
        # a run starts, ticked or not. LiveViewFeed.run returns early while
        # paused, so an open session nobody polls costs nothing. The link
        # protection comes from the pause; this box never contributed to it and
        # does not now. What it buys is the sensor and the whole live view
        # pipeline staying off the body for the length of the run instead of
        # the first few seconds — heat and power, not speed.
        #
        # THIS COMMENT HAS BEEN WRONG TWICE, IN OPPOSITE DIRECTIONS, and both
        # versions are recorded rather than deleted, because the code each one
        # points at is still here and the next person will find it.
        #
        # WRONG ONCE: it said the hide CANNOT be made to hold, on the grounds
        # that the body refuses MfDrive without a live view session —
        # NIKON_NotLiveView, 0xA00B, the code ptp.NikonCamera.drive raises on.
        # That was written down confidently and never tested. It was tested on
        # 2026-08-03, on this rig's Z 6_2, with no shutter actuation: with live
        # view stopped, drive(+500) was ACCEPTED and answered 0x2001 (OK), and
        # the lens really moved — drive_to_stop(NEAR) had to travel 400 units
        # to bring it back. The same sequence with live view UP gave the same
        # 400. (400 rather than 500 is drive_to_stop's own chunked undercount,
        # STOP_SWEEP_CHUNK being 200; the point is that both trials came out
        # identical.) On this body live view makes no difference to the focus
        # motor whatsoever.
        #
        # WRONG AGAIN, the other way: the correction then said the hide could
        # hold but was not implemented, and fenced off capture as unmeasured.
        # Capture was measured on 2026-08-03 too — same body, three shutter
        # actuations. Two consecutive frames with live view DOWN both landed as
        # real NEFs, 31.6 MB, ~1.1 s; a control frame with live view UP came
        # back 31.7 MB in 1.1 s. Two in a row deliberately, because the
        # InvalidObjectHandle failure this repo chased surfaces on the frame
        # AFTER the bad one. LiveViewProhibitCondition (0xD1A4), read after
        # every frame, was 0x00000000 throughout — bit 12, "pending unretrieved
        # SDRAM image", never set, so no buffer was left stuck. Capture is
        # indifferent to live view as well: nothing a run does needs it.
        #
        # Both measurements are about the Z 6_2 and no other body; that fence
        # stays. So does the 0xA00B branch in ptp.NikonCamera.drive, and
        # RC_NIKON_NOT_LIVEVIEW with it — never observed on this body, correct
        # defensive code, and for a body that really does refuse focus without
        # live view, aborting loudly is still the right shape.
        #
        # SO IT IS IMPLEMENTED, and this box now does what its label says.
        # PTPCameraClient.hold_liveview_closed(True) sets a flag read by
        # PTPCameraClient._ensure_for_focus, which the three focus entry points
        # — drive_units, drive_to_stop, measure_travel — go through, so they
        # stop re-opening live view. BridgeWorker.run takes the hold right
        # after its hide_liveview() and releases it in the single try/finally
        # around the whole run loop, so Stop, an exception and a clean finish
        # all give it back; a leaked hold would reach the user as "the preview
        # is broken now". set_zoom is deliberately NOT gated: the magnifier is
        # a live view property (LIVEVIEW_ZOOM, 0xD1BD) and genuinely needs the
        # stream.
        #
        # DELETING THIS ROW WOULD NOT RETIRE THE FEATURE, which is what the
        # previous version of this comment claimed when it said nothing else
        # reads close_lv_run. Something does: BridgeWorker.run reads it as
        # live.get_bool("close_lv_run", True), and that default is TRUE. Drop
        # the checkbox on its own and the hide is pinned permanently on — the
        # opposite of removing it. Retire the reader first, or not at all.
        self.chk_close_lv = QCheckBox("Keep live view closed for the whole run")
        self.chk_close_lv.setChecked(True)
        self.chk_close_lv.setToolTip(
            "Shuts live view on the camera as the run begins and holds it shut\n"
            "until the run ends, however it ends. Focus moves used to re-open it\n"
            "on the first gap move; they no longer do.\n"
            "\n"
            "This is not a speed setting — the on-screen preview stops polling\n"
            "during a run either way. What it saves is the sensor and the live\n"
            "view pipeline running for the hours a run takes: heat and battery.")
        # "next run", NOT "now". This scope is user-facing text, not a hint:
        # `_setting_changed` logs "applies {scope}" during a run. It said "now"
        # while `BridgeWorker.run` reads close_lv_run ONCE at the top of the
        # run and never looks again, so toggling it mid-run does nothing until
        # the next one — the same scope `chk_keep_on_cam` already reports, for
        # the same reason. The colour follows the scope (green for "now", cyan
        # otherwise), so this also stops a change that will not take effect
        # being logged in the colour that says it has.
        self.chk_close_lv.toggled.connect(
            lambda _v: self._setting_changed("Close LiveView for run",
                                             "next run"))
        card.add(self.chk_close_lv)

        # One line, not two: this list sets the height of its whole grid row.
        self.chk_keep_on_cam = QCheckBox(
            "Keep photos on the card (faster but no renaming or foldering)")
        self.chk_keep_on_cam.setChecked(False)
        self.chk_keep_on_cam.setToolTip(
            "Save to the camera's card and skip the USB download — much faster.\n\nNothing is written to the PC: no folders, camera's own filenames\n(DSC_0001…) in shooting order. Offload the card yourself.")
        self.chk_keep_on_cam.toggled.connect(
            lambda _v: self._setting_changed("Keep photos on camera", "next run"))
        card.add(self.chk_keep_on_cam)

        self.chk_beep = QCheckBox("Play sounds (chimes and the device-lost alarm)")
        self.chk_beep.setChecked(True)
        self.chk_beep.setToolTip(
            'All sound in one switch: revolution-finished chime, session-done chime,\nand the repeating alarm when a device drops out mid-run.\n\nOff also silences the drop-out alarm, so an unattended run will wait\nsilently.')
        self.chk_beep.toggled.connect(
            lambda _v: self._setting_changed("Beep", "now"))
        card.add(self.chk_beep)

        # The camera trace lives in the Live view header instead of here —
        # it is a diagnostic you reach for when the lens is not moving, next
        # to the other lens controls, not a preference you set up front.
        parent.addWidget(card)

    def _make_folder_row(self):
        """The Save-to folder picker, now shown with the subject name."""
        folder_row = QHBoxLayout()
        folder_row.setSpacing(5)
        lbl_save = QLabel("Save to:")
        lbl_save.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        folder_row.addWidget(lbl_save)
        self.field_base = QLineEdit(
            str(Path.home() / "Pictures" / "TurntableStacks"))
        self.field_base.setToolTip(
            "Base folder. Each subject gets subject/subject_rev-N/…_pos-NNN\n"
            "sub-folders under here.\n\n"
            "Unused when 'Keep photos on the camera card' is on.")
        self.field_base.editingFinished.connect(
            lambda: self._setting_changed("Base folder", "next stack"))
        folder_row.addWidget(self.field_base, 2)
        browse = QPushButton("…")
        browse.setFixedWidth(34)
        browse.setToolTip("Choose the base folder")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse)
        return folder_row

    def _make_subject_row(self):
        """The subject name, directly under the folder it names."""
        row = QHBoxLayout()
        row.setSpacing(5)
        lbl = QLabel("Subject:")
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl)
        self.field_stack_name = QLineEdit("placeholder_name")
        self.field_stack_name.setPlaceholderText("e.g. dragon_miniature")
        self.field_stack_name.setToolTip(
            "Names every folder and file. 'unicorn' gives:\n   unicorn/unicorn_rev-1/unicorn_rev-1_pos-001/\n      unicorn_rev-1_pos-001_shot-0001.NEF\n\nEvery frame carries its own revolution, table position and shot\nnumber, so it still says where it came from once it has been moved\nout of its folder — the file is its folder's name plus the shot.\n\nLetters, numbers, - and _ only. Unused with card-only saving.")
        self.field_stack_name.textChanged.connect(self._on_stack_name_changed)
        row.addWidget(self.field_stack_name, 1)
        return row

    def _build_progress_card(self, parent):
        card = Card("Progress", ACCENT_BLUE)
        grid = QGridLayout()
        # GENEROUS BARS, kept from when this card shared a row with Options
        # and Timing and had ~40 px of dead space to fill. It is content-height
        # in the left column now, so 26 px bars with 14 px between them are a
        # choice about legibility rather than about filling a box; the log
        # under it absorbs whatever they do not use. If a fifth bar is ever
        # added, take these back down.
        grid.setSpacing(14)
        grid.setColumnStretch(1, 1)

        # No tooltips here: each bar's own label already says what it counts.
        def add(row, text, color, overlay=False):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
            grid.addWidget(lbl, row, 0)
            bar = QProgressBar()
            bar.setMinimumHeight(28 if overlay else 26)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ color: {TEXT_PRIMARY}; font-size: 11px;"
                f" font-weight: 700; }}"
                f"QProgressBar::chunk {{ background-color: {color}; }}")
            grid.addWidget(bar, row, 1)
            if overlay:
                # The ETA text sits half on the green chunk and half on the
                # dark track, so it loses contrast somewhere no matter what
                # colour it is. A drop shadow keeps it readable on both.
                ov = QLabel("", bar)
                ov.setAlignment(Qt.AlignmentFlag.AlignCenter)
                ov.setStyleSheet(
                    "color: #ffffff; font-size: 11px; font-weight: 800;"
                    "background: transparent;")
                sh = QGraphicsDropShadowEffect(ov)
                sh.setBlurRadius(4)
                sh.setOffset(0, 1)
                sh.setColor(QColor(0, 0, 0, 220))
                ov.setGraphicsEffect(sh)
                bar._overlay = ov
                # Keep the overlay stretched over the whole bar as it resizes.
                def _sync(ev, b=bar, o=ov):
                    o.setGeometry(0, 0, b.width(), b.height())
                    return QProgressBar.resizeEvent(b, ev)
                bar.resizeEvent = _sync
            value = QLabel("—")
            # Every pixel this column does not need is a pixel the bar gets,
            # and the bar is the thing being read from across the room. 66 is
            # what "370 / 370" measures at this sheet — the widest reading a
            # 37-stack x 10-photo run produces — with a little slack for a
            # four-digit total. It is a minimum, not a fix: a longer run just
            # takes the space back off the bar.
            value.setMinimumWidth(66)
            value.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
            value.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: 700;"
                f"font-family: 'Cascadia Mono', Consolas, monospace;")
            grid.addWidget(value, row, 2)
            bar._value_label = value
            return bar

        self.pb_stack = add(0, "Photo in stack", ACCENT_AMBER)
        self.pb_round = add(1, "Stacks this revolution", ACCENT_BLUE)
        self.pb_revolution = add(2, "Revolutions", ACCENT_PURPLE)
        self.pb_total = add(3, "Total", ACCENT_GREEN, overlay=True)
        card.add(grid)
        parent.addWidget(card)

    @staticmethod
    def _set_bar(bar: QProgressBar, value: int, maximum: Optional[int] = None):
        if maximum is not None:
            bar.setRange(0, max(maximum, 1))
        bar.setValue(value)
        bar._value_label.setText(f"{value} / {bar.maximum()}")

    def _set_total_text(self, text: str):
        ov = getattr(self.pb_total, "_overlay", None)
        if ov is not None:
            ov.setText(text)

    # ── monitor column ───────────────────────────────────────────────
    def _build_log_card(self, parent):
        """
        Top of the monitor column: the running account of what happened.

        First thing in the window's reading order because it is the first
        thing you look at when something has gone wrong, and the only place
        the run explains itself.
        """
        card = Card("Event log", TEXT_MUTED, expand=True)
        clear = QPushButton("Clear")
        clear.setFixedHeight(22)
        clear.setToolTip("Clear the log.")
        clear.setStyleSheet(
            f"QPushButton {{ font-size: 10px; padding: 2px 9px;"
            f" color: {TEXT_MUTED}; background: transparent;"
            f" border: 1px solid {BORDER}; border-radius: 5px; }}"
            f"QPushButton:hover {{ color: {TEXT_PRIMARY}; }}")
        clear.clicked.connect(lambda: self.log_view.clear())
        card.add_header_widget(clear)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(4000)
        # Low floor on purpose. It is the bottom of the left column and takes
        # whatever height the three content-height cards above it leave, so
        # on any real monitor it gets far more than this without asking —
        # and every pixel it DEMANDS is a pixel added to the layout's floor,
        # which is what decides when the whole window has to scale.
        self.log_view.setMinimumHeight(150)
        card.add(self.log_view)
        card.body.setStretch(0, 1)
        parent.addWidget(card, 1)

    def _build_dial_card(self, parent):
        """
        Where the table is, at a glance — and it is the card that grows in
        the right column, so on a big monitor "at a glance" can mean from
        across the room.

        Sized to be GLANCED at, not stared into. It answers one question
        ("where is the table?") and the angle in its header answers it
        precisely, so what it needs is to be unambiguous from a distance
        rather than detailed up close. The floor below is what buys that:
        TurntableWidget scales everything it draws off `min(w, h)` and is
        Expanding on both axes, so it fills whatever box this card gives it
        and the floor is the only number that decides how small it may get.
        The tick marks are the reason to keep it generous — at 150 px a
        36-position rim renders as a grey smudge and the one highlighted
        tick stops being findable, which is the entire job.
        """
        card = Card("Turntable position", ACCENT_AMBER, expand=True)
        # Angle readout in the header, aligned with the card title.
        self.lbl_angle = QLabel("0.0°")
        self.lbl_angle.setStyleSheet(
            f"color: {ACCENT_AMBER}; font-size: 14px; font-weight: 700;"
            f"font-family: 'Cascadia Mono', Consolas, monospace;")
        card.add_header_widget(self.lbl_angle)
        self.turntable_widget = TurntableWidget()
        self.turntable_widget.setMinimumSize(260, 260)
        self.turntable_widget.angle_changed.connect(
            lambda a: self.lbl_angle.setText(f"{a:.1f}°"))
        card.add(self.turntable_widget)
        card.body.setStretch(0, 1)

        # ── free spin ─────────────────────────────────────────────────
        # Under the dial, in the table's own panel: this turns the SUBJECT,
        # not the lens, and the dial directly above is the feedback for it.
        #
        #        CCW                        CW
        #   ◄  ◄◄  ◄◄◄  ◄◄◄◄        ►►►►  ►►►  ►►  ►
        #
        # FOUR SPEEDS PER SIDE, PICKED DIRECTLY. This replaced a press-count
        # ramp that always started at full speed and had to be tapped down —
        # exactly wrong for the job, which is nudging an asymmetric subject a
        # few degrees to check a focus plane. Now the slow speeds are one
        # click away instead of four.
        #
        # Arrow count is the speed and the glyphs are the focus row's, so the
        # two rows read the same way: more arrows, more movement. Fastest
        # innermost, so the pair for any given speed sits symmetrically about
        # the centre of the row.
        #
        # THESE ARE TOGGLES, and there is no Stop button — pressing the lit
        # button stops the table. A separate Stop was the obvious design and it
        # was the wrong one: it made the row nine buttons wide on a panel that
        # is only as wide as the dial above it, so every button had to be
        # narrow enough to be a poor target for what is a nudge-and-watch
        # control. Stopping is not a distinct intent from "I no longer want
        # this speed", so it did not need its own key. `_spin` in
        # main_window.py does the toggle and `_refresh_spin` lights the
        # running one; the lit button is both the readout and the off switch.
        SPEEDS = 4

        def spin_btn(text: str, accent: str) -> QPushButton:
            b = QPushButton(text)
            # Sized for a pointer, not for density. The row lost the Stop
            # button and its spacer, and the eight that remain took the space.
            # Height is NOT set here — see SPIN_BTN_CSS, whose min-height would
            # override anything set on the widget. Width is safe to set this
            # way because the sheet says nothing about width.
            #
            # 44 is the floor that keeps "►►►►" off the borders at the sheet's
            # 12 px: the label wants 48 px of content, and eight buttons at 44
            # are what sets this panel's minimum width, so raising it further
            # widens the whole window rather than the buttons.
            b.setMinimumWidth(44)
            b.setStyleSheet(self.SPIN_BTN_CSS.format(
                accent=accent, bg=PANEL_BG_SOFT,
                border=rgba(accent, 0.28), hover=rgba(accent, 0.13),
                hover_border=rgba(accent, 0.55),
                muted=TEXT_MUTED, soft=BORDER_SOFT, dark=DARK_BG))
            return b

        # Indexed by SPEED, not by position: index 0 is one arrow (slowest)
        # in both lists, so `_refresh_spin` can light `list[level]` without
        # caring which way round the row is drawn.
        self.btns_spin_ccw = [spin_btn("◄" * (n + 1), ACCENT_AMBER)
                              for n in range(SPEEDS)]
        self.btns_spin_cw = [spin_btn("►" * (n + 1), ACCENT_AMBER)
                             for n in range(SPEEDS)]

        for level in range(SPEEDS):
            for direction, btns in ((1, self.btns_spin_ccw),
                                    (0, self.btns_spin_cw)):
                b = btns[level]
                # Defaults bind the loop variables, and the leading `_` eats
                # the bool Qt passes with `clicked`.
                b.clicked.connect(
                    lambda _=False, d=direction, lv=level: self._spin(d, lv))

        #: Gap between the CCW group and the CW group. Without it the eight
        #: buttons read as one undifferentiated row now that nothing sits in
        #: the middle to divide them, and the two CCW/CW captions above have
        #: nothing to centre on.
        SPIN_GROUP_GAP = 14

        heads = QHBoxLayout()
        heads.setSpacing(3)
        for text, target in (("CCW", "l"), ("CW", "r")):
            lab = QLabel(text)
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 9px; font-weight: 700;"
                f"letter-spacing: 1.2px;")
            if target == "r":
                heads.addSpacing(SPIN_GROUP_GAP)
            heads.addWidget(lab, SPEEDS)

        spin = QHBoxLayout()
        spin.setSpacing(3)
        for b in self.btns_spin_ccw:                    # ◄ ◄◄ ◄◄◄ ◄◄◄◄
            spin.addWidget(b, 1)
        spin.addSpacing(SPIN_GROUP_GAP)
        for b in reversed(self.btns_spin_cw):           # ►►►► ►►► ►► ►
            spin.addWidget(b, 1)

        # Padded on both sides so the block sits in the middle of the space
        # below the dial rather than against the bottom edge.
        card.body.addSpacing(6)
        card.add(heads)
        card.add(spin)
        card.body.addSpacing(9)
        # Stretch 1: this is the card that grows in the right column. Without
        # it a QVBoxLayout shares the slack between all three cards equally,
        # which put a hundred pixels of nothing under Capture's fields and
        # under Start — and the dial, the one thing that gets better bigger,
        # got a third of what it could have had. `_Slot` ignores the factor,
        # so this is harmless if the card is ever handed a grid cell again.
        parent.addWidget(card, 1)

    def _install_shortcuts(self):
        for keys, slot in (("Ctrl+Return", self._start_session),
                           ("Ctrl+Space", self._toggle_pause),
                           ("Ctrl+.", self._stop_session)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(slot)

    # ══════════════════════════════════════════════════════════════════
    # Live settings plumbing
    # ══════════════════════════════════════════════════════════════════
