"""core/speaker.py — optional voice recognition (who is talking to Ted).

Say "Ted, learn my voice" once to enroll; set VOICE_LOCK = True in config.py
and Ted ignores speech that doesn't match your voice — the kids and the TV
can't drive him anymore.

Needs the optional `resemblyzer` package (brings in torch — a big install):
    ~/ted-ai/venv/bin/pip install resemblyzer
Everything degrades gracefully without it: enrollment explains what to
install, and VOICE_LOCK lets speech through rather than bricking Ted.

The profile (a few voice embeddings) lives in data/voice_profile.npy.
"""

import os

import numpy as np

from core.paths import DATA

PROFILE = os.path.join(DATA, "voice_profile.npy")

_encoder = None
_warned_missing = False


def available():
    """True when resemblyzer is importable."""
    try:
        import resemblyzer  # noqa: F401
        return True
    except Exception:
        return False


def _get_encoder():
    global _encoder
    if _encoder is None:
        from resemblyzer import VoiceEncoder
        _encoder = VoiceEncoder()          # loads the model — a few seconds, once
    return _encoder


def enrolled():
    return os.path.exists(PROFILE)


def profile_count():
    try:
        return int(np.load(PROFILE).shape[0])
    except Exception:
        return 0


def _embed(audio_f32_16k):
    from resemblyzer import preprocess_wav
    wav = preprocess_wav(np.asarray(audio_f32_16k, dtype=np.float32), source_sr=16000)
    return _get_encoder().embed_utterance(wav)


def enroll(audio_f32_16k):
    """Add one embedding of the owner's voice to the profile (keeps last 5).
    Returns True on success."""
    try:
        emb = _embed(audio_f32_16k)[None, :]
        if enrolled():
            emb = np.vstack([np.load(PROFILE), emb])[-5:]
        np.save(PROFILE, emb)
        return True
    except Exception as e:
        print(f"[speaker] enroll failed: {e}")
        return False


def forget():
    """Delete the voice profile. Returns True if one existed."""
    try:
        os.remove(PROFILE)
        return True
    except Exception:
        return False


def verify(audio_f32_16k, threshold=0.68):
    """Return True if the audio matches the enrolled voice, False if it clearly
    doesn't, or None when verification can't run (no profile / no package /
    error) — callers must treat None as 'let it through'."""
    global _warned_missing
    if not enrolled():
        return None
    if not available():
        if not _warned_missing:
            _warned_missing = True
            print("[speaker] VOICE_LOCK is on but resemblyzer isn't installed — "
                  "letting all speech through. pip install resemblyzer")
        return None
    try:
        emb = _embed(audio_f32_16k)
        prof = np.load(PROFILE)
        sims = prof @ emb / (np.linalg.norm(prof, axis=1) * np.linalg.norm(emb) + 1e-9)
        return float(np.max(sims)) >= threshold
    except Exception as e:
        print(f"[speaker] verify failed: {e}")
        return None
