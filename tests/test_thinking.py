"""Reasoning depth, and the difference between a silent stream and a thinking one.

Aug 14: the one message in a twelve-message session that was over twelve words
was the only one that failed, twice, with "Something cut out — ask me again."
reasoning_effort_for() switched on word count; the model spent its whole token
budget thinking into a field nothing read, and emitted no content at all.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import llm
fails = [0]
def check(label, ok):
    print(("  ✓ " if ok else "  ✗ ") + label)
    if not ok: fails[0] += 1

print("— reasoning effort follows the request, not its length —")
check("thinking-partner instructions still exist",
      bool(llm.THINKING_CONTEXT) and "ask one real question" in llm.THINKING_CONTEXT)
check("28 words of small talk do not buy hidden reasoning",
      llm.reasoning_effort_for(
          "I game on my xbox ocasianally. not super serious just for fun.  but I "
          "will be going to college soon and will be doing a lot of schoolwork")
      == "none")
check("a long factual answer request stays cheap",
      llm.reasoning_effort_for(
          "what monitor should i buy for school and some light gaming on the side "
          "with a budget of around three hundred dollars") == "none")
check("an explicit analytical verb still reasons",
      llm.reasoning_effort_for("explain how FTS5 ranking works") == "default")
check("a chained instruction still reasons",
      llm.reasoning_effort_for("open notes and then type my essay") == "default")
check("'why' still reasons", llm.reasoning_effort_for("why is this slow") == "default")

print("\n— a stream of pure reasoning is not a dropped connection —")
class D:
    def __init__(s, **kw):
        for k, v in kw.items(): setattr(s, k, v)
class C:
    def __init__(s, d): s.choices = [D(delta=d)]
def drive(chunks, reasoned):
    calls = {}
    g = llm._stream_turn(iter(chunks), calls, reasoned=reasoned)
    out = ""
    try:
        while True: out += g.send(None)
    except StopIteration as e:
        return e.value or "", calls
r = [0]
txt, calls = drive([C(D(content=None, tool_calls=None, reasoning="thinking hard "))
                    for _ in range(3)], r)
check("reasoning deltas are counted, never shown", txt == "" and r[0] == 42)
check("...and produce no tool calls", calls == {})
r2 = [0]
txt2, _ = drive([C(D(content="Hello there, this is a long enough answer to commit.",
                     tool_calls=None))], r2)
check("ordinary content still streams", "Hello there" in txt2 and r2[0] == 0)

print("\n" + "=" * 50)
print(f"{10 - fails[0]} passed, {fails[0]} failed")
