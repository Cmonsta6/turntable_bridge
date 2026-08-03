#!/usr/bin/env python
"""
Turntable Focus-Stack Bridge, launcher.

    python turntable_app.py

Everything lives in the `turntable` package next to this file; this is
just the double-clickable front door. Assets the app expects in this
directory: camera.png (the dial artwork). The chime and alarm .wav files
are synthesised here on first run.

Requirements:
    pip install -r requirements.txt

Camera setup:
    1. Connect the Nikon body over USB and switch it on.
    2. Bind its USB interface to WinUSB with Zadig, or the transport
       cannot claim it. Windows' own MTP driver holds the device.
    3. Leave the lens in Autofocus (AF): the focus motor only takes
       commands in AF. Each shot is fired with autofocus suppressed, so
       the focus plane stays where the stack put it.
"""

from turntable.app import main

if __name__ == "__main__":
    main()
