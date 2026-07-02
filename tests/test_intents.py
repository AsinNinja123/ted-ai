"""Use-case tests for Ted's spoken-command parsing.

Run with:  ~/ted-ai/venv/bin/python tests/test_intents.py
No test framework needed — plain asserts, prints a summary at the end.

Each case documents a real thing Charlie says to Ted and what must happen.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import intents
from core.actions import match_running_app, APPS, _THIS_APP_WORDS

PASS = FAIL = 0

def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("\n— Use case: 'play shake it off' plays the song (no 'open spotify' needed) —")
check("play shake it off → song query",
      intents._parse_song("play shake it off") == ("shake it off", None))
check("play shake it off by taylor swift → title + artist",
      intents._parse_song("play shake it off by taylor swift") == ("shake it off", "taylor swift"))
check("play the song bohemian rhapsody → explicit keyword",
      intents._parse_song("play the song bohemian rhapsody") == ("bohemian rhapsody", None))
check("'play it' is resume, not a song search",
      intents._parse_song("play it") is None)
check("'play music' is a transport phrase (matched upstream)",
      intents._matches("play music", intents._SPOT_PLAY))

print("\n— Use case: 'open spotify and play shake it off' runs both commands —")
parts = intents._split_commands("open spotify and play shake it off")
check("splits into two commands", parts == ["open spotify", "play shake it off"])
check("part 1 opens spotify", intents._parse_open_apps(parts[0]) == ["spotify"])
check("part 2 is the song", intents._parse_song(parts[1]) == ("shake it off", None))
check("'and then' also splits",
      intents._split_commands("open chrome and then play some music")
      == ["open chrome", "play some music"])
check("'message gavin and ask him to golf' stays ONE command",
      len(intents._split_commands("message gavin and ask him if he wants to golf")) == 1)

print("\n— Use case: 'close this app' closes the app I'm looking at —")
check("close this app → 'this app' target", intents._parse_close_apps("close this app") == ["this app"])
check("close the app → 'app' target (filler 'the' stripped)",
      intents._parse_close_apps("close the app")[0] in _THIS_APP_WORDS)
check("'this app' is recognised as a frontmost reference", "this app" in _THIS_APP_WORDS)
check("quit chrome → chrome", intents._parse_close_apps("quit chrome") == ["chrome"])
check("close spotify and messages → both",
      intents._parse_close_apps("close spotify and messages") == ["spotify", "messages"])

print("\n— Use case: misheard/misspelled names still match the right app —")
running = ["Spotify", "Google Chrome", "Visual Studio Code", "Messages", "Finder"]
check("'spotifi' → Spotify",        match_running_app("spotifi", running) == "Spotify")
check("'chrome' → Google Chrome",   match_running_app("chrome", running) == "Google Chrome")
check("'crome' → Google Chrome",    match_running_app("crome", running) == "Google Chrome")
check("'vs code' → VS Code",        match_running_app("vs code", running) == "Visual Studio Code")
check("'blender' (not running) → None", match_running_app("blender", running) is None)
check("open 'spotifi' resolves via fuzzy APPS match",
      intents._parse_open_apps("open spotifi") == ["spotify"])

print("\n— Use case: Whisper mishears the command verb —")
check("'clothes spotify' → 'close spotify'",
      intents._fix_command_words("clothes spotify") == "close spotify")
check("'paws the music' → 'pause the music'",
      intents._fix_command_words("paws the music") == "pause the music")

print("\n— Control phrases still behave —")
check("'okay ted, stop it please' is a stop", intents._is_stop_command("okay ted, stop it please"))
check("'stop' alone is a stop", intents._is_stop_command("stop"))
check("'stopwatch' is NOT a stop", not intents._is_stop_command("stopwatch"))
check("'cancel the timer' is NOT a generic cancel (has its own handler)",
      not intents._is_cancel_command("cancel the timer"))
check("wake phrase strips", intents._strip_wake_phrase("Hey Ted, what time is it")
      == ("what time is it", True))
check("bare 'Ted, …' also wakes", intents._strip_wake_phrase("Ted, play some music")
      == ("play some music", True))
check("'okay ted' wakes", intents._strip_wake_phrase("okay ted what's up")[1])
check("'ted' mid-sentence does NOT wake",
      not intents._strip_wake_phrase("I told ted about it yesterday")[1])
check("'teddy' does NOT wake", not intents._strip_wake_phrase("teddy bear")[1])
check("normalize strips straight apostrophes too",
      intents._normalize_cmd("what's playing") == "whats playing")

print("\n— Spotify tool-call volume actions map to real actions —")
from core.tool_handlers import ACTION_TOOLS, looks_like_failure
check("failure phrasing detected: \"Spotify isn't open right now.\"",
      looks_like_failure("Spotify isn't open right now."))
check("failure phrasing detected: \"I couldn't find an app called Blender.\"",
      looks_like_failure("I couldn't find an app called Blender."))
check("success phrasing not flagged: 'Closed Spotify.'",
      not looks_like_failure("Closed Spotify."))
check("open_app/close_app/play_music are ACTION tools (spoken verbatim)",
      {"open_app", "close_app", "play_music", "spotify_control"} <= ACTION_TOOLS)

print("\n— Misc parsing —")
check("reminder parses", intents._parse_reminder("remind me to call the bank in 10 minutes") is not None)
check("timer request", intents._is_timer_request("set a timer for 5 minutes"))
check("calc: 3 at 45", intents._parse_calc("total on 3 at 45") == "That comes to 135 dollars.")
check("time parse 7:30pm", intents._parse_time_to_24h("7:30pm") == "19:30")
check("playlist parse", intents._parse_playlist("play my workout playlist") == ("workout", False))

print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
