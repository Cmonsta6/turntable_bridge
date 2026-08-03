"""
Application entry point: build the QApplication, apply the theme, show the
window.
"""
from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from .assets import asset_path
from .ui.main_window import MainWindow
from .ui.sound import ensure_alarm, ensure_jingles
from .ui.theme import (
    ACCENT_GREEN, DARK_BG, FIELD_BG, PANEL_BG, PANEL_BG_SOFT, TEXT_PRIMARY,
    full_stylesheet,
)

#: Identity Windows groups taskbar buttons under. Without this the app
#: inherits the host process's identity — running from source that is
#: Python's, so the taskbar shows the Python icon however nicely we set
#: our own. Any stable, unique string works.
APP_ID = "chris.turntable.focusstack.bridge"


def _claim_taskbar_identity() -> None:
    """
    Tell Windows this process is its own application.

    Only affects the taskbar grouping/icon, so every failure here is
    survivable — a wrong icon is not worth refusing to start over.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:                                        # noqa: BLE001
        pass


def _app_icon() -> QIcon:
    """
    The window / taskbar icon.

    Prefers the multi-resolution .ico (Windows picks the right size for
    the title bar, taskbar and Alt-Tab); falls back to the source artwork,
    and finally to an empty icon, which just means Qt's default.
    """
    for name in ("app_icon.ico", "camera.png"):
        path = asset_path(name)
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()


def main():
    _claim_taskbar_identity()          # must happen before any window exists
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("ComXim + Nikon Focus Stacking")
    app.setWindowIcon(_app_icon())
    app.setStyleSheet(full_stylesheet())
    ensure_jingles()          # create chime_*.wav next to the script if absent
    ensure_alarm()            # and the looping camera-drop alarm

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(DARK_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Base, QColor(FIELD_BG))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_GREEN))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(PANEL_BG_SOFT))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT_PRIMARY))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
