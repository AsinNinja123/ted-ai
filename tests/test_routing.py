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
      routing.expected_action_calls("copy this, then read it back") == 2)
check("two different capabilities joined by and are both required",
      routing.expected_action_calls(
          "open Notes and send a message to Gavin") == 2)
check("discussion containing an action verb does not force execution",
      not routing.likely_action_request(
          "I wonder whether I should remove that from my workflow"))
check("a polite direct request still requires execution",
      routing.likely_action_request("Could you remove that from my notes?"))


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
