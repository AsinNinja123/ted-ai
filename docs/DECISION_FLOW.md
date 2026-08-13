# How Ted Decides What To Do

**Updated:** August 13, 2026
**Scope:** the full path from "user says something" to "Ted answers," which files
own each step, and where the weak points are.

---

## The one-paragraph version

Every message first passes a few cheap controls that must be immediate: mute,
stop, UI controls, pending confirmations, and timers/reminders. Ordinary chat
and computer requests then enter **one streamed reasoning loop** with the entire
tool menu attached. The model can answer, call independent tools together, use a
result in a later dependent call, or search the live web. This replaces the old
keyword ladder and separate tool-probe call that frequently stole requests before
the reasoning model could understand the whole outcome.

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

### Gate 5 — Narrow deterministic controls
`_assistant_command()` still contains legacy handlers, but `_use_deterministic_command()`
now allows only local/stateful controls to claim a normal turn: timers, reminders,
explicit personal-memory edits, Ted's modes/shortcuts, mic setup, and proactive
trigger management. Requests involving apps, websites, screen, calendar,
notes, web, clipboard, computer control, habits, weather, music, and calculations
reach the reasoning model and its tool menu. `TED_LEGACY_LADDER=1` restores the
old behavior temporarily.

### Gate 6 — One reasoning and tool loop
The normal path is now one streamed request carrying the full tool menu. It can
answer directly or emit calls. Calls are schema-validated before dispatch; invalid
arguments are returned to the model for repair without executing anything. The
loop supports parallel and dependent calls, is capped at five rounds/ten calls,
and blocks duplicates. Verified action results remain the user-visible truth.
Messages and consequential email changes require a pending yes/no confirmation.

The provider layer tries free Groq Qwen 3.6 first and repeats the same request on
local Ollama Qwen 3.5 35B-A3B if Groq is unavailable. `_try_tools()` now exists
only behind the `TED_LEGACY_LADDER=1` escape hatch.

### Gate 7 — Built-in actions (inside the LLM path)
`detect_action()` in **`core/actions.py`** runs at the top of `ask_streaming()` —
date and location questions only. App launches now use the shared tool loop so
they can participate in larger plans.

> **Oddity worth fixing:** this is a *fourth* place where "is this a command?" is
> decided, and it lives inside the LLM module rather than alongside the other
> gates. It should be folded into Gate 5 or 6. Right now, three different files
> can each independently decide your message is a command.

### Gate 8 — Streaming conversation (`ask_streaming`)
**File:** `core/llm.py`. This is Ted-as-chatbot. In order:

1. **Parallel memory retrieval** (four threads, so they overlap):
   - recent related exchanges — FTS5 keyword search (`core/memory.py`)
   - known facts about you (`facts` table)
   - personal knowledge base (ChromaDB, `core/knowledge.py`)
   - past session + chat-thread summaries (`core/memory.py`)
2. **Assemble the prompt:** `[static system prompt] [history] [per-turn context]
   [your message]` — in that order, deliberately, so the static prefix stays
   byte-identical and Groq's prefix cache can skip reprocessing it.
3. **Mode line** — VOICE (short, spoken, no formatting) or CHAT (full answers,
   fenced code). Regenerated every turn.
4. **Reason or use tools** — the brain answers directly or selects tools such as
   `web_search`; results return into the same bounded loop for synthesis or the
   next dependent call.
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

## What was changed, and what remains

**Completed:** Gate 5 is narrow, the tool probe and conversation pass are one
streamed loop, malformed calls are validated and repaired before execution,
dependent calls can continue for up to five rounds, duplicate calls are blocked,
and free hosted requests fall back to the same workflow on local Ollama.

**Next architectural cleanup:** replace the separate pending-compose,
disambiguation, and confirmation instance variables with one typed pending-flow
object. This is maintainability work, not a blocker for normal use.

**Future paid reasoning (disabled):** add an explicit, user-approved "deep task"
provider only when Charlie chooses to fund it. Do not silently route ordinary
turns to a paid API. The current everyday and offline paths remain free.

---

## Two structural notes for the rebuild

- **Cheap controls before reasoning are still useful.** They now cover Ted's own
  immediate state rather than trying to understand every possible user request.
- **There is one normal command decision point.** `_try_tools()` survives only as
  a temporary environment-flag rollback path; `detect_action()` handles only
  date/location shortcuts. The model tool loop owns ordinary intent.
