"""Behavioral guardrails for Ted's small static persona.

These checks deliberately pin dispositions rather than exact prose. The prompt
has been rewritten several times; what must survive is the distinction between
familiar and performatively casual, and between adult humor and targeted abuse.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm


checks = []


def check(label, condition):
    checks.append(bool(condition))
    print(("  ✓ " if condition else "  ✗ ") + label)


p = llm.SYSTEM_PROMPT.lower()

check("the persona is not performatively casual", "never performatively casual" in p)
check("Ted does not imitate Charlie's slang", "don't imitate his slang" in p)
check("Ted does not nag", "nag him" in p)
check("adult humor is explicitly allowed", all(x in p for x in ("sexual", "vulgar", "explicit")))
check("adult humor is separated from abuse", "adult humor is not the same as targeted abuse" in p)
check("targeted abuse still has a concise boundary", "slurs or abuse aimed at a person" in p)
check("Ted's opinions stay distinct from Charlie's", "memory describes what he likes, not what you think" in p)
check("thinking mode remains defined", "ask one real question" in llm.THINKING_CONTEXT.lower())
check("the prompt has normal sentence boundaries", ".you're" not in p and "linesay" not in p)
check("the persona remains compact", len(p.split()) <= 360)

failed = len(checks) - sum(checks)
print("\n" + "=" * 50)
print(f"{sum(checks)} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
