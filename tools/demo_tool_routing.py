"""Safe hosted-brain check for multi-action intent/tool selection.

The tool names and schemas are real; implementations are fake, so this never
opens an app or types on the Mac.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm, providers
from core.tools import TOOL_SCHEMAS


llm.detect_action = lambda _text: None
llm.get_memory = lambda _query: ""
llm.get_facts_about = lambda _subject: ""
llm.format_memories_for_prompt = lambda: ""
llm.save_memory = lambda *_args, **_kwargs: None
llm.intents._worth_extracting = lambda _text: False
llm.features.HAS_KNOWLEDGE = False

wanted = {"open_app", "type_text", "web_search"}
action_tools = {"open_app", "type_text"}
schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] in wanted]
calls = []


def dispatch(name, args):
    calls.append((name, args))
    return {
        "open_app": "Notes opened.",
        "type_text": "Text typed.",
        "web_search": (
            "Current result for the requested topic. "
            "(Source: https://example.com/current-result)"
        ),
    }[name]


runtime = llm.ToolRuntime(schemas, dispatch, action_tools=action_tools)
conversation = [{"role": "system", "content": "You are Ted."}]
answer = "".join(llm.ask_streaming(
    "Open Notes and then type buy milk at the current cursor.",
    conversation,
    tool_runtime=runtime,
))

print(f"provider={providers.active_provider()}")
print(f"calls={calls!r}")
print(f"answer={answer.strip()!r}")

names = [name for name, _args in calls]
if providers.active_provider() != "groq":
    raise SystemExit("hosted tool-routing demo did not use Groq")
if names != ["open_app", "type_text"]:
    raise SystemExit("hosted tool-routing demo selected the wrong tools or order")

calls.clear()
conversation = [{"role": "system", "content": "You are Ted."}]
answer = "".join(llm.ask_streaming(
    "Search the live web for current Ted model news and cite the source.",
    conversation,
    tool_runtime=runtime,
))
print(f"web_calls={calls!r}")
print(f"web_answer={answer.strip()!r}")
if [name for name, _args in calls] != ["web_search"]:
    raise SystemExit("hosted current-info demo did not select web_search")
if "https://example.com/current-result" not in answer:
    raise SystemExit("hosted current-info demo omitted its source URL")
print("HOSTED TOOL ROUTING + CURRENT SOURCING: PASS")
