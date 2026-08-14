"""Regressions for reflex routing, dynamic tools, and prompt-weight policy."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import routing
from core.tools import TOOL_SCHEMAS


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def names(schemas):
    return [routing.tool_name(schema) for schema in schemas]


print("— conservative zero-model reflexes —")
plan = routing.plan_reflex("close Notes and Calendar")
check("two fully resolved app targets use the reflex lane",
      plan and plan.calls == (
          ("close_app", {"name": "notes"}),
          ("close_app", {"name": "calendar"}),
      ))
plan = routing.plan_reflex("Could you open VS Code, please?")
check("polite natural wrappers still reach the safe reflex",
      plan and plan.calls == (("open_app", {"name": "vs code"}),))
check("a website mixed with an app declines the entire reflex",
      routing.plan_reflex("open Notes and YouTube") is None)
check("a second capability declines the entire reflex",
      routing.plan_reflex("open Notes and set a timer") is None)
check("contextual pronouns are left to reasoning",
      routing.plan_reflex("close it") is None)
check("dependent sequences are left to reasoning",
      routing.plan_reflex("open Notes, then close it") is None)
check("mixed app/web targets require two completed tool calls",
      routing.expected_action_calls("open Notes and open YouTube") == 2)
check("contextual plural targets require two completed tool calls",
      routing.expected_action_calls("close the two apps I just opened") == 2)
check("two target groups across two stages require four calls",
      routing.expected_action_calls(
          "open Notes and Messages, then close both") == 4)
check("dependent non-app stages are counted",
      routing.expected_action_calls(
          "copy this to my clipboard, then read it back") == 2)
check("two different capabilities joined by and are both required",
      routing.expected_action_calls(
          "open Notes and send a message to Gavin") == 2)
check("discussion containing an action verb does not force execution",
      not routing.likely_action_request(
          "I wonder whether I should remove that from my workflow"))
check("a polite direct request still requires execution",
      routing.likely_action_request("Could you pause the music?"))

# Regression: the first version of this classifier matched any sentence opening
# with write/check/show/find/read/tell/create/search/remove. Every line below
# is an ordinary chatbot request that was being treated as a Mac command —
# memory withheld, prose suppressed, and a tool call forced with no tool that
# could satisfy it. A verb that is also conversational must not qualify here;
# missing a real action only costs tool_choice="auto", which already works.
for phrase in ("write me a poem about fall",
               "tell me what you think of this design",
               "check my code for bugs",
               "show me an example of a decorator",
               "find the bug in this function",
               "read this back to me and summarize it",
               "create a function that reverses a string",
               "search for a better approach",
               "remove the third paragraph",
               "send me your best guess"):
    check(f"conversation is not an action: {phrase!r}",
          not routing.likely_action_request(phrase))

for phrase in ("open Notes",
               "close VS Code and Notes",
               "open youtube.com in Brave",
               "play the song Maine",
               "pause the music",
               "text Gavin that I'm running late",
               "set a timer for ten minutes",
               "add it to my calendar",
               "log my workout",
               "copy this to my clipboard"):
    check(f"real action still qualifies: {phrase!r}",
          routing.likely_action_request(phrase))

# Aug 14, from a real session: "play a different one" arrived with an empty
# menu because the music family regex wanted the literal words song/music/
# spotify and "play" was not one of them. Ted burned a find_tools round trip,
# hit the free-tier rate limit mid-recovery, fell through to the local brain,
# and took 7.8 seconds to change a song.
_LAST_PLAY = ("Recent verified actions: play_music({'query': 'Let It Go'}) "
              "-> Playing Let It Go.")
for phrase in ("play a different one", "ok play another disney song",
               "play something else", "its not playing", "it's not playing",
               "skip this one", "play the next one"):
    check(f"music request reaches the music tools: {phrase!r}",
          "play_music" in names(routing.select_tool_schemas(phrase, _LAST_PLAY)))
check("a non-music turn is not given music tools by the continuation words",
      "play_music" not in names(routing.select_tool_schemas("how are you", _LAST_PLAY)))

check("an action turn still gets no episodic recall",
      routing.memory_scope_for("open Notes", []) == "none")
check("a conversational verb keeps its ordinary memory scope",
      routing.memory_scope_for("write me a poem about fall", []) == "relevant")


print("\n— dynamic capability menus —")
chat = routing.select_tool_schemas("how are you")
check("plain conversation carries only capability discovery",
      names(chat) == ["find_tools"])
apps_web = routing.select_tool_schemas("open Notes and YouTube")
check("mixed app/web request gets both relevant families",
      {"find_tools", "open_app", "close_app", "browse_to"}.issubset(names(apps_web)))
clipboard = routing.select_tool_schemas(
    "put this on my clipboard, then read the clipboard")
check("dependent clipboard request gets read and write contracts",
      {"clipboard_read", "clipboard_write"}.issubset(names(clipboard)))
found = routing.discover_tool_schemas("send a text message", exclude={"find_tools"})
check("capability discovery can recover an initially absent message tool",
      "send_message" in names(found))
check("operational actions skip episodic memory",
      routing.memory_scope_for("close the app", apps_web) == "none")
check("explicit recall earns full memory",
      routing.memory_scope_for("what do you remember about me", chat) == "full")
check("ordinary conversation gets relevant retrieval only",
      routing.memory_scope_for("how was your day", chat) == "relevant")


print("\n— prompt weight —")
full_chars = len(json.dumps(TOOL_SCHEMAS, separators=(",", ":")))
app_chars = len(json.dumps(apps_web, separators=(",", ":")))
chat_chars = len(json.dumps(chat, separators=(",", ":")))
check("an app/web request removes at least 70% of tool-schema text",
      app_chars <= full_chars * 0.30)
check("plain conversation removes at least 90% of tool-schema text",
      chat_chars <= full_chars * 0.10)
print(f"  full={full_chars} chars app/web={app_chars} chars chat={chat_chars} chars")


print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
