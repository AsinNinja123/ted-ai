"""Safe end-to-end check of Ted's offline reasoning + sequential tool loop.

This never touches the Mac or Ted's memory. It forces the local provider and
uses two fake tools so it is safe to run during development:

    venv/bin/python tools/demo_local_brain.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm, providers


providers._groq = None
llm.detect_action = lambda _text: None
llm.get_memory = lambda _query: ""
llm.get_facts_about = lambda _subject: ""
llm.format_memories_for_prompt = lambda: ""
llm.save_memory = lambda *_args, **_kwargs: None
llm.intents._worth_extracting = lambda _text: False
llm.features.HAS_KNOWLEDGE = False

schemas = [
    {
        "type": "function",
        "function": {
            "name": "get_seed_number",
            "description": "Return the seed number. This must be called first.",
            "parameters": {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_number",
            "description": "Double a number returned by get_seed_number.",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "integer"}},
                "required": ["number"], "additionalProperties": False,
            },
        },
    },
]

calls = []


def dispatch(name, args):
    calls.append((name, args))
    if name == "get_seed_number":
        return "7"
    if name == "double_number":
        return str(args["number"] * 2)
    return "unknown"


runtime = llm.ToolRuntime(schemas, dispatch)
conversation = [{"role": "system", "content": "You are Ted."}]
prompt = (
    "Use get_seed_number. After you receive its result, call double_number with "
    "that exact integer. Only then tell me the final doubled number."
)
answer = "".join(llm.ask_streaming(prompt, conversation, tool_runtime=runtime))

print(f"provider={providers.active_provider()}")
print(f"calls={calls!r}")
print(f"answer={answer.strip()!r}")

expected = [
    ("get_seed_number", {}),
    ("double_number", {"number": 7}),
]
if calls != expected or "14" not in answer:
    raise SystemExit("offline tool-chain demo failed")
print("LOCAL TOOL CHAIN: PASS")
