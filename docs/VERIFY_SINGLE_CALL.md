# Verifying the single-call migration

One sitting, ~20 minutes, in order. Everything here needs the real Groq API and
real macOS, which is why none of it could be checked before you ran it.

Launch so you can see stdout and barge-in debug at once:

```bash
cd ~/ted-ai && source venv/bin/activate
TED_DEBUG_BARGE=1 python hud.py
```

Keep the terminal visible. Three log lines carry most of the answer:

| Line | Means |
|---|---|
| `[timing] first token 812ms` | how long until text started |
| `[tools] open_app({'name': 'spotify'}) → Opening Spotify.` | a tool ran, with its real result |
| `[timing] tool probe …` | **should never appear again.** If it does, you're on the legacy path |

---

## Phase 1 — does it work at all (2 min)

This is the one unproven assumption: that Groq streams tool calls in the delta
shape the code reassembles. If it doesn't, nothing else in this document
matters, and it fails loudly rather than subtly.

| # | Type this | Expect | Red flag |
|---|---|---|---|
| 1.1 | `hey, what's up` | Text streams in. `[timing] first token`. **No `[tools]` line, no probe line.** | A probe line means the legacy path is on |
| 1.2 | `open spotify` | Spotify opens. `[tools] open_app(...)`. Reply is exactly the tool's words | Ted describes opening Spotify without a `[tools]` line = tool calls aren't being parsed |

If 1.2 produces a chatty non-answer and no `[tools]` line, stop. That's the
delta-shape assumption failing. Fall back with `TED_LEGACY_LADDER=1 python hud.py`
and tell me what the reply was.

---

## Phase 2 — the honesty rule (3 min)

The single most important behavior to protect, and the one most likely to break
in a merge like this. **Quit Spotify completely first** (Cmd-Q, not just close
the window).

| # | Type this | Expect | Red flag |
|---|---|---|---|
| 2.1 | `pause the music` | Something like `Spotify isn't open.` and nothing else | **Any cheerful preamble before it** — "Sure, pausing that for you! Spotify isn't open." That's the buffer failing |
| 2.2 | — | HUD sphere goes yellow, issue toast shows the same text | Sphere stays green — the failure hook isn't firing |
| 2.3 | `close notes` (with Notes already closed) | Honest "not open"-style reply, no invention | A confident "Closed it." |

Read 2.1's reply carefully. The failure mode is not a crash — it's Ted sounding
pleased about something that didn't happen.

---

## Phase 3 — the rest of the tool paths (5 min)

| # | Type this | Expect | Watch for |
|---|---|---|---|
| 3.1 | `open spotify and close notes` | Both run. Two `[tools]` lines. Reply is both results joined | Only one fired → parallel calls aren't being reassembled |
| 3.2 | `what's the weather` | Weather answer, narrated in Ted's voice not raw data. **Two** model calls | Raw handler output verbatim = it was misclassified as an action tool |
| 3.3 | `check the weather and put it in my notes` | Chain: `get_weather` then `notes_add`, then a reply. Note actually appears in Notes.app | Stops after the first tool |
| 3.4 | `if I buy 3 things at 45 dollars each and add 7 percent tax, what's the total` | `[tools] calculate(...)` → `144.45` | No `[tools]` line = the model did the arithmetic itself, which is the thing the tool exists to prevent |
| 3.5 | `what's 8 percent of 250` | Probably answered by gate 5's regex, no `[tools]` line | Either is fine — this one exists to get logged in Phase 7 |

### 3.6 — facts reaching the tool decision

This regressed once before and the new path routes facts differently, so it's
worth the two turns.

```
from now on open youtube in brave
```
then, as a **separate** turn:
```
open youtube
```

Expect `[tools] browse_to({'browser': 'Brave', 'site': 'youtube.com'})`. If it
opens in your default browser instead, the preference isn't reaching the
decision — that's a real regression, tell me.

---

## Phase 4 — is it tool-happy (3 min)

The risk I flagged: with 30 schemas in front of it every turn, the model may
reach for a tool when you just wanted to talk. Each of these should produce
**text only, no `[tools]` line**.

```
what do you think about me switching Ted to a chat-first design
explain what prefix caching actually does
i'm tired of debugging audio
what's the difference between a regex and a parser
```

One stray tool call here is noise; a pattern is a prompt problem worth fixing.
Note which ones misfire — the phrasing matters more than the count.

---

## Phase 5 — latency and the prompt cache (4 min)

The premise of the whole change is that one call beats two. Prove it.

Type **twelve** short conversational turns in a row — anything, keep them
short — and write down `[timing] first token` for each.

| Looking for | Meaning |
|---|---|
| Roughly flat across all twelve | `stable_window` is holding the prefix. Correct |
| Fast for ~4 then a cliff | The old "fast for four replies then slow" bug is back |
| Every turn slow | Prefix cache isn't hitting at all — likely the tool guidance or system prompt isn't byte-identical per turn |

Compare the average against the legacy path for the honest number:

```bash
TED_LEGACY_LADDER=1 python hud.py     # same twelve turns, compare
```

If the new path isn't meaningfully faster, the change bought nothing and I'd
want to know before you build on it.

---

## Phase 6 — voice mode and barge-in (4 min)

Barge-in has never been verified since the Aug 5 rewrite. You're already here.

| # | Do this | Expect |
|---|---|---|
| 6.1 | Click the mic on. Ask something with a long answer | Replies are short and spoken, no markdown read aloud |
| 6.2 | Ask `which mode are you in` | Says voice |
| 6.3 | Interrupt him **mid-sentence** | Stops immediately |
| 6.4 | Interrupt him **at a pause between sentences** | Stops. **This is the case that was broken** |
| 6.5 | Play music on speakers, let Ted talk over it | He doesn't interrupt himself. AEC is gone, so this rests on energy + VAD + pitch |
| 6.6 | Type while he's speaking | Typing still interrupts |
| 6.7 | Mic off. Ask `which mode are you in` | Says chat, formats properly, code in fenced blocks |

### 6.8 — mute, all four states (the bug that was hiding)

In order:

```
mute yourself        →  goes quiet, mic off
mute yourself        →  "Mic's already off."   ← the bug: used to reach the model
unmute               →  "I'm back — listening."
unmute               →  "Mic's already on."
```

Any of these producing a conversational reply *about* muting means the fix
didn't take.

---

## Phase 7 — memory, logs, shutdown (3 min)

```
remember that I'm testing the single-call rewrite tonight
what do you know about me
```

Then have a real 10+ turn conversation about something substantive, and
**close the window** (don't Ctrl-C).

```bash
sqlite3 -box ~/ted-ai/data/memory.db "SELECT id, exchanges, topics, text FROM session_summaries"
```

A new row should exist. `[memory] shutdown: nothing worth remembering this
session` is **correct** for a session of only test commands — selective memory
is working as designed, not failing.

Then:

```bash
python tools/gate5_report.py
```

Every deterministic command you hit tonight should be listed. An empty report
means the logging isn't wired.

```bash
cat ~/ted-ai/ted_errors.log       # should be empty or old
```

---

## Phase 8 — the revert switch

```bash
TED_LEGACY_LADDER=1 python hud.py
```

Ask anything. `[timing] tool probe` should reappear. That confirms the escape
hatch works, which is what makes the new path safe to leave on.

---

## What to report back

Only these matter:

1. Did Phase 1.2 produce a `[tools]` line? (yes/no — everything hangs on it)
2. Phase 2.1's reply, **verbatim**.
3. The twelve first-token numbers from Phase 5, and the legacy comparison.
4. Which Phase 4 prompts triggered a tool.
5. Anything in `ted_errors.log`.

Phases 1, 2 and 5 are the ones that decide whether this change stays. The rest
is coverage.
