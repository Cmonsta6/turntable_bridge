"""
The main window: connections, live-settings plumbing and run control.

Widget construction lives in layout.WindowLayoutMixin. What is left here
is the behaviour — connecting to the camera and the turntable, pushing
settings into the LiveSettings box, starting/resuming/stopping a run, and
reacting to the worker's signals.

Rotation geometry note: positions-per-rev and degrees-per-move are two
views of ONE quantity. Typing in either flips the other to "auto";
`_rotation_geometry()` resolves the pair and is the single source of
truth — every consumer calls it rather than reading the fields.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PySide6.QtCore import QSettings, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDoubleSpinBox, QFileDialog, QMainWindow, QMessageBox,
)

from ..hardware import (
    HARDWARE_AVAILABLE, HARDWARE_IMPORT_ERROR, ComximClient,
)
from ..hardware.constants import CAMERA_UNKNOWN
from ..hardware.camera import PTPCameraClient
from ..hardware.focus import (
    STEP_UNITS, plan_focus_intervals, step_ratios,
)
from ..hardware.settings import LiveSettings
from ..run.checkpoint import RunCheckpoint
from ..run.worker import BridgeWorker
from .layout import WindowLayoutMixin

from .theme import (
    ACCENT_AMBER, ACCENT_CYAN, ACCENT_GREEN, ACCENT_RED, BORDER, BORDER_SOFT,
    DARK_BG, FIELD_BG, PANEL_BG_SOFT, TEXT_MUTED, TEXT_PRIMARY,
    TEXT_SECONDARY, rgba,
)


class MainWindow(WindowLayoutMixin, QMainWindow):
    # Trace callbacks arrive on the worker thread; a signal marshals them
    # back to the GUI thread, which is the only place Qt widgets may be
    # touched.
    _log_relay = Signal(str, str)
    # Same reason: the focus panel raises live view from ITS worker thread, and
    # the preview widget it needs to wake lives on the GUI thread.
    _lv_started = Signal()

    def __init__(self):
        super().__init__()
        self._log_relay.connect(self._append_log)
        self._lv_started.connect(self._show_preview)
        self._debug_file = None
        self._liveview_opened = False
        # LiveView-for-focus state: shut any stale LiveView and open a clean
        # one the first time the user touches a focus control after connect
        # or after a session; keep it until the next session closes it.
        self._lv_fresh = False
        self._lv_lock = threading.Lock()
        # Magnifier steps, asked of the camera once on connect. Empty
        # until then, and empty forever on a body that has none.
        self._zoom_levels: list = []
        # Free-spin state. `_spin_dir` is None when the table is not
        # free-running; `_spin_level` is which button was pressed — the arrow
        # count minus one, i.e. an index into SPIN_GRADES. It is bound to each
        # button at build time in `_build_dial_card` and is never incremented
        # by pressing. It WAS a press count that ramped, and that is exactly
        # why it went: the ramp started at full speed and had to be tapped
        # down, so the slow end — the end the whole feature exists for — was
        # four clicks away. See _spin.
        self._spin_dir: Optional[int] = None
        self._spin_level = 0
        self.setWindowTitle("ComXim + Nikon Focus Stacking")
        # AN OPENING SIZE, and nothing more. `_size_to_fit_panels` runs later
        # in this __init__, once `_build_ui` has made a tree to measure, and
        # replaces it with the content's natural size capped to the screen.
        #
        # There is deliberately NO setMinimumSize call here. There used to be
        # one — 1120x800, justified by `LiveViewPanel.MIN_W/MIN_H` being the
        # camera's own 750x500 frame on a card that spans all three columns,
        # so that anything narrower would show fewer pixels than the camera
        # sent. That argument is still true about the PREVIEW, and the preview
        # still enforces it: its own minimum is untouched. What changed is
        # that the content now sits in a scroll viewport (`_build_ui`), so the
        # preview holding its floor no longer forces the WINDOW to hold one.
        # Drag the window smaller and the content keeps its honest size and
        # scrolls, instead of either refusing to shrink or overlapping itself.
        self.resize(1280, 1000)

        self._camera: Optional[PTPCameraClient] = None
        self._tt: Optional[ComximClient] = None
        self._worker: Optional[BridgeWorker] = None
        self._status = "idle"
        self._elapsed = 0
        self._current_round = 0
        self._current_stack = 0
        self._stacks_per_round = 37
        self._positions_per_rev = 36
        self._last_angle = 0.0
        self._stack_times: deque = deque(maxlen=8)
        self._stacks_done = 0
        self._total_stacks = 0
        self._first_stack_secs: Optional[float] = None
        self._eta_seconds: Optional[float] = None
        # Last resume point reported by a worker. Kept after the run ends so an
        # interrupted run (error, Stop, or a device that never came back) can
        # be picked up with the Recover button. Cleared on a clean finish.
        self._last_checkpoint: "Optional[RunCheckpoint]" = None
        self._live = LiveSettings() if HARDWARE_AVAILABLE else None
        # Widgets are created in two columns; a change signal can fire while
        # the other column does not exist yet, so nothing reads widget state
        # until the whole tree is up.
        self._ui_ready = False
        self._pill_state_text = ""

        self._build_ui()
        self._ui_ready = True
        self._install_shortcuts()
        self._normalise_decimal_inputs()
        # NOT sized here. The content is proxied into a graphics scene and has
        # no real sizeHint until the scene lays it out, so measuring now gives
        # 44x17 and opens a 68x57 window. showEvent defers it — see there.
        self._geometry_applied = False
        self._greet()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

        self._update_button_states()
        self._refresh_spin()
        self._push_live()
        self._update_estimate()

    def _normalise_decimal_inputs(self):
        """
        Tidy up a bare leading decimal point.

        Qt already READS '.1' correctly (the box holds 0.1), but it leaves the
        text showing '.1' until the value is committed, which looks like it
        might not have registered. Rewriting the text from the value the
        moment editing finishes makes it read '0.100' — what you see then
        always matches what the box holds. Applies to every decimal field,
        including any added later.
        """
        for sb in self.findChildren(QDoubleSpinBox):
            def _fix(box=sb):
                box.lineEdit().setText(
                    box.prefix() + box.textFromValue(box.value()) + box.suffix())
            sb.editingFinished.connect(_fix)

    #: Where the window geometry is remembered between sessions. Explicit
    #: names rather than QApplication's, so the frozen .exe and a source run
    #: read the same key instead of quietly keeping two sets.
    SETTINGS_ORG = "TurntableBridge"
    SETTINGS_APP = "TurntableBridge"
    GEOMETRY_KEY = "window/geometry"

    #: Quiet period before a move or resize is written. A drag emits events
    #: continuously, and writing on each one would put hundreds of registry
    #: writes into a single window drag.
    GEOMETRY_SAVE_MS = 700

    def showEvent(self, event):                               # noqa: D102
        super().showEvent(event)
        # SIZED HERE, NOT IN __init__, and only once.
        #
        # `content_widget` lives inside a QGraphicsProxyWidget (see
        # ScalingHost), and a proxied widget has no meaningful sizeHint until
        # the scene has laid it out. In __init__ it answers 44x17 — so the
        # window opened at 68x57, a title bar with a stub under it. Even
        # inside showEvent it is still 44x17; the layout pass has not run yet.
        # A zero-delay singleShot puts this after that pass, where the hint is
        # the real 1298x1528. Measure it there or measure garbage.
        if not getattr(self, "_geometry_applied", False):
            self._geometry_applied = True
            QTimer.singleShot(0, self._apply_startup_geometry)

    def _settings(self) -> "QSettings":
        return QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)

    def _apply_startup_geometry(self):
        """
        Restore last session's window, or open at the content's natural size.

        RESTORE FIRST, because a remembered window is what the user last chose
        and beats anything computed. `restoreGeometry` carries position, size
        and the maximised flag together, so a window closed maximised comes
        back maximised.

        The result is then checked against the screens actually present, and
        the natural size is used instead if it lands nowhere usable. That
        check is a BACKSTOP rather than the mechanism: measured on Qt 6.9,
        `restoreGeometry` already pulls a window saved at 9000,9000 back to
        1659,691 and keeps the size the user chose — which is the right trade,
        reposition without resizing. `_on_a_screen` still earns its place,
        because the failure it guards has no recovery. A title bar with no
        pixels under it cannot be dragged back, so the app would simply look
        broken, and Qt's clamping is observed behaviour rather than a promise
        this code should lean on.
        """
        try:
            saved = self._settings().value(self.GEOMETRY_KEY)
            if saved and self.restoreGeometry(saved) and self._on_a_screen():
                return
        except Exception:                                    # noqa: BLE001
            pass                    # unreadable or stale — fall through
        self._size_to_fit_panels()

    def _on_a_screen(self) -> bool:
        """Is a usable part of the window frame actually on some display?"""
        frame = self.frameGeometry()
        for screen in QApplication.screens():
            visible = screen.availableGeometry().intersected(frame)
            # A sliver is not enough to grab — insist on something a hand can
            # actually hit, roughly a title bar's worth.
            if visible.width() >= 120 and visible.height() >= 40:
                return True
        return False

    def _size_to_fit_panels(self):
        """
        Open at the natural size of the content, capped to the screen.

        The content widget's own sizeHint is the whole answer now the layout
        is a stack of full-width bands — every panel contributes its natural
        height and the viewer row contributes the preview's, which is the
        camera's 750x500 frame plus the focus controls under it.

        CAPPED, NOT CLIPPED, and that cap is what makes this resolution
        independent. Ask for the natural size, take the smaller of it and the
        screen, and `ScalingHost` scales the whole interface to fit whatever
        that turns out to be. A large monitor gets the layout at full size; a
        small one gets the same layout, smaller. Neither gets a window with
        panels cut off the bottom.

        NO WINDOW MINIMUM IS SET, deliberately — see `ScalingHost` for why the
        old one existed and why scaling replaced it. Do not reintroduce one.
        """
        try:
            hint = self.content_widget.sizeHint()
            target_w = hint.width() + 24        # frame borders
            target_h = hint.height() + 40       # title bar slack
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                target_w = min(target_w, avail.width() - 8)
                target_h = min(target_h, avail.height() - 8)
                self.resize(target_w, target_h)
                # Centred, because a first run has no remembered position and
                # Qt's default drops the window at the top-left corner.
                frame = self.frameGeometry()
                frame.moveCenter(avail.center())
                self.move(frame.topLeft())
            else:
                self.resize(target_w, target_h)
        except Exception:                                    # noqa: BLE001
            pass

    # ── remembering the window ───────────────────────────────────────
    def _queue_geometry_save(self):
        """Debounce: one write once the window has been still for a moment."""
        timer = getattr(self, "_geo_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._save_geometry)
            self._geo_timer = timer
        timer.start(self.GEOMETRY_SAVE_MS)

    def _save_geometry(self):
        """
        Write the current geometry.

        Called from the debounce timer AND from closeEvent. The timer is what
        makes this survive a process that never gets to close cleanly — a
        crash, a kill, a power cut — because by then the size has already been
        on disk for as long as the window has been still. closeEvent alone
        would lose the session every time the app did not exit tidily, which
        is exactly when a user most wants their layout back.
        """
        try:
            if not self.isMinimized():
                self._settings().setValue(self.GEOMETRY_KEY,
                                          self.saveGeometry())
        except Exception:                                    # noqa: BLE001
            pass

    def resizeEvent(self, event):                             # noqa: D102
        super().resizeEvent(event)
        if getattr(self, "_geometry_applied", False):
            self._queue_geometry_save()

    def moveEvent(self, event):                               # noqa: D102
        super().moveEvent(event)
        if getattr(self, "_geometry_applied", False):
            self._queue_geometry_save()

    # NOTE ON THE LETTERBOX, because it was deliberately removed and then
    # deliberately put back. The live-view card spans all three grid columns,
    # which is what makes the panels beneath it line up with the ones above.
    # A card that spans cannot also be pinned to the frame's aspect, so where
    # the card is wider than `height x 1.5` the frame is centred with card
    # background either side.
    #
    # No arrangement avoids that AND keeps the column alignment: a full-width
    # row at this window's proportions is simply wider than 3:2. An earlier
    # build pinned the card's width instead and let the dial column absorb the
    # difference; the alignment was judged worth more than the bars.

    def _greet(self):
        """The first lines in the event log, once the window is built."""
        if not HARDWARE_AVAILABLE:
            self._append_log(
                f"The hardware layer could not be imported "
                f"({HARDWARE_IMPORT_ERROR}). Try: pip install pyserial",
                ACCENT_RED)
            return
        self._load_step_sizes()
        self._append_log("Ready. Connect the camera and the turntable "
                         "to begin.", TEXT_MUTED)

    def _load_step_sizes(self):
        """
        Publish the Small/Medium/Large jog sizes to the focus panel.

        Fixed constants (`hardware.focus.STEP_UNITS`). The camera takes an
        arbitrary step count per command, so the three named sizes are a
        convenience for the jog buttons rather than anything the protocol
        imposes; the run itself works in single steps.
        """
        ratio_m, ratio_l = step_ratios()
        self.focus_panel.apply_step_sizes(ratio_m, ratio_l)
        unit = STEP_UNITS["Small"]
        self._small_units = unit
        self._push_live()
        # Driver units, to match the jog buttons and the travel figure
        # calibration reports. The app's internal "step" is 10 of these and
        # is not something the user should have to hold in their head.
        self._append_log(
            f"Jog sizes: {unit} · {5 * unit} · {int(ratio_m) * unit} · "
            f"{int(ratio_l) * unit} driver units.", ACCENT_CYAN)

    # ══════════════════════════════════════════════════════════════════
    # Settings
    # ══════════════════════════════════════════════════════════════════
    def _safe_stack_name(self) -> str:
        import re as _re
        raw = self.field_stack_name.text().strip() if hasattr(
            self, "field_stack_name") else "subject"
        cleaned = "".join(c if (c.isalnum() or c in "-_") else "_"
                          for c in raw)
        cleaned = _re.sub(r"_+", "_", cleaned).strip("_")   # no runs of _
        return cleaned or "subject"

    def _on_stack_name_changed(self, *_):
        self._setting_changed("Subject name", "next stack")

    def _collect_settings(self) -> Dict[str, Any]:
        # Send the RESOLVED pair, so the worker never has to know which of the
        # two fields the user typed into.
        steps_res, deg_res = self._rotation_geometry()
        rng = self.focus_panel.get_range()
        return {
            "base_folder": self.field_base.text().strip() or str(Path.home()),
            "stack_name": self._safe_stack_name(),
            "photo_count": self.spin_photos.value(),
            "capture_wait_s": self.field_capture_wait.value(),
            "wait_for_file": self.chk_wait_file.isChecked(),
            "settle_after_rotate_s": self.field_settle.value(),
            "rotate_timeout_s": self.field_rotate_timeout.value(),
            "steps_per_rev": steps_res,
            "degrees_per_step": deg_res,
            "direction": self.field_direction.currentIndex(),
            "rounds": self.field_rounds.value(),
            "hold_between_rounds": self.chk_hold.isChecked(),
            "beep_enabled": self.chk_beep.isChecked(),
            # One spinbox, both devices — a command that goes unanswered is
            # retried the same number of times whichever device swallowed it.
            "capture_retries": self.field_retries.value(),
            "tt_retries": self.field_retries.value(),
            "close_lv_run": self.chk_close_lv.isChecked(),
            "keep_on_camera": self.chk_keep_on_cam.isChecked(),
            "reset_mode": self.combo_reset.currentData(),
            "renumber_reversed": True,
            "file_stable_ms": 250,
            "pos_a": self.focus_panel.get_a_position() or 0,
            "focus_range": rng or 0,
            "focus_dir": self.focus_panel.get_focus_direction(),
            "focus_homed": self.focus_panel.homed,
            # The step number the near mechanical stop sits at. Needed by the
            # run, not just the panel: repositioning between stacks anchors on
            # the stop, and it has to come out to A in the SAME coordinate
            # frame the panel used when A was set.
            "focus_limit_lo": self.focus_panel.limit_lo or 0,
            # Driver units in one step. Lets the log express a focus plan in
            # physical terms rather than bare counts.
            "small_units": getattr(self, "_small_units", 0),
        }

    def _push_live(self):
        """Copy every widget value into the settings box the worker reads."""
        if self._live is not None and self._ui_ready:
            self._live.update(self._collect_settings())
    def _setting_changed(self, label: str, scope: str):
        if not self._ui_ready:
            return
        self._push_live()
        self._update_estimate()
        if self._status in ("running", "paused", "nocamera", "notable"):
            colour = ACCENT_GREEN if scope == "now" else ACCENT_CYAN
            self._append_log(
                f"↻ {label} updated — applies {scope}.", colour)

    def _on_rotation_changed(self, source: str = ""):
        """
        Positions and Degrees describe the same thing from two ends, so
        typing in one puts the other on 'auto' and derives it. Whichever field
        the user last touched is the master. They share a row in the Capture
        card for exactly this reason.
        """
        if getattr(self, "_rot_sync", False):
            return                          # re-entrancy guard: our own edit
        self._rot_sync = True
        try:
            if source == "deg" and self.field_deg.value() > 0:
                # Degrees is now the master → positions becomes auto.
                if self.field_steps.value() != 0:
                    self.field_steps.setValue(0)        # 0 == "auto"
            elif source == "steps" and self.field_steps.value() > 0:
                # Positions is now the master → degrees becomes auto.
                if self.field_deg.value() != 0:
                    self.field_deg.setValue(0.0)        # 0 == "auto"
            elif (self.field_steps.value() == 0
                  and self.field_deg.value() == 0):
                # Both on auto is meaningless — fall back to the default.
                self.field_steps.setValue(36)
        finally:
            self._rot_sync = False

        steps, angle = self._rotation_geometry()
        self.lbl_rotation_info.setText(
            f"{steps + 1} stacks per revolution  ·  {angle:.2f}° per move")
        if self._ui_ready:
            self._setting_changed("Rotation", "next revolution")
            self._update_estimate()
            # Show the new wedge layout straight away so the dial is a live
            # preview of what you just typed. A run owns the dial while it is
            # turning (the worker drives it), so don't fight it mid-run — the
            # new geometry takes effect on the next revolution anyway.
            if self._status not in ("running", "paused", "nocamera", "notable"):
                self._positions_per_rev = steps
                self._stacks_per_round = steps + 1
                self.turntable_widget.set_state(steps, 0, 0, 0.0)

    def _rotation_geometry(self) -> Tuple[int, float]:
        """
        Resolve (positions_per_rev, degrees_per_move) from whichever of the
        two fields is the master. A degrees value that doesn't divide 360
        evenly is rounded up to the next whole number of positions, so the
        table always closes the loop rather than overshooting the start.
        """
        steps = self.field_steps.value()
        deg = self.field_deg.value()
        if steps > 0:
            return steps, (360.0 / steps)
        if deg > 0:
            steps = max(1, int(math.ceil(360.0 / deg - 1e-9)))
            return steps, deg
        return 36, 10.0

    def _on_reset_mode_changed(self, *_):
        mode = self.combo_reset.currentData()
        if mode == "serpentine":
            self._append_log(
                "Serpentine: alternate stacks are shot far→near and then "
                "renumbered to match.", TEXT_MUTED)
        self._setting_changed("Return-to-A mode", "next stack")

    def _update_estimate(self):
        """
        Shows the run size and warns only about the one thing that silently
        breaks a run: too many photos for the focus range, which makes some
        gaps round to zero so the lens stops moving partway. No time guess —
        that lives on the total progress bar.
        """
        if not self._ui_ready:
            return
        photos = self.spin_photos.value()
        stacks = self._rotation_geometry()[0] + 1
        rounds = self.field_rounds.value()
        total_stacks = stacks * (rounds if rounds > 0 else 1)
        shots = total_stacks * photos
        rng = self.focus_panel.get_range() or 0

        rev_txt = "∞ revolutions" if rounds <= 0 else f"{rounds} revolution" \
            + ("s" if rounds != 1 else "")
        text = (f"{stacks} stacks × {photos} photos × {rev_txt} = "
                f"{shots:,} shots")
        colour = TEXT_SECONDARY

        if rng and photos > 1:
            gap = rng / (photos - 1)
            if gap < 1.0:
                usable = rng + 1
                text = (f"⚠  {photos} photos over a {rng}-step range — only "
                        f"{usable} land on a different focus position. "
                        f"Use {usable} photos, or widen A→B.")
                colour = ACCENT_RED
            else:
                # OWN LINE, AND BOTH UNITS, because this sits a few inches
                # from the jog buttons and the two were counting in different
                # currencies. The gap is in APP steps — the unit A, B and the
                # focus range are all kept in — while the jog buttons are
                # labelled in DRIVER units, so the button marked "100" moves
                # 10 app steps. A bare "16.7 steps" beside a button marked
                # "10" invited exactly the reading it got: that the app was
                # about to make a move smaller than the smallest button.
                #
                # It is also an AVERAGE, and the only fractional number in the
                # focus UI. Nothing ever moves 16.7 of anything:
                # `plan_focus_intervals` splits the range into whole app steps
                # and spreads the remainder, so a 50-step range over 4 photos
                # goes out as 17, 17, 16.
                # THE REAL GAPS, not their average. This printed the mean —
                # "≈16.7 steps" — which was wrong in both directions: it put
                # an approximation sign on a figure that is exact whenever the
                # range divides evenly ("≈5.0 steps" for a plan of 5, 5, 5, 5),
                # and it invented a fractional step the lens can never be
                # asked to move.
                #
                # Nothing has to be approximated, because
                # `plan_focus_intervals` gives out AT MOST TWO distinct
                # values: it hands `remainder` gaps of `base + 1` and the rest
                # `base`. So an even split is one number and an uneven one is
                # a pair, and both are the moves that will actually be issued.
                #
                # Both units, because the jog buttons a few inches away are
                # labelled in driver units while this is in app steps. No
                # explanation on the line: at the layout's floor this label is
                # 481 px wide and spelling the relationship out here wrapped
                # to a third line the box has no room for. The tooltip has it.
                plan = plan_focus_intervals(rng, photos)
                lo, hi = min(plan), max(plan)
                unit = STEP_UNITS["Small"]
                if lo == hi:
                    steps_txt = f"{lo} step{'' if lo == 1 else 's'}"
                    units_txt = f"{lo * unit} driver units"
                else:
                    steps_txt = f"{lo}–{hi} steps"
                    units_txt = f"{lo * unit}–{hi * unit} driver units"
                text += (f"\n{steps_txt} between shots  ·  {units_txt}")
        # No "set A and B" nudge when the range is unset. Start already
        # refuses to run without A and B and says so in a dialog, so this was
        # a second voice telling you the same thing before you had done
        # anything wrong — and it was the longest text this label ever held,
        # which is what pushed the ordinary case onto two lines.

        self.lbl_estimate.setText(text)
        # Colour only — every other rule lives in estimate_style(), so this
        # cannot drift from what _build_transport_card set.
        self.lbl_estimate.setStyleSheet(self.estimate_style(colour))

    # ── camera trace ─────────────────────────────────────────────────
    #: Where the trace is written, inside the save folder.
    TRACE_FILENAME = "camera_trace.log"

    def _on_debug_toggled(self, on: bool):
        """Mirror the camera's own trace into the log and a file."""
        if self._debug_file:
            try:
                self._debug_file.close()
            except Exception:
                pass
            self._debug_file = None

        if self._camera:
            self._camera.set_trace(self._trace_line if on else None)

        if not on:
            self._append_log("Camera trace off.", TEXT_MUTED)
            return

        path = Path(self.field_base.text().strip() or ".").expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            self._debug_file = open(path / self.TRACE_FILENAME, "a",
                                    encoding="utf-8")
            self._debug_file.write(
                f"\n===== session {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        except OSError as e:
            self._append_log(f"Camera trace on (log only — {e}).", ACCENT_AMBER)
            self._debug_file = None
            return

        self._append_log(f"Camera trace on → {path / self.TRACE_FILENAME}",
                         ACCENT_CYAN)
        if not self._camera:
            self._append_log("It starts once the camera is connected.",
                             TEXT_MUTED)

    def _trace_line(self, text: str):
        # Called from the worker and live-view threads; the signal marshals it
        # onto the GUI thread, which is the only one allowed to touch widgets.
        self._log_relay.emit(f"  · {text}", TEXT_MUTED)
        f = self._debug_file
        if f:
            try:
                f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
                f.flush()
            except Exception:
                pass

    def _browse_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select base folder", self.field_base.text())
        if d:
            self.field_base.setText(d)
            self._setting_changed("Base folder", "next stack")

    # ══════════════════════════════════════════════════════════════════
    # Connections
    # ══════════════════════════════════════════════════════════════════
    def _connect_camera(self):
        if not HARDWARE_AVAILABLE:
            self._append_log("Hardware layer not available "
                             "(pip install pyserial).", ACCENT_RED)
            return

        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:
                pass
            self._camera = None

        self.pill_camera.set_state("connecting…", ACCENT_AMBER)
        QApplication.processEvents()

        try:
            camera = PTPCameraClient(timeout_s=30.0)
            name = camera.verify_connection()
            self._camera = camera
            self._zoom_levels = camera.zoom_levels()
            # Now that the levels are known, put the magnifier buttons into a
            # state that matches them. Without this they keep whatever they
            # were constructed with — see `_refresh_zoom`.
            self._refresh_zoom()
            self.focus_panel.set_camera(camera)
            self.liveview.set_camera(camera)
            self.btn_liveview.setChecked(False)
            self.btn_liveview.setText("Start live view")
            if self.chk_debug.isChecked():
                camera.set_trace(self._trace_line)

            self.pill_camera.set_state("connected", ACCENT_GREEN)
            if name == CAMERA_UNKNOWN:
                self._append_log(
                    "Camera connected over USB, but it did not report a model "
                    "name — capture still works.", ACCENT_GREEN)
            else:
                self._append_log(f"Camera connected over USB — {name}.",
                                 ACCENT_GREEN)

            self.btn_connect_camera.setText("Reconnect")
            self._lv_fresh = False
            # Straight into the preview. There is nothing useful to do with a
            # connected camera except look through it, and the focus controls
            # would raise live view on the body anyway the moment they are
            # touched — so opening it here costs nothing and saves a click.
            self._start_preview()

            # Then measure the lens, unprompted. It costs a few seconds and no
            # shutter actuations, and until it has run the app knows where the
            # near stop is but not the far one — which is what lets B be set
            # past the end of the lens and turns the last frames of every
            # stack into duplicates. Doing it here rather than at Start is
            # deliberate: calibrating clears A and B.
            if self.focus_panel.auto_calibrate():
                self._append_log(
                    "Measuring the lens's focus travel — a few seconds, no "
                    "photos. Set A and B once it finishes.", ACCENT_CYAN)

        except Exception as e:                              # noqa: BLE001
            self._camera = None
            self._zoom_levels = []
            # Dead camera, dead magnifier. `live` is False here, so this only
            # disables the buttons — it issues no USB call to a body that just
            # failed to answer.
            self._refresh_zoom()
            self.focus_panel.set_camera(None)
            self.liveview.set_camera(None)
            self.pill_camera.set_state("failed", ACCENT_RED)
            # The overwhelmingly likely cause is the driver binding, and the
            # message from the transport already spells out the Zadig fix.
            self._append_log(f"Camera connection failed: {e}", ACCENT_RED)

        self._update_button_states()

    def _toggle_liveview(self, checked: bool):
        if checked:
            self._start_preview()
        else:
            self.liveview.pause("live view off")
            self.btn_liveview.setChecked(False)
            self.btn_liveview.setText("Start live view")

    def _start_preview(self) -> bool:
        """
        Open live view on the body AND start the on-screen feed.

        These are two different things and both are needed before anything
        appears. `show_liveview` is what makes the body serve frames at all;
        the preview widget runs its own paused/resumed poll thread that asks
        for them. Raise the session without resuming the thread and the camera
        streams to nobody; resume the thread without the session and it polls a
        body with nothing to give. Raising one without the other is what made a
        focus jog quietly stream frames that nothing read.

        This used to say the session was raised "because MfDrive requires it".
        That was believed for a long time and never tested; it was measured on
        this rig's Z 6_2 on 2026-08-03 and is false for this body — MfDrive is
        accepted and moves the lens with live view down, indistinguishably from
        live view up. The numbers are in the `chk_close_lv` note in
        `ui/layout.py`. What live view is genuinely needed for here is the
        picture itself, and the magnifier, which really is a live view property
        (`PTPCameraClient.LIVEVIEW_ZOOM`). Focus is not on that list.

        GUI thread only — see `_show_preview` for the worker-thread route.
        """
        if not self.liveview.start():
            self.btn_liveview.setChecked(False)
            self.btn_liveview.setText("Start live view")
            return False
        self.btn_liveview.setChecked(True)
        self.btn_liveview.setText("Stop live view")
        self._lv_fresh = True
        return True

    def _show_preview(self):
        """
        Wake the on-screen feed for a live view someone else already opened.

        Reached by signal from the focus panel's worker thread, which raises
        live view on the body before jogging. It does not call `start()`
        because the body is already streaming — this only catches the preview
        up. Never runs during a run: the worker owns the link then.
        """
        if self._camera is None or self._worker is not None:
            return
        self.liveview.resume()
        self.btn_liveview.setChecked(True)
        self.btn_liveview.setText("Stop live view")

    def _pause_liveview(self, reason: str = "paused during run"):
        """
        Stop streaming while something else owns the USB link.

        The transport serialises itself so this is not a correctness problem,
        but a run is the thing that must not be slowed down, and a preview
        polling for hours would compete with it for the one link.

        THIS is the link protection, and it runs on every run whether or not
        `chk_close_lv` is ticked. That option closes the session on the body,
        which saves the sensor and the live view pipeline rather than any
        transfers — a session nobody polls serves no frames. Do not let the two
        be conflated again; the checkbox was labelled "(faster)" on the
        strength of exactly that confusion.
        """
        self.liveview.pause(reason)
        self.btn_liveview.setChecked(False)
        self.btn_liveview.setText("Start live view")

    # ── free spin ────────────────────────────────────────────────────
    #: `CT+SETSPEED` grade for each speed button, indexed by ARROW COUNT - 1.
    #: So one arrow is the slowest and four the fastest, matching the focus
    #: row where more arrows means more movement.
    #:
    #: HIGHER GRADE IS SLOWER, which is why this tuple descends. That is
    #: measured on the rig, not read from the manual — the manual describes
    #: CHANGESPEED(1) as "increase speed (add 1 level)", but a larger
    #: SETSPEED grade visibly turns the table slower, so the grade behaves
    #: like a period rather than a rate.
    #:
    #: 1 is the fastest offered rather than 0: 1 has actually been run on
    #: this table, and the bottom of a vendor range is where special-case
    #: meanings hide. All four are far inside the protocol's 0-127, and the
    #: controller range-checks the value itself before it reaches the motor.
    #: MEASURED on this table, by a development script that is not shipped
    #: with the app:
    #:
    #:     grade    1     2     4     8    16    32    64   127
    #:     deg/s  15.7  13.7  12.2  10.0   6.9   5.3   5.3   5.3
    #:
    #: Three facts came out of that, and all three are load-bearing:
    #:
    #: GRADE 0 IS REFUSED (CR+ERR=81), so grade 1 is the fastest this table
    #: has. The four-arrow button cannot be made quicker — that is a
    #: hardware ceiling, not a tuning choice.
    #:
    #: SPEED SATURATES AT 32. Grades 64 and 127 measure identical to 32, so
    #: the usable span is 1..32 and nothing is gained by going higher.
    #:
    #: THE WHOLE RANGE IS ONLY 3x, 15.7 down to 5.3 deg/s. That is why
    #: earlier guesses felt so alike: they sat inside the top third. These
    #: four are spread evenly across the entire span instead — roughly
    #: 15.7 / 12.2 / 8.5 / 5.3 deg/s, or 23s to 68s per revolution.
    SPIN_GRADES = (32, 12, 4, 1)

    #: How long the table is given to come to rest before a run begins, if it
    #: was spinning when Start was pressed. SETSTOP is acknowledged when the
    #: command is ACCEPTED, not when the platter has stopped moving, so
    #: starting a capture on the acknowledgement alone would shoot the first
    #: stack through a subject still coasting.
    SPIN_SETTLE_S = 2.0

    def _spin(self, direction: int, level: int):
        """
        Turn at one of the four speeds. `level` is the arrow count minus one.

        Direct, not a ramp. The predecessor counted presses and started at
        full speed, so the slow end — the end this feature exists for, since
        the job is nudging a subject a few degrees to check a focus plane —
        was four clicks away. Every speed is now one click, in either
        direction, from any state.

        A TOGGLE: pressing the button that is already running stops the table.
        That is why there is no Stop button — see the free-spin block in
        layout.py for why the row is better off without one. The important
        consequence is that stopping and re-selecting go through the same key,
        so the lit button is not just a readout of what is happening, it is
        also the control that ends it. Pressing a DIFFERENT speed switches
        straight to it without stopping first: `_spin_stop` clears
        `_spin_dir`, so routing a change of speed through it would issue a
        SETSTOP the table does not need and would cost a `_resync_dial` round
        trip on the way past.
        """
        if self._tt is None or self._worker is not None:
            return
        if self._spin_dir == direction and self._spin_level == level:
            self._spin_stop()
            return
        try:
            self._tt.spin(direction, self.SPIN_GRADES[level])
        except Exception as e:                               # noqa: BLE001
            self._append_log(f"Could not turn the table: {e}", ACCENT_AMBER)
            self._spin_dir, self._spin_level = None, 0
            self._refresh_spin()
            return
        self._spin_dir, self._spin_level = direction, level
        self._refresh_spin()

    def _spin_stop(self, quiet: bool = False) -> bool:
        """
        Halt a free run and resync the dial. Returns True if it WAS spinning.

        Always safe to call: SETSTOP on a stationary table is a no-op, so
        this is used as a guard on the run path and on disconnect as well as
        by `_spin` when the lit speed button is pressed again. There is no
        Stop button — the toggle replaced it.
        """
        was_spinning = self._spin_dir is not None
        self._spin_dir, self._spin_level = None, 0
        if self._tt is not None:
            try:
                self._tt.stop()
            except Exception as e:                           # noqa: BLE001
                if not quiet:
                    self._append_log(f"Could not stop the table: {e}",
                                     ACCENT_AMBER)
        if was_spinning:
            self._resync_dial()
        self._refresh_spin()
        return was_spinning

    def _resync_dial(self):
        """
        Ask the table where it actually ended up.

        The point of the whole feature. A free spin moves the subject by an
        amount nothing counted, so the dial would otherwise be confidently
        wrong afterwards — the same silent desync the focus ring causes, and
        the reason a Stop button looked like a bad idea before the manual
        turned up GETOFFSETANGLE. The table, unlike the lens, can be asked.
        """
        if self._tt is None:
            return
        try:
            angle = self._tt.get_offset_angle()
        except Exception:                                    # noqa: BLE001
            angle = None
        if angle is None:
            return                  # firmware too old to answer; leave it be
        self._last_angle = angle % 360.0
        self.turntable_widget.set_state(
            self._positions_per_rev, 0, 0, self._last_angle)

    def _refresh_spin(self):
        """
        Enable state, and light the one speed that is actually running.

        The lit button IS the readout — there is no separate caption. It says
        three things at once: which way the table is turning, how fast, and
        which key stops it, since the buttons toggle and pressing the lit one
        is what halts the table.
        """
        live = self._tt is not None and self._worker is None
        for direction, btns in ((1, self.btns_spin_ccw),
                                (0, self.btns_spin_cw)):
            for level, b in enumerate(btns):
                b.setEnabled(live)
                on = (self._spin_dir == direction
                      and self._spin_level == level)
                # Fill only. Size, weight and border width stay put, so
                # lighting a button cannot nudge the row.
                b.setStyleSheet(self.SPIN_BTN_CSS.format(
                    accent=DARK_BG if on else ACCENT_AMBER,
                    bg=ACCENT_AMBER if on else PANEL_BG_SOFT,
                    border=ACCENT_AMBER if on else rgba(ACCENT_AMBER, 0.28),
                    hover=ACCENT_AMBER if on else rgba(ACCENT_AMBER, 0.13),
                    hover_border=ACCENT_AMBER if on
                    else rgba(ACCENT_AMBER, 0.55),
                    muted=TEXT_MUTED, soft=BORDER_SOFT, dark=DARK_BG))

    # ── live view magnifier ──────────────────────────────────────────
    #: Full sensor width in pixels, for turning a zoom region size into a
    #: magnification the user can read. The magnifier's values ARE sensor
    #: pixels — 6048/2048 = 3x, and that is what the three steps look like —
    #: and the body reports 6048x4024 as its image size, so this is its own
    #: number rather than a guess. Only the LABEL depends on it: a body with
    #: a different sensor still zooms correctly, it just annotates the steps
    #: approximately.
    SENSOR_W = 6048

    def _zoom_label(self, region: int) -> str:
        if not region:
            return "1×"
        return f"{round(self.SENSOR_W / region)}×"

    def _step_zoom(self, delta: int):
        """
        Move one step along the magnifier. `delta` is +1 in, -1 out.

        Synchronous, unlike every focus control: this is two property
        transactions and no mechanical movement, so there is nothing to wait
        for and a worker thread would only add a way to get out of order.
        """
        if self._camera is None or self._worker is not None:
            return
        levels = self._zoom_levels or self._camera.zoom_levels()
        if not levels:
            self._append_log("This camera offers no live view magnifier.",
                             ACCENT_AMBER)
            return
        self._zoom_levels = levels
        try:
            current = self._camera.get_zoom()
        except Exception:                                    # noqa: BLE001
            current = None
        try:
            index = levels.index(current)
        except ValueError:
            index = 0
        target = max(0, min(len(levels) - 1, index + delta))
        if target == index:
            return
        try:
            self._camera.set_zoom(levels[target])
        except Exception as e:                               # noqa: BLE001
            self._append_log(f"Zoom failed: {e}", ACCENT_AMBER)
            return
        # Live view has to be up for the magnifier to mean anything, and
        # set_zoom raises it if it was down — so catch the preview up, exactly
        # as a focus jog does.
        self._lv_fresh = True
        if not self.btn_liveview.isChecked():
            self._show_preview()
        self._refresh_zoom(levels[target])

    def _refresh_zoom(self, region: Optional[int] = None):
        """
        Readout and button states.

        Called from `_step_zoom` once a step has landed, `_reset_zoom` when a
        run takes the USB link, `_on_finished` when the run gives it back, and
        BOTH branches of `_connect_camera` — the success path once
        `_zoom_levels` is populated, and the failure path once it is cleared.

        The connect calls close a gap that used to be real: layout.py never
        calls setEnabled on btn_zoom_in or btn_zoom_out, so before this ran at
        connect both buttons kept the enabled state they were constructed with
        until the first zoom step landed or the first run started. With no
        camera attached the two magnifier buttons looked live and did nothing —
        the clicks died silently on the guards at the top of `_step_zoom`.

        Calling it with `region=None` while a camera is live costs one
        GetDevicePropValue round trip to ask the body where the magnifier
        actually is, rather than assuming 1x. On the failure branch `live` is
        False, so no call is made and both buttons simply go dead.
        """
        live = self._camera is not None and self._worker is None
        if region is None and live:
            try:
                region = self._camera.get_zoom()
            except Exception:                                # noqa: BLE001
                region = None
        levels = self._zoom_levels or []
        index = levels.index(region) if region in levels else 0
        self.lbl_zoom.setText(self._zoom_label(region or 0))
        # Written out in full rather than patched. This used to prepend
        # `styleSheet().split("color:")[0]`, meaning to keep whatever came
        # before the colour rule and replace the rest — but layout.py builds
        # this label with `color:` as the very first declaration, so that
        # split always returned "" and the "kept" part was nothing. What it
        # actually did was silently drop the label from the 12px it is
        # constructed at to 10px, the first time anything refreshed it. Size
        # and family now match layout.py deliberately; only the colour moves.
        self.lbl_zoom.setStyleSheet(
            f"color: {ACCENT_CYAN if region else TEXT_MUTED};"
            f"font-size: 12px; font-weight: 700;"
            f"font-family: 'Cascadia Mono', Consolas, monospace;")
        self.btn_zoom_in.setEnabled(live and index < max(len(levels) - 1, 0))
        self.btn_zoom_out.setEnabled(live and index > 0)

    def _reset_zoom(self):
        """
        Drop the magnifier back to 1x. Called when a run starts.

        A run closes live view and holds it closed to the end (`chk_close_lv`
        in `ui/layout.py`), and the fresh session the next focus edit opens
        would come back magnified. That reads as a badly framed subject — the
        two look identical once you have looked away. Cheap to put back, so
        put it back.
        """
        if self._camera is None or not self._zoom_levels:
            return
        try:
            if self._camera.get_zoom():
                self._camera.set_zoom(0)
        except Exception:                                    # noqa: BLE001
            pass                    # never let tidying up break a run
        self._refresh_zoom(0)

    def _invalidate_liveview(self):
        """Something else closed LiveView; forget that we had one open."""
        with self._lv_lock:
            self._lv_fresh = False

    def _ensure_fresh_lv_for_focus(self):
        """
        Called on the focus panel's worker thread the moment the user starts
        adjusting focus. Opens a live-view session, wakes the on-screen preview
        to match, and leaves both up for the rest of the focus editing;
        starting a run closes it and clears the flag, so the next focus edit
        gets a fresh one.

        THE REASON IS THE SCREEN, NOT THE MOTOR. This used to say focus
        commands are inert without a live-view session. That was believed for
        a long time and never tested; measured on this rig's Z 6_2 on
        2026-08-03, it is false for this body — MfDrive is accepted and moves
        the lens with live view down, identically to live view up, and capture
        proved just as indifferent (numbers in the `chk_close_lv` note in
        `ui/layout.py`). What is true is that A and B are set by eye, so a jog
        nobody can see is useless, and that the hardware client would open a
        session behind the window's back on the first focus command without
        telling anyone.

        That second half is narrower than it read here until now, in two ways,
        and the difference is worth stating precisely. Focus commands go
        through `PTPCameraClient._ensure_for_focus`, which only falls through
        to `_ensure_liveview` while no run is holding the session shut
        (`PTPCameraClient.hold_liveview_closed`) — academic on this path, since
        the focus panel is disabled for the length of a run, but no longer
        something to describe as unconditional. And `_ensure_liveview` itself
        starts a session only when the body is not already streaming; it is a
        top-up, not an unconditional restart. What remains true is the case
        this method exists for: the first focus command after a connect or
        after a run, when nothing is streaming and something is about to be.

        Leave it to the client then and the body streams while the button still
        reads "Start live view" and the feed stays paused: the lens jogs with a
        dead panel above it. Opening it here instead, under `_lv_lock` and
        followed by `_lv_started`, is what keeps the preview and the body's
        session agreeing.
        """
        with self._lv_lock:
            if self._lv_fresh or self._camera is None:
                return
            self._log_relay.emit("Opening live view for focus…", ACCENT_CYAN)
            try:
                # show_liveview() blocks on DeviceReady, so there is nothing
                # left to settle for once it returns.
                self._camera.show_liveview()
            except Exception as e:                          # noqa: BLE001
                self._log_relay.emit(f"Could not open live view: {e}",
                                     ACCENT_AMBER)
                return
            self._lv_fresh = True
            # The body is streaming now; wake the preview so the frames are
            # actually shown rather than pulled and dropped.
            self._lv_started.emit()

    def _connect_tt(self) -> bool:
        if not HARDWARE_AVAILABLE:
            self._append_log("Hardware layer not available "
                             "(pip install pyserial).", ACCENT_RED)
            return False
        if self._status in ("running", "paused", "nocamera", "notable"):
            self._append_log("Cannot reconnect the turntable during a run.",
                             ACCENT_AMBER)
            return False

        if self._tt is not None:
            try:
                self._tt.close()
            except Exception:
                pass
            self._tt = None
            self._spin_dir, self._spin_level = None, 0
            self._refresh_spin()

        port = self.field_com.text().strip()
        baud = int(self.field_baud.currentText())
        self.pill_tt.set_state("connecting…", ACCENT_AMBER)
        QApplication.processEvents()

        try:
            tt = ComximClient(port, baud)
            tt.init_session()
            # Beeper off for the session. It only sounds on a command the
            # controller rejects, which should be never — but "should be
            # never" beside a rig running for hours is exactly the noise
            # nobody wants, and it is put back by a power cycle.
            tt.mute(True)
            self._tt = tt
            self._refresh_spin()
            self.pill_tt.set_state(f"{port} @ {baud}", ACCENT_GREEN)
            self._append_log(f"Turntable connected on {port} at {baud} baud.",
                             ACCENT_GREEN)
            self.btn_connect_tt.setText("Reconnect")
            self._update_button_states()
            return True
        except Exception as e:                              # noqa: BLE001
            self._tt = None
            self._spin_dir, self._spin_level = None, 0
            self._refresh_spin()
            self.pill_tt.set_state("failed", ACCENT_RED)
            self._append_log(f"Turntable connection failed: {e}", ACCENT_RED)
            self._update_button_states()
            return False

    # ══════════════════════════════════════════════════════════════════
    # Session control
    # ══════════════════════════════════════════════════════════════════
    def _start_session(self):
        if self._status in ("running", "paused", "nocamera", "notable"):
            return
        if not HARDWARE_AVAILABLE:
            self._append_log("Bridge module not found.", ACCENT_RED)
            return
        if not self._camera:
            QMessageBox.warning(self, "Camera not connected",
                                "Press Connect on the camera before starting.")
            return

        focus_range = self.focus_panel.get_range()
        if not focus_range:
            QMessageBox.warning(
                self, "Focus not set",
                "Set both A (near) and B (far) focus points first.\n\n"
                "Jog the focus to the nearest part of the subject and press "
                "Set A, then to the farthest part and press Set B.")
            return

        # Settle the focus plan before touching any hardware — there is no
        # point opening the serial port to then refuse the run.
        photos_wanted = self.spin_photos.value()
        if photos_wanted > 1 and focus_range < photos_wanted - 1:
            usable = focus_range + 1
            answer = QMessageBox.question(
                self, "More photos than focus steps",
                f"A→B is {focus_range} steps wide, but you have asked for "
                f"{photos_wanted} photos.\n\n"
                f"The gap between shots is a whole number of steps, so only "
                f"{usable} shots can land on a different focus position — "
                f"the rest would be duplicates at the far end.\n\n"
                f"Shoot {usable} photos instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.spin_photos.setValue(usable)
            self._push_live()

        if self._tt is None and not self._connect_tt():
            QMessageBox.warning(
                self, "Turntable not connected",
                f"Could not open {self.field_com.text()}. "
                f"Check the cable and the COM port.")
            return

        base = Path(self.field_base.text().strip()).expanduser()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Bad folder",
                                f"Cannot use {base}:\n{e}")
            return

        self._push_live()

        photos = self.spin_photos.value()
        self._append_log(
            f"Session start — subject '{self._safe_stack_name()}', "
            f"{photos} photos over {focus_range} focus steps, "
            f"{self._rotation_geometry()[0] + 1} stacks per revolution.",
            ACCENT_GREEN)
        # A fresh run supersedes any previous checkpoint.
        self._last_checkpoint = None
        self._spawn_worker()

    def _spawn_worker(self, resume_from: "Optional[RunCheckpoint]" = None,
                      recapture_first: bool = False,
                      _settled: bool = False):
        """
        Build, wire and start a BridgeWorker. Shared by a fresh start
        (resume_from=None) and a Recover (resume_from=checkpoint), so both
        take the exact same signal wiring and progress setup — the only
        difference is where the run begins and whether the first stack is
        re-captured.

        THE TABLE MUST BE STILL FIRST. Pressing Start while a free spin is
        running would shoot the opening stack through a moving subject, and
        SETSTOP is acknowledged when the command is ACCEPTED rather than when
        the platter has stopped — so stopping and starting in the same breath
        would not be enough. This stops it, gives it SPIN_SETTLE_S to coast
        down, and only then re-enters with `_settled`.

        The guard is here rather than in `_start_session` precisely because
        this is the shared path: Recover can be pressed with the table
        spinning just as easily as Start can. Costs nothing on the normal
        path — `_spin_stop` returns False when nothing was turning, and the
        run proceeds without any wait.
        """
        if not _settled and self._spin_stop():
            self._append_log(
                f"The table was still turning — stopped it and waiting "
                f"{self.SPIN_SETTLE_S:.0f}s for it to come to rest before "
                f"the first stack.", ACCENT_AMBER)
            QTimer.singleShot(
                int(self.SPIN_SETTLE_S * 1000),
                lambda: self._spawn_worker(resume_from, recapture_first,
                                           _settled=True))
            return

        photos = self.spin_photos.value()
        self._positions_per_rev = self._rotation_geometry()[0]
        self._stacks_per_round = self._positions_per_rev + 1
        rounds = self.field_rounds.value()
        self._total_stacks = self._stacks_per_round * (rounds if rounds > 0 else 1)

        # On a resume, seed the bars/counters to what's already been shot so the
        # Total bar and ETA continue rather than restarting from zero.
        done_before = 0
        rev_done = 0
        stack_at = 0
        angle_at = 0.0
        if resume_from is not None:
            rev_done = max(0, resume_from.round_idx - 1)
            stack_at = max(0, resume_from.stack_in_round)
            angle_at = resume_from.cumulative_angle
            done_before = rev_done * self._stacks_per_round + stack_at
            done_before = max(0, min(done_before, self._total_stacks))

        self._set_bar(self.pb_stack, 0, photos)
        self._set_bar(self.pb_round, stack_at, self._stacks_per_round)
        self._set_bar(self.pb_revolution, rev_done, rounds if rounds > 0 else 1)
        if rounds <= 0:
            self.pb_revolution._value_label.setText(f"{rev_done} / ∞")
        self._set_bar(self.pb_total, done_before, self._total_stacks)
        pct = int(100 * done_before / self._total_stacks) if self._total_stacks else 0
        self._set_total_text(f"{pct}%")

        self._elapsed = 0
        self._current_round = resume_from.round_idx if resume_from else 0
        self._current_stack = stack_at
        self._stacks_done = done_before
        self._first_stack_secs = None
        self._eta_seconds = None
        # The worker closes LiveView for the run and holds it closed until the
        # run ends; the first focus edit after that opens a fresh one.
        self._lv_fresh = False
        self._stack_times.clear()
        self._last_angle = angle_at
        self.turntable_widget.set_state(self._positions_per_rev,
                                        stack_at, stack_at, angle_at)
        self.pill_elapsed.set_state("00:00", ACCENT_GREEN)

        self._worker = BridgeWorker(
            camera=self._camera, tt=self._tt, live=self._live,
            start_pos=self.focus_panel.get_current_position(),
            resume_from=resume_from, recapture_first=recapture_first,
        )
        w = self._worker
        w.signals.log.connect(self._append_log)
        w.signals.round_changed.connect(self._on_round)
        w.signals.stack_changed.connect(self._on_stack)
        w.signals.images_changed.connect(self._on_images)
        w.signals.angle_changed.connect(self._on_angle)
        w.signals.focus_pos.connect(self.focus_panel.set_position)
        w.signals.stack_done.connect(self._on_stack_done)
        w.signals.revolution_done.connect(self._on_revolution_done)
        w.signals.status_changed.connect(self._on_status)
        w.signals.checkpoint_changed.connect(self._on_checkpoint)
        w.signals.finished.connect(self._on_finished)
        w.start()

        self._status = "running"
        self._style_state_pill()
        # The preview competes with the run for the one USB link. The transport
        # serialises access so this is not a correctness problem, but a run
        # takes hours and is the thing that must not be slowed down.
        self._reset_zoom()
        self._pause_liveview("paused during run")
        self.focus_panel.set_enabled(False)
        self.btn_resume_run.setVisible(False)
        self._update_button_states()

    def _resume_run(self):
        """Recover an interrupted run from the last checkpoint."""
        cp = self._last_checkpoint
        if cp is None:
            return
        if self._status in ("running", "paused", "nocamera", "notable"):
            return
        if not HARDWARE_AVAILABLE:
            self._append_log("Hardware layer not available "
                             "(pip install pyserial).", ACCENT_RED)
            return

        # Reconnect the turntable if its port is gone (unplugged, or a USB
        # reset). _connect_tt reopens it from the COM/baud fields.
        if self._tt is None or not self._tt.is_open:
            self._append_log("Recover: reconnecting the turntable…", ACCENT_CYAN)
            if not self._connect_tt():
                QMessageBox.warning(
                    self, "Turntable not connected",
                    f"Could not open {self.field_com.text()} to resume.\n"
                    f"Check the cable and the COM port, then press Recover "
                    f"again.")
                return

        # Reconnect the camera, or drop the existing transport so the first
        # command of the resumed run goes out on a fresh one.
        if self._camera is None:
            self._append_log("Recover: connecting to the camera…", ACCENT_CYAN)
            self._connect_camera()
            if self._camera is None:
                QMessageBox.warning(
                    self, "Camera not connected",
                    "Could not reach the camera to resume.\n"
                    "Check the USB cable and that the body is awake, then "
                    "press Recover again.")
                return
        else:
            try:
                self._camera.close()
            except Exception:
                pass
            try:
                st = self._camera.camera_status()
            except Exception:              # noqa: BLE001
                st = "unreachable"
            if st != "connected":
                self._append_log(
                    "Recover: camera not detected yet — the run will start "
                    "and then pause with the alarm until it is reconnected.",
                    ACCENT_AMBER)

        self._push_live()
        self._append_log(
            f"Recovering run — revolution {cp.round_idx}, position "
            f"{cp.stack_in_round + 1}. Returning focus to A, re-capturing "
            f"this stack (marked _recaptured), then continuing.", ACCENT_CYAN)
        self._spawn_worker(resume_from=cp, recapture_first=True)

    def _on_checkpoint(self, cp):
        """A worker reported a new resume point; remember it for Recover."""
        self._last_checkpoint = cp

    def _toggle_pause(self):
        if self._worker:
            self._worker.toggle_pause()

    def _stop_session(self):
        if self._worker:
            self._append_log("Stopping…", ACCENT_AMBER)
            self._worker.request_stop()

    def _on_finished(self):
        self._worker = None
        self.focus_panel.set_enabled(True)
        self._refresh_spin()
        self._refresh_zoom()
        if self._status not in ("complete", "error"):
            self._status = "idle"
        # Don't reopen LiveView here — it opens a fresh one automatically the
        # moment the user touches a focus control for the next subject. That
        # works because `BridgeWorker.run` releases its hold_liveview_closed in
        # the finally that precedes this signal, so focus commands can raise a
        # session again by the time anyone can issue one.
        self._lv_fresh = False
        # If the run didn't finish cleanly and we have a checkpoint, offer
        # Recover so the user can pick up where it left off.
        if self._status != "complete" and self._last_checkpoint is not None:
            cp = self._last_checkpoint
            self._append_log(
                f"Run interrupted at revolution {cp.round_idx}, position "
                f"{cp.stack_in_round + 1}. Fix the problem (reconnect the "
                f"camera or turntable, swap the card or battery…), then "
                f"press RECOVER to resume from here.", ACCENT_AMBER)
        self._update_button_states()

    # Display label + colour for each worker state, shared by the header pill
    # (styled immediately on change and again on the 1-second tick).
    _STATE_PILL = {
        "idle":     ("idle",        TEXT_MUTED),
        "running":  ("running",     ACCENT_GREEN),
        "paused":   ("paused",      ACCENT_AMBER),
        "nocamera": ("camera lost", ACCENT_RED),
        "notable":  ("table lost",  ACCENT_RED),
        "complete": ("complete",    ACCENT_GREEN),
        "error":    ("error",       ACCENT_RED),
    }

    def _style_state_pill(self):
        label, colour = self._STATE_PILL.get(
            self._status, (self._status, TEXT_MUTED))
        self.pill_state.set_state(label, colour)
        self._pill_state_text = self._status

    def _on_status(self, status):
        self._status = status
        # Style the pill right away so a camera drop-out is visible instantly
        # rather than up to a second later on the next tick.
        self._style_state_pill()
        if status == "complete":
            # Top every bar off — the run genuinely finished.
            self._set_bar(self.pb_stack, self.pb_stack.maximum())
            self._set_bar(self.pb_round, self.pb_round.maximum())
            self._set_bar(self.pb_revolution, self.pb_revolution.maximum())
            self._set_bar(self.pb_total, self.pb_total.maximum())
            self._set_total_text("100%   ·   done")
            # Nothing left to recover once the run completes cleanly.
            self._last_checkpoint = None
        self._update_button_states()

    def _on_revolution_done(self, r):
        # Fill the revolutions bar as each revolution finishes.
        rounds = self.field_rounds.value()
        self._set_bar(self.pb_revolution, r)
        if rounds <= 0:
            self.pb_revolution._value_label.setText(f"{r} / ∞")

    def _on_round(self, r):
        self._current_round = r
        rounds = self.field_rounds.value()
        target = rounds if rounds > 0 else max(r, 1)
        # Completed revolutions = r - 1, so the bar fills up as revolutions
        # finish (empty at the start of revolution 1).
        self._set_bar(self.pb_revolution, r - 1, target)
        if rounds <= 0:
            self.pb_revolution._value_label.setText(f"{r - 1} / ∞")
        self._set_bar(self.pb_round, 0)

    def _on_stack(self, index_in_round, stacks_per_round):
        self._current_stack = index_in_round
        self._stacks_per_round = stacks_per_round
        self._positions_per_rev = stacks_per_round - 1
        self._set_bar(self.pb_round, index_in_round, stacks_per_round)
        self._set_bar(self.pb_stack, 0)
        self.turntable_widget.set_state(
            self._positions_per_rev, index_in_round, index_in_round,
            self._last_angle)

    def _on_images(self, done, total):
        self._set_bar(self.pb_stack, done, total)

    def _on_angle(self, a):
        self._last_angle = a
        self.turntable_widget.set_state(
            self._positions_per_rev,
            self._current_stack + 1,
            self._current_stack + 1,
            a)

    def _on_stack_done(self, secs: float):
        self._stack_times.append(secs)
        self._stacks_done += 1
        if self._first_stack_secs is None:
            self._first_stack_secs = secs
        self._set_bar(self.pb_round, self._current_stack + 1)
        self._set_bar(self.pb_total, self._stacks_done)
        # Re-estimate from the rolling average whenever a stack finishes,
        # and re-sync the live countdown to it.
        if self.field_rounds.value() > 0 and self._stack_times:
            avg = sum(self._stack_times) / len(self._stack_times)
            self._eta_seconds = avg * max(self._total_stacks - self._stacks_done, 0)
        self._show_eta()

    def _show_eta(self):
        """Paint the Total bar: percent + the live remaining-time countdown."""
        pct_val = int(100 * self._stacks_done / self._total_stacks) \
            if self._total_stacks else 0
        if self._status not in ("running", "paused"):
            self._set_total_text(f"{pct_val}%")
            return
        if self.field_rounds.value() <= 0:
            self._set_total_text(f"{pct_val}%   ·   ∞")
            return
        if self._eta_seconds is None:
            self._set_total_text(f"{pct_val}%   ·   estimating…")
            return
        self._set_total_text(
            f"{pct_val}%   ·   ~{self._fmt_clock(int(self._eta_seconds))} left")

    # ── periodic ─────────────────────────────────────────────────────
    def _tick(self):
        if self._status == "running":
            self._elapsed += 1
            # Count the estimate DOWN in real time. It re-syncs to the
            # rolling average each time a stack finishes. Frozen while
            # paused/holding (this branch only runs when "running").
            if self._eta_seconds is not None and self._eta_seconds > 0:
                self._eta_seconds = max(0.0, self._eta_seconds - 1)
        # Elapsed lives in the header pill now.
        self.pill_elapsed.set_text_only(self._fmt_clock(self._elapsed))
        if self._status in ("running", "paused"):
            self._show_eta()

        # Keep the session pill in step with the worker. Only restyle on an
        # actual change — rebuilding a stylesheet every second is not free.
        if self._status != self._pill_state_text:
            self._style_state_pill()

    @staticmethod
    def _fmt_clock(total: int) -> str:
        m, s = divmod(int(max(total, 0)), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    # ── log ──────────────────────────────────────────────────────────
    def _append_log(self, text, color=TEXT_SECONDARY):
        ts = time.strftime("%H:%M:%S")
        safe = (str(text).replace("&", "&amp;")
                         .replace("<", "&lt;").replace(">", "&gt;"))
        self.log_view.append(
            f'<span style="color:{TEXT_MUTED}">{ts}</span>&nbsp;'
            f'<span style="color:{color}">{safe}</span>')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    # ── button state ─────────────────────────────────────────────────
    def _update_button_states(self):
        active = self._status in ("running", "paused", "nocamera", "notable")
        self.btn_start.setEnabled(not active and HARDWARE_AVAILABLE)
        # Pause only applies while the run is actually turning; while it is
        # blocked waiting for a dropped device, Stop is the only lever.
        self.btn_pause.setEnabled(self._status in ("running", "paused"))
        self.btn_stop.setEnabled(active)
        self.btn_connect_tt.setEnabled(not active)
        self.btn_connect_camera.setEnabled(not active)
        for w in (self.field_com, self.field_baud):
            w.setEnabled(not active)

        # Recover appears only when a run is not active AND there's a
        # checkpoint to pick up from (i.e. a run was interrupted).
        can_recover = (not active and HARDWARE_AVAILABLE
                       and self._last_checkpoint is not None)
        self.btn_resume_run.setVisible(can_recover)
        self.btn_resume_run.setEnabled(can_recover)

        paused = self._status == "paused"
        self.btn_pause.setText("▶   RESUME" if paused else "⏸   PAUSE")
        # BOTH sheets carry the transport height. Setting a stylesheet REPLACES
        # the widget's previous one rather than merging into it, so every sheet
        # applied to this button has to restate min-height or the button snaps
        # back to theme.py's global 20 px the first time the run state changes.
        # That is exactly what happened: Pause sat 20 px shorter than Start and
        # Stop beside it, because layout.py set the height once at build time
        # and this method quietly threw it away on the next update.
        h = self.TRANSPORT_BTN_MIN_H
        if paused:
            self.btn_pause.setStyleSheet(f"""
                QPushButton {{ background-color: #92400e; color: #fde68a;
                               border: 1px solid {rgba(ACCENT_AMBER, 0.55)};
                               border-radius: 9px; font-size: 14px;
                               min-height: {h}px; }}
                QPushButton:hover {{ background-color: #78350f; }}
            """)
        else:
            self.btn_pause.setStyleSheet(f"""
                QPushButton {{ background-color: {PANEL_BG_SOFT};
                               color: {TEXT_PRIMARY};
                               border: 1px solid {BORDER}; border-radius: 9px;
                               min-height: {h}px; }}
                QPushButton:hover {{ background-color: {FIELD_BG}; }}
                QPushButton:disabled {{ background-color: {DARK_BG};
                                        color: {TEXT_MUTED};
                                        border-color: {BORDER_SOFT}; }}
            """)

    # ── shutdown ─────────────────────────────────────────────────────
    def closeEvent(self, event):
        """
        Shut the worker, the preview thread and both device handles down
        before the window goes, so nothing is left driving the camera or
        holding the serial port.
        """
        # Written before anything can refuse the close, so the size is kept
        # even if the user backs out of the "session running" prompt below and
        # the window survives. It is also already on disk from the debounce
        # timer; this is the belt to that braces.
        self._save_geometry()
        if self._worker is not None and self._worker.isRunning():
            answer = QMessageBox.question(
                self, "Session running",
                "A capture session is still running. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.request_stop()
            self._worker.wait(10000)

        # Stop the preview thread before the camera it reads from goes away,
        # or it polls a closed transport on its way out.
        try:
            self.liveview.shutdown()
        except Exception:
            pass

        if self._tt is not None:
            try:
                self._tt.close()
            except Exception:
                pass
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:
                pass
        if self._debug_file is not None:
            try:
                self._debug_file.close()
            except Exception:
                pass
        event.accept()
