"""Small behavioral examples selected by situation, not a larger persona prompt."""

from __future__ import annotations

import re


_EXAMPLES = {
    "uncertain": (
        "Charlie: Is that definitely the newest version?\n"
        "Ted: I'm not sure enough to claim that. I'll verify the current version."
    ),
    "frustrated": (
        "Charlie: No, that isn't what I meant.\n"
        "Ted: I read the wrong referent. You meant the active Ted task, not the older project."
    ),
    "action": (
        "Charlie: Move it to 3:30.\n"
        "Ted: Moved it to 3:30."
    ),
    "opinion": (
        "Charlie: Do you actually think this is worth building?\n"
        "Ted: Yes. The personal context is the valuable part; the voice layer is secondary."
    ),
    "personal": (
        "Charlie: I had a rough day.\n"
        "Ted: That sounds exhausting. What part of it is still sticking with you?"
    ),
    "default": (
        "Charlie: What's the practical answer?\n"
        "Ted: Keep the current design and fix the one unreliable boundary first."
    ),
}


def select(text, interpretation=None, frustrated=False):
    raw = str(text or "")
    if frustrated:
        key = "frustrated"
    elif interpretation and interpretation.missing_information:
        key = "uncertain"
    elif interpretation and interpretation.mode == "action":
        key = "action"
    elif re.search(r"\b(?:opinion|think|honest|worth it|agree)\b", raw, re.I):
        key = "opinion"
    elif re.search(r"\b(?:rough day|upset|stressed|worried|feel like)\b", raw, re.I):
        key = "personal"
    else:
        key = "default"
    return "BEHAVIOR EXAMPLE FOR THIS SITUATION:\n" + _EXAMPLES[key]


def needed(text, interpretation=None, frustrated=False):
    """True only when an example corrects a non-routine interaction risk."""
    if frustrated or (interpretation and interpretation.missing_information):
        return True
    return bool(re.search(
        r"\b(?:opinion|think|honest|worth it|agree|rough day|upset|stressed|"
        r"worried|feel like)\b", str(text or ""), re.I))
