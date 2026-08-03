# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Turntable Focus-Stack Bridge.

Build:
    python -m PyInstaller --noconfirm turntable_app.spec

Produces a single self-contained dist/TurntableBridge.exe — CPython, Qt,
pyserial and every asset are inside it. The target machine needs no Python;
it does need the camera bound to WinUSB via Zadig, and a USB-serial driver
for the turntable.

Both knobs are environment variables, so neither needs an edit here:

    TTB_ONEDIR=1   build dist/TurntableBridge/ (a folder) instead of one
                   .exe. Bigger on disk, starts a little faster, and is
                   flagged by antivirus far less often — it has no
                   self-extracting stub. Pair it with --distpath.
    TTB_CONSOLE=1  keep a console attached. The only way to see a crash
                   that happens before the window exists, since the app's
                   own log panel isn't up yet.

Measured on the dev machine: one-file 46 MB, first window 1.4 s;
one-folder 115 MB, first window 0.8 s.
"""

import os

onefile = os.environ.get('TTB_ONEDIR', '0') != '1'
console = os.environ.get('TTB_CONSOLE', '0') == '1'

block_cipher = None

# Everything the app opens by name at runtime. asset_path() looks in the
# bundle root first, so these go to '.'.
datas = [
    ('camera.png', '.'),
    ('chime_alarm.wav', '.'),
    ('chime_done.wav', '.'),
    ('chime_hold.wav', '.'),
    # Bundled as data as well as compiled into the .exe: the embedded copy
    # is what Explorer shows, but the taskbar button takes its icon from
    # the window, which loads this one at runtime.
    ('app_icon.ico', '.'),
]

hiddenimports = [
    'exifread',
    'serial',
    'serial.tools',
    'serial.tools.list_ports',
    'serial.win32',
]

# This project lives in an Anaconda environment, so without these the
# analysis happily pulls the whole scientific stack into the bundle and
# roughly triples its size. None of it is imported by the app.
#
# Do NOT add shiboken6 here: it is the PySide6 binding runtime, not an
# optional extra, and excluding it makes every Qt import fail at startup.
# Stdlib modules (sqlite3, bz2, lzma, unittest, ...) are likewise left
# alone — the interpreter and its own machinery reach for them in places
# that are hard to predict, and they are small.
excludes = [
    'numpy', 'scipy', 'pandas', 'matplotlib', 'PIL', 'IPython', 'jedi',
    'notebook', 'jupyter', 'sympy', 'pytest',
    'tkinter', 'pydoc_data', 'lib2to3',
    # Qt modules the GUI never touches. QtWidgets/QtGui/QtCore are kept.
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D',
    'PySide6.QtQuickWidgets', 'PySide6.QtQuickControls2',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtGraphs',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner',
    'PySide6.QtHelp', 'PySide6.QtUiTools', 'PySide6.QtPdf',
    'PySide6.QtPdfWidgets', 'PySide6.QtBluetooth', 'PySide6.QtNfc',
    'PySide6.QtPositioning', 'PySide6.QtLocation', 'PySide6.QtSensors',
    'PySide6.QtSerialBus', 'PySide6.QtRemoteObjects', 'PySide6.QtScxml',
    'PySide6.QtSpatialAudio', 'PySide6.QtTextToSpeech',
    'PySide6.QtHttpServer', 'PySide6.QtNetworkAuth',
]

a = Analysis(
    ['turntable_app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if onefile:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='TurntableBridge',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon='app_icon.ico',
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='TurntableBridge',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon='app_icon.ico',
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='TurntableBridge',
    )
