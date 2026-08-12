# How Ted Decides What To Do

**Written:** August 11, 2026
**Scope:** the full path from "user says something" to "Ted answers," which files
own each step, and where the weak points are.

---

## The one-paragraph version

Every message falls down a **ladder of eight gates**. Each gate asks "is this
mine?" and either handles the message and stops, or passes it down. The gates
run cheapest-first: hardcoded string matches, then regex rules, then a fast LLM
call to pick a tool, and finally the full streaming LLM for real conversation.
The first gate that claims the message wins — nothing below it ever runs.

That design is why Ted is fast. It's also the source of most of his weird
behaviour, because **a gate near the top can grab a message that a gate near the
bottom would have handled better**, and once a gate claims a message there is no
appeal.

---

## The ladder, step by step

Everything below happens inside `TedApi._respond()` in **`core/app.py`** unless
noted. That method is the spine — if you want to understand Ted, read it top to
bottom.

### Step 0 — Input arrives
Two entry points, both landing in the same place:

| Source | Path | File |
|---|---|---|
| Typed in the chat box | `ask()` → background thread → `_respond()` | `core/app.py` |
| Spoken | `conversation_loop()` → `capture()` → wake-word strip → `_respond()` | `core/app.py`, `core/audio.py`, `core/voice.py` |

Voice adds transcription first (Groq Whisper or local Whisper) — that's
`core/voice.py`. Everything after this point is identical for voice and text,
which is why a bug in one shows up in the other.

---

### Gate 1 — Mute / unmute
Literal phrase checks (`"unmute"`, `"mic on"`, …). Handled instantly, no LLM.
**File:** `core/app.py`.

### Gate 2 — Stop / cancel
`_is_stop_command()`. Cuts off speech; if Ted wasn't talking, assumes you meant
the music and pauses Spotify. **File:** `core/app.py` + `core/intents.py`.

### Gate 3 — UI commands
"open chat log", "show reminders", "repeat that", "speak faster". Pure UI, never
reaches the brain. **File:** `core/app.py`.

### Gate 4 — Pending multi-turn flows
If Ted asked you a question last turn ("which John?", "what should the message
say?"), your answer is routed to that flow instead of being treated as a new
request. **File:** `core/app.py` (`_handle_pending_compose`,
`_resolve_msg_disambiguation`).

> **This is Ted's only real conversational state machine, and it's ad-hoc.** Each
> flow is a hand-written pair of instance variables and a resolver method. Adding
> a third or fourth flow means more of the same. In the rebuild this should be one
> generic "awaiting answer" object on the bus, not N bespoke variables.

### Gate 5 — Deterministic assistant commands
`_assistant_command()` — **746 lines, ~50 regexes, ~64 branches** in
`core/app.py`. Timers, reminders, habits, "remember that…", calendar phrasing,
email setup, and more. No LLM: pure pattern matching, so it's instant and
predictable.

> **This is the single biggest liability in the codebase.** It's the "hardcode
> every scenario" feeling you described, in literal form. It runs *before* the
> model gets a say, so any phrasing its regexes catch is decided without
> intelligence, and any phrasing they miss falls through to a model that may not
> have the matching tool. Worse, a regex written for one intent can swallow a
> message meant for another.
>
> **Improvement:** shrink this gate to only the things that genuinely must be
> deterministic (stop, mute, timers — where a wrong LLM read is costly), and let
> the tool loop own the rest. Every regex deleted here makes Ted feel more
> intelligent, because the model gets to see more of your messages.

### Gate 6 — The tool loop (`_try_tools`)
This is Ted's actual reasoning step. **File:** `core/app.py`, with the tool menu
in `core/tools.py` and the implementations in `core/tool_handlers.py`.

1. Build a compact prompt: short Ted identity + **known facts about you** +
   last ~8 messages + your message.
2. Send it to Groq with all ~30 tool schemas attached (`tool_choice="auto"`).
3. **Round 1 is a probe.** If the model doesn't call a tool, it replies `CHAT`
   and we bail immediately to Gate 8 — this keeps conversation fast.
4. If it *does* call tools: execute each one via `_dispatch_tool()`, feed the
   real results back, and loop (max 3 rounds).
5. Action tools (things that change the world) report ground truth and Ted
   speaks their result **verbatim** — deliberately, so he can't turn "Spotify
   isn't open" into a cheerful "Playing your music!"

> **Improvement — the big one:** Gates 6 and 8 are two separate LLM calls for
> every single message. Real chat assistants (including me) use **one** streamed
> call that can either emit text *or* emit a tool call. Merging them halves the
> per-turn latency and eliminates the discarded probe. This is the highest-value
> change left, and it's a rebuild-era change because it means rewriting
> `_respond`'s core.
>
> **Improvement — cheaper:** ~30 tool schemas are re-read on every probe. Tool
> descriptions are prompt tokens. Trimming verbose ones, or splitting the menu
> into groups the router picks from, directly cuts time-to-first-token.

### Gate 7 — Built-in actions (inside the LLM path)
`detect_action()` in **`core/actions.py`** runs at the top of `ask_streaming()` —
date questions, location questions, app launches. Answers without any model call.

> **Oddity worth fixing:** this is a *fourth* place where "is this a command?" is
> decided, and it lives inside the LLM module rather than alongside the other
> gates. It should be folded into Gate 5 or 6. Right now, three different files
> can each independently decide your message is a command.

### Gate 8 — Streaming conversation (`ask_streaming`)
**File:** `core/llm.py`. This is Ted-as-chatbot. In order:

1. **Web check** — `_needs_web()`; if live info is needed, Groq's compound model
   searches, with DuckDuckGo as fallback.
2. **Parallel memory retrieval** (four threads, so they overlap):
   - recent related exchanges — FTS5 keyword search (`core/memory.py`)
   - known facts about you (`facts` table)
   - personal knowledge base (ChromaDB, `core/knowledge.py`)
   - past session + chat-thread summaries (`core/memory.py`)
3. **Assemble the prompt:** `[static system prompt] [history] [per-turn context]
   [your message]` — in that order, deliberately, so the static prefix stays
   byte-identical and Groq's prefix cache can skip reprocessing it.
4. **Mode line** — VOICE (short, spoken, no formatting) or CHAT (full answers,
   fenced code). Regenerated every turn.
5. **Stream** tokens to the HUD, sentence-by-sentence to the speaker if voice
   mode is on.
6. **Afterwards, on background threads:** save the exchange, extract facts
   (gated so only statements about *you* are saved), log topic patterns.

---

## Which file owns what

| File | Role | Size/risk |
|---|---|---|
| `core/app.py` | The ladder, tool dispatch, all the deterministic commands | ~103KB. **The monolith.** Highest-risk file in the project |
| `core/llm.py` | Prompts, streaming, memory assembly, fact extraction, web search | Second most important |
| `core/tools.py` | The tool *menu* the model sees (schemas only) | Small, safe to edit |
| `core/tool_handlers.py` | What each tool actually does | Medium |
| `core/memory.py` | SQLite: exchanges, facts, sessions, habits, FTS5 search | Clean, well-bounded |
| `core/intents.py` | Pure phrase-matching helpers (unit-tested) | Small, safe |
| `core/actions.py` | App/URL/Spotify launchers + `detect_action` | Medium |
| `core/voice.py` / `core/audio.py` | STT, TTS, playback, barge-in | Independent of decisions |
| `dashboard/` | Memory dashboard + chat-session storage | New, isolated |

---

## The three changes worth making, in order

**1. Merge Gates 6 and 8 into one streamed call.**
Biggest speed and coherence win. One model call that can talk *or* act, instead
of a probe followed by a second call that redoes the same reading. Requires
touching `_respond` — do it as part of the rebuild, not as surgery on the
monolith.

**2. Gut Gate 5.**
Delete every regex that isn't protecting something time-critical or dangerous.
Each deletion moves a decision from "hardcoded" to "reasoned." This is the direct
antidote to the problem you described, and unlike #1 it can be done incrementally
and safely — delete a few regexes, use Ted for a day, see if anything got worse.

**3. Route by difficulty.**
Currently every turn goes to the same Groq model. Your original plan called for
frontier models on hard turns. A cheap classifier ("is this trivial or hard?")
that sends hard ones to Claude would raise Ted's reasoning ceiling more than any
prompt tuning. Needs an Anthropic key — the config slot exists and is empty.

---

## Two structural notes for the rebuild

- **The ladder is fundamentally sound.** Cheap gates before expensive ones is
  correct design. The problem isn't the ladder, it's that too many rungs are
  hardcoded and two rungs do the same LLM work twice.
- **Four files can each decide "this is a command."** `_respond`,
  `_assistant_command`, `detect_action`, and `_try_tools`. In the event-bus
  rebuild this should be exactly one stage emitting one decision event, which is
  precisely what the `stages/` design in the handoff was for.
