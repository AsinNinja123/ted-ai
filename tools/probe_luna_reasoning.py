"""Does gpt-5.6-luna actually reason, and does it hand the trace back?

probe_luna_responses.py answered "tools work". This answers the harder
question the carry-over cache depends on: are there reasoning items at all?

    cd ~/ted-ai && ./venv/bin/python tools/probe_luna_reasoning.py

Three real API calls. Changes nothing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from openai import OpenAI

MODEL = getattr(config, "PRIMARY_CHAT_MODEL", "gpt-5.6-luna")
client = OpenAI(api_key=config.OPENAI_API_KEY)

TOOLS = [
    {"type": "function", "name": "open_app",
     "description": "Open a macOS application",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"]}},
    {"type": "function", "name": "type_text",
     "description": "Type text into the frontmost app",
     "parameters": {"type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"]}},
    {"type": "function", "name": "press_key",
     "description": "Press a key in the frontmost app",
     "parameters": {"type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"]}},
]

# A request with genuine sequencing in it. A one-step "open Terminal" gives a
# reasoning model nothing to think about, which may be the whole reason the
# first probe saw no trace.
ASK = ("Open a terminal, start claude code in it, and tell it to write a "
       "python calculator. Do as much as you can in one go.")


def reasoning_tokens(resp):
    d = getattr(getattr(resp, "usage", None), "output_tokens_details", None)
    return getattr(d, "reasoning_tokens", None)


def report(label, **kw):
    print(f"\n[{label}]")
    try:
        r = client.responses.create(model=MODEL, input=[
            {"role": "user", "content": ASK}], tools=TOOLS, store=False, **kw)
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {str(exc)[:250]}")
        return None
    kinds = [getattr(i, "type", "?") for i in r.output]
    calls = [i for i in r.output if getattr(i, "type", "") == "function_call"]
    reasons = [i for i in r.output if getattr(i, "type", "") == "reasoning"]
    print(f"    output items      : {kinds}")
    print(f"    tool calls        : {len(calls)}"
          + (f"  -> {[c.name for c in calls]}" if calls else ""))
    print(f"    reasoning items   : {len(reasons)}")
    for i in reasons:
        print(f"      id={getattr(i, 'id', None)} "
              f"encrypted={'yes' if getattr(i, 'encrypted_content', None) else 'no'} "
              f"summary={bool(getattr(i, 'summary', None))}")
    rt = reasoning_tokens(r)
    print(f"    reasoning tokens  : {rt if rt is not None else 'not reported'}")
    return r


a = report("A. effort=high, encrypted content requested",
           reasoning={"effort": "high"},
           include=["reasoning.encrypted_content"])
b = report("B. effort=high, summary=auto",
           reasoning={"effort": "high", "summary": "auto"})
c = report("C. no reasoning block at all (the control)")

print("\n" + "=" * 66)
ra = len([i for i in (a.output if a else []) if getattr(i, "type", "") == "reasoning"])
ta = reasoning_tokens(a) if a else None
ca = len([i for i in (a.output if a else []) if getattr(i, "type", "") == "function_call"])
cc = len([i for i in (c.output if c else []) if getattr(i, "type", "") == "function_call"])

if ra:
    print("Reasoning items come back. Carry-over will work as designed.")
    print("-> set USE_LUNA_RESPONSES = True")
elif ta:
    print(f"No reasoning items, but {ta} reasoning tokens were spent. The model")
    print("thinks; it just will not hand the trace over. Reasoning is then")
    print("per-round only — better than none, and the cache is dead weight.")
    print("-> set USE_LUNA_RESPONSES = True, and I will strip the cache.")
else:
    print("No reasoning items and no reasoning tokens. This model does not")
    print("reason on tool turns however it is asked.")
    print("-> leave the flag OFF. The endpoint was never the problem.")
print(f"\nCalls per response — with reasoning: {ca}, control: {cc}")
print("If those differ, plan depth is the real signal, not the trace.")
print("=" * 66)
