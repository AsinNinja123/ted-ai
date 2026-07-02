"""core/features.py — Optional-module availability flags.

Every optional subsystem is imported exactly once here, wrapped so a missing
dependency (or a broken module) disables that feature instead of crashing Ted.
Import the module objects and HAS_* flags from here rather than re-trying the
imports in every file.
"""

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
