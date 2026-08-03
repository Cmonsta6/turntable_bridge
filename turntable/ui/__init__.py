"""
Qt presentation layer.

Import graph::

    theme ─┬─> widgets ──────────────────────┐
           ├─> dial ─────────────────────────┤
           ├─> liveview ─────────────────────┤
           └─> focus_track ──> focus_panel ──┴─> layout ──> main_window

Every module on that graph imports ``theme`` — ``main_window`` directly as
well as through ``layout``. ``theme`` is the package's only true leaf:
pathlib and PySide6, nothing of this project's.

``sound`` is not on the graph at all: nothing under ``ui`` imports it, its
two importers are ``app`` and ``run.worker``. Nor is it a leaf — both
``jingle_path`` and ``alarm_path`` resolve through ``..assets.asset_path``.
So ``theme`` is the one piece that lifts cleanly into another app, and
``sound`` lifts with ``turntable.assets`` attached to it.

Deliberately empty of imports. ``run.recovery`` and ``run.worker`` import
``ui.theme`` for their log colours, and importing any submodule runs this
file first, so re-exporting ``main_window`` here pulled the entire window
in on the way to fetching a colour constant, and ``main_window`` imports
``run.worker`` straight back. That cycle made ``import turntable.run``
fail outright while ``import turntable.app`` happened to work, purely
because of the order in which the two entry points reached the modules.

Import the modules directly::

    from turntable.ui.main_window import MainWindow
    from turntable.ui.theme import ACCENT_GREEN
"""
