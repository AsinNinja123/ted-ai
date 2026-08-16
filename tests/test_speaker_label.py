"""Voice identity as a LABEL, not a lock.

The distinction is the whole point of E1 and is easy to lose later: this says
who Ted thinks was talking. It must never be the thing that decides whether a
turn is allowed, because a voice is clonable and this is a cosine similarity
with a threshold. VOICE_LOCK — a separate, explicit config setting — is the
only thing permitted to ignore anyone.

resemblyzer is not installed on this machine (it pulls torch), so the encoder
is stubbed. That covers the comparison, the thresholds and every degraded path;
what it does NOT cover is real embeddings of real voices, which stays unverified
until the package is installed.

Run with:  ~/ted-ai/venv/bin/python tests/test_speaker_label.py
"""

import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import speaker  # noqa: E402

speaker.PROFILE = os.path.join(tempfile.mkdtemp(), "voice_profile.npy")

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


CHARLIE = np.array([1.0, 0.0, 0.0], dtype=np.float32)
STRANGER = np.array([0.0, 1.0, 0.0], dtype=np.float32)
SIMILAR = np.array([0.95, 0.31, 0.0], dtype=np.float32)   # ~0.95 cosine

real_available = speaker.available
real_embed = speaker._embed


def stub(vector, present=True):
    speaker.available = lambda: present
    speaker._embed = lambda audio: np.asarray(vector, dtype=np.float32)


print("\n— before anything is set up —")
speaker.available = lambda: False
st = speaker.status()
check("status reports the package missing", st["available"] is False)
check("status reports nothing enrolled", st["enrolled"] is False)
out = speaker.identify([0.0])
check("identify cannot answer without a profile", out["known"] is None)
check("and says why, for the window to repeat",
      "no voice profile" in out["reason"])

print("\n— enrolling —")
stub(CHARLIE)
check("enroll succeeds", speaker.enroll(np.zeros(16000, dtype=np.float32)) is True)
check("a profile now exists", speaker.enrolled() is True)
check("with one sample", speaker.profile_count() == 1)

print("\n— recognising —")
stub(CHARLIE)
out = speaker.identify([0.0])
check("the enrolled voice is known", out["known"] is True)
check("and carries a score the HUD can show", out["score"] > 0.99)

stub(STRANGER)
out = speaker.identify([0.0])
check("a different voice is not known", out["known"] is False)
check("with a low score", out["score"] < 0.1)

# The threshold is 0.68 and must be the same one everywhere.
stub(SIMILAR)
check("a similar voice passes at the default threshold",
      speaker.identify([0.0])["known"] is True)
check("and fails at a stricter one",
      speaker.identify([0.0], threshold=0.99)["known"] is False)

print("\n— verify is the same comparison, not a second one —")
for vec, expected in ((CHARLIE, True), (STRANGER, False)):
    stub(vec)
    check(f"verify agrees with identify ({expected})",
          speaker.verify([0.0]) is speaker.identify([0.0])["known"] is expected)

print("\n— degraded paths let speech through —")
stub(CHARLIE, present=False)
out = speaker.identify([0.0])
check("no package means unknown, never False", out["known"] is None)
check("and explains the missing package", "resemblyzer" in out["reason"])
check("verify agrees, so VOICE_LOCK cannot silently lock Ted out",
      speaker.verify([0.0]) is None)


def exploding(audio):
    raise RuntimeError("encoder blew up")


speaker.available = lambda: True
speaker._embed = exploding
out = speaker.identify([0.0])
check("an encoder crash is unknown, not a rejection", out["known"] is None)
check("and the error is surfaced", "blew up" in out["reason"])

print("\n— forgetting —")
stub(CHARLIE)
speaker.enroll(np.zeros(16000, dtype=np.float32))
check("a second sample is kept alongside the first", speaker.profile_count() >= 2)
check("forget removes the profile", speaker.forget() is True)
check("and it is really gone", speaker.enrolled() is False)
check("forgetting twice is not an error", speaker.forget() is False)
check("identify goes back to unknown", speaker.identify([0.0])["known"] is None)

speaker.available = real_available
speaker._embed = real_embed

print("\n— the label never gates a turn —")
# core/voice.py must only drop a turn when VOICE_LOCK is on. Read the source
# rather than the docs: this is the invariant that would be quietly lost.
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "voice.py"), encoding="utf-8").read()
gate = src.split("Gate 1.5", 1)[1].split("sf.write", 1)[0]
check("the only early return in the identity gate is under VOICE_LOCK",
      gate.count("return None") == 1 and "if VOICE_LOCK" in gate)
check("identification itself runs without VOICE_LOCK",
      "speaker.enrolled()" in gate and gate.index("speaker.enrolled()")
      < gate.index("if VOICE_LOCK"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
