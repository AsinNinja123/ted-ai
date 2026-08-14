# TED — MASTER HANDOFF

**Written:** August 12, 2026 · **Revised:** August 14, 2026
**Written for:** another AI model picking up this project cold, with no prior context
**Subject:** "Ted" — Charlie Rowenhorst's personal AI assistant
**Repo:** `~/ted-ai` on `charlies-macbook-pro-local` (macOS, Apple Silicon, 48 GB / 2 TB)
**Owner:** Charlie Rowenhorst — CS sophomore at Northwest Christian College (NWC), Iowa

> **Aug 14 revision.** Between Aug 12 and Aug 14 the model layer was rebuilt twice
> and the two-call ladder became one streamed call. Three things this document
> previously asserted are now false and were rewritten, not annotated: the model
> stack (§4.1), "no local model now or planned" (§11.3), and the gate structure
> (§4.2). If you are diffing against an older copy, those are the sections that
> moved.

---

## 0. How to read this document, and what it's built from

### 0.1 Provenance

This document was assembled from primary sources, not from chat transcripts. **Past Claude
conversations are not machine-readable** — there is no tool that retrieves them. What was
actually read:

| Source | What it gave |
|---|---|
| `~/ted-ai` working tree | Current code, file sizes, uncommitted changes |
| `git log` (16 commits, Jul 1 – Aug 9 2026) | Dated, authored history with real commit messages |
| `docs/ROADMAP.md` (compiled Aug 8) | Build history + an audit of the plan against the code. **Itself compiled from 10 Cowork sessions**, so it is the closest thing to a chat archive that exists |
| `docs/DECISION_FLOW.md` (Aug 11) | How Ted routes a message; the current critique of that design |
| `docs/BARGE_IN_HANDOFF.md`, `docs/VERIFY_MEMORY_AND_APP.md` | Two worked debugging/verification handoffs |
| `README.md` (rewritten Aug 8) | User-facing description of current behavior |
| `data/memory.db` | Live table counts — what Ted actually knows today |
| Google Drive: `Ted_Handoff` (Jun 23), `jarvis_ai_guide.docx` (Apr 14), `TED` Colab (Apr 15), `Ted Keys` (May 27) | The Windows-era plan and the business plan |
| Charlie's persistent Claude memory (`/areas/ted-ai.md`) | Distilled statements from many chats, including things never written down in the repo — this is where most of the *future plans* come from |

### 0.2 Confidence markers used below

- **[code]** — verified by reading the current working tree. Highest confidence.
- **[git]** — from a commit message with a real timestamp.
- **[doc]** — from a document in the repo or Drive. Was true when written; may have rotted.
- **[stated]** — Charlie said it in a conversation, captured in persistent memory. It is
  what he intends, not what exists.
- **[unverified]** — written but never observed running on the Mac.

### 0.3 The single most important warning

**Plans rot faster than code on this project.** `docs/ROADMAP.md` found that roughly a third
of the standing feature list's "already built" items were wrong within seven weeks, and two
"planned" items had quietly shipped. Before planning off any document — including this one —
re-read the repo. Concretely: run `git status`, `git log --oneline -10`, and
`sqlite3 data/memory.db ".tables"` first.

---

## 1. What Ted is

Ted is a from-scratch personal AI assistant that runs on Charlie's MacBook. Not a wrapper
around ChatGPT or Claude — a custom Python application with its own UI, its own memory
database, its own tool layer, and its own routing logic, calling hosted models for
inference.

**Entry point:** `python hud.py` (or double-click `Ted.app`).
**What opens:** a 1100×720 pywebview desktop window running `ui/ted_hud.html`.
**What it does:** chat with Charlie, remember him across sessions, and take real actions
on the Mac — calendar, notes, email, Spotify, app launching, reminders, timers, clipboard,
keystrokes, screenshots, web search.

### 1.1 The identity shift you must understand

Ted was built as a **voice-first, always-listening Jarvis**. As of **August 2026 it is
being deliberately converted into a chat-first personal AI** with voice as a secondary
mode. [stated] [code]

The reason is practical: Charlie is back at college surrounded by people, and talking out
loud to a computer is socially unusable most of the time. Voice is now a rare use case,
not the primary interface.

The evidence this is real and not aspirational:

- `core/app.py`: `self.muted = True` at startup — **Ted now boots silent**. The mic button
  turns voice on. [code]
- `core/llm.py` `SYSTEM_PROMPT`: was *"You are Ted, Charlie's AI. Think Jarvis from Iron Man"*;
  is now *"You are Ted, Charlie's personal AI chatbot — his primary AI, the one he talks to
  all day in a chat window."* [code]
- Formatting rules inverted: the old prompt banned markdown, lists, and code blocks because
  everything was spoken. The new one **requires** fenced code blocks with language tags. [code]
- A per-turn `CURRENT MODE: VOICE` / `CURRENT MODE: CHAT` line is now injected so the model
  knows which it is and formats accordingly. [code]
- The HUD was rebuilt around a chat transcript with a sidebar of saved chat sessions,
  rather than around a voice orb. [code]

**If you are working on Ted, treat "personalized chatbot that also acts" as the target,
and "voice assistant" as legacy framing that still appears throughout older comments,
docs, and variable names.**

### 1.2 The one-sentence version for a new model

> Ted is Charlie's own AI chatbot: it knows him from a persistent editable memory database,
> answers in a desktop chat window, can speak and listen when asked to, can operate his Mac
> and his accounts through 32 tools, and is meant to eventually replace the Claude app and
> ChatGPT as his daily driver.

---

## 2. Why Charlie is building it

Ordered by how load-bearing each reason is. All [stated] unless noted.

1. **A daily driver he owns.** The end state is Ted replacing the Claude app and ChatGPT
   as his primary chatbot — on Mac and on his phone — with access to his own tools on both
   devices. He currently uses Claude Cowork heavily and wants that capability under his
   own roof.
2. **Something that actually knows him.** A general chatbot starts from zero every time.
   Ted's whole memory architecture — facts, session summaries, patterns, a knowledge base —
   exists so that it doesn't. The "he actually knows me" signal is the thing Charlie keeps
   optimizing for.
3. **Proactive involvement, not just answers.** Class reminders, schedule, to-dos,
   occasional unprompted suggestions. Ted is supposed to bring things up, not wait.
4. **Portfolio.** He's a CS sophomore building toward AI/automation-adjacent internships and
   jobs. Ted is the project he tells the story with. Getting it under git version control
   and onto GitHub was an explicit goal, done Jul 1.
5. **Practical skill-building.** Real audio pipelines, real prompt caching, real SQLite
   schema migrations, real latency work. He bought an M5 Pro MacBook Pro (48 GB / 2 TB)
   specifically to support this kind of work.
6. **A business, historically.** See §11 — there was a serious plan to sell Ted to small
   businesses. That track is currently dormant, not formally killed.

**Financial independence** is the background motivation behind #4 and #6; he also runs an
AI automation agency on the side whose first client is his father's construction company.
**That agency is a separate project** (`~/Dad`, `~/budget-blinds`, `~/todo-list`) — do not
conflate it with Ted.

> **Correction for the next model:** Charlie's persistent memory currently lists Ted's stack
> as *"n8n + Airtable + Claude API + Twilio + Vercel."* That is **wrong** — that's the
> automation-agency stack. Ted's real stack is §4. Don't act on that line.

---

## 3. Full timeline

### Phase 0 — Windows era, "jarvis-ai" (April – late May 2026)

No git history. Reconstructed from the Drive docs and project notes. [doc]

| | |
|---|---|
| Machine | Windows PC, AMD RX 6600 XT (8 GB), 48 GB RAM, venv at `C:\Users\matth\jarvis-ai` |
| LLM | LLaMA 3.2 3B via **Ollama**, local |
| STT / TTS | Whisper (local) / ElevenLabs "Daniel" |
| Wake word | OpenWakeWord — `"hey jarvis"` |
| Memory | ChromaDB → **Neo4j** |
| UI | Streamlit, with a 5-second record button |
| Planning docs | `jarvis_ai_guide.docx` (Apr 14), `TED` Colab notebook (Apr 15), `Ted Keys` (May 27) |

The original vision in `jarvis_ai_guide.docx` was **100% local, offline, no cloud, no
subscriptions** — fine-tuned LLaMA + cloned voice + ChromaDB, all on his own hardware.
Almost none of that survived. Understanding *why* each piece died is more useful than the
list itself; see §12.

### Phase 1 — Mac migration (June 2026)

| Date | What | Notes |
|---|---|---|
| Jun 20 | venv created; Kokoro ONNX model (325 MB) + voices (28 MB) downloaded | The Mac project starts here |
| June | **Always-listening rewrite** | Killed the wake-word gate and the Streamlit record button. `hud.py` + pywebview: auto-start, listen until you stop talking, answer, listen again. Calibrates to room noise at launch. |
| June | **Native Swift audio engine** | `native/ted_audio.swift` + `build.sh`, using Apple Voice Processing for echo cancellation. Debug chain was brutal: CLI-binary mic permission, CoreAudio `-10875`, 9-channel input downmix, unbuffered-pipe short reads, sticky barge flag. |
| June | **Whisper hallucination defenses** | Ted heard "Thank you" in a silent room — Whisper invents the phrase that follows silence in its training data. Fixed with an energy gate + no-speech confidence gate + an exact-match phantom blocklist. |
| Jun 22 | **Spotify Web API** | `authorize_spotify.py`, `core/spotify_web.py`, spotipy. Design call: transport (play/pause/skip) stays local and instant; only *selection* hits the API. |
| **Jun 23** | **`Ted_Handoff` Drive doc** | The business plan. See §11. |
| Jun 26 | Calendar, Notes, screen vision, computer control, knowledge base | `calendar_app.py`, `notes.py`, `screen.py`, `computer.py`, `knowledge.py` |
| Jun 26–27 | **Outlook email over IMAP** | `setup_email.py` → `outlook.office365.com:993`; credentials land in `~/.ted_email_config.json` **in cleartext**. |
| Jun 28 | **Event-bus decomposition scoped — never executed** | The architectural debt that still exists. See §8.1. |
| June | **Azure / Microsoft Graph attempt — abandoned mid-flight** | App registered in the school tenant (client ID `a3df93af-…41289`), public client flows enabled. Stalled on an MSAL error: `offline_access` is reserved and must not be passed in scopes. Email still runs on IMAP because of this. **One-line fix available** — see §8.5. |
| June | **Inventory / Sortly feature** | Built for a fireworks store. Key design call worth keeping: **low-stock math in Python, not in the LLM.** Deleted Jul 2. |

### Phase 2 — Ted v4: 11 commits in 14 hours (Jul 1–2)

The big modular refactor and the burst of feature work that followed it. [git]

| Time | Commit | What |
|---|---|---|
| Jul 1 22:38 | `69ced76` | **Baseline: Ted v4 after modular refactor** — first commit ever; git initialized. `hud.py` (was ~4,450 lines) split into `core/` modules. |
| Jul 1 22:41 | `33ce5bf` | **Neo4j → SQLite** (`data/memory.db`). Neo4j needed Neo4j Desktop running and it usually wasn't. SQLite is a file. FTS5 keyword recall, WAL for thread safety. |
| Jul 1 22:45 | *(mtime)* | `ui/ted_hud.html` — the particle-sphere HUD |
| Jul 1 22:47 | `1ca945e` | **Attention mode** — after `ATTENTION_WINDOW` seconds idle Ted drops to STANDBY; "Hey Ted" re-engages. Replaces wake-word gating. |
| Jul 1 22:49 | `c3b4ead` | Retry malformed tool calls; "actually make it 20 minutes" corrections |
| Jul 1 22:52 | `904c6ee` | Remote GET+token endpoint, executing proactive triggers, daily-briefing config, fireworks sales tally |
| Jul 1 22:56 | `1a0deb3` | **Voice lock** — "Ted, learn my voice" (resemblyzer, threshold 0.68), off by default, fails open |
| Jul 1 23:16 | `125d899` | Fix "Ted can't hear me": wake matching for how Whisper *actually* transcribes ("Hey Tad", "Hated…", "So Ted,…"); `recalibrate` command; attention opens whenever Ted speaks first |
| Jul 1 23:50 | `b16a90b` | **Live info** — routes to `groq/compound-mini` which web-searches server-side; DuckDuckGo fallback; strips URLs and `[n]` citations before TTS |
| Jul 2 10:51 | `38eb55e` | **Brain swap** — `openai/gpt-oss-120b` primary with automatic `llama-3.3-70b-versatile` fallback. Benchmarked 5–9× faster first token. |
| Jul 2 12:10 | `3dd744c` | **Remove all fireworks-store features** — sales tally, goal tracker, holiday countdown, store mode |

### Phase 3 — The silent-bug month (Jul 2 → Aug 5)

Five weeks, zero commits. **Fact extraction was dead the entire time and nobody knew.**
`extract_and_save_facts` asked an 8B model for JSON, got prose back, `json.loads` threw,
and the exception died inside a `print`. The `facts` table had **1 row**.

This is the origin of the project's most-repeated lesson: *silent failures are the
expensive ones.*

The Aug 5 repair pass:

- `core/llm.py` — Groq **JSON mode** + a salvage parser; real failures now go to
  `ted_errors.log` instead of stdout
- `core/memory.py` — `save_fact` **supersedes** single-valued facts instead of stacking them
  (both `LIVES_IN Spirit Lake` and `LIVES_IN Spirit Lake, Iowa` were being injected into
  every prompt); added `forget_fact`, `list_facts`, and a prompt-injection cap
- `core/app.py` — `"remember that"` only matched `"remember this"` and wrote to the wrong
  store; added "what do you know about me" / "forget everything about me"
- `core/voice.py` — `_is_junk_fragment` gate: coughs transcribed as `"Tep."` / `"Start."`
  were **executing as commands**
- Tests: `test_capture_gates.py` (new, 32), `test_memory.py` extended, `test_intents.py` (63)

**Aug 5, 21:03** — `docs/BARGE_IN_HANDOFF.md` written in Cowork, handed to Claude Code.
Diagnosis: Ted was **deaf at every sentence boundary**, because interrupt detection was
gated on `_playing`, which goes false between sentences — exactly where a human interrupts.

**Aug 5, 21:44–22:37** — Claude Code shipped the overhaul: `_in_reply` keeps detection alive
across the whole reply; **webrtcvad + autocorrelation pitch gate** (`PITCH_MIN = 0.5`,
70–320 Hz) because VAD alone calls claps "speech"; sliding 300 ms window
(`BARGE_WINDOW 15`, `BARGE_FRAMES 10`, `BARGE_PITCH_FRAMES 4`); `BARGE_MARGIN` 3.0 → 2.0
with floor 0.012 and ceiling 0.030; `TED_DEBUG_BARGE=1` to make it observable.

**Aug 5, 22:36 — echo cancellation was temporarily removed from the Swift binary, then
restored before the Aug 6 baseline.** Apple's Voice Processing had ducked Spotify audio;
the restored path uses macOS 14's minimum other-audio ducking setting.

### Phase 4 — Commit, migration stage 1, housekeeping (Aug 6–9)

| Time | Commit | What |
|---|---|---|
| Aug 6 21:37 | `e07be84` | Barge-in overhaul + memory/fact fixes. 136 tests passing. |
| Aug 6 21:44 | `9fa57bc` | **Migration stage 1: characterization tests** — `tests/test_pipeline.py`, 20.5 KB, pins `_respond` interception order, deterministic routing, the tool loop, compose/disambiguation flows, mute, frustration tracking, both history trims. **207 checks green across six suites.** Two known quirks pinned *as-is* rather than fixed: "the second one" matches the ordinal "one" and picks the FIRST candidate; "nevermind" during contact disambiguation is swallowed by the cancel branch, leaving the pending question armed until expiry. |
| Aug 8 | — | `docs/ROADMAP.md` compiled — build history + audit |
| Aug 9 | `c3a7c71` | Repo hygiene: `data/` was ignored file-by-file so new runtime files defaulted to *committed*; SQLite WAL sidecars carrying private content had already slipped through. Now `data/*` ignored with opt-ins. Legacy HUD renamed. |
| Aug 9 | `22632cf` | **Session memories fixed.** `session_summaries` had 0 rows since creation — the only write paths were a 30-min idle timer and SIGINT/SIGTERM, and closing the window fires neither. Now one idempotent `_teardown()` reached from the pywebview closing event, `atexit`, and both signals. Idle 30 → 10 min, plus a flush every 12 exchanges. All three paths upsert the **same row**. Typed turns now count. Two-stage selectivity filter (see §6.3). |
| Aug 9 | `905d9f8` | **`Ted.app`** — double-clickable launcher. Opening a terminal was enough friction that Ted only got launched when he was the thing being worked on. Pure-stdlib PNG icon generator, `sips`/`iconutil`, refuses a second instance, rotates `data/ted_launch.log`, `osascript` alert on failure. `Info.plist` carries `NSMicrophoneUsageDescription` and `NSAppleEventsUsageDescription` — without them macOS kills the process the moment it opens the mic. |
| Aug 9 | `381e4c9` | Docs commit: `ROADMAP.md`, `TIMELINE.html`, `VERIFY_MEMORY_AND_APP.md`; README rewritten |

### Phase 5 — The chat-first pivot (Aug 10 – Aug 12)

Startup muted, chatbot persona, inverted formatting rules, prompt-cache work, the
memory dashboard. **Committed Aug 12–13** on branch `arch/single-call`; it is no
longer at risk. §7 describes what it contains.

### Phase 6 — One call, one brain, and a daemon (Aug 12 – Aug 14)

Three days that closed the two largest items on the old roadmap. All committed
on `arch/single-call`, pushed to `origin`.

| Commit | What |
|---|---|
| `6779aa1` | Repair `test_pipeline.py` — it had not run since the chat-first pivot |
| `b1b2762` | **One streamed call per turn instead of two**, plus the data to gut gate 5 |
| `4ac858f` | `docs/VERIFY_SINGLE_CALL.md` — the hands-on checklist for the above |
| `9de0075` | **Five reasoning models down to one** |
| `1135ebe` | Never end a turn silent; stop re-running an identical tool call |
| `535324a` | `CLAUDE.md`, `AGENTS.md`, `docs/AI_WORKFLOW.md` — how two AIs share this repo |
| `36a091f` | **Rebuild the reasoning and tool path on Qwen**, with a local Ollama fallback |
| `436079f` | Let the local HUD save chat history |
| `3f5d2ac` | Four bugs where two places disagreed about one fact (Aug 14) |

**`b1b2762` — the merge.** Gates 6 and 8 were two LLM calls per message: a probe
that asked "does this need a tool?", then a separate streaming call that
composed the answer. They are now one streamed call that can emit text *or* a
tool call. This was named in the Aug 12 draft as "the highest-value change
left." `TED_LEGACY_LADDER=1` still selects the old two-call path; the legacy
code is intact behind that flag.

**`9de0075` — the collapse.** Ted had been thinking with four models plus a dead
relay. `groq/compound-mini` (live web), `llama-3.1-8b-instant` (fact extraction
and summaries), Llama-4-Scout (vision), and an inert `claude-sonnet-5` relay all
went. Everything that thinks now goes through one function.

**`36a091f` — the swap.** Cloud reasoning moved to Qwen, and the availability
twin stopped being a second Groq model and became **a local model running under
Ollama**. That reverses §11.3 of the Aug 12 draft.

**Aug 14 (`3f5d2ac`).** Four disagreements between two places that each thought
they owned one fact: a shortcut gate matching by substring while the dispatch it
guarded matched by prefix; arithmetic falling through to the model after gate 5
was gutted; text streamed alongside a tool call missing from the stored turn, so
Ted remembered saying something different from what he said; and `groq_ok()`
reporting an outage at boot because "no provider yet" was read as "cloud down."

---

## 4. Current architecture

### 4.1 The stack

| Layer | Current | Replaced |
|---|---|---|
| **Reasoning — all of it** | `qwen/qwen3.6-27b` on Groq's free tier. Chat, tool calling, fact extraction, session summaries, vision, and web synthesis all go through this one model | four Groq models + a dead Claude relay |
| **Fallback** | `qwen3.5:35b-a3b` on **local Ollama**, tried automatically when Groq is absent, down, or rate limited | `llama-3.3-70b-versatile`, a second cloud model |
| **Live info** | DuckDuckGo (`ddgs`) snippets dropped into the context block; the same streamed call answers from them. Also exposed to the model as a `web_search` tool | `groq/compound-mini`, which decided by keyword before the model saw the message |
| **Vision** | the primary model, via `chat_create` | Llama-4-Scout |
| **STT** | Groq Whisper (`whisper-large-v3-turbo`); local `openai-whisper` as fallback | local Whisper only |
| **TTS** | **Kokoro** ONNX local, voice `am_michael`; ElevenLabs optional | ElevenLabs "Daniel" |
| **Audio** | native Swift `ted_audio` binary (full-duplex, **no AEC**) over a Unix socket, or `sounddevice` fallback; webrtcvad + pitch barge-in | fixed 5-second recording |
| **Wake** | none required — attention window + "Hey Ted" from standby | OpenWakeWord `"hey jarvis"` |
| **Memory** | SQLite `data/memory.db` | Neo4j ← ChromaDB |
| **Knowledge base** | ChromaDB + fastembed, PDF intake from `inbox/` | — |
| **UI** | pywebview + `ui/ted_hud.html` | Streamlit |
| **Dashboard** | Flask on `127.0.0.1:5175`, auto-started from `hud.py` in a daemon thread | — |
| **Proactive** | `ted_daemon.py` under launchd, outside the HUD process | in-process thread that died with the window |
| **Remote** | Flask on `:5150`, GET `/ask?token=…&text=…` for iOS Shortcuts | — |
| **Tests** | 11 suites, **353 checks** | none |

**One place a model name enters a request.** `core/providers.py` is new and owns
provider routing: `chat_create()` tries Groq, and on *any* cloud failure —
missing key, rate limit, 5xx, lost connection — retries the identical request
against local Ollama. Callers never classify the error. `active_provider()`
reports `groq`, `ollama`, or `none` for the last call, and the HUD's health dot
reads it.

**Ted is no longer fully broken offline.** With Ollama installed and the model
pulled, reasoning survives without a network. Hearing (Groq Whisper) and live
web do not, and `USE_GROQ_STT = False` is the switch for the first of those.

### 4.2 The ladder — how a message becomes an answer

Lives in `TedApi._respond()` in `core/app.py`. Full write-up in
`docs/DECISION_FLOW.md` (updated Aug 13, and more current than this summary).

**This changed shape on Aug 12.** The old design had eight gates and made two
LLM calls per message. It now has a short run of cheap local controls, then
**one streamed reasoning call** with the whole tool menu attached.

| Step | What it is | Why it stays ahead of the model |
|---|---|---|
| 0 | Input arrives — typed (`ask()`) or spoken (`conversation_loop()` → `capture()` → wake-strip). Both converge on `_respond()` | — |
| 1 | Mute / unmute | must be instant, and the model must not "discuss" being muted |
| 2 | Stop / cancel | latency-critical; also pauses Spotify if Ted wasn't talking |
| 3 | UI commands — "open chat log", "repeat that", "speak faster" | drives the window, never a thought |
| 4 | Pending flows — a question Ted asked last turn, or a confirmation awaiting yes/no | conversational state, not a new request |
| 5 | **What's left of the old regex dispatch**, guarded by `_use_deterministic_command()` | see below |
| 6 | **One streamed call** — text or tool calls, chained, with the full menu | everything else |

**Gate 5 has been gutted, not deleted.** `_use_deterministic_command()` now
admits only five kinds of message: the voice shortcuts in `shortcuts.json`;
timers, reminders, corrections and cancellations; explicit memory edits
("remember that…", "what do you know about me"); mic recalibration and voice
enrollment; and **arithmetic**. Everything the regexes used to steal — apps,
screen, calendar, notes, web, computer control — now reaches the model.

Arithmetic is the one that looks out of place and is not. A language model doing
"8 percent of 250" fails *silently*: a wrong number reads exactly like a right
one, there is nothing to log and nothing to notice. That is the whole reason for
principle 1 in §12.

**The Aug 12 draft's two complaints are both resolved.** Gates 6 and 8 are one
call (`b1b2762`). Gate 5 is a short allowlist instead of ~50 regexes and ~64
branches. The remaining structural note stands: `_respond` is still the only
place that decides, and it is still inside the 126 KB monolith.

**The legacy path is still there.** `TED_LEGACY_LADDER=1` restores the old
two-call ladder and the full regex dispatch. Useful for bisecting a regression;
it is not a supported mode.

### 4.3 What the streamed call does per turn

1. **Web check** — `_needs_web()`; if live info is needed, DuckDuckGo snippets
   go into the context block. The model can also call `web_search` itself
2. **Parallel memory retrieval on four threads** (4 s join):
   - recent related exchanges — FTS5 keyword search (`memory.py`)
   - known facts about Charlie (`facts` table)
   - personal knowledge base (ChromaDB, `knowledge.py`)
   - past session + chat-thread summaries (`memory.py`)
3. **Assemble the prompt** — order matters for speed, see §7.2
4. **Mode line** — `CURRENT MODE: VOICE` or `CURRENT MODE: CHAT`, regenerated every turn
5. **Stream** tokens to the HUD; sentence-by-sentence to the speaker if voice is on.
   Tool calls arrive in the same stream; results feed the next round, bounded
6. **Background threads afterwards** — save the exchange, extract facts, log topic patterns

### 4.4 File map

| File | Role | Size / risk |
|---|---|---|
| `hud.py` | Entry point; window creation; teardown; starts the dashboard thread | 5.6 KB |
| `ted_daemon.py` | **New.** Calendar watch under launchd, outside the HUD process | 7.7 KB |
| `core/app.py` | **The monolith.** The ladder, tool dispatch, what remains of the regex dispatch | **126 KB, 2,686 lines.** Highest-risk file in the project |
| `core/llm.py` | Prompts, the streamed turn, memory assembly, fact extraction, web search | 53 KB. Second most important |
| `core/providers.py` | **New.** Groq → Ollama routing; the only place a model name enters a request | 9 KB. Clean seam |
| `core/tools.py` | The tool *menu* the model sees (schemas only) | 24 KB. Safe to edit |
| `core/tool_handlers.py` | What each tool actually does | 12 KB |
| `core/memory.py` | SQLite: exchanges, facts, sessions, habits, patterns, FTS5 | 21 KB. Clean, well-bounded |
| `core/intents.py` | Pure phrase-matching helpers, unit-tested | 32 KB. Safe |
| `core/actions.py` | App/URL/Spotify launchers, contacts, iMessage | 22 KB |
| `core/voice.py` | STT, TTS, capture gates, streaming speech | 27 KB |
| `core/audio.py` | Audio engine, playback, barge-in, sounddevice fallback | 30 KB |
| `core/assistant.py` | Reminders, timers, duration/time parsing, weather, location | 16 KB |
| `core/proactive.py` | Trigger schedules + `daemon_alive()`; hands the calendar watch to the daemon | 15 KB |
| `core/spotify_web.py` / `music.py` | Spotify Web API / spoken routing | 12 KB / 2.8 KB |
| `core/email.py` | Outlook IMAP/SMTP | 8 KB |
| `core/knowledge.py` | ChromaDB knowledge base, `inbox/` PDF intake | 8 KB |
| `core/calendar_app.py` / `notes.py` | Calendar.app / Apple Notes via AppleScript | 5.6 KB / 3.2 KB |
| `core/screen.py` | Screenshot + vision description (in-memory, no disk write) | 2.5 KB |
| `core/computer.py` | Type text, press keys, clipboard | 2.8 KB |
| `core/speaker.py` | Voice lock: enroll/verify owner's voice (resemblyzer, opt-in) | 3 KB |
| `core/remote.py` | Flask `:5150` for iOS Shortcuts | 3.3 KB |
| `dashboard/` | Memory dashboard + chat-session storage | ~1,200 lines |
| `native/ted_audio.swift` + `build.sh` | Swift full-duplex audio engine | — |
| `ui/ted_hud.html` | The live HUD | 37 KB |
| `ui/ted_hud_orb.html` | The orb variant, kept | 26 KB |
| `tools/make_app.sh` | Builds `Ted.app` | — |
| `tools/install_daemon.sh` + `com.charlie.ted-daemon.plist` | **New.** Installs the launchd agent | — |
| `CLAUDE.md`, `AGENTS.md`, `docs/AI_WORKFLOW.md` | **New.** How Claude and ChatGPT share this repo without clobbering each other | — |

---

## 5. What Ted can do today

### 5.1 The tool menu (32 schemas in `core/tools.py`) [code, Aug 14]

```
web_search        open_app          close_app         browse_to
play_music        play_playlist     spotify_control   send_message
set_reminder      set_timer         get_reminders     toggle_clock
get_weather       get_emails        read_email        email_action
send_email        search_knowledge  add_knowledge     calendar_get
calendar_add      notes_add         notes_get         clipboard_read
clipboard_write   system_volume     system_brightness screen_describe
type_text         log_habit         get_habit_streak  calculate
```

Notes:
- `web_search` and `calculate` are new. Both exist to move a decision the code
  used to make by keyword into the model's hands, while keeping the *execution*
  deterministic — the model chooses to search or compute; Python does the
  searching and the arithmetic.
- `browse_to` takes an optional `browser`, so "open YouTube in Brave" is honored.
- `toggle_clock` drives a HUD widget, not an OS action.
- `list_add` / `list_get` were **removed 2026-08 — feature retired** (built,
  never used; `data/assistant.json` showed `"lists": {}`).

### 5.2 Beyond the tool menu

- **Deterministic commands** (what's left of gate 5): timers, reminders,
  corrections, "remember that…", "what do you know about me", math ("total on 3
  at 45", "8 percent of 250"), mic recalibration, voice enrollment, mute, stop.
  Everything else — apps, calendar, notes, screen, web, computer control — now
  goes to the model
- **Voice shortcuts** (`shortcuts.json`): `briefing` / `morning briefing` → morning rundown;
  `think` / `thinking partner` → Socratic mode (`THINKING_CONTEXT` — no advice, only questions)
- **Daily briefing** — set `DAILY_BRIEFING_TIME = "7:30am"`, Ted speaks weather/calendar/reminders unprompted
- **Knowledge base** — drop PDFs into `inbox/`, say "index my documents"
- **Remote** — `http://<mac-ip>:5150/ask?token=…&text=…`; README has the Siri Shortcut recipe
- **HUD health indicator** — particle sphere: green = fine, yellow = something
  failed (a real fall back to local Ollama, or a failed action), red = Python
  side stopped sending heartbeats. GROQ / MEMORY / SPOTIFY dots bottom-left;
  MEMORY and SPOTIFY down don't yellow the sphere. Note `groq_ok()` means "the
  last call was not served locally," not "Groq was reached" — a fresh session
  with no completions yet reports healthy, which is deliberate (it used to cry
  wolf at boot).

### 5.3 The honesty rule (do not remove this)

Action tools report **ground truth** and Ted speaks their result **verbatim**. This is
deliberate so that he cannot turn "Spotify isn't open" into a cheerful "Playing your music!"
It is stated as the one rule the persona never breaks. Any refactor must preserve it.

---

## 6. The memory system

### 6.1 Live table counts — `data/memory.db`, Aug 13 2026

| Table | Rows | Meaning |
|---|---|---|
| `facts` | **21** | Injected into *every* prompt. Was 1 during the silent-bug month. |
| `session_summaries` | 3 | Deliberately selective; see §6.3. |
| `exchanges` (+ FTS5) | 60 | Voice/HUD turn log, FTS5-searchable |
| `chat_sessions` | 38 | Dashboard chat threads |
| `chat_turns` | **340** | Where the real conversation volume lives |
| `patterns` | 122 | Topic patterns. **Accumulating; nothing reads them.** |
| `memory_audit` | 365 | SQLite-trigger audit log of every memory write |
| `audit_context` | 1 | One-row actor-attribution table (`ted` vs `user`) |
| `goals` | 0 | Dead table from the deleted fireworks feature. Should be dropped. |
| `habit_logs` | 0 | Built, never used |

`chat_turns` (340) against `exchanges` (60) is the clearest single number showing
the chat-first pivot is real usage, not a plan. Both roughly doubled between
Aug 12 and Aug 13 — Ted is in daily use.

### 6.2 Facts

Personal statements (anything with *I / my / we*) go to the `facts` table as
subject-relationship-object triples and are injected into every prompt. Impersonal content
goes to the ChromaDB knowledge base and is searched on demand.

**Facts supersede rather than pile up.** For single-valued relationships (where you live,
your age) a new value replaces the old one. When two versions differ only in specificity —
"Spirit Lake" vs "Spirit Lake, Iowa" — the more specific one wins. This is preference-drift
handling, solved.

**The Aug 2026 anti-trivia gate:** the fact-extraction pass kept harvesting world
knowledge out of Ted's *own replies* — it saved "bananas are berries" as a fact about
Charlie. Two defenses now: a hard prompt rule, plus a Python gate that rejects any fact
whose subject never appeared in what the *user* said and isn't Charlie himself.

### 6.3 Session memories, and why most sessions produce none

When a conversation is worth remembering, Ted writes a short dated first-person narrative
memory of it and can bring it up later — *"yesterday you were stuck on that double-firing
webhook."* Recent memories are injected into every reply, so callbacks land
mid-conversation rather than only in the greeting.

**Most sessions produce no memory at all, on purpose.** Two filters: a cheap Python
pre-filter (`session_has_substance`, counting words in *non-routine* turns only), then the
model itself, told explicitly that declining is the right answer most of the time. A memory
list full of "Charlie set a two minute timer" makes callbacks *worse* than having none.

**Seeing `[memory] shutdown: nothing worth remembering this session` in the log is the
system working, not failing.** Do not "fix" this.

Write triggers: 10 minutes idle, every 12 exchanges (crash insurance), and on exit (window
close, Ctrl-C, SIGTERM). All three upsert the **same row**, so one conversation leaves one
memory. `kill -9` loses turns since the last flush — accepted.

Tuning knobs if it misbehaves: `MIN_MEMORY_SUBSTANTIVE_WORDS` (currently 15) and
`_ROUTINE_OPENERS` in `core/llm.py`.

### 6.4 The memory dashboard (new, untracked, `dashboard/`)

Flask app on `127.0.0.1:5175`, auto-started from `hud.py` in a daemon thread and embedded
in the HUD's Memory panel via iframe. Also runnable standalone: `python -m dashboard`.

- **Full CRUD** over `facts`, `session_summaries`, `exchanges`, `goals` via a table registry
  in `dashboard/db.py` that whitelists which columns are readable, editable, and searchable
- **Audit log implemented as SQLite triggers stored in the database file itself** — so Ted's
  own writes, from a *different process*, get logged too, not just dashboard edits
- **Actor attribution** via a one-row `audit_context` table defaulting to `'ted'`. The
  dashboard flips it to `'user'` inside its own uncommitted transaction, then flips it back
  before committing — so a concurrent write from Ted's process never sees the `'user'` flag.
  This is a genuinely nice piece of design; don't flatten it in a refactor.
- **Chat session storage** — `/api/chats` create/read/delete/append-turn/summarize
- `/api/version` exposes a `chats` capability flag, which `hud.py` checks: if port 5175 is
  held by an *old* dashboard without the chat API, it prints a loud warning, because chat
  history would silently fail to save

This directly serves Charlie's stated main target: **fully editable memory with a
read/write/edit dashboard**, plus Claude-style session recording. It is largely built.

---

## 7. The chat-first pivot, in detail (Aug 10 – Aug 12)

Everything in this section was uncommitted when this document was first written
and is now committed on `arch/single-call`. It is kept because it explains *why*
the current code looks the way it does.

### 7.1 The chat-first conversion

Covered in §1.1. Startup is muted, the persona is a chatbot, formatting rules inverted, a
per-turn mode line tells the model which mode it's in *right now* and explicitly instructs
it to distrust earlier turns' claims about mode (because Charlie flips modes
mid-conversation and stale claims were confusing it). The startup greeting is now always
added to the chat transcript, and only *spoken* if unmuted.

### 7.2 Latency work — the prompt-cache fixes

The most technically interesting recent work; understand it before touching
prompt assembly.

**`stable_window()` in `core/llm.py`** — replaces `items[-N:]` for history trimming:

> A sliding window shifts by one every turn, which changes the prompt prefix every
> call and kills the provider's prefix cache — every turn reprocesses the whole
> prompt from scratch. This was the "fast for four replies, then slow" cliff.
> Chunked trimming keeps the prefix byte-identical for whole stretches.

It returns a recent suffix whose *start* only moves once every 8 appends.

**Message order changed** from `[system, context, ...history, user]` to
`[static system, ...history, context, user]`. The per-turn context block changes
every turn, so putting it last keeps the static prefix cacheable; and
instructions closest to the user message are followed more reliably.

**Context caps** — `_cap()` truncates retrieved context: web 2000 chars, facts
1200, past exchanges 1200, knowledge 1500, past sessions 1200. Without these the
block grows with the database and every turn pays to reprocess it.

**The probe is gone.** The Aug 12 draft described tuning round 1 of `_try_tools`
down to `max_tokens=120` and a 6-second timeout. `b1b2762` deleted the probe
outright — there is one streamed call now, so there is no discarded first answer
to optimize. **`[timing] tool probe …` should never appear in the log again; if
it does, you are on the legacy path.**

### 7.3 The keyword gate is gone — this is a philosophy change, not a tweak

```
The old likely_command() keyword gate is gone: it only let the model reason about
turns containing a hardcoded verb, which meant any novel phrasing was locked out of
every tool. Now EVERY turn gets a cheap round-1 look; if the model doesn't reach for
a tool we return None immediately and the streaming path answers with full chat
quality — so conversation costs one fast extra call, and nothing is gated.
```

[stated] Charlie removed keyword triggering because it *"made Ted feel like a robot spitting
back answers."* **The model now decides what to do from the prompt itself.** This is the
direction of travel. Any proposal to add keyword matching back needs a strong reason.

### 7.4 The tool loop now sees what Ted knows

Facts are injected into the tool-loop prompt. The bug this fixed: Charlie said "open YouTube
in Brave from now on," it was stored as a fact, and then ignored at the exact moment it
mattered, because the tool loop ran blind to memory. Confirmed working in
`data/ted_launch.log`:
`[tools] browse_to({'browser': 'Brave', 'site': 'youtube.com'})`.

### 7.5 New persona instructions worth preserving

Two additions to `SYSTEM_PROMPT` that encode how Charlie wants Ted to behave:

- **Handling gaps — never be confused, always have a move.** Two options only: (1) make the
  most reasonable assumption, act, and say which assumption you made; or (2) if the choice
  genuinely changes the outcome, ask ONE short question. *"Never say you're confused, never
  list every interpretation, never freeze. A wrong-but-stated assumption beats a stalled
  conversation — the user will just correct you."*
- **Knowing your limits.** On questions needing deeper reasoning, give a best take and be
  honest about confidence rather than bluffing.
- The "Want me to ask Claude?" offer was conditional on `ANTHROPIC_API_KEY`
  being set. **The whole relay was deleted in `9de0075`** — it never had a key,
  so every path through it answered "I'd need an Anthropic API key." Better gone
  than pretending. If difficulty-based escalation (§9.1) is built, it starts from
  nothing here.

### 7.6 HUD rebuild

`ui/ted_hud.html` grew from a voice orb into a chat application: message transcript, input
box, chat-session sidebar with new-chat, an apps panel showing tools in use (with ✕ to close
apps, backed by the new `close_app_direct`), a clock/date/weather widget, a reminders panel,
an embedded memory-dashboard iframe, toasts, and a right-click copy menu. Background changed
`#0A0E14` → `#171614` (warm dark) and `text_select=True` was added to the window so chat text
can be copied. `ui/ted_hud_orb.html` is kept as the orb variant.

> Note: Charlie's persistent memory describes the redesign as "warm cream/gold." The current
> file is a warm *dark* theme. Trust the file.

---

## 8. Open problems, ranked

Reordered Aug 14. Two of the Aug 12 entries are closed; one is closed pending
verification on the Mac.

### 8.1 `core/app.py` is 126 KB and the decomposition never happened — **the central debt**

Scoped **June 28**. Stage 1 (characterization tests) landed **Aug 6**. **No code
has moved yet**, seven weeks on. It was ~103 KB when scoped, 118 KB on Aug 12,
and **126 KB today** — it is growing faster than it is being cleaned.

The planned decomposition is event-bus + contracts + stages. The mechanical
first step, from `ROADMAP.md`: the seam is the old `_assistant_command` dispatch
chain, whose branches can each move into the module they already delegate to
(email → `email.py`, reminders → `assistant.py`, music → `music.py`).
`test_pipeline.py` is the safety net. **One domain per commit, green before the
next.**

Gutting gate 5 made this *smaller* — most of that chain is now unreachable in
the default path — but it did not delete it, and every other subsystem still
reaches into this class.

[stated] Claude has recommended finishing this refactor before expanding Ted
further; Charlie has repeatedly chosen features instead. That's a real decision
he keeps making, not an oversight, and 11 days before a semester starts is not
the moment to overturn it.

### 8.2 The Ollama fallback works, but the *handover* has never been watched

§11.3 records that `qwen3.5:35b-a3b` (Q4_K_M, ~24 GB, 262K context) was pulled
and verified on the Mac, so the local brain itself is real. What has not been
observed is the moment of handover — Groq failing mid-use and `chat_create`
retrying locally. Two things to check before trusting it:

1. Force a real fallback (blank the Groq key, or pull the network) and time it.
   `_ollama_create` allows a **180-second** timeout for a cold model load. If the
   model has to load from disk, Ted goes quiet for minutes and reads as a crash.
   There is no "switching to the local brain" message; there probably should be.
2. Confirm the HUD's health dot follows. `groq_ok()` was fixed on Aug 14 to
   report an outage only on a real fall back to Ollama, and that fix has only
   been unit-tested.

### 8.3 The daemon is built but unverified on macOS

`ted_daemon.py` + `tools/install_daemon.sh` were written Aug 14 in the Linux
sandbox. Logic is unit-tested (`tests/test_daemon.py`, 15 checks); **nothing has
run on macOS.** The likely failure is permissions: macOS gates AppleEvents per
calling binary, and a launchd-spawned python is a different caller from your
terminal, so Calendar access and notifications may both need granting by hand.
Checklist in `docs/DAEMON_HANDOFF.md`.

Until that passes, "proactive class reminders" is still blocked — just for a
different reason than in the Aug 12 draft.

### 8.4 Barge-in has never been verified since the fix

The Aug 5–6 overhaul was written and unit-tested; there is still no record of
the specific test: interrupt mid-sentence, interrupt *at* a sentence pause (the
case that was broken), confirm typing still interrupts, and confirm Ted doesn't
interrupt *himself* on speakers now that AEC is gone. Run with
`TED_DEBUG_BARGE=1`. This has been outstanding for nine days.

### 8.5 Keys are in a Google Doc, and the email password is cleartext

The Drive doc `Ted Keys` contains **live API keys in plain text** — Groq,
ElevenLabs, and multiple Airtable tokens including production ones for the
dispatch app. One bad share link from public. Rotate and move to a password
manager. **No keys are reproduced in this document.**

`~/.ted_email_config.json` holds the Outlook password in plain text. The
Microsoft Graph path is **90% done and one line from working**: drop
`offline_access` from the scopes (MSAL adds it automatically and rejects the
request if you name it) → `['Mail.ReadWrite', 'Mail.Send']`. **Check admin
consent on the school tenant before investing more time** — that is the thing
most likely to kill it.

### 8.6 The confirmation gate is partial

`send_message`, `send_email`, and `email_action` now declare that Ted requires
confirmation before executing, and `_pending_tool_confirmation` carries the
yes/no. `type_text`, `clipboard_write`, and app control still run unconfirmed.
Low risk today because there is no browser automation; it becomes real the
moment DOM automation lands.

Related: `VOICE_LOCK = True` gates **everything**, not just destructive actions.
Voice is replayable and clonable — if voice lock is ever turned on and treated
as security, that is the mismatch to fix. Gate destructive tools specifically,
with a second factor.

### 8.7 Dead weight

- `goals` table — 0 rows, left over from the deleted fireworks feature. Drop it.
- `habit_logs` — 0 rows. Built, never used.
- `patterns` — 122 rows accumulating, **nothing reads them**. Either wire them
  into the correction-log idea (§9.3) or cut them.
- `TED_REFERENCE.txt` — untracked at the repo root, last updated June, and
  describes fireworks "store mode", Neo4j, and the Claude relay. All three are
  gone. It is a user-facing guide that would actively mislead. Rewrite or delete.
- `data/` accumulates `.fuse_hidden*` files — artifacts of the remote-device
  mount, not project files.
- Stale `index.lock` files appear in `.git/` when git is driven through the
  Cowork device bridge. Harmless once removed (`rm -f .git/index.lock`), but
  they block the next git command until you do.

### 8.8 Two pinned quirks (deliberate, documented in `9fa57bc`)

- "the second one" matches the ordinal "one" and picks the **first** contact candidate
- "nevermind" during contact disambiguation was swallowed by the cancel branch;
  the cancel branch now clears pending state, so re-check whether this still holds

These are pinned in tests *as current behavior*. Fixing them means updating the tests.

### 8.9 Closed since Aug 12

- **The uncommitted working tree.** Committed on `arch/single-call` and pushed.
- **Two LLM calls per message.** Merged into one streamed call (`b1b2762`).
- **Four models plus a dead relay.** Collapsed to one (`9de0075`).
- **Gate 5's ~50 regexes.** Reduced to a short allowlist.

## 9. Plans and intended add-ons

### 9.1 Near-term, explicitly stated [stated]

| | |
|---|---|
| **Memory overhaul before Aug 25** (school restart) | Fully editable memory with a read/write/edit dashboard, plus Claude-style session recording. **Built and committed.** What is left is using it for a week and fixing what annoys him. |
| **Multi-model routing** | Free/cheap model for simple lookups; a frontier API for coding and deep reasoning. **Half done, differently than planned**: routing exists as cloud→local *availability* fallback, not difficulty-based escalation. Nothing currently sends a hard question to a stronger model — the Claude relay was deleted in `9de0075` because it never had a key. |
| **Find a good base / daily-driver model** | Qwen 3.6 27B is the current answer, chosen Aug 12. Open question whether it holds. |
| **Chat search** | `sqlite-vec` / FTS5-style search so Ted can pull up a past chat by topic + timeframe. FTS5 is already in use for `exchanges`, so the pattern exists. With 340 chat turns this is starting to matter. |
| **Proactive involvement** | Class reminders, schedule, to-dos. **Unblocked in code** (§8.3) — needs the macOS verification pass. |

### 9.2 Long-term [stated]

- **Ted replaces the Claude app and ChatGPT** as Charlie's primary chatbot
- **Mac + phone app**, with access to his tools on *both* devices
- Cloud/Vercel hosting was considered to make phone access easier. **Claude
  flagged serverless as a poor fit for a persistent voice assistant** and that
  objection stands for anything needing long-lived state, audio, or a background
  daemon. If phone access is the real goal, the existing `:5150` remote endpoint
  plus a small always-on host is a closer fit. Unresolved — and note
  `REMOTE_TOKEN` is still blank, which disables that server entirely.

### 9.3 On the roadmap but not started

- **Difficulty-based model escalation** — the half of "multi-model routing" that
  does not exist. Would need a stronger model's key and a rule for when to spend it
- **Finish gutting gate 5** — the allowlist is short now; the dead branches below
  it are still 700-odd lines of `core/app.py`
- **Correction / feedback log** — raw material exists (`patterns`, plus
  frustration tracking in `app.py`) and nothing reads it
- **Todo / assignment tracking** — the old named-lists feature was retired; this
  needs rebuilding against EventKit/Reminders rather than a JSON file
- **Self-narration** — "checking your calendar…" spoken aloud. The HUD shows
  state visually; Ted doesn't say it
- **DOM-based browser interaction** (Playwright/Selenium) and a unified
  perceive-screen layer (DOM → vision fallback). Only the vision half exists
- **Blackboard integration** — Charlie was manually feeding Ted his Blackboard
  URL on Aug 12. Clear signal this is wanted, and the semester makes it timely
- **Investment / market monitoring**, news / GitHub / papers polling
- **Emotional prosody / style tags** — not supported in the current Kokoro ONNX path

## 10. Things tried and abandoned — do not repeat these

| Thing | Why it died |
|---|---|
| **Fine-tuning for personality** | Unsloth + LoRA, 143 examples, in Colab (the AMD GPU couldn't run the libraries locally). Loss stayed too high. **A system prompt on the base model beat it outright.** That decision still holds and is settled. |
| **Voice cloning** (Coqui / XTTS-v2 / OpenVoice) | Audio quality too poor. Kokoro `am_michael` is the answer. |
| **ElevenLabs free tier** | Exhausted — which is what forced local TTS in the first place. Still available as an option behind `USE_ELEVENLABS`. |
| **Neo4j** | Required Neo4j Desktop to be running and it usually wasn't. Replaced by SQLite Jul 1. |
| **Streamlit UI + 5-second record button** | Replaced by pywebview and always-listening. |
| **OpenWakeWord / "hey jarvis"** | Replaced by the attention window. Not in `requirements.txt`, not imported. |
| **Local LLM via Ollama** | Abandoned in June, **reversed in August.** `qwen3.5:35b-a3b` is now the fallback brain. See §11.3. |
| **Native Swift AEC (echo cancellation)** | Built and active. Apple Voice Processing is enabled; on macOS 14+ other-audio ducking is set to minimum. |
| **Microsoft Graph for email** | Stalled on the MSAL `offline_access` scope error. IMAP shipped instead. Recoverable — §8.5. |
| **Fireworks-store features** (sales tally, goals, countdown, store mode) | Seasonal, deleted Jul 2 in `3dd744c`. |
| **Inventory / Sortly tracking** | Deleted with the above. **Keep the design calls if it ever returns:** math in Python not the LLM; folders → categories; Min Level → reorder point; and the seasonality warning — naive units-per-day velocity is worse than useless against a July 4th spike. |
| **Named lists / to-do tools** | Removed Aug 2026. Built, never used — `data/assistant.json` had `"lists": {}`. |
| **Keyword-gated tool triggering** (`likely_command()`) | Removed Aug 2026. Made Ted feel like a robot spitting back answers, and locked novel phrasings out of every tool. |
| **The tool probe** | Removed Aug 12. A cheap first call asking "does this need a tool?", then a second call to compose the answer. One streamed call does both. |
| **The `claude-sonnet-5` relay** | Removed Aug 12. Never had an API key, so every path through it returned "I'd need an Anthropic API key." |

---

## 11. Things deliberately NOT built

These are decisions, not gaps. Do not "helpfully" implement them.

### 11.1 Ted cannot modify its own code

[stated] Charlie **does not want Ted able to edit its own code yet** — he doesn't trust the
model with a fragile codebase, and wants any such capability removed for now, revisited
later. Nothing in `core/tools.py` touches the filesystem for code, and that's correct.

Related and also correctly absent:
- **Full autonomous self-modification** — rejected.
- **Recursive self-improvement** — rejected as unrealistic.
- A *narrow, human-reviewed* self-edit loop was considered and is the only version that
  might come back, but it is not started and is not currently wanted.

### 11.2 No named sub-agents

The decision was **routing, not named sub-agents**. It has held. Don't add a "Ted researcher"
and a "Ted coder."

### 11.3 Local fallback is active

As of Aug 13, Ted uses free hosted inference for normal latency but has a genuine
offline fallback: Ollama `qwen3.5:35b-a3b`, Q4_K_M, approximately 24 GB, with
reasoning, tools, vision, and a 262K context window. It was downloaded and verified
on Charlie's 48 GB M5 Pro. Do not remove it merely because Groq is usually faster.

### 11.4 Also intentionally absent

- **Sortly / inventory** — parked deliberately (§10)
- **Emoji in replies** — banned in the persona
- **Padding, recaps, self-summaries, "Great question"** — hard-banned in the persona
- **Cheerful lies about actions** — the honesty rule (§5.3)

---

## 12. Standing design principles

These recur across the project's history and should survive any rewrite.

1. **Math in Python, words in the LLM.** The inventory feature worked because counts were
   computed in code and only *narrated* by the model.
2. **Silent failures are the expensive ones.** Fact extraction was dead five weeks because
   an exception died in a `print`. Barge-in died silently because nothing reported the
   threshold. Both fixes included *making it observable* — that's the pattern to keep.
   Real failures go to `ted_errors.log`, not stdout.
3. **Ground truth over optimism.** Action tools report what actually happened, verbatim.
4. **Cheap gates before expensive ones is correct** — the ladder design is sound.
   What was wrong was how many rungs were hardcoded, and that two rungs did the
   same LLM work twice. Both fixed in August; the principle survives the fix.
5. **Deleting a regex is a feature.** Every one removed moves a decision from "hardcoded"
   to "reasoned," and makes Ted feel more intelligent.
6. **No second source of truth.** Calendar and Notes go through AppleScript to the *real*
   apps rather than keeping a parallel copy. Correct call; extend it (EventKit for Reminders).
7. **Selective memory beats complete memory.** Remembering everything makes callbacks worse.
8. **Prompt prefix stability is a performance feature.** Keep the static prefix
   byte-identical; put volatile per-turn context last.
9. **Know which tool can run the thing.** Cowork reads and edits the repo but
   runs in a Linux sandbox — it **cannot** execute a macOS venv, touch CoreAudio,
   run AppleScript, call Groq with the real key, reach Ollama, or build a `.app`.
   Runtime, audio, hardware, and permission bugs belong in Claude Code on the Mac.
   The `BARGE_IN_HANDOFF.md` pattern — diagnose in one, hand off to the other, and
   **state plainly which claims are unverified** — worked twice more since
   (`VERIFY_SINGLE_CALL.md`, `DAEMON_HANDOFF.md`). Reuse it.

   The pure-Python suites *can* be run off-Mac by stubbing `groq` and mocking
   `osascript`; that is how 353 checks were confirmed green on Aug 14. It proves
   logic and proves nothing about macOS.

11. **Two places must never own one fact.** The recurring bug in this codebase is
    not complexity, it is duplication of judgment: a gate matching by substring
    while the dispatch below it matched by prefix; a stored reply diverging from
    the spoken one; a health check inferring "cloud down" from "no calls yet."
    Every one of those was two pieces of code answering the same question
    differently. When adding a check, find who already answers it.
10. **Plans rot faster than code.** Re-audit against the repo before planning off any
    document, including this one.

---

## 13. The fork: two roadmaps pointing different directions

This is unresolved and worth surfacing to Charlie explicitly.

**Track A — personal assistant.** Everything actually built since June serves this:
Spotify, Notes, Calendar, screen vision, iMessage, voice lock, the memory dashboard, the
chat pivot. Readiness was scored **78%** in June and everything since has been incremental
polish on that number.

**Track B — business product.** The `Ted_Handoff` Drive doc (Jun 23) describes a different
project entirely: a deployable AI business assistant for small businesses. Clone Ted per
client, connect their POS/scheduling/SMS/payroll, tiers at **$299 / $499 / $799 per month**,
auto repair shops as niche #1, a VPS per client (~$8–16/mo), a "Mission Control" dashboard
with animated pixel avatars per client. Stated cost per client $20–100/mo, so $200–500
kept. **"First client deployed by end of August."**

Its own five-item "what gets Ted to 90%" list:

1. Refactor core to load from `config.yaml` instead of hardcoded values
2. First POS integration — Square or Lightspeed
3. Twilio voice-call handling for inbound customer calls
4. Basic monitoring so you know when a client's system breaks
5. Deploy for one real client

**None of the five exist.** No `config.yaml`, no `clients/` directory, no POS, no Twilio, no
monitoring. Its June scorecard read Business-Ready **32%** / Scalable **12%** and neither
number has moved. Meanwhile the feature that *was* the business core — inventory tracking,
item #1 on its client-automation list — was **deleted on Jul 2**.

Its own honest-warnings section is worth quoting because it was right: *sales is harder than
building; support will eat you if error handling isn't bulletproof; API costs can spike;
**fall semester will conflict with client obligations — scope honestly**; your moat is
personal service, not features.*

**Assessment for the next model:** Track B is effectively dormant. The August deadline
passed unmet, the semester starts Aug 25, and every line of code written since June went to
Track A — including a deliberate identity change (voice → chat) that makes Ted *less*
suited to Track B, whose entire differentiator was voice for people who can't type. Treat
Track A as the live project. If Charlie raises Track B, the honest framing is that it needs
a restart from a config-driven multi-tenant core, not a continuation — and that it competes
directly with his AI automation agency, which is already further along and already has a
paying-adjacent client.

---

## 14. How to work with Charlie

From his stated preferences and from what the project history shows works.

- **Be honest, including when an idea is bad.** Don't soften real problems, don't validate
  to be encouraging. If something is good, say so — but only if you mean it.
- **Calibrate pushback to stakes.** Push hard on significant time/money commitments, on
  plan evaluations, and on consequential decisions. **Don't push back on execution
  requests** — if he asks you to build, explain, or draft something, do it. Save critique
  for when he asks what you think or when there's a real problem in the thing itself.
- **Assume he knows the fundamentals.** Skip setup, get to the part he doesn't know. Don't
  manufacture concerns to fill space.
- **Simple words.** Only reach for bigger terminology when it's genuinely the precise term.
- **No filler.** No "great question," no unnecessary affirmations. Smart friend, not
  assistant.
- **Label your own ideas.** If you think of a better version of his idea, put it at the end
  and mark it clearly as yours.
- **Treat projects as standalone** unless he brings up the connection. Ted, the automation
  agency, and the dispatch app are separate. Ask once, briefly, if it changes your answer.
- **State what's unverified.** He works across Cowork (Linux sandbox) and Claude Code (the
  Mac). Claims that were never run on real hardware must be flagged as such. He has been
  burned by this exact thing.

---

## 15. Practical reference

### 15.1 Commands

```bash
# Run Ted
cd ~/ted-ai && source venv/bin/activate && python hud.py

# Or: double-click Ted.app  (rebuild with: bash tools/make_app.sh)

# Full test suite — 11 suites, 353 checks
cd ~/ted-ai && source venv/bin/activate
for t in tests/test_*.py; do printf "%-34s " "$t"; python "$t" | tail -1; done

# What is Ted, right now — generated from the code, never hand-written
python tools/ted_map.py            # writes ted_map.html
python tools/ted_map.py --markdown # the same facts as text
python tools/ted_map.py --sync     # refresh the block in CLAUDE.md / AGENTS.md
bash tools/install_hooks.sh        # once per clone: the pre-commit hook does --sync for you
#   ...or open http://127.0.0.1:5175/map while Ted is running

# Which brain answered last, and why
grep '\[provider\]' data/ted_launch.log     # only prints when Groq failed over
ollama list                                  # is qwen3.5:35b-a3b actually pulled?

# The calendar daemon
venv/bin/python ted_daemon.py --once         # one poll, verbose
bash tools/install_daemon.sh                 # install the launchd agent
bash tools/install_daemon.sh --uninstall
launchctl print gui/$(id -u)/com.charlie.ted-daemon | head -20
tail -f data/ted_daemon.log

# Build the native audio engine (needs swiftc; xcode-select --install)
cd ~/ted-ai/native && ./build.sh

# Memory dashboard standalone
cd ~/ted-ai && python -m dashboard        # -> http://127.0.0.1:5175

# One-time setup
python setup_email.py          # Outlook IMAP
python authorize_spotify.py    # Spotify OAuth

# Inspect memory
sqlite3 -box ~/ted-ai/data/memory.db "SELECT id, exchanges, topics, text FROM session_summaries"

# Debug barge-in
TED_DEBUG_BARGE=1 python hud.py

# Fall back to the old two-call ladder (bisecting only)
TED_LEGACY_LADDER=1 python hud.py

# Logs
cat ~/ted-ai/data/ted_launch.log      # everything Ted prints (no terminal when launched via .app)
cat ~/ted-ai/ted_errors.log           # real failures only
```

### 15.2 Config keys (`config.py`, gitignored; template in `config.example.py`)

| Key | Value | Notes |
|---|---|---|
| `GROQ_API_KEY` | set | Ted runs on local Ollama alone without it, slowly |
| `CLOUD_CHAT_MODEL` | `"qwen/qwen3.6-27b"` | tried first |
| `LOCAL_CHAT_MODEL` | `"qwen3.5:35b-a3b"` | Ollama fallback; must be pulled |
| `OLLAMA_URL` | `"http://127.0.0.1:11434"` | `providers.py` will start `ollama serve` if idle |
| `USE_GROQ_STT` | `True` | False → local Whisper (the offline path for hearing) |
| `USE_ELEVENLABS` / `ELEVENLABS_API_KEY` | `False` / set | Kokoro otherwise |
| `ATTENTION_WINDOW` | `180` | 0 = always listen |
| `VOICE_LOCK` / `VOICE_LOCK_THRESHOLD` | `False` / 0.68 | needs `resemblyzer` |
| `FALLBACK_VOICE_BARGEIN` | `True` | disable if Ted interrupts himself on speakers |
| `OWNER_NAME` | `"Charlie"` | used in greetings and fact subjects |
| `WEATHER_LOCATION` | `""` | auto-detected via IP if blank |
| `DAILY_BRIEFING_TIME` | unset | e.g. `"7:30am"` |
| `REMOTE_PORT` / `REMOTE_TOKEN` | 5150 / unset | **blank token disables the server** — this is why iPhone access does not work |
| `SPOTIFY_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | set | needs Premium |

The three model keys were added Aug 14; before that `providers.py` fell through
to its own hardcoded defaults, which still happens if the lines are absent.
`ANTHROPIC_API_KEY` / `CLAUDE_MODEL` and the stale Neo4j password were removed.

### 15.3 Glossary

| Term | Meaning |
|---|---|
| **The ladder** | The routing in `TedApi._respond()` (§4.2). Six steps now, not eight |
| **Gate 5** | What remains of `_assistant_command()` — a short allowlist in `_use_deterministic_command()`, with ~700 lines of now-mostly-unreachable dispatch below it |
| **The probe** | The old cheap "does this need a tool?" call. **Deleted Aug 12** — if `[timing] tool probe` appears in the log, you are on the legacy path |
| **`chat_create`** | `core/providers.py` — the single door every thinking request goes through |
| **The handover** | Groq failing and the same request being retried on local Ollama |
| **Attention window** | Idle timeout after which Ted needs "Hey Ted" again |
| **Barge-in** | Interrupting Ted by talking over him |
| **AEC** | Acoustic echo cancellation — **removed Aug 5**; the name lingers in code |
| **The monolith** | `core/app.py`, 126 KB |
| **Migration / stage 1** | The event-bus decomposition; stage 1 = characterization tests, done |
| **Cowork vs Claude Code** | Cowork = Linux sandbox, can read/edit but not run Ted. Claude Code = on the Mac, can run it |

### 15.4 Where things live

```
~/ted-ai/                          the project
~/ted-ai/data/memory.db            everything Ted knows (gitignored)
~/.ted_email_config.json           Outlook creds, CLEARTEXT
~/ted-ai/data/ted_launch.log       stdout when launched via Ted.app
~/ted-ai/ted_errors.log            real failures
~/ted-ai/inbox/                    drop PDFs here, then "index my documents"
```

Google Drive: `Ted_Handoff` (business plan, Jun 23) · `jarvis_ai_guide.docx` (Windows plan,
Apr 14) · `TED` Colab (fine-tuning attempt, Apr 15) · `Ted Keys` (**contains live
credentials — rotate**).

Separate projects, not Ted: `~/Dad`, `~/budget-blinds`, `~/todo-list`.

---

## 16. If you are the next AI picking this up — start here

**First five minutes, before doing anything:**

```bash
cd ~/ted-ai
git status && git log --oneline -8 && git branch --show-current
sqlite3 data/memory.db ".tables"
tail -40 data/ted_launch.log
```

Work happens on `arch/single-call`, not `main`. Charlie also runs ChatGPT on
this repo — read `docs/AI_WORKFLOW.md` before editing. If `git status` shows
files you did not modify, someone else is mid-task; say so rather than editing
them.

**Then read, in this order:** the generated block in `CLAUDE.md` (current
facts, refreshed by the commit hook) → `docs/DECISION_FLOW.md` (how it thinks,
updated Aug 13) → `README.md` (what it does) → `core/providers.py` (small, and
it is the door every thought goes through) → `core/app.py::_respond` top to
bottom.

**Do not trust this document's numbers over `python tools/ted_map.py`.** That
script reads the code; this file was written by hand and has been wrong before.
Where they disagree, the script is right and this file needs fixing.

**Highest-value actions available right now, in order:**

1. **Verify the daemon on the Mac** — `docs/DAEMON_HANDOFF.md`. It is written and
   unit-tested and has never run on macOS. This is the last thing between Charlie
   and proactive class reminders, and the semester starts Aug 25.
2. **Watch one real Groq→Ollama handover** (§8.2). A 180-second cold load that
   nobody has timed is the difference between a fallback and a hang.
3. **Verify barge-in** with `TED_DEBUG_BARGE=1`, including the sentence-boundary
   case and self-interruption on speakers. Outstanding since Aug 5.
4. **Rotate the keys in the `Ted Keys` Drive doc** (§8.5). Ten minutes, and the
   only item here with an unbounded downside.
5. **Chat search** — 340 chat turns and no way to find one. FTS5 is already in
   use for `exchanges`; the pattern exists.
6. **Stage 2 of the decomposition** — one domain out of the old dispatch per
   commit, tests green between each. Correct, and repeatedly deferred in favor of
   features. That is a real decision Charlie keeps making, not an oversight.

**Do not:** add keyword triggers, restore the tool probe, give Ted the ability to
edit its own code, rebuild named lists, re-attempt fine-tuning or voice cloning,
remove the local Ollama fallback because Groq is usually faster, or plan off any
document — including this one — without re-checking the repo first.

---

*End of handoff. Compiled from the repo, git history, in-repo docs, Google Drive
documents, and persistent memory. Revised Aug 14, 2026 against the working tree
at `3f5d2ac`. Past chat transcripts were not machine-readable; where a claim
comes only from conversation it is marked [stated].*
