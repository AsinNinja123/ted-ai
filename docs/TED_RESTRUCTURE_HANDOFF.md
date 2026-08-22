# Ted — Restructure Handoff

**Written:** 2026-08-20 · **Updated:** 2026-08-22
**For:** an AI or developer with no prior context, who can see the `~/ted-ai` repo as it stands today

---

## Status — read this first

Steps 0–4 of §17 have landed, and step 7 has started. What exists **and is live**:

- `core/events.py` — one bounded event bus, SSE at `/api/events`, thought bubble in the HUD.
- `core/agents/base.py` — `AgentResult`, `Plan`, `Delegation`, `ConfirmationGate`, `BaseAgent`.
- `core/agents/mac.py` — **`MacAgent`, wired into the live path.** All 13 Mac tools route
  through it; `clean_up` closes every open app in one call instead of one model round trip
  per app.
- The cleanup lane in `_respond` — a pattern decides *cleanup, not four closes* (zero
  tokens); `llama3.2:3b` reads any tail like "but leave Brave" (~0.1s, no cloud tokens).

**The Code Book is the reference, not this document.** Chapter 36 covers the agent layer
end to end — the contract, the async bridge, `_from_agent`, who decides "consequential",
the event stream, and how to add the next agent. §7.7 covers the cleanup lane. Read those
before changing anything under `core/agents/`.

What has **not** happened: the other six agents, deleting `_dispatch_tool`'s 508 lines,
deleting `_assistant_command_impl`'s 761, rewriting `_respond`, decoupling voice, and the
provider budget work. Sections 7–17 below are still the plan for those.

---

## 0. How to use this document

This is a design and execution brief for restructuring Ted. It assumes you can read
the repo but know nothing about the conversations that produced it.

Read sections 1–4 before touching anything — they explain what Ted is and exactly
how he is broken. Sections 5–12 are the target design. Section 13 is the order of
work. Section 15 lists things that are decided and must not be relitigated.

**Do not trust numbers in this document over the code.** Every figure here was read
off the repo on 2026-08-20. Run `python tools/ted_map.py` and `wc -l` yourself before
relying on any of them.

---

## 1. What Ted is

Ted is Charlie's own AI assistant, running on his Mac. Not a voice assistant —
that is legacy framing that still appears throughout older comments, docs, and
variable names. **Ted is a personalized chatbot that also acts.**

The one-sentence version:

> Ted knows Charlie from a persistent editable memory database, answers in a desktop
> chat window, can speak and listen when asked to, can operate his Mac and his accounts
> through 63 tools, and is meant to eventually replace the Claude app and ChatGPT as his
> daily driver.

Voice still exists and still matters, but it is now a **rare** mode — Charlie is at
college and usually around people. Design for text-first, voice-capable. Not the
reverse. This is a reversal from Ted's original framing and it is load-bearing for
every decision below.

### Why he is building it

1. A daily driver he owns, on Mac and eventually phone.
2. Something that actually knows him — the memory system is the point, not a feature.
3. Proactive involvement: class reminders, schedule, to-dos, unprompted suggestions.
4. Portfolio. He is a CS sophomore building toward AI/automation internships.
5. Practical skill-building — real audio pipelines, real SQLite migrations, real latency work.

### What Ted is not

Not the AI automation agency. Charlie runs a separate side business whose stack is
n8n + Airtable + Claude API + Twilio + Vercel, with projects in `~/Dad`,
`~/budget-blinds`, `~/todo-list`. **If you find a note claiming that is Ted's stack,
it is wrong.** Ted's real stack is §3.

---

## 2. The repo as it stands

```
~/ted-ai/
├── core/            34 modules, the actual assistant
├── dashboard/       Flask server + memory/diagnostics/notebook web UIs
├── ui/              ted_hud.html (2,068 lines) + two legacy HUDs
├── native/          Swift audio engine (ted_audio.swift) + launcher
├── docs/            TED_CODE_BOOK.md/.pdf, DECISION_FLOW.md, handoffs
├── tests/           characterization tests — these are your safety net
├── tools/           ted_map.py — reads the code and reports real structure
├── data/            memory.db (gitignored), ted_launch.log
├── inbox/           drop PDFs here, then "index my documents"
└── _to_delete/      dead code, 20 files, ~1,314 lines — delete on sight
```

**Size:** 37,608 lines total — 31,706 Python, 5,599 HTML, 303 JS.

**Ten biggest Python files:**

| File | Lines | Note |
|---|---|---|
| `core/app.py` | 3,973 | the monolith |
| `core/llm.py` | 2,055 | prompt assembly + streaming |
| `core/tools.py` | 1,329 | 63 tool schemas, pure data, 0 functions |
| `core/providers.py` | 975 | the single door to every model call |
| `core/intents.py` | 914 | pure text helpers, testable |
| `tools/ted_map.py` | 912 | code introspection tool |
| `core/tool_handlers.py` | 886 | the tools' actual implementations |
| `core/memory.py` | 874 | **the crown jewel — do not touch** |
| `core/routing.py` | 743 | cheap pre-model routing |
| `core/spotify_web.py` | 730 | |

**Three functions inside `core/app.py` account for 1,699 lines — 43% of the file:**

| Function | Lines |
|---|---|
| `_assistant_command_impl` | 761 |
| `_dispatch_tool` | 508 |
| `_respond` | 430 |

Those three are the restructure. Everything else in §13 is downstream of them.

**Git:** work happens on branch `arch/single-call`, not `main`. Charlie also runs
ChatGPT (Codex) against this repo — read `docs/AI_WORKFLOW.md` before editing. If
`git status` shows files you did not modify, someone else is mid-task; say so
rather than editing them.

**Runtime:** venv is Python 3.12 (system python3 is 3.10 — use the venv).

---

## 3. Current stack

| Layer | What |
|---|---|
| Brain | `qwen/qwen3.6-27b` on Groq's **free tier** — chat, tools, fact extraction, summaries, vision, web synthesis all through this one model |
| Fallback | `qwen3.5:35b-a3b` on local Ollama, automatic when Groq is absent/down/limited |
| Speech in | Groq-hosted `whisper-large-v3-turbo` by default; local Whisper when `USE_GROQ_STT=False` |
| Speech out | Kokoro local TTS, voice `am_michael`; ElevenLabs behind a config flag, unused |
| Audio engine | Swift (`native/ted_audio.swift`) over a Unix socket, sounddevice fallback |
| Memory | SQLite `data/memory.db` — FTS5 keyword search, WAL mode |
| UI | `ui/ted_hud.html` — chat transcript + saved-session sidebar |
| Web/UI transport | Flask REST on `127.0.0.1:5175`, **polled** by the HUD on timers |

---

## 4. How Ted is broken — evidence, not theory

All of this is from `data/ted_launch.log` and `ted_errors.log` on 2026-08-19.

### 4.1 One user intent costs many API calls — fixed for cleanup, still true elsewhere

Charlie typed **"clean up"**. Ted closed Terminal, Finder, Code, and ChatGPT — as
four separate round trips to the model:

```
[tools] close_app({'name': 'Terminal'}) → Closed Terminal.
[timing] round 2 after 2186ms
[tools] close_app({'name': 'Finder'}) → Closed Finder.
...
[timing] round 2 after 12586ms
[tools] close_app({'name': 'ChatGPT'}) → Closed ChatGPT.
```

The model decides one step, waits for the result, decides the next. Four closes =
four full requests against a rate-limited free tier. **This is the core disease.**

`MacAgent` plus the cleanup lane fixed this exact case — "clean up" is now one turn at
~0.1s with no cloud call. The disease is still present everywhere the other six agents
have not been built.

Worth knowing before you fix the next one the same way: giving the model a `clean_up`
tool did **not** work. It was in the menu, listed first, with a description ending "Do
NOT chain close_app calls to do this." qwen3.6-27b chained `close_app` anyway, twice.
The decision had to move out of the model. Code Book §7.7.

### 4.2 The token diet crashes him

`core/routing.py::select_tool_schemas` narrows the tool list per turn to keep the
prompt cheap. The log shows it working:

```
[prompt] scope=none tools=3 ~1885 input tokens
```

Then this, from `ted_errors.log`:

```
groq.APIError: tool call validation failed:
attempted to call tool 'browse_to' which was not in request.tools
```

The model believed it had a tool that was not sent, called it, and the entire stream
died. There is a `find_tools` meta-tool meant as the escape hatch — the model did not
use it. **Dynamic tool subsetting is a band-aid on the token problem, and the band-aid
tears.**

### 4.3 The rate limit math does not work

```
[provider] rate-limit hints disagree: 5.3s/6.0s/47.7s/259.2s — using 5.3s
[provider] RATE LIMITED on qwen/qwen3.6-27b — trying local qwen3.5:35b-a3b; cloud paused for 5.3s
```

Groq free tier is **6,000 tokens/minute**. Turns run ~1,900–6,400 tokens. Sustained,
that is roughly **one message per minute** — and a four-step "clean up" eats the whole
minute. The retry logic is also parsing four contradictory hints and guessing.

### 4.4 The fallback is slow

Same log: a cloud round trip took 2,186ms; the round after falling back to local
Ollama took 12,586ms. Ollama can also need up to three minutes to load a
multi-gigabyte model cold, during which Ted appears frozen. There is a 6-second
budget and a 5-minute cooldown to limit this, but the fallback is a degraded path,
not a real offline mode.

### 4.5 The root cause

All four are one problem:

> **The big model is inside the loop for everything.**

It is asked both *what should happen* and *how to do each step*. Every step of every
task is a network round trip against a per-minute ceiling.

---

## 5. Reference projects — what to steal

Charlie is studying **Naz Louis** (GitHub: `nazirlouis`), a YouTuber who builds DIY AI
assistants. Two of his repos are directly relevant. Both solve §4.5, differently.

### 5.1 `ada_v2` — Gemini live-audio desktop assistant

Electron + React frontend, Python FastAPI + Socket.IO backend, Gemini 2.5 native
audio. Scope is CAD generation, 3D printing, gestures, smart home — narrower than
Ted, but the code matches the scope.

**Take:**

- **Per-domain agent classes.** `CadAgent`, `PrinterAgent`, `WebAgent`, `KasaAgent`.
  The model delegates *once*; the agent runs however many steps it needs on its own.
  This is the direct fix for §4.1.
- **`asyncio.TaskGroup` + bounded queues.** Concurrent tasks (mic in, send, receive,
  play) where one failure cancels the rest cleanly. This is the template for the
  event-bus decomposition that has been scoped in Ted since June and never executed.
- **Generalized tool confirmation.** One mechanism: an `asyncio.Future` per pending
  call, keyed by request id, that blocks until the UI approves or denies. One
  implementation used by every tool, not per-tool special cases.
- **Reconnect with context restoration.** On connection loss, replay recent chat
  history into the new session so the model resumes seamlessly.

**Do not take:** its memory model (per-project JSON files and chat logs — far weaker
than Ted's), its single global `audio_loop` session (no multi-device path), or its
total dependence on one cloud API with no fallback.

### 5.2 `ada_local` — fully offline Windows assistant

Ollama + Whisper + Piper, PySide6 GUI, SQLite chat history. Wake word "Jarvis".

**Take:**

- **A fine-tuned intent router.** He trained a small Gemma model ("FunctionGemma") to
  do one job: classify a request and emit `call:function_name{key:value}`. Regex-parseable,
  deterministic, **50–100ms**, zero API calls. Falls through to a passthrough class
  when it is not a known intent.
- **Optimizing the cheap path separately.** `torch.inference_mode()`, KV caching,
  optional `torch.compile()` on the router only. Right instinct: make the fast path
  fast, leave the smart path smart.
- **Genuine offline capability** as a design goal rather than a degraded fallback.

**Do not take:** its 9 fixed intents (adding a capability means retraining), its
1.5–1.7B models (fine for "turn on the lights", useless for reasoning), its absent
memory layer, or its Windows/CUDA lock-in.

### 5.3 The honest scorecard

Rough maturity ratings, for calibration only:

- **ada_v2 — 71/100.** Narrower scope, but the execution matches it.
- **ada_local — 65/100.** One clever idea (the router), heavily constrained otherwise.
- **Ted — 58/100.** Most ambitious by far, best memory system of the three, worst
  execution debt. The ceiling is highest; the current build is lowest.

---

## 6. The principle

> **The expensive model decides *what should happen*. Cheap local code decides *how*, and does it.**

Ted currently asks the expensive model both questions on every step. Every design
decision below follows from separating them.

---

## 7. Target architecture — three layers

### Layer 0 — Router (local, ~50ms, zero API calls)

Answers one question: **does this request need the brain at all?**

"Turn on the lights", "skip this song", "set a timer for 10", "what's the weather" —
none of those need a 27B model. They need a function call.

**Start deterministic.** `core/intents.py` (914 lines) and `core/routing.py` (743)
already encode most of this knowledge. Build the first router from that, measure the
miss rate, and only train a small classifier later if pattern matching proves brittle.

**There is already a working precedent — read it first.** `routing.py::plan_reflex`
(line 690) handles complete, reversible app open/close requests at **zero tokens**, and
returns `None` the moment anything is unclear so the model gets the turn. That is
exactly the Layer 0 shape, already proven in this codebase. Generalize it; do not
invent a new pattern beside it.

**Critical constraint — read this before writing a single pattern.** In August 2026
Charlie deliberately removed keyword-based triggering for actions because it made Ted
feel like "a robot spitting back answers". `core/routing.py`'s own header documents the
same lesson: an earlier Ted had ~50 regexes that "tried to be the assistant" and any
phrasing the author had not anticipated simply did not work.

The line, stated in that header and still correct:

> **The router picks capabilities. It never decides the answer.**

A router that classifies *intent* with a confidence threshold, and falls through to the
brain whenever it is unsure, is not the thing that was deleted. A router that pattern-matches
surface words and returns a canned reply is. Keep the distinction or you will rebuild
the vending machine.

Ted already has a primitive version — `[router] cloud — tool use needs the stronger
brain [rule]` in the log. That rule is too blunt: "involves a tool" does not mean
"needs the brain."

### Layer 1 — Agents (own a whole task, loop locally)

`ada_v2`'s pattern. The brain delegates once; the agent does the work, including its
own multi-step loops and lookups.

"Clean up" becomes **one** call to `MacAgent`, which enumerates open apps and closes
them in a plain Python loop. One API call, not four.

This also fixes §4.2 properly. Instead of 63 fine-grained tool schemas (~3,645 tokens),
the brain sees **7 agent-level tools plus 4 direct tools** — roughly 900 tokens. The
model always sees the complete set, so dynamic subsetting can be deleted and the
`browse_to` crash class becomes impossible.

### Layer 2 — Brain (the big model, reasoning only)

Conversation, judgment, synthesis, genuinely open-ended requests. Memory feeds it.
It should be handling perhaps a third of the turns it handles today.

### Memory is a service, not an agent

**This distinction matters more than it looks.**

Consider: *"play my favorite song and email my professor that paper we wrote last night."*
Both branches need memory — "favorite song", "professor", "that paper". If memory were a
peer agent, agents would have to message it, and now agents call agents. That is a mesh,
and meshes are where this architecture dies: circular waits, no single place that knows
what happened, and debugging that means tracing a conversation between four components.

So: **every agent reads memory directly, like a database.** Memory is infrastructure.
It is an *agent* only for user-facing operations — "what do you know about me",
"remember this", "forget that".

### Topology rules — non-negotiable

1. **Star, not mesh.** Brain at the center, agents on spokes. Agents never call each other.
2. If two agents must coordinate, **the brain coordinates them** — it is the only
   component that should compose intent.
3. **Parallel across agents, sequential inside one.** Independent branches of a
   compound request run concurrently. A dependency ("find the paper, *then* email it")
   is handled inside a single agent, not chained through the brain.

---

## 8. Agent roster

Mapped from the 63 tools currently in `core/tools.py`.

### 1. `MacAgent` — 13 tools

`open_app`, `close_app`, `press_key`, `type_text`, `scroll`, `system_volume`,
`system_brightness`, `clipboard_read`, `clipboard_write`, `screen_describe`,
`ui_inspect`, `ui_fill`, `ui_press`

Do not split app control from screen/UI control — same job, and splitting forces the
brain to know which to call. This is where "clean up" becomes one call.

### 2. `MusicAgent` — 8 tools

`play_music`, `play_playlist`, `create_playlist`, `delete_playlist`, `add_to_playlist`,
`remove_from_playlist`, `spotify_control`, `now_playing`

Preserve the existing design call: transport (play/pause/skip) stays local and instant;
only *selection* hits the Spotify Web API.

### 3. `CommsAgent` — 9 tools

`get_emails`, `read_email`, `send_email`, `email_action`, `send_message`, `text_respond`,
`bouncer_status`, `bouncer_toggle`, `bouncer_watch`

Email and messages merge deliberately. The reasoning is identical — read, decide,
respond — and "reply to Mom" should not require the brain to first work out which
channel she is on. One agent, channel as a parameter.

### 4. `PlannerAgent` — 9 tools

`calendar_add`, `calendar_get`, `set_reminder`, `get_reminders`, `set_timer`,
`notes_add`, `notes_get`, `log_habit`, `get_habit_streak`

**This is the agent Ted's stated purpose runs on.** Proactive involvement — class
reminders, schedule, to-dos — lives here. Currently scattered across nine tools with
no owner.

### 5. `MemoryAgent` — 11 tools

`search_knowledge`, `add_knowledge`, `search_chats`, `notebook_read`, `notebook_write`,
`notebook_edit`, `notebook_search`, `notebook_delete`, `learn_lingo`, `clarify_lingo`

User-facing memory operations only (see §7). Multi-step recall — "what did I say about
the fireworks inventory in July" — is currently several brain round trips; as an agent
it is one call that searches, filters, and returns.

### 6. `WebAgent` — 3 tools

`browse_to`, `web_search`, `play_youtube`

Small, but this is where `ada_v2`'s ground-truth pattern belongs: after opening a page,
poll the browser for the actual frontmost URL and title (AppleScript to Chrome/Brave/Safari
— `core/actions.py` already drives these) with a short bounded retry, and return that
instead of the intent.

### 7. `CodeAgent` (read-only) — 6 tools

`code_read`, `code_search`, `code_tree`, `code_overview`, `code_diff`, `code_history`

**Drop `code_write`.** Charlie has explicitly decided Ted should not modify his own code
yet — he does not trust the model with a fragile codebase. Keeping the schema means it
stays callable. `core/codebase.py` already refuses writes without `confirmed=True`;
remove the tool from the schema entirely rather than relying on that.

### Not agents — leave as direct tools

`calculate`, `get_weather`, `show_image`, `create_document`

Single-shot, no multi-step work, no state. Wrapping them in agent ceremony adds
indirection for nothing.

Better still: `calculate` and `get_weather` should never reach the brain. Both are pure
router targets — pattern match, call the function, return. `ada_local` hits Open-Meteo
straight from its router with zero LLM involvement.

### Growth slots

Add without touching anything else: **`FilesAgent`** (there is an `inbox/` and document
indexing but no tools for it), **`MediaAgent`** (image generation is a stated want),
**`DeviceAgent`** (if the phone version happens). Each is one file plus one registry line.

---

## 9. The agent contract

Every agent returns the same shape. **This is the most important interface in the rebuild.**

```python
@dataclass
class AgentResult:
    ok: bool
    did: str                    # what actually happened, past tense
    evidence: dict              # ground truth: real URL, real track name, real filename
    failed: str | None = None   # why, if it did
    duration_ms: int = 0
```

**The brain composes its reply only from these fields.** It never narrates what it
*asked* for — only what came back with evidence.

Why this matters: Ted has a documented history of claiming actions he did not take.
There is `[honesty]` logging in the codebase specifically for phantom actions, and
`docs/` contains a fix request for `browse_to` returning intent instead of ground truth.
Making `evidence` a required field of a universal contract turns a per-tool patch into
a structural guarantee.

**Partial failure is the case that will bite you.** Music succeeds, email cannot resolve
the professor's address. Ted must say exactly that — not "done!" and not "I couldn't do
it." With three agents in flight the odds of a wrong summary triple.

### Base class responsibilities

- The `AgentResult` shape.
- **Confirmation gating**, per agent per action — `ada_v2`'s pattern: an `asyncio.Future`
  keyed by request id, blocking until the UI approves or denies. Music plays immediately;
  an email to a professor does not. Configurable, inherited by every future agent.
- **`describe()`** — see §16.
- **`dry_run`** — see §16.

---

## 10. The plan object

The brain emits an explicit plan, not loose tool calls:

```python
@dataclass
class Plan:
    heard: str                  # the brain's parse of the request
    steps: list[Delegation]     # agent + method + args
    parallel: bool
```

Logged as one line:

```
[plan] MusicAgent.play | CommsAgent.send
```

Today the log shows tool calls one at a time with no record of the intent that spawned
them — which is why "clean up" closing four apps reads as four unrelated events. With a
plan object you can see what Ted *intended* versus what the agents *reported*, on one screen.

Worked example — *"play my favorite song and email my professor that paper we wrote last night"*:

1. **Brain call #1** parses and emits two delegations.
2. Both run **in parallel**. MusicAgent resolves "favorite song" against memory and plays
   it. CommsAgent resolves the professor against contacts, resolves "the paper we wrote
   last night" against documents, drafts, and **pauses for confirmation**.
3. **Brain call #2** composes from both results: *"Playing Nights — Frank Ocean. I drafted
   an email to Professor Hensley with essay_v3.docx attached — send it?"*

Two brain calls. Today that same sentence is six-plus round trips, which on the current
rate limit is several minutes.

---

## 11. The thought bubble

A faint, collapsed-by-default trace attached to each of Ted's messages, showing his
reasoning, the actions taken, and whether each succeeded.

```
▸ thought                                              1.4s
    heard  play favorite song · email professor the paper
    plan   MusicAgent.play("favorite song")
           CommsAgent.send(to="professor", attach="paper, last night")

    ✓ MusicAgent    playing "Nights" — Frank Ocean        420ms
    ⚠ CommsAgent    drafted → Prof. Hensley, essay_v3.docx
                    waiting on you                        1.2s
```

Collapsed state is a single faint line: `▸ thought · 2 actions · 1.4s`.

**The rule that makes this worth having:**

> **The bubble renders events. It never renders narration.**

The tempting implementation is to ask the model to explain its thinking and print that.
**Do not.** You get a fluent, plausible story with no causal connection to what ran —
the model inventing an account after the fact. That is the phantom-success problem
wearing a nicer hat, and it is worse than no bubble because it looks like proof.

Render only from the `Plan` object and the `AgentResult` fields. Then the bubble cannot
lie: empty `evidence` means nothing to display.

This makes the feature load-bearing rather than cosmetic — it is the UI enforcement of
the §9 contract, and it is the instrument panel for the rebuild itself. Build it early,
not as final polish. It is how you will see whether "clean up" became one call, whether
parallel branches actually ran in parallel, and where latency sits.

**One emitter.** The bubble and `data/ted_launch.log` must read the same event stream.
Two sources of truth about what Ted did means debugging the disagreement between them.

---

## 12. The event channel — the one piece of new plumbing

`ui/ted_hud.html` currently **polls**: `fetch()` on timers against
`/api/diagnostics/turns`, `/api/provider`, `/api/diagnostics/stats`. There is no
streaming channel anywhere in the codebase — no SSE, no WebSocket.

Polling is wrong for the thought bubble. The trace should fill in as it happens: plan
appears, then each agent line lands as it finishes. Polled, it arrives in lumps after
the fact and the effect is lost.

**Build one SSE endpoint.** Server-Sent Events is a one-way stream where the server
pushes messages to the browser over a held-open HTTP connection; the browser listens
with `EventSource`. Simpler than WebSocket, and one-way is all this needs. One Flask
route in `dashboard/app.py`, roughly 40 lines, and the HUD subscribes instead of polling.

**Build it as a general event channel, not a bubble-specific one.** There is an existing
request for memory-write toasts pushed to the HUD (`Memory updated: …`, clickable to open
the Memory panel). Same pipe. One `emit(kind, payload)` function, one subscriber.

---

## 13. Speech overhaul

### What exists today

`core/voice.py` (728 lines) owns both directions:

- **Hearing:** `capture()` records a turn, transcribes via Groq-hosted
  `whisper-large-v3-turbo` (or local Whisper when `USE_GROQ_STT=False`).
- **Speaking:** `synth()` uses Kokoro, local, voice `am_michael`. ElevenLabs sits behind
  a config flag and is unused. `speak_streaming()` consumes the generator from
  `core/llm.ask_streaming` and speaks sentence-by-sentence as text arrives, so Ted starts
  talking almost immediately.
- **Audio engine:** Swift (`native/ted_audio.swift`) over a Unix socket, sounddevice fallback.

`capture()` has four hallucination gates, in order: duration+loudness (RMS), Whisper's own
no-speech score, an exact-match blocklist of phantom phrases, and a junk-fragment filter.
**Each exists because of a specific real failure** — Whisper returns "Thank you" in a silent
room; coughs came back as "Tep." and "Start." and were *executed as commands*. Deleting a
gate brings its failure back. Keep all four.

### What to change, and what not to

`ada_v2` uses a native-audio model — the model takes microphone audio and produces speech
directly, no separate transcribe → think → synthesize chain. As of August 2026 the options
are **Gemini 3.1 Flash Live** (~200ms to first audio, free tier via AI Studio, strong
function calling) and **GPT Realtime 1.5** (~300ms, ~$0.06/min, native MCP support).

**The honest recommendation: do not rewrite Ted around native audio.**

It is the right architecture for a voice-first assistant. Ted is not one anymore — voice is
now a rare mode (§1). Rebuilding the core around a speech-to-speech model would optimize
the path Charlie uses least, hand the brain to a third provider, and cost the text-mode
memory injection and tool architecture that everything else here depends on.

**Do this instead:**

1. **Decouple voice from the brain.** Voice should be an input/output *mode* around the
   same router → agents → brain pipeline, not a parallel path through it. Today `core/voice.py`
   is coupled to `core/llm.ask_streaming`'s generator. Invert it: the pipeline emits text
   events, and a voice sink subscribes to them — the same event channel from §12.
   This alone removes voice as a source of architectural drag.

2. **Keep Kokoro.** Still competitive for local TTS on Apple Silicon in 2026, already tuned,
   already streaming. No reason to change.

3. **Keep the Whisper gates.** They encode real, expensive lessons.

4. **Make native audio a pluggable voice mode later, not now.** Once voice is a subscriber
   rather than a branch, adding a `NativeAudioMode` that bypasses STT/TTS for
   conversation-only turns is a contained experiment. Gemini Live's free tier makes it cheap
   to try. Defer until the restructure lands.

5. **Fix the actual voice pain, which is latency, not quality.** Ted's slowness in voice mode
   is the §4 problem, not the speech stack. Router + agents fix it. Measure again after.

There is a `docs/BARGE_IN_HANDOFF.md` — read it before touching interruption. Note also that
acoustic echo cancellation was removed on Aug 5; the name still appears in code and comments.

---

## 14. Provider and rate-limit fix

Do this last, after §7 reduces call volume.

1. **Track the token budget locally.** Count what you are about to send and refuse to send
   when it would exceed the window — rather than sending, getting a 429, and parsing four
   contradictory retry hints (§4.3).
2. **Delete `select_tool_schemas` dynamic subsetting** once agents land. The agent-level
   tool list is small enough to always send in full. This removes the §4.2 crash class.
3. **Reconsider the free tier.** Layers 0 and 1 should cut call volume enough that a paid
   tier is cheap, and it removes an entire category of failure. Worth pricing before
   engineering around a 6,000 tokens/minute ceiling.
4. **Keep `chat_create()` as the single door.** That design is correct — one file where a
   model name enters a request. Do not add a second path.

---

## 15. Decided — do not relitigate

- **Memory stays.** `core/memory.py`, `core/knowledge.py`, `core/notebook.py`, the SQLite
  schema, FTS5 search. It is better than anything in either reference project — neither has
  fact extraction, summarization, or searchable recall. Restructure *around* it.
- **Tool implementations stay.** `core/tool_handlers.py` is real work, correctly factored.
  Agents wrap it; they do not replace it.
- **No self-modifying code.** `code_write` comes out of the schema.
- **No keyword-triggered answers.** The router picks capabilities, never answers (§7).
- **Voice is secondary.** Text-first, voice-capable.
- **The characterization tests in `tests/` are the safety net.** Stage 1 of the original
  event-bus migration was writing them, and that is already done. Use them as the gate on
  every cutover step.
- **Charlie writes the code now.** As of Aug 18 he wants to write and change Ted himself
  rather than delegate it, and asked for the codebase commented plus a book-length guide
  (`docs/TED_CODE_BOOK.pdf`). Write specs and reference implementations he can work
  against; do not silently rewrite large areas.

---

## 16. Two patterns worth building in from the start

**`dry_run` on every agent.** A mode that returns what the agent *would* do without doing
it. This gives you verification and confirmation gates for free, and it is the natural
place to hang the `ada_v2` approval pattern. Given Ted's phantom-success history, an agent
that can describe its own plan before executing is worth the small extra surface.

**`describe()` on every agent.** A one-line report of what the agent can *currently* do,
based on live state — `MusicAgent: Spotify not running`, `CommsAgent: email configured, 3 unread`.
Inject these instead of static tool schemas. The brain then knows what is actually available
rather than inferring it from a fixed list, which is a second and sneakier cause of phantom
tool calls.

---

## 17. Order of work

Each step is independently shippable. Do not start the next until the previous passes `tests/`.

| # | Step | Status | Why this order |
|---|---|---|---|
| 0 | Delete `_to_delete/` | todo | Free, zero risk, ~1,314 lines |
| 1 | **Event channel (SSE)** + `emit()` | **done** | Everything else reports through it |
| 2 | **`AgentResult` + `Plan` + agent base class** | **done** | The contract before the implementations |
| 3 | **`MacAgent`, cut over** | **done** | It was the §4.1 example — one call now replaces four |
| 4 | **Thought bubble** | **done** | You can now *see* whether a change worked |
| 5 | Remaining six agents, one at a time | **next** | Each behind the tests. Recipe: Code Book §36.8 |
| 6 | Delete `_dispatch_tool` (508 lines) | todo | Only once every domain has an agent |
| 7 | **Router** | **started** | The cleanup lane is the first one. Generalise it per domain |
| 8 | Delete `_assistant_command_impl` (761 lines) | todo | Prove unreachable with logging *before* deleting |
| 9 | Rewrite `_respond` (430 lines) | todo | The ladder collapses once router + agents carry it |
| 10 | Decouple voice to an event subscriber | todo | §13 |
| 11 | Provider budget accounting; drop schema subsetting | todo | §14 |

Steps 1–4 were the spine and they are in. Step 5 is the work now: six more agents on the
pattern `MacAgent` demonstrates. Follow Code Book §36.8 rather than copying `mac.py`
blindly — the `_from_agent` flag and the empty `CONSEQUENT_METHODS` are load bearing, and
the reasons are written down there.

---

## 18. Open questions for Charlie

1. **Paid Groq tier, or stay free?** Changes how hard §14 has to work.
2. **Router: deterministic patterns first, or train a small classifier straight away?**
   Recommendation is patterns first, measure, then decide.
3. **Confirmation defaults** — which agent actions require approval out of the box?
   Suggested: `CommsAgent.send`, anything destructive in `MacAgent`, playlist deletion.
4. **Does the phone target change the agent boundaries?** If Ted becomes a service with
   two clients, agents may need to be process-separable rather than in-process classes.

---

## 19. Glossary

| Term | Meaning |
|---|---|
| **The ladder** | The routing chain in `TedApi._respond()` — what this restructure replaces |
| **Gate 5** | What remains of `_assistant_command()` — a short allowlist over ~700 lines of mostly-unreachable dispatch |
| **The monolith** | `core/app.py`, 3,973 lines |
| **The one door** | `chat_create()` in `core/providers.py` — every model call goes through it |
| **The handover** | Groq failing and the same request retrying on local Ollama |
| **Barge-in** | Interrupting Ted by talking over him |
| **Attention window** | Idle timeout after which Ted needs "Hey Ted" again |
| **AEC** | Acoustic echo cancellation — removed Aug 5, name lingers in code |
| **SSE** | Server-Sent Events — one-way server→browser stream over held-open HTTP |
| **STT / TTS** | Speech-to-text (Whisper) / text-to-speech (Kokoro) |
| **Native audio** | A model that takes audio in and emits speech out directly, no STT/TTS chain |
| **Star topology** | Brain at center, agents on spokes, agents never call each other |

---

## 20. First fifteen minutes, if you are new

```bash
cd ~/ted-ai
git status && git log --oneline -8 && git branch --show-current
sqlite3 data/memory.db ".tables"
tail -60 data/ted_launch.log
tail -30 ted_errors.log
python tools/ted_map.py          # trust this over any number in this document
```

Then read, in order: §4 above (how he is broken) → the generated block in `CLAUDE.md`
(current facts, refreshed by the commit hook) → `docs/DECISION_FLOW.md` → `core/providers.py`
(small, and every thought goes through it) → `core/app.py::_respond` top to bottom.

`docs/AI_WORKFLOW.md` before you edit anything — Charlie runs more than one AI on this repo.
