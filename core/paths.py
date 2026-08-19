"""core/paths.py — Canonical filesystem locations for Ted.

Derived from this file's own location (ted-ai/core/paths.py) so the project
keeps working even if the folder is moved or renamed.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 29 (§29.4)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Where everything lives. Fifteen lines, and every path is derived from this
#      file's own location rather than hardcoded — so moving or renaming the ted-ai
#      folder does not break Ted.
#
# =============================================================================

import os

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/ted-ai
DATA = os.path.join(HOME, "data")
INPUT_FILE = os.path.join(DATA, "input.wav")          # scratch capture file for STT
UI_HTML = os.path.join(HOME, "ui", "ted_hud.html")    # the HUD window
LOG_PATH = os.path.join(HOME, "ted_errors.log")
SHORTCUTS_PATH = os.path.join(HOME, "shortcuts.json")
GATE5_LOG = os.path.join(DATA, "gate5_usage.jsonl")   # which regex branches actually fire
