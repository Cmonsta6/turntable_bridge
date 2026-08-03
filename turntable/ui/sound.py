"""
Synthesised chimes and the looping device-lost alarm.

The three .wav files ARE shipped: chime_done.wav, chime_hold.wav and
chime_alarm.wav sit in the project root and turntable_app.spec bundles
them, so `asset_path` hands back the shipped copy and a normal install
synthesises nothing. The synthesiser is the fallback for a file that was
deleted or never arrived, and it re-makes the whole tune — the 7-note
done arpeggio, the 3-note hold call, the 14-note alarm scale — writes it
where `asset_path` points and plays that, rather than degrading to a
beep; there is no beep path in this module. Playback is winsound
(Windows) and every call is best-effort — a machine with no audio must
not break a run, which between SND_NODEFAULT and the bare `except: pass`
means a machine where synthesis fails runs silent rather than loud.

The alarm is deliberately distinct from the two chimes: it means a device
vanished mid-run and the table is sitting idle waiting for you.
"""
from __future__ import annotations

import math
from pathlib import Path

from ..assets import asset_path


_JINGLE_SR = 22050

# (frequency Hz, time-to-next-note s). Notes ring and overlap, so the tune
# sounds legato. DONE: a bright major arpeggio that lifts and resolves on the
# octave — upbeat and finished. HOLD: a short rising three-note call that
# ends UP, so it feels expectant, like "your turn".
_JINGLE_NOTES = {
    "done": [(523.25, .13), (659.25, .13), (783.99, .13), (1046.50, .20),
             (783.99, .11), (987.77, .11), (1046.50, .55)],
    "hold": [(659.25, .12), (783.99, .12), (1046.50, .40)],
}


# The camera-drop alarm: a gentle scale that walks up an octave and back
# down, then rests briefly, looping until the camera is reconnected. A scale
# (rather than a klaxon) is attention-getting without being harsh, and it
# returns to its starting note so the loop seam is seamless.
_ALARM_SCALE = [
    523.25, 587.33, 659.25, 698.46, 783.99, 880.00, 987.77, 1046.50,   # up
    987.77, 880.00, 783.99, 698.46, 659.25, 587.33,                    # down
]


def _synth_jingle(path, notes, ring=0.45, tail_s=0.0):
    import wave
    import struct
    n_total = int(_JINGLE_SR * (sum(s for _, s in notes) + ring + tail_s)) + 1
    buf = [0.0] * n_total
    t0 = 0.0
    ring_n = int(_JINGLE_SR * ring)
    for freq, step in notes:
        start = int(_JINGLE_SR * t0)
        for i in range(ring_n):
            t = i / _JINGLE_SR
            # fast attack, exponential decay -> a plucky, bell-like note
            env = math.exp(-4.3 * t) * min(1.0, t / 0.004)
            w = freq * 2.0 * math.pi
            s = (math.sin(w * t)
                 + 0.35 * math.sin(2 * w * t)
                 + 0.14 * math.sin(3 * w * t))
            j = start + i
            if j < n_total:
                buf[j] += env * s
        t0 += step
    peak = max(1e-6, max(abs(x) for x in buf))
    scale = 0.82 / peak
    frames = bytearray()
    for x in buf:
        v = int(max(-1.0, min(1.0, x * scale)) * 32767)
        frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as wv:
        wv.setnchannels(1)
        wv.setsampwidth(2)
        wv.setframerate(_JINGLE_SR)
        wv.writeframes(bytes(frames))


def jingle_path(kind: str) -> Path:
    return asset_path(f"chime_{kind}.wav")


def ensure_jingles():
    """Generate the .wav files in the project root if they are missing."""
    for kind, notes in _JINGLE_NOTES.items():
        p = jingle_path(kind)
        if not p.is_file():
            try:
                _synth_jingle(p, notes)
            except Exception:
                pass


def play_jingle(kind: str):
    """Play a jingle asynchronously via the OS WAV player (non-blocking)."""
    try:
        import winsound
        p = jingle_path(kind)
        if not p.is_file():
            _synth_jingle(p, _JINGLE_NOTES[kind])
        winsound.PlaySound(str(p), winsound.SND_FILENAME
                           | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


def alarm_path() -> Path:
    return asset_path("chime_alarm.wav")


_alarm_on = False


def start_alarm():
    """
    Begin the looping camera-drop alarm (the up-and-down scale). It keeps
    playing until stop_alarm() is called. Safe to call twice — SND_LOOP just
    restarts the same clip.
    """
    global _alarm_on
    try:
        import winsound
        p = alarm_path()
        if not p.is_file():
            _synth_jingle(p, [(f, 0.16) for f in _ALARM_SCALE],
                          ring=0.34, tail_s=0.5)
        # SND_LOOP requires SND_ASYNC; the sound repeats until we purge it.
        winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_LOOP | winsound.SND_NODEFAULT)
        _alarm_on = True
    except Exception:
        pass


def stop_alarm():
    """Silence the camera-drop alarm immediately."""
    global _alarm_on
    try:
        import winsound
        # PlaySound(None, SND_PURGE) stops any async clip this process started,
        # which is exactly the looping alarm.
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass
    _alarm_on = False


def ensure_alarm():
    """Pre-generate the alarm WAV so the first drop-out doesn't stutter."""
    p = alarm_path()
    if not p.is_file():
        try:
            _synth_jingle(p, [(f, 0.16) for f in _ALARM_SCALE],
                          ring=0.34, tail_s=0.5)
        except Exception:
            pass
