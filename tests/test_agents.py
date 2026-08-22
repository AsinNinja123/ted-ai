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

    # MacAgent holds NO opinion about consequence. TedApi asks
    # core/tool_handlers.needs_confirmation before the call ever reaches an
    # agent, and that flow asks in chat and answers on the next turn rather
    # than blocking this thread on a click — which matters because _run_agent
    # drives the agent from Ted's synchronous turn thread, and in voice mode
    # there is no button to press at all.
    check("MacAgent defers the consequence decision to the one gate",
          not agent.needs_confirmation("clean_up", {})
          and not agent.needs_confirmation("close_app", {"name": "Notes"}))

    done = await agent.execute("clean_up", plan_id=plan.id)
    check("one agent call owns the whole close loop",
          calls == [("close_app", {"name": "Notes"}),
                    ("close_app", {"name": "Calendar"})])
    check("the result reports only verified per-app outcomes",
          done.ok and done.evidence["closed"] == ["Notes", "Calendar"]
          and done.did == "Closed Notes, Calendar.")

    # Sparing an app is a narrowing of the same request, not a new one.
    calls.clear()
    spare = MacAgent(dispatch, lambda: ["Ted", "Notes", "Calendar", "Brave Browser"])
    peek = await spare.execute("clean_up", {"exclude": ["Brave Browser"]},
                               dry_run=True)
    check("a spared app is absent from what the confirmation offers",
          peek.evidence["would_close"] == ["Notes", "Calendar"]
          and peek.evidence["spared"] == ["Brave Browser"])
    kept = await spare.execute("clean_up", {"exclude": ["Brave Browser"]})
    check("and it is never actually closed",
          [name for _, args in calls for name in [args["name"]]]
          == ["Notes", "Calendar"])
    check("Ted says which app he left alone",
          kept.did == "Closed Notes, Calendar. Left Brave Browser open.")

    # The gate itself still has to work: agents that genuinely own an async
    # approval (CommsAgent sending an email) will use it, so its contract is
    # covered here with a stub rather than deleted along with MacAgent's use.
    class GatedAgent(MacAgent):
        name = "GatedAgent"
        CONSEQUENT_METHODS = frozenset({"close_app"})

    calls.clear()
    gated = GatedAgent(dispatch, lambda: list(open_apps), confirmation_gate=gate)
    declined_task = asyncio.create_task(gated.execute(
        "close_app", {"name": "Notes"}, confirmation_timeout=1))
    for _ in range(20):
        await asyncio.sleep(.01)
        pending = gate.pending_ids()
        if pending:
            break
    check("a consequential task pauses behind one confirmation Future", len(pending) == 1)
    gate.resolve(pending[0], False)
    declined = await declined_task
    check("a denial is an honest non-success", not declined.ok
          and declined.evidence == {"confirmed": False})
    check("a denied action never reaches the old dispatcher", calls == [])

    # Default agents share the gate exposed by /api/confirm/<request-id>.
    routed = GatedAgent(dispatch, lambda: ["Ted", "Notes"])
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
