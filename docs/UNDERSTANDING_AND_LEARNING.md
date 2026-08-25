# Ted's understanding and learning loop

The August 25 upgrade adds an inspectable layer around the existing single
streamed model loop. It does not replace Ted with a framework or add another
cloud call to every message.

## Turn flow

1. Expand Charlie's saved lingo.
2. Build a local `TurnInterpretation`: mode, goal, references, constraints,
   missing information, confidence, clarification policy, and retrieval sources.
3. Resolve referring language against a durable active task or recent verified
   action before asking Charlie to repeat himself.
4. Give the main model the original words plus the advisory interpretation,
   active task, approved relationship memory, one situation-matched behavior
   example, and live operational state.
5. Execute tools through the existing bounded loop.
6. Normalize each result into an `ActionOutcome` and record whether observed
   state matched the goal.
7. Keep failed or confirmation-blocked tasks waiting; mark verified successes
   complete.

## Memory boundaries

- `facts`: biography and durable facts.
- `lingo`: Charlie's shorthand before routing.
- `session_summaries`: episodic conversation memory.
- `active_tasks` and `task_events`: commitments and work in progress.
- `relationship_memory`: interaction preferences and higher-level lessons.

Explicit relationship preferences can be active immediately. Inferences from a
session or repeated negative feedback are `proposed` and appear in the dashboard
for approval or rejection. Ted never silently turns one exchange into a permanent
personality rule.

## Evaluation

`python tools/run_charlie_evals.py` runs 55 stable scenarios without touching the
Mac or an API. They cover references, corrections, casual conversation, long-term
memory, multi-step actions, freshness, and consequential actions. Every fixed
misunderstanding should become another scenario or regression test.
