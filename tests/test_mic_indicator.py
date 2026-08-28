"""Ted must not hold the microphone open just because he is running.

Charlie opened Ted and macOS showed the orange recording indicator, with voice
switched off and nothing listening. core/voice.py is imported during startup
and called engine.start() at module level, which installed a mic tap before
anything asked whether voice was wanted. The loop was already correct — it
skips capture while mic_on is False — so nothing was being recorded. The device
was simply claimed and never released.

These checks pin the fix: no tap until the voice or transcribe button is
pressed, and the engine reports the tap honestly rather than reporting intent.
"""

import os
import sys
import time
import types

import numpy as _np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# sounddevice is a macOS runtime dependency; the logic under test is not.
_fake_sd = types.ModuleType("sounddevice")


class _Stream:
    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def read(self, frames):
        # The background reader thread is not what this file tests; keep it
        # quietly idle instead of letting it log a read error.
        time.sleep(0.05)
        return _np.zeros((frames, 1), dtype="float32"), False


OPENED = []


def _input_stream(**kw):
    s = _Stream(**kw)
    OPENED.append(s)
    return s


_fake_sd.InputStream = _input_stream
_fake_sd.OutputStream = lambda **kw: _Stream(**kw)
sys.modules.setdefault("sounddevice", _fake_sd)

from core import audio
from core.audio import AudioEngine


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def engine_started(mic):
    """A sounddevice-mode engine, with the native binary forced absent."""
    OPENED.clear()
    real, audio.BINARY = audio.BINARY, "/nonexistent/ted_audio"
    try:
        e = AudioEngine()
        mode = e.start(mic=mic)
        return e, mode
    finally:
        audio.BINARY = real


print("— booting does not claim the microphone —")
e, mode = engine_started(mic=False)
check("the engine still starts", mode == "fallback")
check("no input stream is opened", e._in_stream is None and OPENED == [])
check("the engine reports the mic as closed", e.mic_is_open() is False)
check("it knows it is muted", e._mic_muted is True)
check("Ted can still speak — the output stream is open",
      e._out_stream is not None and e._out_stream.started)

print("\n— the mic is claimed only when voice is turned on —")
e.unmute_mic()
check("unmuting opens the input stream", e._in_stream is not None)
check("the engine now reports the mic as open", e.mic_is_open() is True)
check("exactly one input stream was ever opened", len(OPENED) == 1)

print("\n— and released again on mute —")
opened = e._in_stream
e.mute_mic()
check("muting closes the input stream", e._in_stream is None and opened.closed)
check("the engine reports the mic as closed again", e.mic_is_open() is False)
check("muting twice is harmless", (e.mute_mic(), e.mic_is_open())[1] is False)

print("\n— asking for a mic at start still works, for callers that want one —")
e2, mode2 = engine_started(mic=True)
check("mic=True opens the input stream as before",
      mode2 == "fallback" and e2._in_stream is not None)
check("and reports itself open", e2.mic_is_open() is True)
check("a mic opened at start needs no deferred device check",
      e2._mic_verified is True)

print("\n— a released mic never reports itself as open —")
e3, _ = engine_started(mic=False)
e3.mode = "aec"
e3.proc = None
check("aec mode with no live process is not holding the mic",
      e3.mic_is_open() is False)
e3._mic_muted = False
check("still not open when the process is gone, whatever the mute flag says",
      e3.mic_is_open() is False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
