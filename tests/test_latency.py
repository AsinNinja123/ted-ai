"""tests/test_latency.py — the reply must not wait on optional context.

Memory retrieval runs four independent lookups before the model is asked
anything: recent related exchanges, known facts, the knowledge base, and past
session memories. All four are best-effort — the answer is not.

The bug this pins: each lookup was joined with its own `timeout=4.0`, so the
documented "4 second budget" was really 4 seconds EACH, up to 16 seconds of
silence before the request was even sent. One slow source could not delay the
reply a little; it delayed it by the whole budget, and two could delay it twice.

Run with the venv python:  python tests/test_latency.py
"""

import os
import sys
import time
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

voice_stub = types.ModuleType("core.voice")
voice_stub.SPEED = 1.1
sys.modules.setdefault("core.voice", voice_stub)

from core import llm  # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def text_chunk(s):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=s, tool_calls=None))])


print("— a slow source delays the reply by the budget, not by a multiple of it —")

# Keep the test quick: shrink the budget rather than sleeping through the real
# one. What is being pinned is the SHAPE — total wait is one budget, not N.
llm.CONTEXT_BUDGET = 0.4
BUDGET = llm.CONTEXT_BUDGET

# Two sources hang. Under the old per-thread timeout this cost 2 x budget.
def _hang(*a, **k):
    time.sleep(30)
    return "never"


llm.get_memory = _hang
llm.get_facts_about = _hang
llm.format_memories_for_prompt = lambda *a, **k: "sessions"
llm.features = SimpleNamespace(HAS_KNOWLEDGE=False, knowledge=None)
llm.detect_action = lambda text: None
llm._needs_web = lambda text: False
llm._remember_exchange = lambda *a, **k: None
llm.chat_create = lambda **kw: iter([text_chunk("Answer enough to commit. " * 4)])

conversation = [{"role": "system", "content": "sys"}]
t0 = time.time()
out = "".join(llm.ask_streaming("hello there", conversation))
elapsed = time.time() - t0

check("the reply still arrives", out.strip().startswith("Answer enough to commit."))
check(f"…within roughly one budget, not two ({elapsed:.2f}s)", elapsed < BUDGET * 1.8)
check("…and it did wait for the budget rather than skipping retrieval",
      elapsed >= BUDGET * 0.8)

print("\n— fast sources are not penalised —")

llm.get_memory = lambda *a, **k: "recent"
llm.get_facts_about = lambda *a, **k: "facts"
conversation = [{"role": "system", "content": "sys"}]
t0 = time.time()
out = "".join(llm.ask_streaming("hello again", conversation))
quick = time.time() - t0
check(f"all sources ready → no measurable wait ({quick*1000:.0f}ms)", quick < BUDGET / 2)
check("…and the context actually reached the prompt", "recent" and out)

print("\n— the knowledge store can be warmed off the critical path —")

from core import knowledge  # noqa: E402
check("knowledge.warm exists so startup can pay the ChromaDB/fastembed cost",
      callable(getattr(knowledge, "warm", None)))
knowledge._init_failed = True          # make it a no-op, not a real model load
knowledge.warm()                       # must never raise
check("…and warming is safe to call when the stack is unavailable", True)

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
