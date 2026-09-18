"""
Focus A/B setup panel — jog controls, Set A / Set B, and the travel track.

Focus is RELATIVE: Set A defines zero and B is a signed offset from it.
Nothing here can ask the lens where it is, so the panel keeps its own
count and every jog is bookkept against that.

Steps run on a worker thread so a slow focus command cannot freeze the
window.
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..hardware.camera import PTPCameraClient
from ..assets import WRITABLE_DIR
from ..hardware import lensdata
from ..hardware.focus import STEP_UNITS, anchored_drive, drive_focus
from ..hardware.focus_cal import (
    USE_STOP_BASED, CalibrationError, measure_travel, measure_travel_by_stops,
)
from .focus_track import FocusTrackWidget
from .theme import (
    ACCENT_AMBER, ACCENT_BLUE, ACCENT_CYAN, ACCENT_GREEN, ACCENT_RED, BORDER,
    BORDER_SOFT, DARK_BG, FIELD_BG, PANEL_BG_SOFT, TEXT_MUTED,
    btn_style, rgba,
)


class FocusPanel(QWidget):
    """
    Focus A/B setup.

    Position is tracked in "steps" — the APP's quantum, 10 driver units, not
    the smallest move the lens can make: `ptp.drive` takes any integer number
    of units and `ptp.measure_travel` creeps its last stretch in ones.
    Medium and Large are FIXED CONSTANTS, not measurements: `step_ratios()`
    turns `hardware.focus.STEP_UNITS` ({"Small": 10, "Medium": 100,
    "Large": 500} driver units) into the multiples 10x and 50x, and
    MainWindow._load_step_sizes hands them here through apply_step_sizes().
    Calibration measures the lens TRAVEL and nothing else — it never writes
    a ratio.
    """

    #: Height of the one control row, and of every button in it. See the
    #: HEIGHTS note in `_build_ui` — this is not simply each button's
    #: setFixedHeight, because a stylesheet-derived minimum outranks that.
    ROW_H = 34

    log_message = Signal(str, str)
    changed = Signal()                       # A/B/ratios changed

    _step_done = Signal(int)
    _step_error = Signal(str)
    _drive_done = Signal()
    _home_done = Signal()
    _cal_done = Signal(int)
    _cal_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera: Optional[PTPCameraClient] = None
        # Positions are RELATIVE: setting A rebases the origin so that A is
        # always exactly 0 and B is a signed offset from it. There is no
        # absolute lens coordinate to speak of — the camera reports none — so
        # pretending otherwise is what made the old readout meaningless.
        self._position = 0
        self._a_set = False
        self._pos_b: Optional[int] = None
        self._busy = False
        self._travel: Optional[int] = None
        # Mechanical stops, in the same relative coordinates. Known only
        # after calibration; None means "unknown, do not clamp".
        self._limit_lo: Optional[int] = None
        self._limit_hi: Optional[int] = None
        self._ratio_m = 5.0
        self._ratio_l = 20.0
        # Set by MainWindow: called (on the worker thread, before the focus
        # command) the first time focus is touched after connect/a session,
        # to shut any stale LiveView and open a clean one.
        self.ensure_liveview = None
        # Set by MainWindow too: called after this panel closes LiveView on
        # its own, so the window knows to open a fresh one next time.
        self.liveview_invalidated = None

        self._step_done.connect(self._on_step_done)
        self._step_error.connect(self._on_step_error)
        self._drive_done.connect(self._on_drive_done)
        self._home_done.connect(self._on_home_done)
        self._cal_done.connect(self._on_cal_done)
        self._cal_failed.connect(self._on_cal_failed)

        self._build_ui()
        self._relabel_jog_buttons()
        self._update_display()

    # ── plumbing ─────────────────────────────────────────────────────
    def set_camera(self, camera: Optional["PTPCameraClient"]):
        self._camera = camera
        self._update_button_states()

    # Step sizes are fixed constants, published via apply_step_sizes();
    # there is no editor widget.
    @property
    def ratio_m(self) -> float:
        return self._ratio_m

    @property
    def ratio_l(self) -> float:
        return self._ratio_l

    @property
    def travel(self) -> Optional[int]:
        return self._travel

    @property
    def homed(self) -> bool:
        return self._limit_lo is not None

    @property
    def limit_lo(self) -> Optional[int]:
        return self._limit_lo

    def _clamp(self, value: int) -> Tuple[int, bool]:
        """Clamp a candidate position to the known stops. Returns (v, hit)."""
        if self._limit_lo is not None and value < self._limit_lo:
            return self._limit_lo, True
        if self._limit_hi is not None and value > self._limit_hi:
            return self._limit_hi, True
        return value, False

    # ── UI ───────────────────────────────────────────────────────────
    def _build_ui(self):
        """
        Build every control, then hand out ONE mountable piece.

        `controls_widget` is a single row — Go to A · jog near · Set A ·
        Set B · jog far · Go to B — mounted directly under the live view,
        because jogging focus and watching the plane move is one action and
        splitting them across the window makes it two. It is laid out like
        the travel it drives: near at the left, far at the right, the two
        commits in the middle where the lens is while you are deciding. Read
        left to right it is near→far, matching the arrows and the track
        floating over the image above it.

        The commits used to have a second row of their own, on the argument
        that they are not nudges and do not belong in the strip you click
        twenty times in a row. That row cost ~50 px on the axis this window
        has least of, and it was guarding against the wrong button: the click
        that actually destroys work is Calibrate, and Calibrate is not here —
        it and Home sit in the card header, out of the hand's way. What is
        left is recoverable (a stray Set A is undone by pressing Set A in the
        right place), and the group gaps plus the blue/amber fills keep the
        pair visually distinct from the arrows either side.

        This widget itself stays the owner of all of them, so the enable/
        disable and busy handling below is unchanged by where they end up.
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        # -- jog -------------------------------------------------------
        # NEAR and FAR are no longer QLabels bracketing this row: the track
        # paints them at the ends of its own rail now (END_PAD_L/R in
        # FocusTrackWidget). That is the axis they annotate, and it buys back
        # the ~90 px this row needs now that twelve buttons share it.
        self.btn_near_L = self._make_step_btn("◄◄◄", "Minus", "Large", ACCENT_BLUE)
        self.btn_near_M = self._make_step_btn("◄◄", "Minus", "Medium", ACCENT_BLUE)
        self.btn_near_S = self._make_step_btn("◄", "Minus", "Small", ACCENT_BLUE)
        self.btn_near_5 = self._make_multi_btn(5, "Minus", ACCENT_BLUE)
        self.btn_far_5 = self._make_multi_btn(5, "Plus", ACCENT_AMBER)
        self.btn_far_S = self._make_step_btn("►", "Plus", "Small", ACCENT_AMBER)
        self.btn_far_M = self._make_step_btn("►►", "Plus", "Medium", ACCENT_AMBER)
        self.btn_far_L = self._make_step_btn("►►►", "Plus", "Large", ACCENT_AMBER)

        # -- track -----------------------------------------------------
        # Built in overlay mode: the window floats it over the bottom of the
        # live image rather than stacking it underneath, so the preview keeps
        # the ~64 px the track would otherwise have taken.
        self.track = FocusTrackWidget(overlay=True)
        self.track.setToolTip(
            'Focus travel. Cyan dot is the lens now; A is the near end (always 0),\nB the far end.')

        self.btn_home = QPushButton("Home")
        self.btn_home.setToolTip(
            "Drive to the lens's near mechanical stop and re-zero the counter there.\n\nDo this before setting A and B, and any time the positions look wrong.")
        # Deliberately the quieter of the two: muted text, no accent, and off
        # at the far right past a gap. Calibrate runs itself on connect and is
        # the one to notice; Home is the cheap fix you reach for only when the
        # numbers look wrong, and neither wants to be hit by accident while
        # aiming for Go to B.
        self.btn_home.setStyleSheet(f"""
            QPushButton {{ background-color: transparent;
                           color: {TEXT_MUTED};
                           border: 1px solid {BORDER_SOFT};
                           padding: 4px 12px; font-size: 11px;
                           font-weight: 700; }}
            QPushButton:hover {{ background-color: {PANEL_BG_SOFT};
                                 color: {ACCENT_CYAN};
                                 border-color: {rgba(ACCENT_CYAN, 0.4)}; }}
            QPushButton:disabled {{ background-color: transparent;
                                    color: {BORDER};
                                    border-color: {BORDER_SOFT}; }}
        """)
        self.btn_home.clicked.connect(self._home)

        self.btn_cal = QPushButton("Calibrate")
        self.btn_cal.setToolTip(
            "Measure the lens's full focus travel, so the app can refuse to\n"
            "drive past either end.\n\n"
            + ("A few seconds, no photos. Clears A and B.\n\n"
               if USE_STOP_BASED else
               "About a minute and roughly 10 frames; the shutter will fire.\n"
               "Needs a Nikon shooting RAW to the PC. Clears A and B.\n\n")
            + "Once per lens, and again after a zoom or a lens change.")
        # Same quiet style as Home. It used to be the accented primary action
        # here, back when a new user had to know to press it; calibration now
        # runs itself on connect, so this is only the manual re-run — after a
        # lens change or a zoom — and should not be shouting for attention.
        self.btn_cal.setStyleSheet(self.btn_home.styleSheet())
        self.btn_cal.clicked.connect(self._calibrate_clicked)

        # -- A/B -------------------------------------------------------
        # "(near)" / "(far)" only. The label used to spell out "near = 0",
        # which is true and is the one fact the track already shows in ink —
        # the A marker is drawn AT 0 and captioned "A = 0". Saying it a
        # second time cost the row width it now needs, and the tooltip still
        # explains the rebasing for anyone who wants it.
        self.btn_set_a = QPushButton("Set A  (near)")
        self.btn_set_a.setToolTip(
            "Record the lens's current position as the NEAR end (position 0).\n\nUse the arrows, not the focus ring — a ring turned by hand is invisible\nto the app. If it's been touched, press Home first.")
        self.btn_set_a.setStyleSheet(
            btn_style("#16243d", ACCENT_BLUE, rgba(ACCENT_BLUE, 0.35), "#1b2d4d"))
        self.btn_set_a.clicked.connect(self._set_a)

        self.btn_set_b = QPushButton("Set B  (far)")
        self.btn_set_b.setToolTip(
            "Record the lens's current position as the FAR end. A→B is the range\neach stack covers.")
        self.btn_set_b.setStyleSheet(
            btn_style("#2a2210", ACCENT_AMBER, rgba(ACCENT_AMBER, 0.35), "#382c12"))
        self.btn_set_b.clicked.connect(self._set_b)

        self.btn_goto_a = QPushButton("Go to A")
        self.btn_goto_a.setToolTip('Drive the lens to A.')
        self.btn_goto_a.setStyleSheet(
            btn_style(PANEL_BG_SOFT, ACCENT_BLUE, BORDER, FIELD_BG))
        self.btn_goto_a.clicked.connect(self._goto_a)

        self.btn_goto_b = QPushButton("Go to B")
        self.btn_goto_b.setToolTip('Drive the lens to B.')
        self.btn_goto_b.setStyleSheet(
            btn_style(PANEL_BG_SOFT, ACCENT_AMBER, BORDER, FIELD_BG))
        self.btn_goto_b.clicked.connect(self._goto_b)

        # HEIGHTS. All twelve land on ROW_H so the row is a single plane
        # rather than a jog strip with four taller buttons dropped into it.
        #
        # setFixedHeight alone will not do this, and the way it fails is worth
        # writing down. Qt derives a minimum height from the stylesheet —
        # `min-height` plus vertical padding plus border — and APPLIES IT TO
        # THE WIDGET on polish, which happens after the constructor runs. So
        # it lands on top of whatever setFixedHeight set, and setFixedHeight
        # is left controlling only the maximum. With the global sheet's 7 px
        # padding these four came out with min 36 against max 34, and Qt
        # resolves that in the minimum's favour: 36 px buttons, and a row that
        # grew to match. The jog buttons escaped it only because their own
        # sheet already overrides the padding down to 2 px.
        #
        # So: relax the padding here too, which drops the derived minimum
        # safely below ROW_H, and pin the actual height on the container
        # instead (below) — a plain QWidget nothing in the sheet addresses.
        for b in (self.btn_set_a, self.btn_set_b,
                  self.btn_goto_a, self.btn_goto_b):
            b.setStyleSheet(b.styleSheet()
                            + "QPushButton { padding: 2px 14px;"
                              " min-height: 0px; }")
            b.setFixedHeight(self.ROW_H)

        # -- the row ---------------------------------------------------
        # Go to A · ◄◄◄ ◄◄ ◄ ◄ · Set A Set B · ► ► ►► ►►► · Go to B
        #
        # Only the arrows stretch. The four end/middle buttons are carried at
        # their natural width, so the row's landmarks stay put as the window
        # is resized and the slack all goes to the things you aim at most.
        #
        # GAP has to beat the 3 px between arrows by enough to read as a
        # break rather than a wider arrow — that separation is the only thing
        # telling you Set A is not the next step size along. 4-5x it.
        #
        # PAIR_GAP sits between the two commits, and is the one gap here
        # chosen for the hand rather than the eye: Set A and Set B are the
        # two buttons in this row whose consequences differ most, and at the
        # arrows' 3 px a slip of a few pixels lands on the wrong one. Kept
        # below GAP so they still read as a pair rather than a third group.
        # Three tiers, and they have to stay ordered — the row's whole
        # structure is carried by these numbers. Layout spacing adds 3 to
        # each, so on screen: 3 px inside a group, 13 between the commits,
        # 21 between groups.
        GAP = 18
        PAIR_GAP = 10
        row = QHBoxLayout()
        row.setSpacing(3)

        row.addWidget(self.btn_goto_a)
        row.addSpacing(GAP)
        for b in (self.btn_near_L, self.btn_near_M, self.btn_near_5,
                  self.btn_near_S):
            row.addWidget(b, 1)
        row.addSpacing(GAP)
        row.addWidget(self.btn_set_a)
        row.addSpacing(PAIR_GAP)
        row.addWidget(self.btn_set_b)
        row.addSpacing(GAP)
        for b in (self.btn_far_S, self.btn_far_5, self.btn_far_M,
                  self.btn_far_L):
            row.addWidget(b, 1)
        row.addSpacing(GAP)
        row.addWidget(self.btn_goto_b)

        # ── the mountable piece ───────────────────────────────────────
        # Handed out as a widget so the window can put it hard against the
        # bottom of the preview. Its buttons are children of this widget until
        # something re-parents them; nothing here cares where they end up,
        # because every enable/disable below addresses the BUTTONS directly
        # rather than relying on this widget's own state propagating down a
        # tree it may no longer be part of.
        # The track is NOT in here — the window floats it over the image (see
        # `LiveViewPanel.set_overlay`).
        self.controls_widget = QWidget()
        cw = QVBoxLayout(self.controls_widget)
        cw.setContentsMargins(0, 0, 0, 0)
        cw.setSpacing(0)
        cw.addLayout(row)
        # This is what actually sets the row height (see the HEIGHTS note
        # above): a bare QWidget, so no stylesheet rule derives a minimum for
        # it and overwrites the pin. Every button's derived minimum is now
        # below ROW_H and its maximum is exactly ROW_H, so the layout fills
        # them all to it and the twelve tops and bottoms line up.
        self.controls_widget.setFixedHeight(self.ROW_H)

        # Calibrate and Home are NOT in it. They are once-per-lens setup, not
        # part of framing a shot, and Calibrate clears A and B — so the window
        # mounts them in the card header, out of the hand's way and off the
        # height budget.
        self.cal_buttons = (self.btn_cal, self.btn_home)

        # Default arrangement, used if nothing re-parents it. Keeps this
        # widget usable on its own — a panel that only works when a specific
        # window happens to dismantle it is a trap for the next person.
        layout.addWidget(self.controls_widget)

    def _make_step_btn(self, text, direction, size, color):
        btn = QPushButton(text)
        btn.setFixedHeight(self.ROW_H)
        btn.setMinimumWidth(34)
        btn.setToolTip(f"{size} step {'towards far' if direction == 'Plus' else 'towards near'}")
        btn.setStyleSheet(f"""
            QPushButton {{ color: {color}; font-size: 12px; font-weight: 700;
                           border: 1px solid {rgba(color, 0.28)};
                           padding: 2px 4px; background-color: {PANEL_BG_SOFT}; }}
            QPushButton:hover {{ background-color: {rgba(color, 0.13)};
                                 border-color: {rgba(color, 0.55)}; }}
            QPushButton:disabled {{ color: {TEXT_MUTED};
                                    border-color: {BORDER_SOFT};
                                    background-color: {DARK_BG}; }}
        """)
        btn.clicked.connect(lambda: self._do_step(direction, size))
        return btn

    def _make_multi_btn(self, steps: int, direction: str, color: str):
        """
        A jog button for a size that sits between the three native ones.

        The three arrow pairs fire 10, 100 and 500 driver units, which leaves
        a wide gap at the fine end — this fills it. Text and tooltip are set
        by `_relabel_jog_buttons` along with the rest.
        """
        btn = QPushButton()
        btn.setFixedHeight(self.ROW_H)
        btn.setMinimumWidth(34)
        btn.setStyleSheet(f"""
            QPushButton {{ color: {color}; font-size: 12px; font-weight: 700;
                           border: 1px solid {rgba(color, 0.28)};
                           padding: 2px 4px; background-color: {PANEL_BG_SOFT}; }}
            QPushButton:hover {{ background-color: {rgba(color, 0.13)};
                                 border-color: {rgba(color, 0.55)}; }}
            QPushButton:disabled {{ color: {TEXT_MUTED};
                                    border-color: {BORDER_SOFT};
                                    background-color: {DARK_BG}; }}
        """)
        btn.clicked.connect(lambda: self._do_multi(direction, steps))
        return btn

    def _do_multi(self, direction: str, steps: int):
        """
        Jog `steps` steps — ONE confirmed command, like every other jog.

        Nothing is composed or paced here: `drive_focus` hands the whole
        distance to MfDrive in one command and does not return until the
        camera confirms the lens stopped — see the module docstring of
        `hardware.focus`. `steps` is APP steps; the buttons are labelled
        in driver units, so the one that calls this with 5 reads "50".
        """
        if not self._camera or self._busy:
            return
        self._busy = True
        self._set_controls_enabled(False)

        def _run():
            try:
                if self.ensure_liveview:
                    self.ensure_liveview()
                hit_stop = []
                moved = drive_focus(self._camera, direction, steps,
                                    on_limit=lambda: hit_stop.append(True))
                sign = 1 if direction == "Plus" else -1
                new_pos, hit = self._clamp(self._position + sign * moved)
                self._position = new_pos
                if hit_stop and not hit:
                    self._report_lens_stop(direction)
                self._step_done.emit(1 if hit else 0)
            except Exception as e:                       # noqa: BLE001
                self._step_error.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    # ── stepping ─────────────────────────────────────────────────────
    def _units(self, size: str) -> int:
        if size == "Small":
            return 1
        return int(round(self.ratio_m if size == "Medium" else self.ratio_l))

    def _do_step(self, direction: str, size: str):
        """One jog-button move, issued off the GUI thread."""
        if not self._camera or self._busy:
            return
        units = self._units(size)
        self._busy = True
        self._set_controls_enabled(False)

        def _run():
            try:
                if self.ensure_liveview:
                    self.ensure_liveview()
                hit_stop = []
                # Through drive_focus so steps -> driver units happens in
                # exactly one place. `units` here is STEPS (1/10/50).
                drive_focus(self._camera, direction, units,
                            on_limit=lambda: hit_stop.append(True))
                sign = 1 if direction == "Plus" else -1
                new_pos, hit = self._clamp(self._position + sign * units)
                self._position = new_pos
                if hit_stop and not hit:
                    self._report_lens_stop(direction)
                self._step_done.emit(1 if hit else 0)
            except Exception as e:                       # noqa: BLE001
                self._step_error.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_step_done(self, hit_limit):
        self._busy = False
        if hit_limit:
            which = "near" if self._position == self._limit_lo else "far"
            self.log_message.emit(
                f"At the {which} focus limit — the lens cannot go further "
                f"this way.", ACCENT_AMBER)
        self._update_display()
        self._set_controls_enabled(True)

    def _on_step_error(self, msg):
        self._busy = False
        self.log_message.emit(f"Focus step failed: {msg}", ACCENT_RED)
        self._update_display()
        self._set_controls_enabled(True)

    def _report_lens_stop(self, direction: str):
        """
        Say that the CAMERA reported a mechanical stop during a jog.

        Not the same fact as the `hit` `_on_step_done` reports. That one is
        this panel's own `_clamp` seeing the counter reach a limit the app
        already knows about — and it knows about none until Home or Calibrate
        has run, which is exactly the state in which a jog can walk into a
        stop with nothing noticing. So the callers only call this when the
        clamp did NOT fire: where it did, the position it pinned is right and
        `_on_step_done` has already said so, and repeating it here would
        claim a counter error the clamp had just removed.

        "MAY now be" in the message, not "is". All AT_LIMIT proves is that a
        stop was reached; how much of the jog ran before it is not knowable
        (see `drive_focus`), and a jog that lands EXACTLY on the stop is
        indistinguishable from one cut short — the counter is then perfectly
        right and there is nothing to correct. Claiming an error outright
        would be false in that case, and it is not a rare one: it is what
        every deliberate jog down onto the near stop looks like.

        Emitted from the worker thread, as the log lines in `_drive_to`
        already are — `log_message` is a Qt signal, so the delivery is queued
        onto the GUI thread rather than touching widgets from here.
        """
        self.log_message.emit(
            f"The lens reached its {'far' if direction == 'Plus' else 'near'} "
            f"mechanical stop. The camera does not say how much of that jog ran "
            f"before it did, so the position shown may now be further out than "
            f"the lens actually is — press Home to re-zero on the near stop if "
            f"the numbers stop matching what you see.", ACCENT_AMBER)

    def _drive_to(self, target_pos: int):
        if not self._camera or self._busy:
            return
        target_pos, _hit = self._clamp(target_pos)
        if target_pos == self._position and self._limit_lo is None:
            # Only safe to skip while there is no stop to anchor on. Once
            # there is, "the counter already says A" is exactly the state
            # worth distrusting: a dropped command leaves the counter
            # reading A while the lens sits somewhere else, and refusing
            # to move is what makes that error permanent. Re-anchor.
            self.log_message.emit("Already there.", TEXT_MUTED)
            return
        diff = target_pos - self._position
        direction = "Plus" if diff > 0 else "Minus"
        distance = abs(diff)
        self._busy = True
        self._set_controls_enabled(False)
        self.log_message.emit(
            f"Driving to step {target_pos}"
            + (" via the near stop, so it lands in the same place every time…"
               if self._limit_lo is not None
               else f" ({distance} steps {direction.lower()})…"), ACCENT_CYAN)

        def _run():
            try:
                if self.ensure_liveview:
                    self.ensure_liveview()
                if self._limit_lo is not None:
                    # Anchored on the near stop. See anchored_drive() for why
                    # this beats driving target-minus-counter: the counter is
                    # the thing that goes wrong, and a stop is the only
                    # reference on this rig that cannot.
                    #
                    # `expected_from` turns the sweep into a free audit of the
                    # datum as well — the one check that catches a focus ring
                    # turned by hand, which nothing else here can see. Emitted
                    # amber rather than raised: the drive itself completes, it
                    # just completes somewhere the app was wrong about.
                    before = self._position
                    self._position = anchored_drive(
                        self._camera, target_pos, self._limit_lo,
                        expected_from=before,
                        warn=lambda m: self.log_message.emit(m, ACCENT_AMBER))
                else:
                    moved = drive_focus(self._camera, direction, distance)
                    sign = 1 if direction == "Plus" else -1
                    self._position += sign * moved
                self.log_message.emit(
                    f"Arrived at step {self._position}.", ACCENT_GREEN)
            except Exception as e:                       # noqa: BLE001
                self.log_message.emit(f"Drive failed: {e}", ACCENT_RED)
            finally:
                self._drive_done.emit()

        threading.Thread(target=_run, daemon=True).start()

    def _on_drive_done(self):
        self._busy = False
        self._update_display()
        self._set_controls_enabled(True)

    # ── A/B ──────────────────────────────────────────────────────────
    def _set_a(self):
        """
        A defines the origin: wherever the lens is now becomes position 0,
        and everything else shifts to match.  That is what makes the numbers
        mean something — "B is 340 steps past the near plane" rather than
        two arbitrary counts whose difference happens to be the range.
        """
        shift = self._position
        self._position = 0
        self._a_set = True
        if self._pos_b is not None:
            self._pos_b -= shift
        if self._limit_lo is not None:
            self._limit_lo -= shift
        if self._limit_hi is not None:
            self._limit_hi -= shift
        self.log_message.emit(
            "A (near plane) set — this is now position 0.", ACCENT_BLUE)
        self._update_display()
        self.changed.emit()

    def _set_b(self):
        if not self._a_set:
            # Setting B first would leave the range measured from a
            # meaningless origin, so adopt the current spot as A instead.
            self.log_message.emit(
                "Set A first — the near plane defines position 0.",
                ACCENT_AMBER)
            return
        if self._position == 0:
            self.log_message.emit(
                "B is at the same place as A — jog the focus first.",
                ACCENT_AMBER)
            return
        self._pos_b = self._position
        self.log_message.emit(
            f"B (far plane) set at {self._pos_b:+d} steps from A.",
            ACCENT_AMBER)
        self._update_display()
        self.changed.emit()

    def _home(self):
        """
        Drive into the NEAR mechanical stop and re-zero the counter.

        The one absolute reference this rig has: no absolute focus position
        comes over the wire, but the body reports arrival at the stop, so
        afterwards the lens is somewhere known rather than somewhere inferred.
        That makes homing self-correcting — however the count drifted, one
        home wipes it.

        The first home defines the origin. Later ones restore it.
        """
        if not self._camera or self._busy:
            return
        self._busy = True
        self._set_controls_enabled(False)
        first = self._limit_lo is None
        self.log_message.emit(
            f"Homing: driving into the near stop "
            f"to {'set' if first else 'restore'} the reference…", ACCENT_CYAN)

        def _run():
            try:
                if self.ensure_liveview:
                    self.ensure_liveview()
                # Stops when the CAMERA reports the stop, rather than
                # driving a blind 800 steps and trusting physics.
                self._camera.drive_to_stop(toward_far=False)
                # Deliberately NOT "+= moved": the whole point is that the
                # lens is now at a known place regardless of how many of
                # those steps actually did anything.
                self._home_done.emit()
            except Exception as e:                       # noqa: BLE001
                self._step_error.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_home_done(self):
        if self._limit_lo is None:
            # First home: the stop becomes the origin. A and B, if they were
            # set before this, were measured in a frame that no longer means
            # anything, so they go.
            self._position = 0
            self._limit_lo = 0
            if self._a_set or self._pos_b is not None:
                self._a_set = False
                self._pos_b = None
                self.log_message.emit(
                    "Homed. The near stop is now position 0. A and B were "
                    "set against the old reference so they have been "
                    "cleared — set them again from here.", ACCENT_AMBER)
            else:
                self.log_message.emit(
                    "Homed. The near stop is now position 0.", ACCENT_GREEN)
            first_home = True
        else:
            first_home = False
            drift = self._position - self._limit_lo
            self._position = self._limit_lo
            self.log_message.emit(
                f"Homed. The counter was {drift:+d} steps out; it is now "
                f"exact again.", ACCENT_GREEN)
        self._busy = False
        self._update_display()
        self._set_controls_enabled(True)
        self.changed.emit()
        # The stop-based measurement needs nothing installed, so the tip is
        # always worth showing; the frame-based one is useless without
        # exifread, and offering it then would only lead to a dialog saying so.
        if first_home and self._limit_hi is None and (USE_STOP_BASED
                                                      or lensdata.available()):
            self.log_message.emit(
                "Tip: press Calibrate to measure this lens's focus travel. "
                "The app can then refuse to send the lens past either end.",
                TEXT_MUTED)

    # ── travel calibration ───────────────────────────────────────────
    def _calibrate_clicked(self):
        """The Calibrate button: confirm, then measure."""
        if self._camera is None or self._busy:
            return
        # Only the frame-based path reads the lens position out of a captured
        # file, so only it needs exifread. The stop-based one asks the camera.
        if not USE_STOP_BASED and not lensdata.available():
            QMessageBox.information(
                self, "Calibration unavailable",
                "Measuring the focus travel needs the exifread package, "
                "which reads the lens position out of the captured file.\n\n"
                "Install it with:  pip install exifread")
            return
        again = ("\n\nThis lens has already been measured at "
                 f"{self._limit_hi} steps; running again will replace that "
                 "and clear A and B." if self._limit_hi is not None else "")
        how = (
            "The lens is driven into each mechanical stop in turn and the "
            "distance between them measured. The camera reports each stop as "
            "the lens reaches it, so nothing is photographed or inferred.\n\n"
            "Takes a few seconds. No photos are taken and the shutter does "
            "not fire, so it works whatever the camera is set to record. "
            "Leave the focus ring alone while it runs."
            if USE_STOP_BASED else
            "Takes about a minute and roughly 10 photos. The shutter will "
            "fire repeatedly; leave the camera alone while it runs.\n\n"
            "Needs a Nikon shooting RAW to the PC.")
        answer = QMessageBox.question(
            self, "Measure the focus travel?",
            "Measure how far this lens can actually focus?\n\n"
            "Until this is done the app knows where the near stop is but not "
            "the far one, so nothing stops B being set past the end of the "
            "lens.\n\n" + how + again,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer == QMessageBox.StandardButton.Yes:
            self._calibrate()

    def auto_calibrate(self) -> bool:
        """
        Run the calibration unprompted, right after the camera connects.

        Returns False if it was skipped. Deliberately connect-time and not
        run-time: `_on_cal_done` CLEARS A and B, because the measurement
        re-zeros the coordinate frame they were expressed in. At connect there
        is nothing to lose; at Start it would silently wipe the focus range
        the user had just dialled in.

        Skipped once the travel is known, so reconnecting mid-session does not
        throw away A and B. Press Calibrate to force it — after a lens change,
        or after zooming, since the travel is per focal length.
        """
        if self._camera is None or self._busy:
            return False
        if self._limit_hi is not None:
            return False            # already measured this session
        if not USE_STOP_BASED and not lensdata.available():
            return False            # the frame-based path needs exifread
        self._calibrate()
        return True

    def _calibrate(self):
        if not self._camera or self._busy:
            return
        self._busy = True
        self._set_controls_enabled(False)
        self.log_message.emit(
            "Measuring the focus travel between the two mechanical stops. "
            "No photos; leave the focus ring alone."
            if USE_STOP_BASED else
            "Measuring the focus travel. The shutter will fire several "
            "times; leave the camera alone.", ACCENT_CYAN)

        def report(msg):
            self.log_message.emit(f"  {msg}", TEXT_MUTED)

        def _run():
            try:
                # Establish a live-view session first, like every other focus
                # path in this panel. This used to give the reason as "focus
                # commands are inert without one", which is what the whole
                # repo believed; it was measured on this rig's Z 6_2 on
                # 2026-08-03 and is false for this body. With live view
                # stopped, drive(+500) was accepted, answered RC_OK, and moved
                # the lens — indistinguishable from the same drive with live
                # view up. This call is not what makes the motor listen.
                #
                # What it IS for: `ensure_liveview` is MainWindow's
                # `_ensure_fresh_lv_for_focus`, and its real payload is the
                # window's own bookkeeping and the picture on the screen. It
                # opens a session only when `_lv_fresh` is down, then emits
                # `_lv_started` to wake the on-screen preview. `_lv_fresh` is
                # cleared on connect and at both ends of a run, so the first
                # focus touch after any of those is what puts the image back —
                # and this is a panel you drive by eye. The session itself
                # would be raised regardless, one layer down: focus calls go
                # through `PTPCameraClient._ensure_for_focus`, which raises one
                # unless a run has taken the hold (`hold_liveview_closed`).
                # That caveat costs this panel nothing, because the panel is
                # disabled for the duration of a run — but name the right
                # method, because `_ensure_liveview` is now only the ungated
                # path and `set_zoom`'s. What is lost by skipping this call is
                # the window knowing about it and the operator seeing
                # anything. It also keeps `_lv_fresh` true-to-life for the
                # `liveview_invalidated` call in the finally block below.
                if self.ensure_liveview:
                    self.ensure_liveview()
                if USE_STOP_BASED:
                    travel = measure_travel_by_stops(self._camera,
                                                     progress=report)
                else:
                    travel = self._calibrate_by_frames(report)
                self._cal_done.emit(travel)
            except CalibrationError as e:
                self._cal_failed.emit(str(e))
            except Exception as e:                       # noqa: BLE001
                self._cal_failed.emit(f"{e}")
            finally:
                # Only the frame-based path shuts live view behind the
                # window's back, and only then does the window's "one is
                # already up" flag have to be cleared — otherwise the next
                # focus jog fires into a closed session. The stop-based path
                # leaves the session it was given exactly as it found it, so
                # invalidating here would throw away a working live view.
                if not USE_STOP_BASED and self.liveview_invalidated:
                    self.liveview_invalidated()

        threading.Thread(target=_run, daemon=True).start()

    def _calibrate_by_frames(self, report) -> int:
        """
        The frame-based fallback: shoot a short series and read the lens
        position out of each file. Only reached with USE_STOP_BASED off.

        Kept whole rather than deleted because it is the independent witness.
        It measures in NEF encoder counts rather than driver units, and run
        head to head on the Z 6_2 it agreed to the unit — 1556 either way —
        differing only by its own max(3.0, 2%) pad, 152 steps against 155. A
        body where MfDriveStepEnd does not behave as it does here would need
        this path, and it is the only way to check the other one.
        """
        folder = WRITABLE_DIR / "_focus_calibration"
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob("*"):
            try:
                stale.unlink()
            except OSError:
                pass
        # The frames have to reach the PC to be read, so card-only mode comes
        # off. Not restored afterwards on purpose: every run sets the transfer
        # mode itself from the checkbox, so leaving it on PC transfer cannot
        # affect a later capture, and guessing what to put back would be worse.
        try:
            self._camera.set_transfer_mode(False)
        except Exception:                                # noqa: BLE001
            pass
        self._camera.set_filename_template("cal_[Counter 4 digit]")
        self._camera.begin_stack(folder)
        return measure_travel(self._camera, folder, progress=report)

    def _on_cal_done(self, travel: int):
        # BOTH measurement paths park the lens against the near stop before
        # returning, and the travel is measured from there, so one run settles
        # both limits. If a third method is ever added it has to do the same:
        # this line is what makes the counter true.
        self._position = 0
        self._limit_lo = 0
        self._limit_hi = int(travel)
        self._travel = int(travel)
        self._a_set = False
        self._pos_b = None
        self._busy = False
        self.log_message.emit(
            f"Focus travel is {travel} steps. Both ends are now known, so "
            f"A and B cannot be set past them. Jog out to your near plane "
            f"and press Set A.", ACCENT_GREEN)
        self._update_display()
        self._set_controls_enabled(True)
        self.changed.emit()

    def _on_cal_failed(self, msg: str):
        self._busy = False
        self.log_message.emit(f"Travel calibration stopped: {msg}", ACCENT_AMBER)
        self.log_message.emit(
            "Homing still works, so positions stay correct as long as you "
            "keep A and B inside the lens's range.", TEXT_MUTED)
        self._update_display()
        self._set_controls_enabled(True)

    def _goto_a(self):
        if self._a_set:
            self._drive_to(0)

    def _goto_b(self):
        if self._pos_b is not None:
            self._drive_to(self._pos_b)

    # ── jog sizes ────────────────────────────────────────────────────
    def apply_step_sizes(self, ratio_m: float, ratio_l: float):
        """Adopt the Medium/Large jog sizes, as multiples of one step."""
        self._ratio_m = float(ratio_m)
        self._ratio_l = float(ratio_l)
        self._relabel_jog_buttons()
        self._update_display()
        self.changed.emit()

    def _relabel_jog_buttons(self):
        """
        Label the arrows in DRIVER UNITS — the currency MfDrive actually
        takes: 10 · 50 · 100 · 500 once MainWindow has published the real
        ratios through `apply_step_sizes`.

        The "step" is an invented unit worth 10 driver units
        (STEP_UNITS["Small"]), inherited from a UI that only offered the
        three fixed sizes `_make_step_btn` still builds — and it is what
        everything else on this axis counts in.

        These labels are the ONE thing in this panel that is not in app
        steps. `_position`, `_limit_lo`, `_limit_hi`, A, B and everything
        FocusTrackWidget draws are steps, and so is the travel figure
        calibration reports: `measure_travel_by_stops` returns
        `(units - 1) // STEP_UNITS["Small"]`, so the Z 6_2's 1556 driver
        units come back as 155 steps and `_on_cal_done` logs "Focus
        travel is 155 steps." The axis really does carry two scales and
        these buttons are the odd ones out — an earlier version of this
        docstring had it backwards and claimed travel was reported in
        units. Whichever way it is left, convert explicitly: comparing
        app steps against driver units raw was a live factor-of-10 bug
        in `focus.anchored_drive`.
        """
        unit = max(STEP_UNITS["Small"], 1)
        sizes = (
            (self.btn_near_S, self.btn_far_S, 1 * unit),
            (self.btn_near_5, self.btn_far_5, 5 * unit),
            (self.btn_near_M, self.btn_far_M, int(round(self.ratio_m)) * unit),
            (self.btn_near_L, self.btn_far_L, int(round(self.ratio_l)) * unit),
        )
        for near, far, units in sizes:
            near.setText(f"◄ {units}")
            far.setText(f"{units} ►")
            for btn, way in ((near, "near"), (far, "far")):
                btn.setToolTip(
                    f"Move {units} driver units towards {way}.\n"
                    f"Opens live view first if it isn't running.")

        # ONE MINIMUM FOR ALL EIGHT: the widest label's preferred width. The
        # row hands the arrows equal stretch, so at the row's own preferred
        # width they all come out the SAME size — the average of their hints
        # — and the two widest ("◄ 500", "500 ►") lose the 4 px their text
        # needs and render as "◄ 50". Only visible when the live-view card is
        # at its floor, which the column layout actually reaches (a portrait
        # frame on a 1080p screen). Setting every arrow's minimum to the
        # widest hint makes the equal share never smaller than the widest
        # label, and re-running it on every relabel keeps it true whatever
        # the sizes come out as.
        arrows = [b for pair in sizes for b in pair[:2]]
        widest = max(b.sizeHint().width() for b in arrows)
        for b in arrows:
            b.setMinimumWidth(widest)

    # ── display ──────────────────────────────────────────────────────
    # There is no numeric readout. The track draws position, A, B, the range
    # between them and both mechanical stops, all to scale — a row of
    # "pos 0 · A — · B — · range —" underneath was the same four facts in a
    # form you had to read rather than see.
    def _update_display(self):
        self.track.set_state(self._position, self._a_set, self._pos_b,
                             self._limit_lo, self._limit_hi)
        self._update_button_states()

    def set_position(self, pos: int):
        """Called while a run is in progress so the track follows the lens."""
        self._position = pos
        self.track.set_state(self._position, self._a_set, self._pos_b,
                             self._limit_lo, self._limit_hi)

    def _set_controls_enabled(self, enabled: bool):
        for b in (self.btn_near_L, self.btn_near_M, self.btn_near_5,
                  self.btn_near_S, self.btn_far_S, self.btn_far_5,
                  self.btn_far_M, self.btn_far_L, self.btn_set_a,
                  self.btn_home, self.btn_cal):
            b.setEnabled(enabled and self._camera is not None)
        self.btn_set_b.setEnabled(
            enabled and self._camera is not None and self._a_set)
        self.btn_goto_a.setEnabled(
            enabled and self._camera is not None and self._a_set)
        self.btn_goto_b.setEnabled(
            enabled and self._camera is not None and self._pos_b is not None)

    def _update_button_states(self):
        self._set_controls_enabled(not self._busy)

    @property
    def busy(self) -> bool:
        """
        Is a focus command in flight on this panel's worker thread?

        Exists for the single-shot button in the live-view header, which is the
        first control outside this panel that can drive the camera while a jog
        is running. Greying the panel's own buttons is not enough there: the
        two are separate widgets and a shutter release landing between a
        MfDrive and the `wait_ready` that confirms it would interleave two
        multi-transaction sequences on one USB link. The transport's lock makes
        that safe transaction by transaction, not sequence by sequence.
        """
        return self._busy

    # ── getters used by the run ───────────────────────────────────────
    def get_range(self) -> Optional[int]:
        if self._a_set and self._pos_b is not None:
            return abs(self._pos_b)
        return None

    def get_a_position(self) -> Optional[int]:
        return 0 if self._a_set else None

    def get_b_position(self) -> Optional[int]:
        return self._pos_b

    def get_current_position(self) -> int:
        return self._position

    def get_focus_direction(self) -> str:
        """A is 0, so the sign of B is the direction the stack runs."""
        if self._pos_b is not None and self._pos_b < 0:
            return "Minus"
        return "Plus"

    def set_enabled(self, enabled: bool):
        """
        Lock or release the whole panel — called when a run starts and ends.

        Addresses the buttons directly rather than calling setEnabled() on
        this widget: its controls are mounted inside the live-view card, so
        they are no longer this widget's children and disabling `self` would
        grey out nothing at all.
        """
        if enabled:
            self._update_button_states()
        else:
            self._set_controls_enabled(False)
