"""Does this account's Luna actually do function tools AND reasoning?

Everything in core/luna_responses.py assumes the answer is yes. Run this
before trusting any of it:

    cd ~/ted-ai && ./venv/bin/python tools/probe_luna_responses.py

It makes two real API calls (a few cents at most) and changes nothing. Read
the verdict at the bottom.
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from openai import OpenAI

MODEL = getattr(config, "PRIMARY_CHAT_MODEL", "gpt-5.6-luna")
client = OpenAI(api_key=config.OPENAI_API_KEY)

TOOLS = [{
    "type": "function",
    "name": "open_app",
    "description": "Open a macOS application",
    "parameters": {"type": "object",
                   "properties": {"name": {"type": "string"}},
                   "required": ["name"]},
}]

print(f"model: {MODEL}\n")

# ── 1. tools + reasoning at all ───────────────────────────────────────────────
print("[1] tools + reasoning on /v1/responses ...")
ok_basic = False
try:
    r = client.responses.create(
        model=MODEL,
        input=[{"role": "user", "content": "Open Terminal, please."}],
        tools=TOOLS,
        reasoning={"effort": "medium"},
        store=False,
        include=["reasoning.encrypted_content"],
    )
    kinds = [getattr(i, "type", "?") for i in r.output]
    print(f"    OK — output items: {kinds}")
    for i in r.output:
        if getattr(i, "type", "") == "function_call":
            print(f"    call: {i.name}({i.arguments})  call_id={i.call_id}")
        if getattr(i, "type", "") == "reasoning":
            enc = getattr(i, "encrypted_content", None)
            print(f"    reasoning item: id={getattr(i, 'id', None)} "
                  f"encrypted_content={'yes' if enc else 'NO'}")
    ok_basic = any(getattr(i, "type", "") == "function_call" for i in r.output)
    has_reasoning = any(getattr(i, "type", "") == "reasoning" for i in r.output)
    first = r.output
except Exception as exc:
    print(f"    FAILED: {type(exc).__name__}: {str(exc)[:300]}")
    first, has_reasoning = [], False

# ── 2. does the reasoning survive a tool round trip? ──────────────────────────
print("\n[2] echoing the reasoning back with a tool result ...")
if ok_basic:
    try:
        follow = []
        call_id = None
        for i in first:
            item = i.model_dump() if hasattr(i, "model_dump") else dict(i)
            if item.get("type") in ("reasoning", "function_call"):
                follow.append(item)
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
        follow.insert(0, {"role": "user", "content": "Open Terminal, please."})
        follow.append({"type": "function_call_output",
                       "call_id": call_id,
                       "output": "Opened Terminal."})
        r2 = client.responses.create(
            model=MODEL, input=follow, tools=TOOLS,
            reasoning={"effort": "medium"}, store=False,
            include=["reasoning.encrypted_content"],
        )
        print("    OK — the API accepted echoed reasoning items.")
        print(f"    reply: {(r2.output_text or '')[:160]}")
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {str(exc)[:300]}")
        print("    -> carry-over will not work; the adapter still gives you")
        print("       reasoning within a single round.")
else:
    print("    skipped (step 1 did not produce a tool call)")

# ── verdict ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 62)
if ok_basic and has_reasoning:
    print("VERDICT: set USE_LUNA_RESPONSES = True in config.py.")
elif ok_basic:
    print("VERDICT: tools work, but no reasoning item came back. Turning the")
    print("flag on is safe; carry-over will simply miss. Worth checking")
    print("whether this model exposes reasoning at all before spending on it.")
else:
    print("VERDICT: do NOT turn the flag on. This model does not do tools on")
    print("/v1/responses the way the adapter expects. The bottleneck is the")
    print("model, not the endpoint — pick a different primary.")
print("=" * 62)
