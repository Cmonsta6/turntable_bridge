"""
Turntable Focus-Stack Bridge — a Qt desktop app that drives a Comxim
serial turntable and a Nikon camera over raw PTP/USB, shooting a focus
stack at each rotation position.

Layout
------
``turntable.hardware``   device clients and focus maths — no Qt imports
``turntable.run``        the capture worker thread and its mixins
``turntable.ui``         theme, widgets, and the main window
``turntable.app``        ``main()`` — the entry point

Run it with ``python turntable_app.py`` from the project root.
"""

__version__ = "1.0"
