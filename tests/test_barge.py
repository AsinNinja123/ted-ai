"""tests/test_barge.py — the voice barge-in state machine in core/audio.py.

Guards two failure classes:
  • "I talked over Ted and he kept going" — detection must stay live across
    sentence boundaries of a streamed reply (state is reset once per reply,
    not once per sentence).
  • "Ted stops when I clap" — a frame must be loud AND classified as speech by
    webrtcvad, with BARGE_FRAMES voiced frames inside a BARGE_WINDOW sliding
    window. A clap is only ~4 loud frames, so it can never reach the floor.

AudioEngine's __init__ and _ingest_frame don't touch any audio device (only
start() does), so all of this runs with synthetic frames. Voiced frames are a
120 Hz harmonic "vowel" (webrtcvad accepts it as speech); claps are decaying
white-noise bursts.

Run with the venv python:  python tests/test_barge.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.audio import (AudioEngine, BARGE_FRAMES, BARGE_MARGIN, BARGE_THRESHOLD_MAX,
                        BARGE_WINDOW, FRAME, SAMPLE_RATE)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def make_engine(threshold=0.006, mode="aec", fallback_bargein=True):
    e = AudioEngine(threshold=threshold, fallback_bargein=fallback_bargein)
    e.mode = mode          # normally set by start(); no hardware in tests
    return e


def voiced_frames(n, rms=0.05):
    """n consecutive 20 ms frames of a 120 Hz harmonic vowel at the given RMS —
    continuous phase across frames, so webrtcvad sees ongoing speech."""
    t = np.arange(n * FRAME) / SAMPLE_RATE
    sig = sum(np.sin(2 * np.pi * 120 * k * t) / k for k in range(1, 9))
    sig = sig * (rms / np.sqrt(np.mean(sig ** 2)))
    return [sig[i * FRAME:(i + 1) * FRAME].astype(np.float32) for i in range(n)]


def clap_frames(n=4, peak=0.3):
    """A clap: n frames of decaying white noise, loud enough to clear any bar."""
    rng = np.random.default_rng(42)
    sig = rng.standard_normal(n * FRAME).astype(np.float32)
    sig *= np.exp(-np.linspace(0, 4, n * FRAME)).astype(np.float32) * peak
    return [sig[i * FRAME:(i + 1) * FRAME] for i in range(n)]


QUIET = np.full(FRAME, 0.002, dtype=np.float32)


def feed(e, frames):
    for f in frames:
        e._ingest_frame(f)


print("— speech over Ted triggers, transients don't —")
e = make_engine()
e._set_playing(True)
feed(e, voiced_frames(BARGE_FRAMES - 1))
check(f"not yet triggered at {BARGE_FRAMES - 1} voiced frames", not e.barge_in)
feed(e, voiced_frames(3))
check(f"sustained speech triggers within the {BARGE_WINDOW}-frame window", e.barge_in)

e = make_engine()
e._set_playing(True)
feed(e, clap_frames())
feed(e, [QUIET] * BARGE_WINDOW)
check("a clap (4 loud transient frames) never triggers", not e.barge_in)

e = make_engine()
e._set_playing(True)
feed(e, clap_frames())
feed(e, [QUIET] * 10)
feed(e, clap_frames())
feed(e, [QUIET] * BARGE_WINDOW)
check("a double clap never triggers", not e.barge_in)

print("\n— VAD + pitch gates: loud non-speech is rejected in the synth gap —")
# The pitch gate applies in the gaps between sentences, where mic audio is
# clean. (While audio actually PLAYS in aec mode the pitch gate is waived —
# macOS voice processing garbles the little near-end audio it passes, and has
# itself already suppressed echo and non-speech.)
e = make_engine()
e.set_in_reply(True)
feed(e, [np.full(FRAME, 0.05, dtype=np.float32)] * 30)   # loud DC hum, clearly not speech
check("sustained loud DC hum in the gap does not trigger", not e.barge_in)

# Sustained broadband noise (a long loud "shhh", air vent, applause) fools
# webrtcvad — fricatives ARE broadband noise — but has no vocal pitch.
e = make_engine()
e.set_in_reply(True)
rng = np.random.default_rng(7)
noise = (rng.standard_normal(40 * FRAME) * 0.08).astype(np.float32)
feed(e, [noise[i * FRAME:(i + 1) * FRAME] for i in range(40)])
check("sustained loud broadband noise in the gap does not trigger (pitch gate)",
      not e.barge_in)

print("\n— speech with a brief dip still triggers (window, not consecutive run) —")
e = make_engine()
e._set_playing(True)
feed(e, voiced_frames(7))
feed(e, [QUIET])                       # 20 ms dip mid-word
feed(e, voiced_frames(5))
check("12 voiced frames around a 1-frame dip trigger", e.barge_in)

print("\n— no detection when Ted is idle —")
e = make_engine()
feed(e, voiced_frames(BARGE_WINDOW * 2))
check("speech while idle never sets barge_in", not e.barge_in)

print("\n— THE regression: interrupting in the gap between sentences —")
e = make_engine()
e.set_in_reply(True)          # speak_streaming sets this for the whole reply
e._set_playing(True)          # sentence 1 playing
e._ingest_frame(QUIET)
e._set_playing(False)         # sentence 1 ended — synth gap begins
feed(e, voiced_frames(BARGE_FRAMES + 2))
check("voice in the synth gap sets barge_in", e.barge_in)

print("\n— evidence survives a sentence boundary —")
e = make_engine()
e.set_in_reply(True)
e._set_playing(True)
feed(e, voiced_frames(BARGE_FRAMES - 2))   # speech starts at end of sentence 1
e._set_playing(False)                      # boundary — used to zero all state
e._set_playing(True)                       # sentence 2 starts
feed(e, voiced_frames(4))
check("speech spanning sentences 1→2 triggers", e.barge_in)

print("\n— reset is per reply, not per sentence —")
e = make_engine()
e.set_in_reply(True)
e._set_playing(True)
feed(e, voiced_frames(BARGE_FRAMES + 2))
e._set_playing(False)
e._set_playing(True)                   # next sentence: play_samples must NOT clear it
check("barge_in survives the next sentence starting", e.barge_in)
e.set_in_reply(False)
check("barge_in still readable after the reply ends", e.barge_in)
e.reset_barge_in()
check("explicit reset clears flag and window",
      not e.barge_in and sum(e._barge_hits) == 0)

print("\n— partial evidence does not leak past the reply —")
e = make_engine()
e.set_in_reply(True)
feed(e, voiced_frames(BARGE_FRAMES - 1))
e.set_in_reply(False)
check("clearing in_reply empties the window", sum(e._barge_hits) == 0)

print("\n— noisy-room calibration can no longer make barge-in impossible —")
e = make_engine(threshold=0.025)       # calibrate()'s cap: uncapped bar would be 0.075
e._set_playing(True)
feed(e, voiced_frames(BARGE_FRAMES + 2, rms=0.05))   # normal speech, not a shout
check(f"0.05 RMS speech clears the capped bar ({BARGE_THRESHOLD_MAX})", e.barge_in)
check("cap is below the old worst case", BARGE_THRESHOLD_MAX < 0.025 * BARGE_MARGIN)

print("\n— ambient noise stays below the bar —")
e = make_engine()
e.set_in_reply(True)
feed(e, [QUIET] * 50)
check("room noise during a reply never triggers", not e.barge_in)

print("\n— fallback mode honours the FALLBACK_VOICE_BARGEIN switch —")
e = make_engine(mode="fallback", fallback_bargein=False)
e.set_in_reply(True)
e._set_playing(True)
feed(e, voiced_frames(BARGE_WINDOW * 2))
check("disabled fallback barge-in stays off even in-reply", not e.barge_in)
e = make_engine(mode="fallback", fallback_bargein=True)
e._set_playing(True)
feed(e, voiced_frames(BARGE_FRAMES + 2))
check("enabled fallback barge-in still works", e.barge_in)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
