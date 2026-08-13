# Ted — Roadmap, Build History & Feature Audit

**Compiled:** 2026-08-08
**Sources:** `git reflog` (real commit timestamps), file mtimes in `~/ted-ai`, the live
`README.md` / `requirements.txt` / `config.example.py` / `core/audio.py`, `docs/BARGE_IN_HANDOFF.md`,
10 Cowork sessions, the `Ted_Handoff` Drive doc (6/23), and the **Feature & Idea Roadmap**
compiled from the ~6/20, 6/21, 6/25, 7/12 conversations.

Times are America/Chicago. Dates from file mtimes rather than commits are marked *(mtime)*.

> **Part 3 is the important part.** The Feature & Idea Roadmap carried a disclaimer —
> *"Status/sequencing notes reflect what was said in those sessions, not a current audit
> of the codebase."* Part 3 is that audit. Roughly a third of its "already built"
> assumptions are now wrong, and several planned items were quietly shipped.

---

## Part 1 — What happened, in order

### Phase 0 · Windows era — "jarvis-ai" (through late May 2026)

No git history; reconstructed from project notes and the Drive docs.

| What | Detail |
|---|---|
| Machine | Windows PC, AMD RX 6600 XT, 48 GB RAM, user `matth`, venv at `C:\Users\matth\jarvis-ai` |
| LLM | LLaMA 3.2 3B via **Ollama** |
| Voice in / out | Whisper · ElevenLabs "Daniel" |
| Wake word | OpenWakeWord — `"hey jarvis"` |
| Memory | ChromaDB → **Neo4j** |
| Dashboard | Streamlit |
| Planning | `jarvis_ai_guide.docx` (Apr 14), the `TED` Colab notebook (Apr 15), `Ted Keys` (May 27) |

**Three things tried and abandoned — don't repeat them:**

- **Fine-tuning for personality.** Unsloth + LoRA, 143 examples, in Colab (the AMD GPU
  couldn't run the libraries locally). Loss stayed too high. **A system prompt on the base
  model beat it outright.** That decision still holds.
- **Voice cloning.** Audio quality too poor.
- **ElevenLabs free tier.** Exhausted — which is what eventually forced local TTS.

### Phase 1 · Mac migration (June 2026)

| Date | What | How / Why |
|---|---|---|
| June | **Always-listening rewrite** | Killed the wake-word gate and the Streamlit 5-second record button. `hud.py` + **pywebview**: auto-starts, listens until you stop talking, answers, listens again. Calibrates to room noise at startup. |
| June | **Native Swift audio engine** | `native/ted_audio.swift` + `build.sh` using Apple **Voice Processing** for echo cancellation. Debug chain: CLI-binary mic permission, CoreAudio `-10875`, 9-channel input downmix, unbuffered-pipe short reads, sticky barge flag. |
| June | **Whisper hallucination defenses** | Ted heard "Thank you" in a silent room — Whisper invents the phrase that follows silence in its training data. Energy gate + no-speech confidence gate + exact-match phantom blocklist. |
| **Jun 22, 21:28** *(mtime)* | **Spotify Web API** | `authorize_spotify.py`, `core/spotify_web.py`, spotipy. Transport stays local/instant; only selection hits the API. |
| **Jun 23** | **`Ted_Handoff` Drive doc** | The business plan — see Part 4. |
| **Jun 26, 20:07** *(mtime)* | **Outlook email over IMAP** | `setup_email.py` → `outlook.office365.com:993`, credentials to `~/.ted_email_config.json`. |
| **Jun 28** | Event-bus decomposition **scoped, not executed** | See Part 3 §0 — this is the migration that finally started Aug 6. |
| June | **Azure / Graph attempt — never landed** | App registered in the Northwestern tenant (client ID `a3df93af-…41289`), "Allow public client flows" enabled. Stalled on the MSAL error: `offline_access` is reserved and must not be passed in scopes. Email still runs on IMAP. |
| June | **Inventory / Sortly feature** | Firework store. Key call: **low-stock math in Python, not the LLM.** Deleted Jul 2. |

### Phase 2 · Ted v4 — 11 commits in 14 hours (Jul 1–2, 2026)

| Timestamp | Commit | What |
|---|---|---|
| Jul 1, 18:01 *(mtime)* | — | `hud.py` rewritten for the modular layout |
| **Jul 1, 22:38:49** | `69ced76` | **Baseline: Ted v4 after modular refactor** — first commit, git initialized |
| **Jul 1, 22:41:27** | `33ce5bf` | **Neo4j → SQLite** (`data/memory.db`). Neo4j needed Neo4j Desktop running and it usually wasn't. SQLite is a file. |
| Jul 1, 22:45:21 *(mtime)* | — | `ui/ted_hud.html` — particle-sphere HUD |
| **Jul 1, 22:47:17** | `1ca945e` | **Attention mode** — `ATTENTION_WINDOW = 90`; after 90 s idle Ted drops to standby, "Hey Ted" re-engages |
| **Jul 1, 22:49:40** | `c3b4ead` | Retry malformed tool calls; "actually make it X" corrections |
| **Jul 1, 22:52:55** | `904c6ee` | Remote GET+token endpoint, executing proactive triggers, daily-briefing config |
| **Jul 1, 22:56:27** | `1a0deb3` | **Voice lock** — "Ted, learn my voice" (resemblyzer, threshold 0.68) |
| Jul 1, 23:14:25 *(mtime)* | — | `core/proactive.py` |
| **Jul 1, 23:16:33** | `125d899` | Fix "Ted can't hear me": robust wake matching, `recalibrate`, post-reminder attention |
| **Jul 1, 23:50:44** | `b16a90b` | **Live info** — routes to `groq/compound-mini` (web-searches before answering), DuckDuckGo fallback |
| **Jul 2, 10:51:58** | `38eb55e` | **Brain swap** — `openai/gpt-oss-120b`, auto-fallback to `llama-3.3-70b-versatile` |
| **Jul 2, 12:10:11** | `3dd744c` | **Remove all fireworks-store features** — sales tally, goals, countdown, store mode |

### Phase 3 · The silent-bug month (Jul 2 → Aug 5)

Nothing committed for five weeks. **Fact extraction was silently dead the whole time** —
`extract_and_save_facts` asked an 8B model for JSON, got prose, `json.loads` threw, and the
exception died inside a `print`. The `facts` table had 1 row.

| Timestamp *(mtime)* | File | Fix |
|---|---|---|
| Aug 5, 20:42 | `core/llm.py` | Groq **JSON mode** + salvage parser; real failures → `ted_errors.log` |
| Aug 5, 20:45 | `core/memory.py` | `save_fact` **supersedes** single-valued facts instead of stacking (both `LIVES_IN Spirit Lake` and `LIVES_IN Spirit Lake, Iowa` were in every prompt); added `forget_fact`, `list_facts`, prompt-injection cap |
| Aug 5 | `core/app.py` | `"remember that"` only matched `"remember this"` and wrote to the wrong store. Added "what do you know about me" / "forget everything about me". |
| Aug 5 | `core/voice.py` | `_is_junk_fragment` gate 3.5 — coughs transcribed as "Tep." / "Start." were **executing as commands** |
| Aug 5 | `tests/` | `test_capture_gates.py` new (32), `test_memory.py` extended (22), `test_intents.py` (63) |

**Aug 5, 21:03:34** — `docs/BARGE_IN_HANDOFF.md` written in Cowork, handed to Claude Code.
Diagnosis: Ted was **deaf at every sentence boundary** — detection was gated on `_playing`,
which went false between sentences, exactly where a human interrupts.

**Aug 5, 21:44 → 22:37** — Claude Code shipped the overhaul: `_in_reply` keeps detection alive
across the whole reply · **webrtcvad + autocorrelation pitch gate** (`PITCH_MIN = 0.5`, 70–320 Hz)
because VAD alone calls claps "speech" · sliding 300 ms window (`BARGE_WINDOW 15`, `BARGE_FRAMES 10`,
`BARGE_PITCH_FRAMES 4`) · `BARGE_MARGIN` 3.0 → **2.0** with floor `0.012` and ceiling `0.030` ·
`TED_DEBUG_BARGE=1` to make it observable.

**Aug 5, 22:36 — echo cancellation was temporarily removed from the Swift binary, then restored
before the Aug 6 baseline.** Voice Processing had ducked Spotify; the restored path uses macOS
14's minimum other-audio ducking setting.

### Phase 4–5 · Commit, migration, housekeeping (Aug 6–8)

| Timestamp | What |
|---|---|
| Aug 6, 21:33:30 | `data/memory.db` last written — **last time Ted was actually run** |
| **Aug 6, 21:37:47** | `e07be84` — Barge-in overhaul + memory/fact fixes (pre-migration baseline) |
| Aug 6, 21:43:46 | `tests/test_pipeline.py` — 20.5 KB characterization suite |
| **Aug 6, 21:44:30** | `9fa57bc` — **Migration stage 1: characterization tests** |
| Aug 8, 22:04 | README rewritten; repo debris cleared; handoff moved to `docs/` |

---

## Part 2 — What Ted actually runs on today

| Layer | Current | Replaced |
|---|---|---|
| **LLM** | Free-tier Groq `qwen/qwen3.6-27b` → local Ollama `qwen3.5:35b-a3b` for any hosted failure | GPT-OSS + cloud fallback |
| **Vision** | Same hosted/local Qwen provider route | Groq-only vision |
| **Live info** | Model-selected `web_search` tool over DuckDuckGo (`ddgs`) | keyword routing |
| **STT** | **Groq Whisper cloud** (`USE_GROQ_STT = True`) → automatic local `openai-whisper` fallback | local Whisper only |
| **TTS** | **Kokoro** ONNX local, `am_michael` (310 MB); ElevenLabs optional | ElevenLabs Daniel |
| **Audio** | native Swift `ted_audio` (full-duplex + Apple Voice Processing AEC) or sounddevice; webrtcvad + pitch barge-in | fixed 5-second recording |
| **Wake** | none required — 90 s attention window, "Hey Ted" from standby | OpenWakeWord "hey jarvis" |
| **Memory** | SQLite `data/memory.db` — exchanges, facts, habits, patterns, session_summaries | Neo4j ← ChromaDB |
| **Knowledge** | ChromaDB + fastembed, PDF intake from `inbox/` | — |
| **UI** | pywebview + `ui/ted_hud.html` — particle sphere as health indicator | Streamlit |
| **Integrations** | Calendar.app + Notes (AppleScript), Outlook IMAP/SMTP, Spotify local + Web API, iMessage/contacts, screen vision, computer control (type/keys/clipboard), Flask remote `:5150` for iOS Shortcuts | app launchers only |
| **Tests** | `test_intents` (63) · `test_capture_gates` (32) · `test_memory` (22) · `test_barge` · `test_pipeline` | none |

**Run it:** `cd ~/ted-ai && source venv/bin/activate && python hud.py`

---

## Part 3 — The Feature & Idea Roadmap, audited against the code

Legend: **✅ built** · **🟡 partial** · **⬜ not started** · **⚠️ list is out of date** · **❌ superseded**

### §0 Foundation / architectural debt

| Item | Status | Reality as of today |
|---|---|---|
| **Event-bus decomposition of `core/app.py`** | 🟡 **started Aug 6** | Scoped 6/28, untouched for six weeks, then `9fa57bc` landed **stage 1: characterization tests** (`tests/test_pipeline.py`). No code has moved yet. ⚠️ The list says "~103 KB" — it's now **110 KB**. It grew while waiting. |
| Background daemon / `launchd` always-on service | ⬜ | `core/proactive.py` exists but runs **in-process** — it dies with the HUD window. No plist, no `.app`. This still blocks every proactive feature. |
| **Conversation-history / coreference bug** | ✅ **fixed** | README: *"In-session: recent turns are sent to the model each reply."* `test_pipeline.py` pins **both** conversation-history trim behaviors. ⚠️ Cross this off. |
| Model-agnostic interface | ✅ | `core/providers.py` owns Groq→Ollama routing and adapts streaming, tools, JSON, and vision to one response shape. |

### §1 Core voice pipeline

| Item | Status | Reality |
|---|---|---|
| Always-listening, no wake word | ✅ | Attention window, not wake word. |
| Barge-in / interrupt-on-speech | ✅ but **unverified** | ⚠️ The list describes "Web Audio API monitors mic during playback, halts `sd.play()`". That's not the implementation anymore — it's webrtcvad + pitch + sliding window in `core/audio.py`. **And it has never been run since the fix landed.** |
| Streaming sentence-by-sentence TTS | ✅ | `speak_streaming` in `core/voice.py`. |
| **Native Swift AEC engine** | ✅ | Apple Voice Processing is enabled; on macOS 14+ other-audio ducking is set to minimum so Spotify is not heavily ducked. |
| Mute button | ✅ | Covered in `test_pipeline`. |
| **`SILENCE_HANG` = 1.0 s** | ⚠️ wrong | It's **1.35** (`1.0 + 350 ms` buffer for trailing consonants). Someone raised it. |
| **"Kokoro, Whisper, OpenWakeWord — all local"** | ⚠️ wrong on two of three | Kokoro is local. **STT defaults to Groq Whisper cloud.** **OpenWakeWord is gone** — not in `requirements.txt`, not imported. |
| HUD redesign | ✅ but ⚠️ | Built — though it's the dark particle-sphere HUD, not the "warm cream/gold" described. `ui/ted_hud_legacy.html` is the older one. |

### §2 Local / multi-model architecture

| Item | Status | Reality |
|---|---|---|
| **Local Qwen stack (3.6 35B-A3B / 27B dense)** | ⬜ **never happened** | ⚠️ The list frames Groq as "the original/interim backbone **before** the local Qwen switch." There was no switch. Ted is **100 % cloud** today — Groq for brain, STT, vision, and search. There is no local LLM at all. This is the single biggest gap between the plan and reality. |
| Small/fast model for routing | ❌ removed | Fact extraction and summaries now use the same reasoning model as chat; the retired 8B path is gone. |
| Vision: Claude primary, local fallback | ❌ different | `core/screen.py` routes to **Groq Qwen 3.6**, not Claude, and has no local fallback. |
| Routing, not named sub-agents | ✅ decision held | No sub-agents were built. Good. |
| Hybrid local/cloud with connectivity fallback | ⬜ | Fallbacks are all cloud→cloud. **Ted is fully offline-broken today.** |
| "Ask Claude" second brain | ❌ removed | The unused relay was removed during the one-reasoning-model migration. |

### §3 Self-improvement / code editing

| Item | Status | Reality |
|---|---|---|
| Narrow human-reviewed self-edit loop | ⬜ | Nothing in `core/tools.py` touches the filesystem for code. |
| Full autonomous self-modification — **rejected** | ✅ held | Still correctly absent. Keep it that way. |
| Recursive self-improvement — **rejected as unrealistic** | ✅ held | — |
| **Correction / feedback log** | 🟡 accidental partial | Not built as designed, but `memory.db` has a `patterns` table (44 rows) and `app.py` has **frustration tracking** (pinned in `test_pipeline`). Raw material for this already exists and nothing reads it. |
| Decision / audit trail | ⬜ | `ted_errors.log` only. |

### §4 Voice cloning, speaker recognition, security

| Item | Status | Reality |
|---|---|---|
| Voice cloning (XTTS-v2 / OpenVoice) | ❌ | Tried in the Windows era, quality too poor. Kokoro `am_michael` is the answer. |
| **Speaker recognition for personalization** | ✅ **shipped Jul 1** | ⚠️ Listed as an idea. It's commit `1a0deb3` — `core/speaker.py`, resemblyzer, `VOICE_LOCK_THRESHOLD = 0.68`, "Ted, learn my voice". Off by default. |
| Speaker verification as a *security gate* | 🟡 + ⚠️ risk | `VOICE_LOCK = True` currently gates **everything**, not just destructive actions. The list's own warning applies — voice is replayable and clonable. Ted can type keystrokes, control the clipboard, and send email. If you ever turn voice lock on and treat it as security, that's the mismatch to fix: gate destructive tools specifically, with a second factor. |

### §5 Personality, memory, "feels like a person"

| Item | Status | Reality |
|---|---|---|
| Consistent personality with pushback | ✅ | System-prompt persona in `core/llm.py`. |
| Self-narration of in-progress actions | ⬜ | The HUD sphere shows state visually (swirl = thinking) but Ted doesn't *say* "checking your calendar." |
| Emotional prosody / style tags | ⬜ | Kokoro ONNX — no style-tag support in the current path. |
| **Long-term autobiographical recall + callbacks** | 🟡 **structurally blocked** | `facts` works again as of Aug 5, but **`session_summaries` has 0 rows** — the write only fires on 30 min idle with the process alive or a clean pywebview shutdown, and normal window-close fires neither. So the "last time we talked about X" callback — the list's #1 "he actually knows me" signal — **cannot happen today.** This is the highest-value small fix on the whole list. |
| Behavioral-pattern model | 🟡 | `patterns` table accumulating, nothing reads it. |
| Preference drift | ✅ partially solved | Aug 5 fact supersession does exactly this for single-valued facts. |
| Graceful ambiguity / clarifying questions | ✅ | Disambiguation flows pinned in `test_pipeline`. |
| Latency consistency > average latency | 🟡 | Cloud-everything helps consistency; the `gpt-oss-120b` → llama fallback on rate-limit is the hang risk. |

### §6 Proactive / life-management layer

| Item | Status | Reality |
|---|---|---|
| **Daemon-based monitoring — `daemon.py`, `integrations/`, Neo4j email/news nodes** | ⬜ + ⚠️ **plan is dead** | The scoping said this was "purely additive — `core/ted.py`, Neo4j, Streamlit, `runted` all stay intact." **All four of those are gone.** `core/ted.py` doesn't exist, Neo4j was replaced Jul 1, Streamlit was replaced by pywebview, and the README documents `python hud.py`. This section needs re-scoping from scratch against SQLite + `core/proactive.py`. |
| News / GitHub / papers / market monitoring | ⬜ | Nothing polls anything. |
| Todo / assignment tracking | 🟡 | Lists exist in `core/assistant.py` — and `data/assistant.json` shows `"lists": {}`. **Built, never used.** |
| **Email integration — "hardest piece, Gmail OAuth, Pub/Sub"** | ✅ **shipped, different shape** | ⚠️ It's **Outlook IMAP**, shipped Jun 26 — not Gmail, no OAuth, no Pub/Sub. Polling, not push. Password stored in cleartext at `~/.ted_email_config.json`. The "hardest piece" got done the easy way. |
| Reuse native macOS sources (EventKit / Mail) | ✅ partially | `calendar_app.py` and `notes.py` go through AppleScript to the real apps — exactly the "no second source of truth" call. Reminders/EventKit not wired. |
| Outlook via Microsoft Graph | 🟡 **90 % done, stalled** | Tenant app registered, public client flows on. One-line blocker: drop `offline_access` from scopes (MSAL adds it automatically and rejects the request if you name it) → `['Mail.ReadWrite', 'Mail.Send']`. **Check admin consent on the school tenant before investing more.** This is what removes the plaintext password. |
| Blackboard | ⬜ | — |
| **Desktop icon / `.app` bundle** | ⬜ | "The easy part," still not done. No bundle, no launcher. |
| Investment/market analysis | ⬜ | Live info via `compound-mini` could feed this. |
| Daily briefing | ✅ | `DAILY_BRIEFING_TIME` in config. |

### §7 Screen & environment awareness

| Item | Status | Reality |
|---|---|---|
| **Vision-based screen awareness** | ✅ **shipped** | ⚠️ Listed as a plan. `core/screen.py` — screenshot + description. |
| DOM-based interaction (Playwright/Selenium) | ⬜ | No browser automation at all. `core/actions.py` opens apps and URLs only. |
| Unified "perceive screen" layer (DOM → vision fallback) | ⬜ | Only the vision half exists. |
| Computer use (type, keys, clipboard) | ✅ | `core/computer.py`. |
| **Route tutoring to Claude, not local vision** | ❌ **currently routed to Groq** | The list's reasoning was about local vs. cloud accuracy; the actual choice landed on a *different cloud* model. Worth an A/B on real homework before deciding. |
| Confirmation step on agentic actions | ⚠️ **gap** | Ted can type keystrokes and control the clipboard with no confirmation gate. Low risk today because there's no browsing; it becomes real the moment DOM automation lands. |
| In-memory screenshots, no disk write | ✅ | — |

### §8 Bigger-picture product questions

Unresolved, and now colliding with a second plan — see Part 4.

---

## Part 4 — The thing the audit surfaces: two roadmaps, different directions

Three days after this Feature & Idea list started (6/20–6/25), the **`Ted_Handoff`** Drive doc
was written (6/23). It describes a different project:

> A deployable AI business assistant for small businesses. Clone Ted per client, connect their
> POS/scheduling/SMS/payroll, $299 / $499 / $799 monthly tiers, auto repair shops as niche #1,
> a VPS per client, a Mission Control dashboard. **"First client deployed by end of August."**

Its five-item "what gets Ted to 90 %" list:

1. Refactor core to load from `config.yaml` instead of hardcoded values
2. First POS integration — Square or Lightspeed
3. Twilio voice-call handling for inbound customer calls
4. Basic monitoring so you know when a client's system breaks
5. Deploy for one real client

**None of the five exist.** No `config.yaml`, no `clients/` directory, no POS, no Twilio, no
monitoring. Its own scorecard said Business-Ready **32 %** / Scalable **12 %** in June; nothing
since has moved either number. Meanwhile the feature that *was* the business core — inventory
tracking, item #1 on its client-automation list — was **deleted on Jul 2** (`3dd744c`).

Everything built since June (Spotify, Notes, Calendar, screen vision, iMessage, voice lock)
served the personal-assistant track. That's the 78 % going to 85 %.

It's Aug 8. This isn't a judgment on which plan is right — it's that they need different next
actions, and the `app.py` migration you're mid-way through serves **neither** until it finishes.
Worth picking one before the semester starts.

---

## Part 5 — Suggested sequence

Ordered by what unblocks what, not by appeal.

**Do first — cheap, and everything else is less reliable without them**

- [ ] **Run Ted.** He hasn't been launched since Aug 6, 21:33 — four minutes *before* the commit
      containing the barge-in fix. Test with `TED_DEBUG_BARGE=1`: interrupt mid-sentence, then
      interrupt exactly at a sentence pause (the case that was broken), confirm typing still
      interrupts, and confirm native AEC prevents self-interruption without noticeably ducking Spotify.
- [ ] **Fix session summaries.** 0 rows. This is what blocks autobiographical callbacks — the
      list's own #1 "feels like a person" signal. *My suggestion: move the write to `atexit` +
      a SIGTERM handler rather than pywebview's closing hook, and flush every N exchanges so a
      crash doesn't lose the session.*
- [ ] **Delete the dead `goals` table** and the stale Neo4j password in `config.py`.

**Then — finish the migration**

- [ ] **Stage 2+: break up `core/app.py`.** The seam is `_assistant_command` — a long dispatch
      chain whose branches can each move into the module they already delegate to (email → `email.py`,
      reminders → `assistant.py`, music → `music.py`). Mechanical, not a redesign.
      `test_pipeline.py` is the net; run it between every slice plus the manual `conversation_loop`
      checklist. *One domain per commit, green before the next.*
      **This is §0 item 1, scoped 6/28 and stalled ever since. Every other subsystem on the
      list — daemon, email, todo, coding loop — reaches into this same class.**

**Then — pick the fork (Part 4) and commit to it**

- [ ] *Personal-assistant track:* daemon/`launchd` (unblocks all proactive features) → `.app`
      bundle → autobiographical callbacks → todo/EventKit → then screen-aware tutoring.
- [ ] *Business track:* `config.yaml` extraction → rebuild inventory → monitoring → Twilio →
      one real client. Be honest about the calendar: fall semester is three weeks out.

**Standing gaps worth a decision, not necessarily work**

- [ ] **There is no local model.** The Qwen plan never happened; Ted is 100 % cloud and fully
      broken offline. Either build the local fallback the list describes, or drop it from the
      plan — right now it's neither.
- [ ] **Email password in cleartext.** The Graph path is one line from working; check admin
      consent on the school tenant first.
- [ ] **Vision routing.** Groq Qwen 3.6 today, Claude recommended by the list. A/B it on
      real homework before spending anything.
- [ ] **Confirmation gate on agentic actions.** Ted can type keys and drive the clipboard with
      no gate. Fine today; not fine the day browser automation lands.
- [ ] **`patterns` (44 rows) and `habit_logs` (0) are accumulating with nothing reading them.**
      Wire them into the correction-log idea or cut them. *(Counts from the Aug 5 dump — re-check.)*
- [ ] **The `memory.md` in the Claude "TED Ai" project is months stale** — still says Ollama,
      LLaMA 3.2 3B, Neo4j, Streamlit, OpenWakeWord, and lists "swap Ollama for Groq" as the
      *next* step. Replace it with Part 2 of this document.

**Parked deliberately**

- **Sortly / inventory** — deleted in `3dd744c`. If it returns, reuse the design calls: math in
  Python not the LLM, folders → categories, Min Level → reorder point, and the seasonality
  warning (naive units-per-day velocity is worse than useless against a July 4th spike).
- **Fine-tuning for personality** — settled; the system prompt won.
- **Full autonomous self-modification** — correctly rejected. Keep it rejected.

---

## Part 6 — Standing lessons

- **Math in Python, words in the LLM.** The inventory feature worked because counts were
  computed in code and only narrated by the model.
- **Silent failures are the expensive ones.** Fact extraction was dead five weeks because the
  exception died in a `print`. Barge-in died silently because nothing reported the threshold.
  Both fixes included *making it observable* — that's the pattern to keep.
- **Know which tool can run the thing.** Cowork reads and edits the repo but runs in a Linux
  sandbox — it cannot execute a macOS venv or touch CoreAudio. Runtime, audio, and hardware bugs
  belong in Claude Code. The `BARGE_IN_HANDOFF.md` pattern — diagnose in one, hand off to the
  other, and state plainly which claims are unverified — worked. Reuse it.
- **Plans rot faster than code.** A third of the Feature & Idea list's "already built" items were
  wrong within seven weeks, and two planned items had quietly shipped. Re-audit against the repo
  before planning off any document, including this one.
