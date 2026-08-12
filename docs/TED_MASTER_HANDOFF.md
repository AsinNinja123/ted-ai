# TED — MASTER HANDOFF

**Written:** August 12, 2026
**Written for:** another AI model picking up this project cold, with no prior context
**Subject:** "Ted" — Charlie Rowenhorst's personal AI assistant
**Repo:** `~/ted-ai` on `charlies-macbook-pro-local` (macOS, Apple Silicon, 48 GB / 2 TB)
**Owner:** Charlie Rowenhorst — CS sophomore at Northwest Christian College (NWC), Iowa

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

The evidence this is real and not aspirational, all in the current uncommitted working tree:

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
> and his accounts through ~30 tools, and is meant to eventually replace the Claude app and
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

**Aug 5, 22:36 — echo cancellation was removed from the Swift binary.** Apple's Voice
Processing ducked Spotify audio. The `"aec"` mode name in the code is now historical and
means nothing. On speakers, barge-in rests entirely on energy + VAD + pitch.

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

### Phase 5 — The chat-first pivot (Aug 10 – Aug 12, **uncommitted**)

This is live work in the working tree. `git status` shows 8 modified files and 3 untracked
paths; ~1,100 insertions / ~544 deletions. **None of it is committed.** See §7 for detail.

`data/memory.db` was last written **Aug 12, 02:36** — Ted is being run daily right now.

---

## 4. Current architecture

### 4.1 The stack

| Layer | Current | Replaced |
|---|---|---|
| **Main LLM** | Groq `openai/gpt-oss-120b` (replies + tool calling), `reasoning_effort=low` for latency → auto-fallback to `llama-3.3-70b-versatile` on rate limit / 5xx / 413 / 404 | Ollama + LLaMA 3.2 3B, local |
| **Fast model** | `llama-3.1-8b-instant` — fact extraction, session summaries | — |
| **Vision** | Llama-4-Scout via Groq — screenshot description | — |
| **Live info** | `groq/compound-mini` (searches server-side before answering) → DuckDuckGo (`ddgs`) fallback | raw DuckDuckGo |
| **Second brain** | optional `claude-sonnet-5` relay via `ANTHROPIC_API_KEY` — "ask Claude…". **Config slot exists and is empty.** | — |
| **STT** | Groq Whisper cloud (`USE_GROQ_STT = True`); local `openai-whisper` as fallback | local Whisper only |
| **TTS** | **Kokoro** ONNX local, voice `am_michael` (325 MB model + 28 MB voices); ElevenLabs optional | ElevenLabs "Daniel" |
| **Audio** | native Swift `ted_audio` binary (full-duplex, **no AEC**) over a Unix socket, or `sounddevice` fallback; webrtcvad + pitch barge-in | fixed 5-second recording |
| **Wake** | none required — attention window + "Hey Ted" from standby | OpenWakeWord `"hey jarvis"` |
| **Memory** | SQLite `data/memory.db` | Neo4j ← ChromaDB |
| **Knowledge base** | ChromaDB + fastembed, PDF intake from `inbox/` | — |
| **UI** | pywebview + `ui/ted_hud.html` | Streamlit |
| **Dashboard** | Flask on `127.0.0.1:5175`, auto-started from `hud.py` in a daemon thread | — |
| **Remote** | Flask on `:5150`, GET `/ask?token=…&text=…` for iOS Shortcuts | — |
| **Tests** | 6 suites, 207 checks as of `9fa57bc` | none |

**Ted is 100% cloud for inference.** There is no local model. He is fully broken offline.
This is a deliberate reversal of the original vision — see §12.5.

### 4.2 The decision ladder — how a message becomes an answer

This is the most important thing to understand about the codebase. It lives in
`TedApi._respond()` in `core/app.py`. Full write-up in `docs/DECISION_FLOW.md`. [doc, Aug 11]

Every message falls down **eight gates**, cheapest first. Each asks "is this mine?" and
either handles it and stops, or passes it down. **The first gate that claims a message
wins, and there is no appeal.**

| Gate | What it is | File |
|---|---|---|
| 0 | Input arrives — typed (`ask()`) or spoken (`conversation_loop()` → `capture()` → wake-strip). Both converge on `_respond()`. | `app.py`, `audio.py`, `voice.py` |
| 1 | Mute / unmute — literal phrase match, no LLM | `app.py` |
| 2 | Stop / cancel — `_is_stop_command()`; if Ted wasn't talking, pauses Spotify instead | `app.py`, `intents.py` |
| 3 | UI commands — "open chat log", "repeat that", "speak faster" | `app.py` |
| 4 | Pending multi-turn flows — if Ted asked a question last turn, your answer routes to that flow | `app.py` |
| 5 | **Deterministic assistant commands** — `_assistant_command()`, ~746 lines, ~50 regexes, ~64 branches | `app.py` |
| 6 | **The tool loop** — `_try_tools()`, real LLM reasoning with ~30 tool schemas | `app.py`, `tools.py`, `tool_handlers.py` |
| 7 | Built-in actions — `detect_action()` runs *inside* `ask_streaming()`: dates, location, app launches | `actions.py` |
| 8 | **Streaming conversation** — `ask_streaming()`, Ted-as-chatbot | `llm.py` |

**The two things Charlie considers wrong with this design** (his words, captured in
`DECISION_FLOW.md`):

1. **Gate 5 is the biggest liability in the codebase.** It's "hardcode every scenario" in
   literal form. It runs *before* the model gets a say, so any phrasing its regexes catch is
   decided without intelligence, and any phrasing they miss falls through to a model that
   may not have the matching tool. A regex written for one intent can swallow a message
   meant for another. **The fix is to delete regexes, not add them** — shrink Gate 5 to only
   what must be deterministic (stop, mute, timers) and let the tool loop own the rest.
2. **Gates 6 and 8 are two separate LLM calls for every single message.** Real chat
   assistants use one streamed call that can emit text *or* a tool call. Merging them halves
   per-turn latency and eliminates the discarded probe. This is the highest-value change
   left, and it's a rebuild-era change because it means rewriting `_respond`.

Also noted: **four different files can independently decide "this is a command"** —
`_respond`, `_assistant_command`, `detect_action`, `_try_tools`. In the event-bus rebuild
this should be exactly one stage emitting one decision event.

### 4.3 Gate 8 in detail — what `ask_streaming` does per turn

1. **Web check** — `_needs_web()`; if live info is needed, compound model searches, DDG fallback
2. **Parallel memory retrieval on four threads** (4 s join):
   - recent related exchanges — FTS5 keyword search (`memory.py`)
   - known facts about Charlie (`facts` table)
   - personal knowledge base (ChromaDB, `knowledge.py`)
   - past session + chat-thread summaries (`memory.py`)
3. **Assemble the prompt** — order matters for speed, see §7.2
4. **Mode line** — `CURRENT MODE: VOICE` or `CURRENT MODE: CHAT`, regenerated every turn
5. **Stream** tokens to the HUD; sentence-by-sentence to the speaker if voice is on
6. **Background threads afterwards** — save the exchange, extract facts, log topic patterns

### 4.4 File map

| File | Role | Size / risk |
|---|---|---|
| `hud.py` | Entry point; window creation; teardown; starts the dashboard thread | 5.6 KB |
| `core/app.py` | **The monolith.** The ladder, tool dispatch, every deterministic command | **118 KB.** Highest-risk file in the project |
| `core/llm.py` | Prompts, streaming, memory assembly, fact extraction, web search | 43 KB. Second most important |
| `core/tools.py` | The tool *menu* the model sees (schemas only) | 24 KB. Safe to edit |
| `core/tool_handlers.py` | What each tool actually does | 12 KB |
| `core/memory.py` | SQLite: exchanges, facts, sessions, habits, patterns, FTS5 | 21 KB. Clean, well-bounded |
| `core/intents.py` | Pure phrase-matching helpers, unit-tested | 32 KB. Safe |
| `core/actions.py` | App/URL/Spotify launchers, contacts, iMessage, `detect_action` | 22 KB |
| `core/voice.py` | STT, TTS, capture gates, streaming speech | 27 KB |
| `core/audio.py` | Audio engine, playback, barge-in, sounddevice fallback | 30 KB |
| `core/assistant.py` | Reminders, timers, duration/time parsing, weather, location | 16 KB |
| `core/proactive.py` | Calendar alerts + user-defined scheduled triggers. **In-process — dies with the window.** | 13 KB |
| `core/spotify_web.py` / `music.py` | Spotify Web API / spoken routing | 12 KB / 2.8 KB |
| `core/email.py` | Outlook IMAP/SMTP | 8 KB |
| `core/knowledge.py` | ChromaDB knowledge base, `inbox/` PDF intake | 8 KB |
| `core/calendar_app.py` / `notes.py` | Calendar.app / Apple Notes via AppleScript | 5.6 KB / 3.2 KB |
| `core/screen.py` | Screenshot + Groq vision description (in-memory, no disk write) | 2.5 KB |
| `core/computer.py` | Type text, press keys, clipboard | 2.8 KB |
| `core/speaker.py` | Voice lock: enroll/verify owner's voice (resemblyzer, opt-in) | 3 KB |
| `core/remote.py` | Flask `:5150` for iOS Shortcuts | 3.3 KB |
| `core/features.py`, `paths.py`, `logs.py`, `hud_bridge.py` | Plumbing | small |
| `dashboard/` | **New, untracked.** Memory dashboard + chat-session storage | 1,158 lines |
| `native/ted_audio.swift` + `build.sh` | Swift full-duplex audio engine | — |
| `ui/ted_hud.html` | The live HUD (heavily rewritten Aug 10–12) | 37 KB |
| `ui/ted_hud_orb.html` | **Untracked.** The orb-style HUD kept alongside the new chat HUD | 26 KB |
| `ui/ted_hud_legacy.html` | Retired. Nothing imports it. | 52 KB |
| `tools/make_app.sh` | Builds `Ted.app` | — |

---

## 5. What Ted can do today

### 5.1 The tool menu (~30 schemas in `core/tools.py`) [code, Aug 12]

```
open_app          close_app         browse_to         play_music
play_playlist     spotify_control   send_message      set_reminder
set_timer         get_reminders     toggle_clock      get_weather
get_emails        read_email        email_action      send_email
search_knowledge  add_knowledge     calendar_get      calendar_add
notes_add         notes_get         clipboard_read    clipboard_write
system_volume     system_brightness screen_describe   type_text
log_habit         get_habit_streak
```

Notes:
- `browse_to` gained an optional `browser` parameter in the uncommitted work — Charlie can
  say "open YouTube in Brave" and it honors it.
- `toggle_clock` is new — it drives a HUD widget, not an OS action.
- `list_add` / `list_get` were **removed 2026-08 — feature retired** (built, never used;
  `data/assistant.json` showed `"lists": {}`).

### 5.2 Beyond the tool menu

- **Deterministic commands** (Gate 5): timers, reminders, habits, "remember that…", calendar
  phrasing, email setup, math ("total on 3 at 45", "8 percent of 250"), mute, stop, recalibrate
- **Voice shortcuts** (`shortcuts.json`): `briefing` / `morning briefing` → morning rundown;
  `think` / `thinking partner` → Socratic mode (`THINKING_CONTEXT` — no advice, only questions)
- **Daily briefing** — set `DAILY_BRIEFING_TIME = "7:30am"`, Ted speaks weather/calendar/reminders unprompted
- **Knowledge base** — drop PDFs into `inbox/`, say "index my documents"
- **Remote** — `http://<mac-ip>:5150/ask?token=…&text=…`; README has the Siri Shortcut recipe
- **HUD health indicator** — particle sphere: green = fine, yellow = something failed
  (Groq unreachable or an action failed), red = Python side stopped sending heartbeats.
  GROQ / MEMORY / SPOTIFY dots bottom-left; MEMORY and SPOTIFY down don't yellow the sphere.

### 5.3 The honesty rule (do not remove this)

Action tools report **ground truth** and Ted speaks their result **verbatim**. This is
deliberate so that he cannot turn "Spotify isn't open" into a cheerful "Playing your music!"
It is stated as the one rule the persona never breaks. Any refactor must preserve it.

---

## 6. The memory system

### 6.1 Live table counts — `data/memory.db`, Aug 12 2026

| Table | Rows | Meaning |
|---|---|---|
| `facts` | **19** | Injected into *every* prompt. Was 1 during the silent-bug month. |
| `session_summaries` | **3** | Was 0 for weeks; the Aug 9 fix works. |
| `exchanges` (+ FTS5) | 33 | Voice/HUD turn log, FTS5-searchable |
| `chat_sessions` | 13 | New — dashboard chat threads |
| `chat_turns` | **237** | New — the real conversation volume now lives here, not in `exchanges` |
| `patterns` | 117 | Topic patterns. **Accumulating; nothing reads them.** |
| `memory_audit` | 336 | New — SQLite-trigger audit log of every memory write |
| `audit_context` | 1 | One-row actor-attribution table (`ted` vs `user`) |
| `goals` | 0 | Dead table from the deleted fireworks feature. Should be dropped. |
| `habit_logs` | 0 | Built, never used |

The `chat_turns` (237) vs `exchanges` (33) gap is the clearest single number showing the
chat-first pivot is real usage, not a plan.

### 6.2 Facts

Personal statements (anything with *I / my / we*) go to the `facts` table as
subject-relationship-object triples and are injected into every prompt. Impersonal content
goes to the ChromaDB knowledge base and is searched on demand.

**Facts supersede rather than pile up.** For single-valued relationships (where you live,
your age) a new value replaces the old one. When two versions differ only in specificity —
"Spirit Lake" vs "Spirit Lake, Iowa" — the more specific one wins. This is preference-drift
handling, solved.

**The Aug 2026 anti-trivia gate** (uncommitted): the fast model kept harvesting world
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

## 7. Where the project is *right now* (Aug 12, 2026)

**All of the following is in the working tree and uncommitted.** `git stash` or
`git checkout -- .` would destroy several days of work. The last commit, `381e4c9`, is clean.

```
 M core/app.py           |  106 ++-      M hud.py          |   37 +-
 M core/llm.py           |  183 ++-      M ui/ted_hud.html | 1184 ++++----
 M core/memory.py        |   35 +-      ?? dashboard/
 M core/tool_handlers.py |   30 +-      ?? docs/DECISION_FLOW.md
 M core/tools.py         |   41 +-      ?? ui/ted_hud_orb.html
 M core/voice.py         |   26 +-
```

### 7.1 The chat-first conversion

Covered in §1.1. Startup is muted, the persona is a chatbot, formatting rules inverted, a
per-turn mode line tells the model which mode it's in *right now* and explicitly instructs
it to distrust earlier turns' claims about mode (because Charlie flips modes
mid-conversation and stale claims were confusing it). The startup greeting is now always
added to the chat transcript, and only *spoken* if unmuted.

### 7.2 Latency work — the prompt-cache fixes

This is the most technically interesting recent work and it's worth understanding before
touching prompt assembly.

**`stable_window()` in `core/llm.py`** — replaces `items[-N:]` for history trimming:

> A sliding window shifts by one every turn, which changes the prompt prefix every call and
> kills the provider's prefix cache — every turn reprocesses the whole prompt (system, tool
> schemas, history) from scratch. This was the "fast for four replies, then slow" cliff: the
> tool probe's 8-message window filled after four exchanges and started sliding. Chunked
> trimming keeps the prefix byte-identical for whole stretches.

It returns a recent suffix whose *start* only moves once every 8 appends.

**Message order changed** from `[system, context, ...history, user]` to
`[static system, ...history, context, user]`. Two reasons: the per-turn context block
changes every turn, so putting it last keeps the static prefix byte-identical and cacheable;
and instructions closest to the user message are followed more reliably (recency wins in
attention).

**Context caps** — `_cap()` truncates retrieved context: web 2000 chars, facts 1200,
past exchanges 1200, knowledge 1500, past sessions 1200. Without these the block grows with
the database and every turn pays to reprocess it. This is the "slow creep after the database
has been in use a while" fix.

**Probe optimization in `_try_tools`** — round 1 is now an explicit cheap probe:
`max_tokens=120` (down from 300), `timeout=6.0s` (down from 12.0), and an injected
instruction to reply with exactly `CHAT` if no tool is needed. Without this, conversational
turns made the model compose a full answer that was then thrown away — doubling response
time for plain chat. Probe latency is now logged: `[timing] tool probe 815ms`.

**Tool-loop system prompt shrunk** — deliberately *not* the full persona, because every
token is re-read on the probe that runs before every reply.

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
- The "Want me to ask Claude?" offer is now **conditionally appended only if
  `ANTHROPIC_API_KEY` is set** — otherwise Ted was offering a phone that isn't plugged in.

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

### 8.1 `core/app.py` is 118 KB and the decomposition never happened — **the central debt**

Scoped **June 28**. Untouched for six weeks. Stage 1 (characterization tests) landed
**Aug 6**. **No code has moved yet.** It was ~103 KB when scoped; it grew to 118 KB while
waiting.

The planned decomposition is event-bus + contracts + stages. The mechanical first step,
from `ROADMAP.md`: the seam is `_assistant_command` — a long dispatch chain whose branches
can each move into the module they already delegate to (email → `email.py`, reminders →
`assistant.py`, music → `music.py`). `test_pipeline.py` is the safety net. **One domain per
commit, green before the next.**

Every other subsystem on the roadmap — daemon, email, todo, coding loop — reaches into this
same class. [stated] Claude has recommended finishing this refactor before expanding Ted
further; Charlie has repeatedly chosen features instead. That's a real decision he keeps
making, not an oversight.

### 8.2 No background daemon — this blocks every proactive feature

`core/proactive.py` exists but runs **in-process**. It dies when the HUD window closes.
No `launchd` plist. Charlie's stated goal of proactive class reminders and nudges
**cannot work** until this is solved.

Note that `Ted.app` (Aug 9) was the *launcher* fix, not the daemon fix. Different problem.

### 8.3 The uncommitted work is at risk

Several days of real work — the chat pivot, the latency fixes, the whole dashboard — sits
uncommitted with no branch. Highest-value five-minute action available on this project:
`git add -A && git commit`.

### 8.4 Barge-in has never been verified since the fix

The Aug 5–6 overhaul was written and unit-tested, but `ROADMAP.md` flagged that Ted hadn't
been launched since four minutes *before* the commit containing it. He has been run many
times since (launch log, Aug 12), but there's no record of the specific test being done:
interrupt mid-sentence, interrupt *at* a sentence pause (the case that was broken), confirm
typing still interrupts, and confirm he doesn't interrupt *himself* on speakers now that AEC
is gone. Run with `TED_DEBUG_BARGE=1`.

### 8.5 Email password in cleartext

`~/.ted_email_config.json` holds the Outlook password in plain text. The Microsoft Graph
path is **90% done and one line from working**: drop `offline_access` from the scopes list
(MSAL adds it automatically and rejects the request if you name it) → `['Mail.ReadWrite',
'Mail.Send']`. **Check admin consent on the school tenant before investing more time** —
that's the thing most likely to kill it.

### 8.6 No confirmation gate on agentic actions

Ted can type keystrokes, drive the clipboard, and send email with no confirmation step. Low
risk today because there's no browser automation. It becomes a real problem the moment DOM
automation lands.

Related: `VOICE_LOCK = True` gates **everything**, not just destructive actions. Voice is
replayable and clonable — if voice lock is ever turned on and treated as security, that's
the mismatch to fix. Gate destructive tools specifically, with a second factor.

### 8.7 Dead weight

- `goals` table — 0 rows, left over from the deleted fireworks feature. Drop it.
- `habit_logs` — 0 rows. Built, never used.
- `patterns` — 117 rows accumulating, **nothing reads them**. Either wire them into the
  correction-log idea (§9.3) or cut them.
- Stale Neo4j password still in `config.py`.
- `data/` contains 10 `.fuse_hidden*` files (32 KB each) — artifacts of the remote-device
  mount, not project files.

### 8.8 Two pinned quirks (deliberate, documented in `9fa57bc`)

- "the second one" matches the ordinal "one" and picks the **first** contact candidate
- "nevermind" during contact disambiguation is swallowed by the cancel branch, leaving the
  pending question armed until it expires

These are pinned in tests *as current behavior*. Fixing them means updating the tests.

### 8.9 Security: `Ted Keys` Google Doc

The Drive doc `Ted Keys` (created May 27, modified Aug 9) contains **live API keys in plain
text** — Groq, ElevenLabs, and multiple Airtable personal access tokens including production
ones for the dispatch app. Those should be rotated and moved to a password manager. Keys in a
Google Doc are one bad share link from being public. **No keys are reproduced in this
document.**

---

## 9. Plans and intended add-ons

### 9.1 Near-term, explicitly stated [stated]

| | |
|---|---|
| **Memory overhaul before Aug 25** (school restart) | Fully editable memory with a read/write/edit dashboard, plus Claude-style session recording. **Largely built** in `dashboard/` — needs committing and finishing. |
| **Multi-model routing** | Free/cheap model (current Groq setup) for simple lookups; a frontier API for coding and deep reasoning. **Memory informs how requests get handled.** The config slot for Anthropic exists and is empty. |
| **Find a good base / daily-driver model** | For chat + tool use (calendar, Mac apps), with frontier APIs layered on for hard tasks. Open question. |
| **Chat search** | `sqlite-vec` / FTS5-style search so Ted can pull up a specific past chat by topic + timeframe instead of scanning everything. FTS5 is already in use for `exchanges`, so the pattern exists. |
| **Proactive involvement** | Class reminders, schedule, to-dos, occasional suggestions. **Blocked on §8.2.** |

### 9.2 Long-term [stated]

- **Ted replaces the Claude app and ChatGPT** as Charlie's primary chatbot
- **Mac + phone app**, with access to his tools on *both* devices
- Cloud/Vercel hosting was considered to make phone access easier. **Claude flagged
  serverless as a poor fit for a persistent voice assistant** and that objection stands for
  anything needing long-lived state, audio, or a background daemon. If phone access is the
  real goal, the existing `:5150` remote endpoint plus a small always-on host is a closer
  fit than serverless. Unresolved.

### 9.3 On the roadmap but not started

- **Merge Gates 6 and 8** into one streamed call — highest-value remaining change (§4.2)
- **Gut Gate 5** — delete regexes incrementally, use Ted for a day between batches
- **Correction / feedback log** — raw material exists (`patterns`, plus frustration tracking
  in `app.py`) and nothing reads it
- **Todo / assignment tracking** — the old named-lists feature was retired; this needs
  rebuilding against EventKit/Reminders rather than a JSON file
- **Self-narration** — "checking your calendar…" spoken aloud. The HUD shows state visually;
  Ted doesn't say it.
- **DOM-based browser interaction** (Playwright/Selenium) and a unified perceive-screen layer
  (DOM → vision fallback). Only the vision half exists.
- **Blackboard integration** — Charlie was manually feeding Ted his Blackboard URL on Aug 12
  (`add_knowledge({'source': 'blackboard_url', ...})`). Clear signal this is wanted.
- **Investment / market monitoring**, news / GitHub / papers polling
- **Emotional prosody / style tags** — not supported in the current Kokoro ONNX path

---

## 10. Things tried and abandoned — do not repeat these

| Thing | Why it died |
|---|---|
| **Fine-tuning for personality** | Unsloth + LoRA, 143 examples, in Colab (the AMD GPU couldn't run the libraries locally). Loss stayed too high. **A system prompt on the base model beat it outright.** That decision still holds and is settled. |
| **Voice cloning** (Coqui / XTTS-v2 / OpenVoice) | Audio quality too poor. Kokoro `am_michael` is the answer. |
| **ElevenLabs free tier** | Exhausted — which is what forced local TTS in the first place. Still available as an option behind `USE_ELEVENLABS`. |
| **Neo4j** | Required Neo4j Desktop to be running and it usually wasn't. Replaced by SQLite Jul 1. |
| **Streamlit UI + 5-second record button** | Replaced by pywebview and always-listening. |
| **OpenWakeWord / "hey jarvis"** | Replaced by the attention window. Not in `requirements.txt`, not imported. |
| **Local LLM via Ollama** | Never made it to the Mac. See §12.5. |
| **Native Swift AEC (echo cancellation)** | Built, worked, **removed Aug 5** because Apple's Voice Processing ducked Spotify audio. The Swift engine is still used for full-duplex I/O; the `"aec"` mode name is historical. |
| **Microsoft Graph for email** | Stalled on the MSAL `offline_access` scope error. IMAP shipped instead. Recoverable — §8.5. |
| **Fireworks-store features** (sales tally, goals, countdown, store mode) | Seasonal, deleted Jul 2 in `3dd744c`. |
| **Inventory / Sortly tracking** | Deleted with the above. **Keep the design calls if it ever returns:** math in Python not the LLM; folders → categories; Min Level → reorder point; and the seasonality warning — naive units-per-day velocity is worse than useless against a July 4th spike. |
| **Named lists / to-do tools** | Removed Aug 2026. Built, never used — `data/assistant.json` had `"lists": {}`. |
| **Keyword-gated tool triggering** (`likely_command()`) | Removed Aug 2026. Made Ted feel like a robot spitting back answers, and locked novel phrasings out of every tool. |

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

### 11.3 No local model — and this is now a choice, not an accident

[stated, Aug 2026] **No local AI model in Ted now or planned.** Cloud models only — Groq for
simple work, stronger Claude/GPT models for specific use cases.

This is a full reversal of the April vision (100% local, offline, private, no subscriptions)
and of the June plan (Qwen 3 35B-A3B via Ollama as the target local brain, with the Groq
hybrid evaluated as superior for real-time voice latency — that evaluation is what won).
The M5 Pro was bought partly to support local models and they aren't being used for Ted.

**Consequence to state plainly: Ted is fully broken offline.** All fallback chains are
cloud→cloud. If offline capability is ever wanted again it's a new project, not a fallback.

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
4. **Cheap gates before expensive ones is correct** — the ladder design is sound. The
   problem is that too many rungs are hardcoded and two rungs do the same LLM work twice.
5. **Deleting a regex is a feature.** Every one removed moves a decision from "hardcoded"
   to "reasoned," and makes Ted feel more intelligent.
6. **No second source of truth.** Calendar and Notes go through AppleScript to the *real*
   apps rather than keeping a parallel copy. Correct call; extend it (EventKit for Reminders).
7. **Selective memory beats complete memory.** Remembering everything makes callbacks worse.
8. **Prompt prefix stability is a performance feature.** Keep the static prefix
   byte-identical; put volatile per-turn context last.
9. **Know which tool can run the thing.** Cowork reads and edits the repo but runs in a
   Linux sandbox — it **cannot** execute a macOS venv, touch CoreAudio, call Groq with the
   real key, or build a `.app`. Runtime, audio, and hardware bugs belong in Claude Code on
   the Mac. The `BARGE_IN_HANDOFF.md` pattern — diagnose in one, hand off to the other, and
   **state plainly which claims are unverified** — worked. Reuse it.
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

# Full test suite
cd ~/ted-ai && source venv/bin/activate
for t in test_memory test_session_memory test_intents test_capture_gates test_barge test_pipeline; do
    echo "— $t"; python tests/$t.py | tail -1
done

# Build the native audio engine (needs swiftc; xcode-select --install)
cd ~/ted-ai/native && ./build.sh

# Memory dashboard standalone
cd ~/ted-ai && python -m dashboard        # → http://127.0.0.1:5175

# One-time setup
python setup_email.py          # Outlook IMAP
python authorize_spotify.py    # Spotify OAuth

# Inspect memory
sqlite3 -box ~/ted-ai/data/memory.db "SELECT id, exchanges, topics, text FROM session_summaries"

# Debug barge-in
TED_DEBUG_BARGE=1 python hud.py

# Logs
cat ~/ted-ai/data/ted_launch.log      # everything Ted prints (no terminal when launched via .app)
cat ~/ted-ai/ted_errors.log           # real failures only
```

### 15.2 Config keys (`config.py`, gitignored; template in `config.example.py`)

| Key | Default | Notes |
|---|---|---|
| `GROQ_API_KEY` | — | **Required** |
| `USE_GROQ_STT` | `True` | False → local Whisper |
| `USE_ELEVENLABS` / `ELEVENLABS_API_KEY` | `False` | Kokoro otherwise |
| `ATTENTION_WINDOW` | 90 (README) / 180 (bumped in `125d899`) | 0 = always listen |
| `VOICE_LOCK` / `VOICE_LOCK_THRESHOLD` | `False` / 0.68 | needs `resemblyzer` |
| `FALLBACK_VOICE_BARGEIN` | `True` | disable if Ted interrupts himself on speakers |
| `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | `""` / `claude-sonnet-5` | **empty — the "ask Claude" relay is inert** |
| `OWNER_NAME` | `"Charlie"` | used in greetings and fact subjects |
| `WEATHER_LOCATION` | `""` | auto-detected via IP if blank |
| `DAILY_BRIEFING_TIME` | `""` | e.g. `"7:30am"` |
| `REMOTE_PORT` / `REMOTE_TOKEN` | 5150 / `""` | **set the token** |
| `SPOTIFY_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | — | needs Premium |

Also present and stale: a leftover Neo4j password. Remove it.

### 15.3 Glossary

| Term | Meaning |
|---|---|
| **The ladder** | The 8-gate routing in `TedApi._respond()` (§4.2) |
| **Gate 5** | `_assistant_command()` — the 746-line regex dispatch; the main liability |
| **The probe** | Round 1 of `_try_tools`, a cheap "does this need a tool?" call |
| **Attention window** | Idle timeout after which Ted needs "Hey Ted" again |
| **Barge-in** | Interrupting Ted by talking over him |
| **AEC** | Acoustic echo cancellation — **removed Aug 5**; the name lingers in code |
| **The monolith** | `core/app.py`, 118 KB |
| **Migration / stage 1** | The event-bus decomposition; stage 1 = characterization tests, done |
| **Cowork vs Claude Code** | Cowork = Linux sandbox, can read/edit but not run Ted. Claude Code = on the Mac, can run it. |

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
git status && git log --oneline -5
sqlite3 data/memory.db ".tables"
tail -40 data/ted_launch.log
```

**Then read, in this order:** `docs/DECISION_FLOW.md` (how it thinks) → `README.md` (what it
does) → `docs/ROADMAP.md` (how it got here) → `core/app.py::_respond` top to bottom.

**Highest-value actions available right now, in order:**

1. **Commit the working tree.** Days of good work, zero safety net.
2. **Verify barge-in on the Mac** with `TED_DEBUG_BARGE=1`, including the sentence-boundary
   case and self-interruption on speakers.
3. **Finish and commit the memory dashboard** — it is most of Charlie's stated pre-Aug-25 goal.
4. **Start Stage 2 of the decomposition** — one domain out of `_assistant_command` per commit,
   `test_pipeline.py` green between each.
5. **Solve the daemon** (`launchd`) — nothing proactive is possible without it.
6. Then: merge Gates 6 and 8, gut Gate 5, wire difficulty-based model routing.

**Do not:** add keyword triggers, give Ted the ability to edit its own code, add a local
model, rebuild named lists, re-attempt fine-tuning or voice cloning, or plan off any
document without re-checking the repo first.

---

*End of handoff. Compiled from the repo, git history, in-repo docs, Google Drive documents,
and persistent memory. Past chat transcripts were not machine-readable; where a claim comes
only from conversation it is marked [stated].*
