"""core/features.py — Optional-module availability flags.

Every optional subsystem is imported exactly once here, wrapped so a missing
dependency (or a broken module) disables that feature instead of crashing Ted.
Import the module objects and HAS_* flags from here rather than re-trying the
imports in every file.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 29 (§29.3)
# =============================================================================
#
#  WHAT THIS FILE IS
#      One place that answers "is this optional piece installed and working?"
#
#      Every optional subsystem is imported here exactly once, wrapped so that a
#      missing dependency disables that feature instead of crashing Ted. Everything
#      else in the codebase imports the module object and the HAS_* flag FROM HERE
#      rather than re-trying the import itself.
#
#  WHY THIS PATTERN MATTERS
#      If ten files each try `import chromadb` in a try/except, you get ten slightly
#      different opinions about whether the knowledge base works. One file, one
#      answer. Same principle as §34.
#
# =============================================================================

try:
    from core import assistant
    HAS_ASSISTANT = True
except Exception as e:
    print("Assistant module unavailable:", e)
    assistant = None
    HAS_ASSISTANT = False

try:
    from core import spotify_web
    HAS_SPOTIFY_WEB = True
except Exception as e:
    print("Spotify Web module unavailable:", e)
    spotify_web = None
    HAS_SPOTIFY_WEB = False

try:
    from core import knowledge
    HAS_KNOWLEDGE = True
except Exception as e:
    print("Knowledge module unavailable:", e)
    knowledge = None
    HAS_KNOWLEDGE = False

try:
    from core import calendar_app as calendar
    HAS_CALENDAR = True
except Exception as e:
    print("Calendar module unavailable:", e)
    calendar = None
    HAS_CALENDAR = False

try:
    from core import notes
    HAS_NOTES = True
except Exception as e:
    print("Notes module unavailable:", e)
    notes = None
    HAS_NOTES = False

try:
    from core import computer
    HAS_COMPUTER = True
except Exception as e:
    print("Computer module unavailable:", e)
    computer = None
    HAS_COMPUTER = False

try:
    from core import screen
    HAS_SCREEN = True
except Exception as e:
    print("Screen module unavailable:", e)
    screen = None
    HAS_SCREEN = False
