"""Meaning resolution, durable tasks, relationship review, and outcome contracts."""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (conversation_examples, outcomes, providers, relationship,
                  task_state, understanding)  # noqa: E402
from types import SimpleNamespace

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "understanding.db")
task_state.DB_PATH = db
relationship.DB_PATH = db

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("— references become inspectable interpretations —")
active = {"id": 7, "goal": "Schedule class at 3 PM", "status": "active"}
turn = understanding.resolve("Actually move it back an hour.", active_task=active)
check("a correction continues the active task",
      turn.mode == "action" and turn.continues_task_id == 7)
check("the interpretation names its resolved referent",
      "Schedule class" in turn.references.get("referring language", ""))
check("unresolved short references ask one precise question",
      understanding.resolve("Do it.").clarification_policy == "ask_one_question")
check("repeat language resolves against the current task",
      understanding.resolve("Do that again.", active_task=active).continues_task_id == 7)
recalled = understanding.resolve("What were we doing?", active_task=active)
check("asking what we were doing retrieves rather than restarts the task",
      recalled.mode == "information" and recalled.continues_task_id == 7)
check("a complete clause is not mistaken for an old referent",
      not understanding.resolve("Text Gavin that I am late.", action_likely=True).missing_information)
check("behavior examples are selected by situation",
      "Moved it to 3:30" in conversation_examples.select("move it", turn))

print("\n— tasks survive turns and carry verified events —")
task_id = task_state.begin_or_continue(42, turn)
task_state.save_interpretation(42, turn, task_id)
saved = task_state.active_for(42)
check("the active goal is persisted", saved and saved["id"] == task_id)
continued = understanding.resolve("Make it 4 instead.", active_task=saved)
check("referring correction reuses one task",
      task_state.begin_or_continue(42, continued) == task_id)
outcome = outcomes.normalize("set_reminder", {"time": "4 PM"}, "Reminder set.",
                             is_failure=lambda value: False)
task_state.record_action(task_id, "set_reminder", outcome)
check("one verified step does not prematurely complete the whole task",
      task_state.list_recent()[0]["status"] == "active")
check("the task card names verified progress",
      "Reminder set" in task_state.format_for_prompt(task_state.active_for(42)))
check("the whole turn explicitly completes the task",
      task_state.complete(task_id, "Reminder set for 4 PM.")
      and task_state.list_recent()[0]["status"] == "completed")
again = understanding.resolve("Do it again.", active_task=task_state.active_for(42))
check("a completed task remains available as a follow-up referent",
      again.continues_task_id == task_id)

print("\n— task scratchpad separates actions from observations —")
scratch_turn = understanding.resolve(
    "Open Outlook and click New mail", action_likely=True)
scratch_id = task_state.begin_or_continue(84, scratch_turn)
task_state.set_plan(scratch_id, 2, ["Open Outlook", "Click New mail"])
failed_open = outcomes.normalize(
    "open_app", {"name": "outlook"},
    "I couldn't verify that Outlook opened.",
    is_failure=lambda _value: True)
task_state.record_action(scratch_id, "open_app", failed_open)
task_state.record_observation(
    scratch_id, "ui_inspect", {"query": "mail"},
    "AXWindow: Mail - Outlook - Google Chrome\nAXButton: New mail")
scratch = task_state.active_for(84)
scratch_progress = task_state.progress(scratch_id)
check("later page evidence resolves a false-negative launch",
      scratch_progress["unresolved_failures"] == 0
      and scratch["failed_steps"][0]["resolved"])
check("recovered state is explicit that causation is unknown",
      scratch["completed_steps"][0]["changed"] == "unknown")
check("an observation does not itself satisfy the remaining click",
      scratch_progress["verified"] == 1 and scratch_progress["remaining"] == 1
      and not scratch_progress["ready"]
      and "Click New mail" in scratch_progress["next_step"])
click = outcomes.normalize(
    "ui_press", {"label": "New mail"},
    "Pressed New mail; verified that To appeared.",
    is_failure=lambda _value: False)
task_state.record_action(scratch_id, "ui_press", click)
check("the scratchpad names zero remaining work only after the real action",
      task_state.progress(scratch_id)["ready"]
      and "audit" in task_state.progress(scratch_id)["next_step"].lower())
scratch_card = task_state.format_for_prompt(task_state.active_for(84))
check("the prompt card exposes progress, changed state, evidence, and next step",
      "progress=2/2" in scratch_card and "changed=unknown" in scratch_card
      and "latest observed state=" in scratch_card
      and "next required step=" in scratch_card)

with sqlite3.connect(db) as conn:
    conn.execute("CREATE TABLE chat_turns(id INTEGER PRIMARY KEY,session_id INTEGER,"
                 "role TEXT,content TEXT,ts TEXT)")
    conn.executemany("INSERT INTO chat_turns VALUES(?,?,?,?,?)", [
        (1, 42, "user", "Open Google.", "2026-09-03T10:00:00"),
        (2, 42, "ted", "Opened Google.", "2026-09-03T10:00:01"),
        (3, 42, "user", "Do it again.", "2026-09-03T10:01:00"),
    ])
    conn.commit()
restored = task_state.load_chat_history(
    42, exclude_trailing_user="Do it again.")
check("reopening a chat restores role-correct model history",
      restored == [{"role": "user", "content": "Open Google."},
                   {"role": "assistant", "content": "Opened Google."}])

print("\n— inferred relationship lessons wait for Charlie —")
proposal = relationship.save(
    "interaction_lesson", "short_answers", "Keep routine answers short.",
    confidence=.72, evidence=[{"turn_id": 1}], source="test")
check("an inference starts proposed", proposal["status"] == "proposed")
check("proposals are absent from working context",
      "Keep routine" not in relationship.working_context())
check("Charlie can approve it", relationship.review(proposal["id"], "approve"))
check("approved lessons enter working context",
      "Keep routine answers short" in relationship.working_context())

with sqlite3.connect(db) as conn:
    conn.execute("CREATE TABLE turn_log(id INTEGER PRIMARY KEY,rating INTEGER,feedback_reason TEXT)")
    conn.executemany("INSERT INTO turn_log VALUES(?, -1, 'Wrong tone')", [(10,), (11,)])
    conn.commit()
repeat = relationship.propose_from_feedback("Wrong tone", 11)
check("repeated feedback creates a reviewable proposal",
      repeat and repeat["status"] == "proposed" and len(repeat["evidence"]) >= 2)

real_chat_create = providers.chat_create
providers.chat_create = lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
    message=SimpleNamespace(content='{"observations":[{"key":"directness","value":"Charlie prefers direct replies.","kind":"preference","confidence":0.8,"explicit":true,"quote":"be direct"}]}'))])
reflected = relationship.reflect_session([
    {"role": "user", "content": value} for value in
    ("first substantial thought", "second substantial thought", "be direct please", "fourth thought")])
providers.chat_create = real_chat_create
check("session reflection promotes only a quote found in Charlie's words",
      reflected and reflected[0]["status"] == "active" and reflected[0]["explicit"])

print("\n— every tool can expose one outcome shape —")
bad = outcomes.normalize("send_message", {}, "I couldn't send it.",
                         is_failure=lambda value: "couldn't" in value)
check("legacy failure strings become typed failures",
      not bad.ok and not bad.matches_goal and bad.recoverable)
check("outcomes carry expected and observed state",
      bad.expected_state["tool"] == "send_message" and bool(bad.observed_state))

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
