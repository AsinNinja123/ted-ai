"""Contracts and the first task-owning agent."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import events
from core.agents import (AgentResult, ConfirmationGate, Delegation, MacAgent,
                         Plan)
from core.agents.base import DEFAULT_CONFIRMATION_GATE
from dashboard.app import app


PASS = FAIL = 0


def check(label, ok):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


print("— universal result and plan contracts —")
result = AgentResult(True, "Closed Notes.", {"app": "Notes"}, duration_ms=12)
check("results separate the report from its evidence",
      result.did == "Closed Notes." and result.evidence == {"app": "Notes"})
try:
    AgentResult(True, "Closed Notes.", None)
    refused = False
except TypeError:
    refused = True
check("evidence cannot silently disappear", refused)

seen = events.subscribe()
plan = Plan(
    heard="close Notes and Calendar",
    steps=[Delegation("MacAgent", "close_app", {"name": "Notes"}),
           Delegation("MacAgent", "close_app", {"name": "Calendar"})],
    parallel=False,
).announce()
plan_event = seen.get(timeout=.1)
check("plans are explicit, structured events",
      plan_event.kind == "plan" and plan_event.payload["plan_id"] == plan.id
      and len(plan_event.payload["steps"]) == 2)
seen.close()


async def exercise_agent():
    calls = []
    open_apps = ["Ted", "Notes", "Calendar"]

    def dispatch(name, args):
        calls.append((name, dict(args)))
        return f"Closed {args['name']}." if name == "close_app" else "Done."

    gate = ConfirmationGate()
    agent = MacAgent(dispatch, lambda: list(open_apps), confirmation_gate=gate)
    check("describe() reflects live state, not a static schema",
          agent.describe() == "MacAgent: 2 closable user apps open")

    dry = await agent.execute("clean_up", dry_run=True, plan_id=plan.id)
    check("dry_run reports the exact proposed targets",
          dry.ok and dry.evidence["would_close"] == ["Notes", "Calendar"])
    check("dry_run has no side effects", calls == [])

    task = asyncio.create_task(agent.execute(
        "clean_up", plan_id=plan.id, confirmation_timeout=1))
    for _ in range(20):
        await asyncio.sleep(.01)
        pending = gate.pending_ids()
        if pending:
            break
    check("a consequential task pauses behind one confirmation Future", len(pending) == 1)
    check("the UI can resolve that Future by request id", gate.resolve(pending[0], True))
    done = await task
    check("one agent call owns the whole close loop",
          calls == [("close_app", {"name": "Notes"}),
                    ("close_app", {"name": "Calendar"})])
    check("the result reports only verified per-app outcomes",
          done.ok and done.evidence["closed"] == ["Notes", "Calendar"]
          and done.did == "Closed Notes, Calendar.")

    calls.clear()
    declined_task = asyncio.create_task(agent.execute(
        "close_app", {"name": "Notes"}, confirmation_timeout=1))
    for _ in range(20):
        await asyncio.sleep(.01)
        pending = gate.pending_ids()
        if pending:
            break
    gate.resolve(pending[0], False)
    declined = await declined_task
    check("a denial is an honest non-success", not declined.ok
          and declined.evidence == {"confirmed": False})
    check("a denied action never reaches the old dispatcher", calls == [])

    # Default agents share the gate exposed by /api/confirm/<request-id>.
    routed = MacAgent(dispatch, lambda: ["Ted", "Notes"])
    route_task = asyncio.create_task(routed.execute(
        "close_app", {"name": "Notes"}, confirmation_timeout=1))
    for _ in range(20):
        await asyncio.sleep(.01)
        pending = DEFAULT_CONFIRMATION_GATE.pending_ids()
        if pending:
            break
    response = app.test_client().post(
        "/api/confirm/" + pending[0], json={"approved": True})
    route_done = await route_task
    check("the HUD endpoint resolves the shared default gate",
          response.status_code == 200 and route_done.ok)


print("\n— MacAgent owns multi-step execution —")
asyncio.run(exercise_agent())

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
