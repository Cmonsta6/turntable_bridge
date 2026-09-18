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
from ..hardware.images import list_image_names
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
    # And again for the single shot, which fires on a thread of its own so the
    # window does not freeze for the length of a frame. The path written, or
    # the reason nothing was.
    _shot_done = Signal(str)
    _shot_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._log_relay.connect(self._append_log)
        self._lv_started.connect(self._show_preview)
        self._shot_done.connect(self._on_shot_done)
        self._shot_failed.connect(self._on_shot_failed)
        # A single shot is in flight. Read by `_update_button_states`, which
        # runs before the first shot can ever be taken, so it is set here
        # rather than lazily.
        self._shooting = False
        self._shot_resume_preview = False
        # A user-driven reconnect is in flight. Separate from `_shooting`
        # because they are separate facts — `_after_shot` restores the preview
        # and a reconnect must not — but both hold the same link, so both are
        # read through `_link_busy`.
        self._reconnecting = False
        # Last `_run_is_parked()` the tick saw, so the buttons are refreshed
        # when a paused run actually comes to rest rather than every second.
        self._parked_seen = False
        # Has Stop been asked for on the current run? Drives the escalation in
        # `_stop_session`: a second press cuts links rather than repeating a
        # request the run has already proved it cannot hear.
        self._stop_requested = False
        # Why the last pre-run SETSTOP failed, or None. Read by
        # `_table_link_ready` — a table that will not take a command before the
        # run starts will not take one during it either.
        self._tt_stop_error: Optional[str] = None
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
        # After the greeting so its note reads as a follow-up rather than the
        # first thing in the log, and unconditional — the rotation is a display
        # setting, so it applies whether or not the hardware layer imported.
        self._restore_rotation()

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
    #: Preview rotation, remembered because it describes the RIG rather than a
    #: session — a camera bolted in portrait is still in portrait tomorrow, and
    #: re-setting it on every launch would be a chore with no purpose. Kept
    #: beside the geometry key because it is the same kind of fact: how the
    #: window should look, not what the run should do. Deliberately NOT in
    #: `_collect_settings`; the worker has no use for it and must not acquire
    #: one, since nothing about a saved frame depends on it.
    LIVEVIEW_ROTATION_KEY = "liveview/rotation"

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

        The content widget's own sizeHint is the whole answer: the Set up bar
        contributes its height, the viewer row the taller of its three columns
        — which is the preview's generous `sizeHint`, so the window asks for
        as much height as the screen will give — and the width is the two
        side columns plus what the preview would like.

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

    # NOTE ON THE LETTERBOX, because it was removed, put back, and has now
    # been removed a second time by a different route. The live-view card
    # used to span all three grid columns as a band, which lined the panels
    # beneath it up with the ones above but left the frame drawing at a
    # fraction of a box far wider than 3:2 — and a seventh of it when rotated
    # to portrait. The card is now the whole MIDDLE COLUMN instead, and its
    # width is capped on every resize and rotation at what its frame can fill
    # (`layout._fit_viewer_width`), with the side columns absorbing the rest.
    # The full reasoning is the map at the top of layout.py.

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
    def _link_busy(self) -> bool:
        """
        Is the window itself holding a device link right now?

        The union of the two things it can be doing on its own account — a
        single shot and a user-driven reconnect. Both take a camera link that
        something else may want, so everything that would compete is held off
        for the length of either, and neither may start while the other runs.
        """
        return self._shooting or self._reconnecting

    def _run_is_parked(self) -> bool:
        """
        Is a run in progress AND sitting genuinely still?

        True while a paused run — whether the user pressed Pause or a
        revolution ended on a hold — is waiting inside `BridgeWorker.parked`,
        which is only set between device operations. That property carries the
        reasoning, including why the drop-out waits ("nocamera"/"notable")
        deliberately do not count.

        This is what makes the two Reconnect buttons live mid-run, and it is
        checked again inside each of them rather than trusted from the button
        state: a run can leave the parked state at any moment (the user presses
        Resume) and a click already in flight would arrive just after.
        """
        w = self._worker
        return (w is not None and self._status == "paused"
                and not self._link_busy() and w.parked)

    def _connect_camera(self):
        if not HARDWARE_AVAILABLE:
            self._append_log("Hardware layer not available "
                             "(pip install pyserial).", ACCENT_RED)
            return

        # A run is up and at rest: re-open the link the run is already holding
        # rather than building a replacement it would never see.
        if self._worker is not None:
            self._reconnect_camera_in_run()
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

    def _reconnect_camera_in_run(self):
        """
        Re-open the camera link while a paused run holds it.

        IN PLACE, ON THE SAME CLIENT OBJECT, which is the whole reason this is a
        separate method rather than a flag on `_connect_camera`. `BridgeWorker`
        is handed `camera=self._camera` at construction and keeps that
        reference for its lifetime, so building a fresh `PTPCameraClient` here
        and assigning it to `self._camera` would leave the run driving the old,
        closed one — a reconnect that appears to work, reports success, and
        breaks the run at its next frame. `close()` plus a `verify_connection()`
        that re-opens through `_ensure` gives the identical result without
        touching identity, and is what `_recover_camera` already does.

        IT DOES MUCH LESS THAN A COLD CONNECT, deliberately. The full path also
        starts the preview, re-reads the magnifier steps and kicks off an
        automatic lens calibration — and that last one drives the lens to both
        mechanical stops and clears A and B. Mid-run that is not a reconnect,
        it is a destroyed session. The preview is no better: `liveview.start()`
        calls `show_liveview()` directly, which does not go through the run's
        `hold_liveview_closed`, so it would raise a live-view session the user
        explicitly asked to keep shut for the run and compete with it for the
        link. None of that belongs here. What is left is the link itself, the
        pill and a log line.

        Nothing is re-pointed at the camera either (`focus_panel.set_camera`,
        `liveview.set_camera`) because they already hold this same object.
        """
        if not self._run_is_parked():
            self._append_log(
                "The run has not come to rest yet — wait for it to finish the "
                "frame it is on, then reconnect.", ACCENT_AMBER)
            return
        if self._camera is None:                    # cannot happen with a run up
            return

        self.pill_camera.set_state("reconnecting…", ACCENT_AMBER)
        # Latched because of the processEvents below: without it, a second click
        # queued while the pill repainted would be delivered straight into a
        # reconnect already under way.
        self._reconnecting = True
        self._update_button_states()
        QApplication.processEvents()
        try:
            self._camera.close()
            name = self._camera.verify_connection()
            self.pill_camera.set_state("connected", ACCENT_GREEN)
            self._append_log(
                f"Camera link re-opened mid-run — "
                f"{'no model reported' if name == CAMERA_UNKNOWN else name}. "
                f"The run picks it up from here; press Resume when ready.",
                ACCENT_GREEN)
            # Worth saying out loud, because it is invisible and it is the one
            # way a successful reconnect can still ruin a run. Switching the
            # body off and on is the usual reason to press this, and a body that
            # has been power-cycled may have parked its lens — which moves the
            # focus plane without moving the app's step counter, so A and B now
            # point somewhere else. Nothing here can detect that: PTP reports no
            # absolute lens position, which is why the counter exists at all.
            self._append_log(
                "If the camera was switched off and on, the lens may have "
                "parked itself — A and B would then be measured from the wrong "
                "place. Stop, press Home, and set them again if the next stack "
                "looks wrong.", ACCENT_AMBER)
        except Exception as e:                              # noqa: BLE001
            # The client is left CLOSED, not None: the worker holds this object
            # and `_ensure` re-opens it on the next command, so a failure here
            # costs nothing the run's own recovery cannot still fix.
            self.pill_camera.set_state("failed", ACCENT_RED)
            self._append_log(
                f"Could not re-open the camera: {e}. The run will keep trying "
                f"by itself when it resumes.", ACCENT_RED)
        finally:
            self._reconnecting = False
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

    # ── single shot ──────────────────────────────────────────────────
    def _singleshot_folder(self) -> Path:
        """
        Where the single-shot button writes: <base>/<subject>_singleshots.

        BESIDE the run's tree, not inside it. A run builds
        base/subject/subject_rev-N/subject_rev-N_pos-NNN, and everything in a
        position folder is treated as one focus stack — `worker.run` already
        warns when it finds stale frames there and says they "will be mixed in
        with this stack". A test exposure dropped anywhere under `subject/`
        would be exactly that kind of contamination, with the added confusion
        of being a frame nobody remembers shooting. At the top level, next to
        `subject/`, it still says which subject it belongs to while being
        somewhere no run ever looks.
        """
        base = Path(self.field_base.text().strip() or Path.home()).expanduser()
        return base / f"{self._safe_stack_name()}_singleshots"

    def _single_shot(self):
        """
        Fire one frame, now, into <base>/<subject>_singleshots.

        The table does not turn and the focus does not move: this captures
        exactly what the preview is showing. It is the test exposure you want
        before committing to a run — check the light, the framing, and that the
        focus plane you just set is actually where you think it is.

        OFF THE GUI THREAD, like every focus control, because a capture is not
        quick. `ptp.capture` waits out the exposure AND the image commit, and
        its budget for the latter is CAPTURE_SETTLE_S (180 s); inline, that
        would freeze the window for the whole frame.

        THE PREVIEW IS PAUSED FOR THE DURATION, which is not tidiness. The
        transport serialises one TRANSACTION at a time, not one sequence, so a
        live-view poll can land between `capture()` announcing the SDRAM object
        and `shoot()` reading it back — the window
        LiveViewProhibitCondition bit 12, "pending unretrieved SDRAM image",
        exists to describe. A run avoids this by pausing the feed for its whole
        length (`_pause_liveview`); this pauses it for a second or two and puts
        it straight back.

        The live-view SESSION is left alone, and only the poll is stopped,
        because capture does not care either way: measured on this rig's Z 6_2
        on 2026-08-03, two consecutive frames with the stream down came out as
        real 31.6 MB NEFs matching a stream-up control, with 0xD1A4 clean after
        every one (numbers in the `chk_close_lv` note in ui/layout.py). Closing
        the session would only cost the next focus command a state change to
        wait out.

        The focus panel is locked for the same reason the feed is paused — see
        `FocusPanel.busy`.
        """
        if self._shooting:
            return
        if self._camera is None:
            self._append_log("Connect the camera before taking a single shot.",
                             ACCENT_AMBER)
            return
        if self._worker is not None:
            self._append_log(
                "A session is running — it owns the USB link. Single shots are "
                "for before or after a run.", ACCENT_AMBER)
            return
        if self.focus_panel.busy:
            self._append_log(
                "A focus move is still finishing — take the single shot again "
                "in a moment.", ACCENT_AMBER)
            return

        folder = self._singleshot_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Bad folder",
                                f"Cannot write to {folder}:\n{e}")
            return

        # Numbered from what is already in the folder, so the second shot does
        # not read as the first. A STARTING POINT ONLY: `ptp.shoot` adds a
        # numeric suffix rather than overwriting, so a miscount here costs a
        # name and never a frame.
        stem = (f"{self._safe_stack_name()}_single_"
                f"{len(list_image_names(folder)) + 1:03d}")

        if self.chk_keep_on_cam.isChecked():
            self._append_log(
                "'Keep photos on the camera card' is on for runs, but a single "
                "shot is always pulled to the PC — otherwise there would be "
                "nothing in the folder.", ACCENT_CYAN)

        self._shooting = True
        self._shot_resume_preview = self.liveview.streaming
        if self._shot_resume_preview:
            self.liveview.pause("taking a single shot…")
        self.focus_panel.set_enabled(False)
        # Disabled, not relabelled. The header's two ends were given matching
        # minimum widths at build time from their own sizeHints
        # (`Card.center_header_group`), so a button that changed width mid-shot
        # would nudge the centred lens-button group sideways and back.
        self._update_button_states()
        self._append_log(f"Single shot → {folder.name}/{stem}", ACCENT_AMBER)

        camera = self._camera

        def _run():
            try:
                self._shot_done.emit(str(camera.capture_to(folder, stem)))
            except Exception as e:                          # noqa: BLE001
                self._shot_failed.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _after_shot(self):
        """Give the link back to the preview and the focus panel."""
        self._shooting = False
        self.focus_panel.set_enabled(True)
        # Only if this shot was the thing that stopped it, and only if nothing
        # else has taken the link meanwhile.
        if self._shot_resume_preview and self._worker is None:
            self.liveview.resume()
        self._shot_resume_preview = False
        self._update_button_states()

    def _on_shot_done(self, path: str):
        self._after_shot()
        if path and path != "OK":
            self._append_log(f"Single shot saved — {path}", ACCENT_GREEN)
        else:
            # Only reachable if the camera fired but handed back no file, which
            # `capture_to` does not do on this path — say so plainly rather
            # than reporting a save that did not happen.
            self._append_log(
                "The shutter fired but no file came back — nothing was saved.",
                ACCENT_AMBER)

    def _on_shot_failed(self, msg: str):
        self._after_shot()
        # The shutter may well have fired anyway: the trigger and the transfer
        # fail separately, which is why nothing in this app re-fires a capture
        # on the strength of an error alone (see `ptp.capture`).
        self._append_log(f"Single shot failed: {msg}", ACCENT_RED)

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
        # Cleared and re-set on every call, so `_table_link_ready` reads the
        # outcome of THIS stop and not a stale one. It is recorded rather than
        # only logged because this failing on the run path is not cosmetic —
        # it is proof the link is already dead, and the run that followed one
        # of these hung on its first rotation 25 seconds later.
        self._tt_stop_error = None
        if self._tt is not None:
            try:
                self._tt.stop()
            except Exception as e:                           # noqa: BLE001
                self._tt_stop_error = str(e)
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
        # `is_open` as well as not-None, because the two can now differ: a Stop
        # that escalated closes the port and leaves the client in place
        # (`_force_stop_table`). Without this the speed buttons came back
        # looking live on a closed port and every press logged a
        # TurntablePortClosed instead of turning anything.
        live = (self._tt is not None and self._tt.is_open
                and self._worker is None)
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

    # ── preview rotation ─────────────────────────────────────────────
    def _step_rotation(self):
        """
        Advance the preview one quarter turn clockwise, and remember it.

        NEEDS NO CAMERA AND NO LIVE VIEW, unlike every other control in this
        group. It is a fact about how the body is mounted, so it can be set
        before connecting and it survives a run, a disconnect and a restart —
        `LiveViewPanel.set_rotation` carries why this cannot reach a saved
        frame.
        """
        options = self.liveview.ROTATIONS
        nxt = options[(options.index(self.liveview.rotation) + 1) % len(options)]
        self._apply_rotation(nxt)
        self._append_log(
            f"Preview rotated to {nxt}° — display only, saved photos are "
            f"unchanged." if nxt else
            "Preview back to its original orientation.", ACCENT_CYAN)

    def _apply_rotation(self, degrees: int, persist: bool = True):
        """Set the rotation, re-place the cards if needed, relabel, store."""
        # The window is ARRANGED for the frame's family — see the two maps
        # in layout.py. Done before the panel turns, so the width cap that
        # `set_rotation` triggers is computed inside the new arrangement.
        # `_arrange` is a no-op when the family did not change (0° to 180°).
        self._arrange(self.orientation_for(degrees))
        self.liveview.set_rotation(degrees)
        # The panel's floor turns with it (750x500 <-> 500x750). Nothing more
        # to do here about the window: the card re-caps itself off the
        # panel's `fit_changed`, and `ScalingHost` watches the content for the
        # layout request that follows and re-fits the whole interface if the
        # new floor no longer fits the window.
        self.btn_rotate.setText(f"⟳ {degrees}°")
        if not persist:
            return
        try:
            self._settings().setValue(self.LIVEVIEW_ROTATION_KEY, degrees)
        except Exception:                                    # noqa: BLE001
            pass                    # a preference nobody should lose a run over

    def _restore_rotation(self):
        """
        Re-apply the remembered rotation at startup.

        Anything unreadable, stale or hand-edited falls back to upright rather
        than raising: `set_rotation` rejects a value that is not a quarter turn,
        and this runs during window construction where an exception would mean
        no window at all.
        """
        try:
            saved = int(self._settings().value(self.LIVEVIEW_ROTATION_KEY, 0))
        except (TypeError, ValueError):
            saved = 0
        if saved not in self.liveview.ROTATIONS:
            saved = 0
        # persist=False: restoring is not a change, and writing here would turn
        # every launch into a settings write.
        self._apply_rotation(saved, persist=False)
        if saved:
            self._append_log(
                f"Preview rotation {saved}° restored from last session.",
                ACCENT_CYAN)

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
        # Checked before the status test below, because with a worker up the
        # status is always one of those four and the two paths would otherwise
        # be unreachable in the wrong order.
        if self._worker is not None:
            return self._reconnect_tt_in_run()
        if self._status in ("running", "paused", "nocamera", "notable"):
            # A backstop now rather than the gate it used to be: `_on_finished`
            # puts the status back to idle when a worker ends, so reaching here
            # means the status outlived its run.
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

    def _reconnect_tt_in_run(self) -> bool:
        """
        Re-open the serial port while a paused run holds it.

        `ComximClient.reconnect` is built for exactly this and says so: it
        reopens IN PLACE, "keeping this object's identity so every reference
        (the worker's and the window's) stays valid". So unlike the cold path
        above, nothing here constructs a client — a fresh `ComximClient`
        assigned to `self._tt` would leave `BridgeWorker.tt` pointing at the
        closed one, and the run would rotate against a dead port for the rest
        of the night.

        It also re-does the CT+STARTUP / CT+ACK / CT+EVENT handshake
        (`handshake=True` is the default) and returns True only once the
        controller has actually answered it, so a True result means the table
        is genuinely ready to turn rather than that a port opened.

        THE PORT CANNOT CHANGE HERE. `reconnect` reuses the port and baud this
        client was built with, and the COM field stays disabled for the length
        of a run — deliberately, since re-pointing a run at a different device
        mid-flight is not a reconnect. If Windows has re-enumerated the table on
        another COM port, that needs Stop, the new port, and Recover; the log
        says so on failure rather than leaving it to be guessed.
        """
        if not self._run_is_parked():
            self._append_log(
                "The run has not come to rest yet — wait for it to finish the "
                "move it is on, then reconnect.", ACCENT_AMBER)
            return False
        if self._tt is None:                        # cannot happen with a run up
            return False

        self.pill_tt.set_state("reconnecting…", ACCENT_AMBER)
        self._reconnecting = True
        self._update_button_states()
        QApplication.processEvents()
        try:
            # One retry, matching `_recover_turntable`: Windows is often still
            # releasing the port on the first attempt after a replug.
            ok = self._tt.reconnect(retries=1)
        except Exception as e:                              # noqa: BLE001
            ok = False
            self._append_log(f"Turntable reconnect raised: {e}", ACCENT_RED)
        try:
            if ok:
                # Re-muted because the beeper is exactly what a power cycle puts
                # back, and a power cycle is the usual reason to press this.
                # Best-effort inside `mute`, so an older firmware that has no
                # SPKMUTE verb does not turn a good reconnect into a failure.
                self._tt.mute(True)
                self.pill_tt.set_state(
                    f"{self._tt.port} @ {self._tt.baudrate}", ACCENT_GREEN)
                self._append_log(
                    f"Turntable link re-opened mid-run on {self._tt.port} — the "
                    f"controller answered the handshake. Press Resume when "
                    f"ready.", ACCENT_GREEN)
            else:
                self.pill_tt.set_state("failed", ACCENT_RED)
                self._append_log(
                    f"Could not re-open {self._tt.port}: the port did not open, "
                    f"or opened and the controller stayed mute. Check the cable "
                    f"and power. If Windows has moved the table to a different "
                    f"COM port, that needs Stop, the new port, then Recover — a "
                    f"mid-run reconnect can only reopen the one it started on.",
                    ACCENT_RED)
            return ok
        finally:
            self._reconnecting = False
            self._update_button_states()

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

        # The `_spin_stop` above just proved whether the table answers at all.
        # Do not start a run on a link that failed it.
        if not self._table_link_ready():
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

        # A fresh run has not been asked to stop, whatever the last one did.
        # Left set, the first Stop press on this run would escalate straight to
        # cutting the port.
        self._stop_requested = False
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

    def _table_link_ready(self) -> bool:
        """
        Don't start a run on a turntable link that has already failed.

        WRITTEN FROM A REAL LOG, and the timings are the argument. A recovered
        run went:

            17:47:57  Could not stop the table: Write timeout
            17:47:57  Resuming from revolution 3, position 1 …
            17:48:22   Rotating 9.47°…          ← and never returned

        The app had positive proof the port was dead twenty-five seconds before
        it hung on it: `_spawn_worker` sends SETSTOP through `_spin_stop` on
        every start, that write failed, and the failure was logged in amber and
        otherwise ignored. Everything after it was certain to hang, because the
        first thing a run does after a stack is rotate.

        SETSTOP is a good probe precisely because it is already being sent. It
        is a no-op on a stationary table, and `_command` waits for the
        controller's CR+OK, so a clean return proves the link works in both
        directions — not merely that a write was accepted.

        One reconnect is attempted before giving up, since the common cause is a
        port Windows has re-enumerated under the same name after a replug, which
        reopening fixes outright. Refusing to start is the fallback, and it is a
        far better outcome than the alternative: a run that shoots one stack and
        then wedges, which is what actually happened.
        """
        err = self._tt_stop_error
        if self._tt is None or not err:
            return True

        self._append_log(
            f"The turntable did not accept the pre-run stop command ({err}). "
            f"The link is already broken, and a run started now would hang on "
            f"its first rotation — re-opening the port before going on…",
            ACCENT_AMBER)
        try:
            ok = self._tt.reconnect(retries=1)
        except Exception as e:                              # noqa: BLE001
            ok = False
            self._append_log(f"  Re-opening raised: {e}", ACCENT_RED)
        if ok:
            self._tt_stop_error = None
            self._tt.mute(True)
            self.pill_tt.set_state(
                f"{self._tt.port} @ {self._tt.baudrate}", ACCENT_GREEN)
            self._append_log(
                f"Turntable port re-opened on {self._tt.port} and the "
                f"controller answered — starting.", ACCENT_GREEN)
            return True

        self.pill_tt.set_state("failed", ACCENT_RED)
        self._append_log(
            "Could not re-open the turntable port, so the run has NOT been "
            "started — nothing is lost and no frames were taken.", ACCENT_RED)
        QMessageBox.warning(
            self, "Turntable not responding",
            f"The turntable on {self._tt.port} is not accepting commands, and "
            f"re-opening the port did not fix it.\n\n"
            f"A run started now would shoot its first stack and then hang on "
            f"the first rotation, so it has not been started.\n\n"
            f"Check the cable and that the table is powered on; if it is "
            f"wedged, power-cycle it. Then press Reconnect under TABLE.")
        self._update_button_states()
        return False

    #: How long a polite Stop is given before Stop starts cutting links.
    #:
    #: Stop sets a flag the run reads at its next gate, so a healthy run ends
    #: within a fraction of a second — the flag is also checked every 0.2 s
    #: inside `ComximClient.wait_for`, so even mid-rotation it is prompt. This
    #: only has to outlast the ordinary case by enough that a normal Stop never
    #: escalates. It does NOT have to cover a capture: a frame in progress can
    #: legitimately take much longer than this, and the escalation deliberately
    #: does not touch the camera (see `_force_stop_table`).
    STOP_GRACE_MS = 4000

    def _stop_session(self):
        """
        Stop the run — and mean it.

        THIS USED TO BE A REQUEST AND NOTHING MORE, which was fine until the
        run stopped being able to hear it. `request_stop` sets a flag the worker
        reads at its next gate; if the worker is blocked in a call that never
        returns, the flag is never read and Stop does nothing at all. That
        happened: with no write timeout on the serial port (see
        `ComximClient.WRITE_TIMEOUT_S`) a rotation wedged inside `send()`, and
        an overnight session ended with seven "Stopping…" lines in the log, a
        dead Pause, a Reconnect button correctly greyed out because the run was
        not parked, and no way out but killing the process.

        So Stop now escalates. First press asks politely and starts a timer;
        if the run is still alive when it fires — or if the user presses Stop
        again, which is what anyone does — the links the run could be stuck on
        are cut out from under it. `_force_stop_table` explains what is cut and
        what deliberately is not.

        The polite path is unchanged and is still what runs 99% of the time:
        nothing is cut when a run stops the way it should.
        """
        if not self._worker:
            return
        if self._stop_requested:
            # Second press. The user has already waited; do not make them wait
            # out the timer as well.
            self._force_stop_table(pressed_again=True)
            return
        self._stop_requested = True
        self._append_log("Stopping…", ACCENT_AMBER)
        self._worker.request_stop()
        QTimer.singleShot(self.STOP_GRACE_MS, self._stop_watchdog)

    def _stop_watchdog(self):
        """Fired `STOP_GRACE_MS` after a Stop that has not taken effect."""
        if self._worker is None or not self._stop_requested:
            return                          # stopped cleanly; nothing to do
        if not self._worker.isRunning():
            return                          # thread is on its way out
        self._force_stop_table(pressed_again=False)

    def _force_stop_table(self, pressed_again: bool):
        """
        Break a wedged run out by closing the turntable's serial port.

        WHY CLOSING THE PORT IS THE LEVER, rather than sending a stop command:
        the thread that is stuck is stuck *inside* a write, holding
        `ComximClient._tx_lock`. Any command this method tried to send would
        queue up behind that lock and freeze the GUI thread too — turning one
        wedged thread into a wedged application. `close()` takes no lock and
        issues `CancelIoEx`, which pyserial's `write()` treats as a clean
        return, so the stuck thread is released rather than merely joined by
        a second victim. `ComximClient._teardown` carries the details.

        WHAT HAPPENS NEXT, and why this ends the run rather than corrupting it:
        the released `send()` returns, `rotate_single` moves on to `wait_for`,
        and that checks its abort callback — `self._stop.is_set`, already true —
        every 0.2 s, so it raises `InterruptedError` promptly. `BridgeWorker.run`
        catches that as "Stopped by user", runs its `finally` (alarm off, live
        view hold released, transfer mode restored) and emits finished. The run
        ends through its ordinary Stop path, with the checkpoint from the last
        stack intact, so **Recover** picks it up. That is the whole point: the
        session survives.

        THE CAMERA IS DELIBERATELY NOT CUT. Every camera call is already
        time-bounded — 6 s per ordinary transfer, 90 s for the shutter, 180 s
        for the commit — so a camera that goes quiet unwinds by itself, slowly
        but surely. Closing its transport mid-transfer means calling into
        libusb on a handle another thread is inside, which risks taking the
        process down; and a killed process is exactly the outcome this method
        exists to prevent. A wedged capture is a wait. A wedged write was
        forever. Only the second one earns this.
        """
        if self._tt is None:
            self._append_log(
                "Stop is still waiting on the run, and there is no turntable "
                "link left to cut. If it does not end, the last stack's "
                "checkpoint is saved — Recover will pick it up.", ACCENT_AMBER)
            return
        self._append_log(
            ("Stop pressed again — " if pressed_again else
             f"Stop has not taken effect in {self.STOP_GRACE_MS / 1000:g}s — ")
            + "cutting the turntable link to break the run out of whatever it "
              "is waiting on.", ACCENT_RED)
        try:
            self._tt.close()
        except Exception as e:                              # noqa: BLE001
            self._append_log(f"  Closing the port raised: {e}", ACCENT_AMBER)
        self.pill_tt.set_state("link cut", ACCENT_RED)
        self._spin_dir, self._spin_level = None, 0
        self._refresh_spin()
        self._update_button_states()
        self._append_log(
            "Turntable port closed. The run should end within a second or two "
            "and the last stack's checkpoint is saved. Press Reconnect under "
            "TABLE to re-open the port, then RECOVER to carry on from where it "
            "stopped.", ACCENT_AMBER)

    def _on_finished(self):
        self._worker = None
        # Cleared here as well as at spawn, so a watchdog still in flight from
        # the Stop that just worked finds nothing to escalate against.
        self._stop_requested = False
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

        # THE RECONNECT BUTTONS LIGHT UP HERE, and this tick is the only thing
        # that can do it. Pressing Pause tells the worker to stop, but it does
        # not stop until its next gate — so "the run is now at rest" is a fact
        # that becomes true on the worker thread with nothing emitted when it
        # does. Rather than add a signal for it, the second-hand clock that is
        # already running asks. The visible effect is worth having on its own:
        # the buttons come alive a moment after Pause, which is exactly when the
        # run really has come to rest.
        #
        # Edge-triggered, not level. `_update_button_states` rewrites button
        # stylesheets, and doing that once a second for the length of a pause is
        # the same waste the pill check above avoids. It owns `_parked_seen` —
        # this only compares against it, so the flag can never drift from what
        # the buttons are showing.
        if self._run_is_parked() != self._parked_seen:
            self._update_button_states()

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
        # A single shot or a mid-run reconnect holds the camera for a second or
        # two. Everything that would take the same link is held off for that
        # long — Start and Recover because a worker opening mid-capture would
        # interleave two multi-transaction sequences, and each of those two
        # actions against the other because they CLOSE the transport the other
        # is still reading through.
        busy = self._link_busy()
        self.btn_start.setEnabled(not active and not busy and HARDWARE_AVAILABLE)
        # Pause only applies while the run is actually turning; while it is
        # blocked waiting for a dropped device, Stop is the only lever.
        #
        # AND NOT DURING A RECONNECT, for the length of one. Resume there would
        # unpark the worker while `close()` is still tearing the transport down:
        # `close()` nulls the client's handle before the old one has finished
        # closing, so the worker's next command would find None, build a fresh
        # transport, and have the interface pulled out from under it as the old
        # close completes. `PTPCameraClient._open_lock` does not cover that —
        # it serialises opens against opens, and its own docstring calls the
        # orphaned-handle result "unrecoverable without a device reset".
        self.btn_stop.setEnabled(active)
        self.btn_pause.setEnabled(self._status in ("running", "paused")
                                  and not busy)
        # Stop stays live throughout, deliberately: it is the emergency lever
        # and it only sets a flag the worker reads at its next gate.

        # BOTH RECONNECTS STAY LIVE THROUGH A PAUSE, which is the one place
        # `active` is not the whole answer. A paused run that has actually come
        # to rest holds both links open and idle, and re-opening one there is
        # the difference between fixing a knocked cable in place and throwing
        # away the revolution. `_run_is_parked` is what makes "at rest" a fact
        # rather than a label — a run is flagged paused the instant the button
        # is pressed, seconds before it reaches a point where it is safe. The
        # two `_reconnect_*_in_run` methods do the work, in place on the client
        # objects the worker already holds.
        parked = self._run_is_parked()
        # RECORDED HERE, not in the tick that reads it, and the difference is a
        # bug that was in this code for one revision. The tick only decides
        # whether to CALL this method; the value the buttons actually reflect is
        # decided here. Tracking it in the tick instead let the two disagree:
        # pause (buttons off, tick records parked) → resume → pause again left
        # the flag reading True while this method had recomputed the buttons off,
        # so when the run parked the tick saw no change and never lit them. The
        # invariant is simply "this flag is the parked value the buttons show".
        self._parked_seen = parked
        self.btn_connect_tt.setEnabled((not active or parked) and not busy)
        self.btn_connect_camera.setEnabled((not active or parked) and not busy)
        # The port and baud fields do NOT come back with them. A reconnect
        # re-opens the device the run started on; pointing a run at a different
        # one halfway through is not the same act, and `ComximClient.reconnect`
        # reuses its own stored port regardless of what the field says — so
        # leaving these editable would only invite a change that silently did
        # nothing.
        for w in (self.field_com, self.field_baud):
            w.setEnabled(not active)

        # One frame, no rotation, into <subject>_singleshots — see
        # `_single_shot`. Needs a camera and an idle link; the guards at the top
        # of that method are the real gate, this is what makes the state visible
        # rather than leaving a live-looking button that does nothing.
        #
        # NOT extended to a parked run, unlike the two reconnects above. A
        # reconnect puts back something that is broken; a single shot costs an
        # actuation and lands a frame in a folder, and a paused run is a run
        # that is going to carry on — so a test exposure taken in the middle of
        # one would leave a frame nobody can place afterwards.
        self.btn_single_shot.setEnabled(
            not active and not busy and self._camera is not None)

        # Recover appears only when a run is not active AND there's a
        # checkpoint to pick up from (i.e. a run was interrupted).
        can_recover = (not active and not busy and HARDWARE_AVAILABLE
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
