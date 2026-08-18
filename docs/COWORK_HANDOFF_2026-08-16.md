# HANDOFF — Cowork → Claude Code, August 16 2026

**Audited against:** working tree at `3ed27ee`, branch `arch/single-call`
**Audited by:** Claude (Cowork, Linux sandbox). Read the repo, `data/memory.db`,
`data/ted_launch.log`, `ted_errors.log`. **Ran nothing.**
**For:** Claude Code on `charlies-macbook-pro-local`

---

## 0. Read this first

Everything below was found by reading code and logs. Nothing was executed —
Cowork cannot run the venv, macOS, AppleScript, CoreAudio, or the real Groq API.
Each item is marked:

- **[verified-static]** — read directly out of the current working tree or a log
  file. The line numbers are real. The *behaviour* is inferred.
- **[inferred]** — a diagnosis that fits the evidence but has not been observed.
- **[unverified]** — a proposed fix. Nobody has run it.

`docs/TED_MASTER_HANDOFF.md` is **stale** — it describes 32 tools, 353 checks,
and a 126 KB `core/app.py`. Reality on Aug 16: **41 tools, 655 checks across 21
suites, `core/app.py` at 152 KB**, plus five modules that document does not
mention: `routing.py`, `telemetry.py`, `system_state.py`, `routines.py`,
`lingo.py`. Trust `python tools/ted_map.py` over any prose.

**Scope decisions Charlie made on Aug 16, honor them:**

- The calendar daemon is **out of scope**. Leave `ted_daemon.py` alone.
- Skip/transport needs to be a **button in the UI**, not only a spoken command.
- The transcribe button fills the **chat input box**; it does not auto-send.
- Token reduction is always welcome.

---

## PART 1 — The freezing. Do this before anything else.

Charlie's report: *"Ted cuts out sometimes and says something cut off rather
than just handing it off to the other model."*

He is describing two bugs that compound. The fallback fires correctly. It is
what happens **inside** the fallback that breaks.

### 1.1 The local rescue is given 25 seconds to cold-load a 24 GB model

**[verified-static]** `core/providers.py:504-510` sets a generous budget and
explains exactly why:

```python
def _ollama_create(**kwargs):
    _ensure_ollama()
    payload = _ollama_payload(kwargs)
    # A cold 24 GB model can take much longer to load than a network request.
    timeout = max(float(kwargs.get("timeout") or 30.0), 180.0)
    if payload["stream"]:
        return _OllamaStream(payload, timeout)
```

**[verified-static]** `core/providers.py:462-465` then discards it:

```python
self._client = httpx.Client(timeout=httpx.Timeout(
    timeout, connect=4.0, read=min(timeout, 25.0)))
```

`min(180.0, 25.0)` is `25.0`. The streaming rescue path — the one that runs on
every rate-limited foreground turn — gets a 25-second read timeout.

**[verified-static]** The consequence is in `ted_errors.log`, with a full
traceback ending:

```
httpx.ReadTimeout: timed out
...
RuntimeError: Both brains failed; Groq: Error code: 429 - {'error':
  {'message': 'Rate limit reached for model `qwen/qwen3.6-27b` ... on tokens
   per minute (TPM): Limit 8000, Used 6501, Requested 4219. Please try again
   in 20.4s...'}}; Ollama: timed out
```

**[inferred]** A cold `qwen3.5:35b-a3b` (Q4_K_M, ~24 GB) cannot load from disk
and emit a first token inside 25 seconds. When it is already resident the
rescue works — the same log shows `[timing] request accepted after 3532ms
(ollama)` and `first token 223ms`. So this fails **only when the local model is
cold**, which is exactly when it is most needed.

This is the codebase's own recurring failure mode, principle 11 in the master
handoff: two lines owning one fact and answering differently.

**[unverified] Fix.** The read timeout is per socket read, so the first chunk
(model load + prefill) and every chunk after it are governed by the same number.
Split them:

- Give the **first** chunk the full `timeout` (180 s).
- After the first chunk arrives, tighten to something like 30 s so a genuinely
  stalled generation still fails fast rather than hanging for three minutes.

`httpx` will not do this for you in one client. The straightforward version is
to construct `_OllamaStream` with `read=timeout`, and have `__iter__` enforce
its own inter-chunk deadline once `self._first_chunk_seen` is True.

**[unverified] Second fix, do both.** `core/providers.py:419-421`:

```python
"keep_alive": "10m" if model == LOCAL_CHAT_MODEL else "5m",
```

The **tool** model is the rescue model, and it has the *shorter* keep-alive. It
unloads first and is therefore reliably cold at the moment it is needed. Give
`LOCAL_TOOL_MODEL` the longer keep-alive, or make both `30m`. On a 48 GB machine
this is affordable; measure resident size with `ollama ps` before committing to
a number.

**[unverified] Third, cheap and high value.** There is no user-visible signal
that a handover is happening. Print to the HUD when the cloud fails over —
`"switching to the local brain…"` — so a slow local turn reads as *degraded*
rather than *frozen*. `_turn.degraded_reason` is already populated at
`core/llm.py:1053-1058`; it just is not surfaced during the wait, only after.

### 1.2 The cloud cooldown can lock Ted out for twenty-two minutes

**[verified-static]** `core/providers.py:125-155`. The docstring states the
intent precisely:

> *Honor the earliest provider-approved retry, not the full bucket reset. Groq's
> `x-ratelimit-reset-tokens` can describe when the entire token bucket is full
> again (several minutes), while the 429 body says the request can be retried in
> fifteen seconds. Taking the maximum is why Ted stayed on Ollama long after the
> cloud was usable again.*

The code does not implement it:

```python
seconds = retry_after or body_wait or (
    min(reset_candidates) if reset_candidates else 0.0)
```

`retry_after` (the `retry-after` **header**) short-circuits the whole chain. It
is never compared against `body_wait`. If the header carries a bucket-refill
figure, the long value wins outright — which is the exact behaviour the
docstring says was fixed.

**[verified-static]** `data/ted_launch.log` shows this happening. The 429 body
in `ted_errors.log` says *"Please try again in 13.185s"*, while the launch log
prints:

```
[provider] RATE LIMITED on qwen/qwen3.6-27b — trying local qwen3.5:35b-a3b; cloud paused for 1321.0s
[provider] RATE LIMITED ... cloud paused for 982.0s
[provider] RATE LIMITED ... cloud paused for 697.0s
[provider] RATE LIMITED ... cloud paused for 682.0s
[provider] RATE LIMITED ... cloud paused for 660.0s
```

38 `RATE LIMITED` lines in one launch log.

**[verified-static]** Line 152 makes it worse:

```python
_cloud_retry_at = max(_cloud_retry_at, time.time() + seconds)
```

The cooldown can only ever extend. One bad long value pins the cloud off for its
full duration even if the next 429 says thirteen seconds.

**[unverified] Fix.**

1. Take the **minimum of all non-zero candidates** (`retry_after`, `body_wait`,
   the two reset headers) rather than the first truthy one. That is what the
   docstring already promises.
2. Clamp the result — something like `min(seconds, 120.0)`. A retry after two
   minutes that turns out to be premature costs one 429; a twenty-two-minute
   blackout costs the whole session.
3. Reconsider the `max()`. Replacing an existing cooldown with a shorter
   provider-approved one is correct, not a regression.
4. Log both numbers when they disagree — `header=1321s body=13.2s using=13.2s` —
   so the next person can see the disagreement instead of re-deriving it.

**Why 1.1 and 1.2 compound:** during a 22-minute cooldown, `chat_create` skips
Groq entirely (`cooling_down` at line 549) and goes straight to Ollama. Every one
of those turns is now a cold-model race against a 25-second timeout. Fix either
one and the symptom improves; fix both and it should disappear.

### 1.3 Where the user-facing string comes from

**[verified-static]** `core/llm.py:1444-1447`:

```python
full_reply = (
    "I couldn't complete that action — nothing ran. Try again in a moment."
    if require_tool else "Something cut out — ask me again.")
```

This is the honest last-resort message and should stay. It fires when a turn
produced no text and no tool call. Once 1.1 and 1.2 are fixed it should become
rare; if it stays common after both fixes, the cause is elsewhere and the
telemetry table (`core/telemetry.py`) is the place to look, not this line.

### 1.4 Verification for Part 1

Do these on the Mac, in this order:

```bash
# 1. Confirm the cold-load failure exists before fixing it.
ollama stop qwen3.5:35b-a3b      # force cold
# then in Ted, pin local and send a tool-bearing message:
#   set provider mode to "local" via the diagnostics tab
# Expect: httpx.ReadTimeout in ted_errors.log within ~25s.

# 2. After the fix, same test. Expect a slow but successful answer.
#    Time it. Note the number here in the commit message.

# 3. Cooldown: trigger a 429 and read both values.
grep '\[provider\] RATE LIMITED' data/ted_launch.log | tail -20
# Expect: paused for values in the tens of seconds, not the hundreds.

# 4. Regression suite.
for t in tests/test_*.py; do printf "%-34s " "$t"; python "$t" | tail -1; done
# test_providers.py and test_latency.py are the relevant ones.
```

---

## PART 2 — `ted_errors.log` is 100% false alarms

**[verified-static]** Every one of the 24 ERROR entries in `ted_errors.log` is
the same line:

```
[memory] fact extraction returned unparseable output: '```json\n{"facts": []}\n```'
```

**[verified-static]** `core/llm.py:227` — `_parse_fact_payload` strips fences on
its **own local copy** of the string:

```python
raw = raw.replace("```json", "").replace("```", "").strip()
```

**[verified-static]** `core/llm.py:302-305` — the caller then tests the
**unstripped** original:

```python
facts = _parse_fact_payload(raw)
if not facts and raw and raw not in ('{"facts": []}', '{"facts":[]}'):
    error_log.error(f"[memory] fact extraction returned unparseable output: {raw[:200]!r}")
```

When the model returns a correctly-empty result wrapped in a code fence,
`_parse_fact_payload` parses it fine and returns `[]`, but the caller's `raw`
still carries the fences, so the literal comparison misses and a success is
logged as a failure.

**No facts are being lost.** The damage is that the "real failures only" channel
is now pure noise — which by principle 2 is the same class of problem as a
silent failure, just inverted. A real error landing in that file today would be
invisible among 24 false ones.

**[unverified] Fix.** Normalize once, before both the parse and the check. Either
hoist the fence-stripping into `extract_and_save_facts` and pass the clean string
down, or have `_parse_fact_payload` return a `(facts, cleaned_raw)` pair and
compare on `cleaned_raw`. Do not add a second `.replace()` at the call site —
that recreates the two-places-one-fact bug this codebase keeps paying for.

Add a test to `tests/test_memory.py`: a fenced `{"facts": []}` must parse to `[]`
**and** must not log an error.

Then truncate the log so the next reader starts from a clean slate.

---

## PART 3 — Near-term features Charlie asked for

### 3.1 Spotify: transport button in the UI + playlist editing

**Already works, do not rebuild:** `core/spotify_web.py:355` `transport(action)`
handles `next`, `previous`, `pause`, `play`/`resume`, and `current`, against
whichever device is actually playing. Skip is done at the Python layer.

**What Charlie wants (Aug 16):** a **skip button in the UI**, not only a spoken
or typed command. Add transport controls (previous / play-pause / next) to the
HUD's music area in `ui/ted_hud.html`, wired to the existing `tool_spotify_control`
(`core/tool_handlers.py:116`) through the pywebview API, not through the model.
A button press should never cost a token.

**Missing — build these.** Add to `core/spotify_web.py`, expose in
`core/tools.py`, dispatch in `core/tool_handlers.py`:

| Function | spotipy call | Note |
|---|---|---|
| `add_to_playlist(playlist, track_query=None)` | `playlist_add_items` | Default to the **currently playing** track when no query is given — that is the common case ("add this to X") |
| `remove_from_playlist(playlist, track_query=None)` | `playlist_remove_all_occurrences_of_items` | Same default |
| `create_playlist(name, public=False, description="")` | `user_playlist_create` | |
| `delete_playlist(name)` | `current_user_unfollow_playlist` | **There is no delete endpoint.** Unfollowing is what the Spotify client itself does. Say so in the tool description so the model does not report something it did not do |

Reuse `match_playlist()` (`spotify_web.py:181`) for name resolution and
`_get_playlists(sp, refresh=True)` after any mutation, or the cache goes stale.

**Auth — this will silently fail if skipped.** `authorize_spotify.py` must
request the new scopes:

```
playlist-read-private playlist-read-collaborative
playlist-modify-private playlist-modify-public
```

Then **delete the cached token file** (spotipy's `.cache`, or whatever path
`authorize_spotify.py` configures) and re-run it. spotipy will happily reuse a
cached token that lacks the new scopes and you will get 403s that look like a
code bug.

**Honesty rule (§5.3 of the master handoff) applies.** Every one of these must
report ground truth. `playlist_add_items` returning 200 is not proof the track is
in the playlist — follow the `_confirm_playing` pattern at
`spotify_web.py:206` and re-read the playlist before claiming success.

### 3.2 Transcribe button — mic on, speakers off

**Charlie's exact ask:** a button that turns the mic on but not the speakers, so
he can talk and have the text land **in the chat input box**. Not auto-sent.

**[verified-static]** The blocker is structural. `muted` is one flag doing two
jobs. `ui/ted_hud.html:892-893`:

```javascript
$('mic').classList.toggle('on', !muted);
$('mic').title = muted ? 'Voice mode off — click to talk' : 'Voice mode ON — click to mute';
```

and `core/app.py` boots `self.muted = True`.

**[unverified] The change.** Split the flag in two:

- `mic_on` — is capture running
- `speech_on` — is TTS allowed to play

Today's states map to `(False, False)` and `(True, True)`. The new button is
`(True, False)`. Keep `muted` as a computed property for backward compatibility
so the ~40 existing references and the pinned tests in `tests/test_pipeline.py`
do not all have to change at once.

Then add a third HUD button next to `#mic`. On press: start capture, run STT,
and instead of routing the transcript to `_respond()`, **write it into the input
box** and let Charlie edit and press enter. That is easier and safer than
auto-send, and it is what he asked for.

Gates to keep on this path: `_is_junk_fragment` in `core/voice.py` (coughs
transcribed as commands) and the Whisper phantom blocklist. They protect the
transcript regardless of what happens to it afterwards.

`tests/test_capture_gates.py` and `tests/test_pipeline.py` both pin mute
behaviour. Expect to update them; do not delete assertions to make them pass.

### 3.3 Ted.app icon does not restore a minimized window

**[verified-static]** The mechanism exists. `hud.py:199-220` installs a
`SIGUSR1` handler when `TED_NATIVE_HOST=1`, and it does:

```python
for native_window in app.windows():
    if native_window.title().startswith("Ted"):
        native_window.makeKeyAndOrderFront_(None)
        native_window.orderFrontRegardless()
```

**[inferred]** Neither `makeKeyAndOrderFront:` nor `orderFrontRegardless` restores
a **miniaturized** window in AppKit. A window in the Dock stays there. That fits
Charlie's symptom precisely: clicking the icon works when Ted is merely behind
another app, and does nothing when Ted is minimized.

**[unverified] Fix.** Inside `raise_on_main`, before ordering front:

```python
if native_window.isMiniaturized():
    native_window.deminiaturize_(None)
```

**Check first, in this order:**

1. Is `TED_NATIVE_HOST=1` actually set when launched from the Dock? If not, the
   signal handler is never installed and the deminiaturize fix will not help.
   Add a startup log line either way.
2. Is the native host actually sending `SIGUSR1` on a second activation, or only
   on a second *launch*? Clicking the Dock icon of a running app fires
   `applicationShouldHandleReopen:`, not a fresh launch.
3. Only then apply the fix.

Log each step. This is three lines of code sitting behind two questions that can
only be answered on the Mac.

### 3.4 See what YouTube video is playing

**[verified-static]** 80% built. `core/tool_handlers.py:316` `_browser_video_state`
already runs JavaScript in the active browser tab over Apple Events and returns
`playing` / `paused` / `""`, with a genuinely clever fallback: when Chromium
blocks JS-from-Apple-Events (the default), it reads the Accessibility window
title for `"Audio playing"` instead of asking Charlie to weaken a browser
security setting. Keep that.

**What is missing:** the title and URL. The AppleScript already targets the
active tab; ask it for `title of active tab of front window` and `URL of active
tab of front window` in the same call.

**[unverified] Build:** a `now_playing` tool (or extend `screen_describe`'s
family) returning `{app, title, url, state}`. Then Ted can answer "what am I
watching" without a screenshot, which is both faster and cheaper than the vision
path.

Handle the case where the active tab is not the video tab — Charlie will have
other tabs open. Searching all tabs for a `youtube.com/watch` URL is more useful
than reading only the frontmost one.

### 3.5 Prune the chat sidebar without losing history

**[verified-static]** Live counts from `data/memory.db` on Aug 16:

| | |
|---|---|
| `chat_sessions` | 89 |
| ...with zero turns | 18 |
| ...still titled `New chat` | 52 |
| `chat_turns` | 1037 |
| `facts` | 58 |

52 of 89 sessions are untitled and 18 are completely empty. That is the sidebar
problem, quantified.

**Charlie's requirement:** remove them from the left sidebar, **keep every turn
and everything Ted knows.**

**[verified-static]** `dashboard/db.py:188-194` — `chat_sessions` has no
archive/hidden column:

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id      INTEGER PRIMARY KEY,
    title   TEXT NOT NULL DEFAULT 'New chat',
    summary TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
)
```

**[unverified] Build:**

1. Add `archived INTEGER NOT NULL DEFAULT 0`. Migration must be additive —
   `ALTER TABLE ... ADD COLUMN` guarded by a `PRAGMA table_info` check, matching
   how the rest of `dashboard/db.py` handles schema changes.
2. `/api/chats` list endpoint filters `WHERE archived = 0` by default, with
   `?include_archived=1` for the memory dashboard.
3. HUD sidebar gets an archive action per row. **Archive, not delete** — the
   existing `DELETE` route must not be what the button calls.
4. Retrieval (`core/memory.py` FTS5 search, session summaries) must **ignore**
   `archived` entirely. Archiving is a display decision, not a memory decision.
   Add a test asserting that an archived session's turns are still findable.
5. Auto-archive the 18 empty sessions on startup, and never create a session row
   until its first turn is written. That stops the problem recurring.

### 3.6 Dark humor — this is already done in the prompt

**[verified-static]** `core/llm.py:171-210`. `SYSTEM_PROMPT` was trimmed on Aug 14
from ~1,120 tokens to ~470, and it already contains:

> *"He can ask you to steelman a position, play devil's advocate, drop caveats,
> or be blunter. Humor may be dry, dark, profane, sexual, vulgar, or explicit
> when he asks for it; adult humor is not the same as targeted abuse. Follow the
> request without a lecture. Slurs or abuse aimed at a person are the line;
> decline that part in one sentence and move on."*

**So if Ted is still hedging, the prompt is not the cause — the model is.**
Do not spend the session rewriting a paragraph that already says what Charlie
wants. Diagnose instead:

1. Pin the provider to `cloud` and send the same prompt three times. Then pin to
   `local` and repeat. `set_provider_mode` exists at `core/providers.py:238` and
   is exposed in the diagnostics tab.
2. If cloud refuses and local complies, it is `qwen/qwen3.6-27b`'s own training
   and no prompt edit fixes it. The lever is a different `CLOUD_CHAT_MODEL` —
   that is a one-line config change, and worth testing two or three candidates.
3. If both refuse, the constraint is upstream of Ted entirely. Tell Charlie
   plainly rather than iterating on wording.

The one prompt change worth making regardless: the current text conditions the
permission on *"when he asks for it."* Charlie's Aug 16 message is a standing
request, not a per-turn one. Change it to a default posture rather than an
unlock, and leave the targeted-abuse line exactly as written.

---

## PART 4 — Reducing tokens per call

**[verified-static]** Current cost, straight from `data/ted_launch.log`:

```
[prompt] scope=relevant tools=2  ~1854 input tokens
[prompt] scope=relevant tools=3  ~1832 input tokens
[prompt] scope=relevant tools=14 ~4578 input tokens
```

Against a **8,000 TPM** ceiling (`Limit 8000` in the 429 body). The baseline is
already good — the persona trim and `select_tool_schemas` did their job. **The
spike is the whole problem.** A 4,578-token turn plus its output leaves almost
nothing for the next minute.

Levers, in order of value:

**4.1 — Drop discovered schemas after they are used. [unverified]**
`core/routing.py:203` `discover_tool_schemas(query, exclude=(), limit=8)` loads up
to 8 additional tools when the model calls `find_tools`. Those schemas then stay
in `msgs` for every remaining round of the turn. Once the model has committed to
a tool, the other seven are dead weight being re-billed each round. Prune to the
called tool (plus `find_tools`) before the next round.

**4.2 — Lower `reasoning_effort` on simple turns. [unverified]**
`core/providers.py:576-578` sets `reasoning_effort="default"` for any
tool-bearing turn, and `reasoning_format="hidden"`. **Groq bills hidden reasoning
tokens as output, and TPM counts input plus output.** So a turn that thinks hard
about "skip this song" is spending real budget invisibly.
`reasoning_effort_for(user_input)` already exists in `core/llm.py` — extend it to
return `"low"` or `"none"` for turns matched by `plan_reflex`
(`core/routing.py:359`) or by a single unambiguous tool family. Measure with the
telemetry table before and after; do not guess at the saving.

**4.3 — Trim tool *results* between rounds. [unverified]**
Tool results accumulate in `msgs` across rounds of the same turn. A verbose
result (email bodies, calendar dumps, `ui_inspect` output) is re-sent on every
subsequent round. Cap them the way `_cap()` already caps retrieved context in
`core/llm.py`.

**4.4 — The facts block. [verified-static, low priority]**
58 facts, capped at 1200 chars ≈ 300 tokens on every single turn. It is not the
spike and it is load-bearing for the thing Charlie values most ("he actually
knows me"). **Leave it alone** unless 4.1–4.3 are exhausted. Noted here only so
nobody rediscovers it and cuts it first.

**4.5 — Already done, do not redo.**
- Persona trimmed to ~470 tokens (Aug 14).
- `TOOL_RULES + TOOL_GUIDANCE` only attached when real tools are present
  (commit `46cd040`).
- Background helpers (titles, fact extraction, summaries) already routed to the
  local brain via `_ted_workload="background"` (`core/providers.py:533-543`), so
  they no longer spend cloud budget. This is a good design — keep it.

**The honest framing for Charlie:** at ~1,850 tokens a turn against 8,000 TPM,
he gets roughly four messages a minute. That is a usable conversation. At 4,578
he gets one. **Killing the spike is worth more than shaving the baseline**, and
Groq's Dev Tier removes the ceiling entirely for money — which may simply be the
right answer once the code-side wins above are taken.

---

## PART 5 — Long term, from Charlie's Aug 16 list

Scoped, not started. Ordered by how much groundwork already exists.

### 5.1 Voice recognition

**[verified-static]** Partially built. `core/speaker.py` (3 KB) does
enroll/verify with resemblyzer, threshold 0.68, `VOICE_LOCK = False` in config.

Two different features hide under this name — confirm which Charlie means:

- **Speaker identification** — "who is talking." The existing module does this.
  Work needed: turn it on, re-enroll, and decide what Ted does with a non-match.
- **Better transcription accuracy** — a different problem entirely, solved by STT
  model choice, not by `speaker.py`.

**If it is identification:** §8.6 of the master handoff has the standing warning
and it is correct — voice is replayable and clonable. Gate *destructive* tools
specifically, with a second factor. Do not make voice a blanket auth mechanism.

### 5.2 Ted adjusts notifications

**[unverified]** Two separable capabilities:

- **Ted sends notifications** — trivial, `osascript -e 'display notification'`.
  Note that AppleEvents permission is per calling binary, which ties into 5.4.
- **Ted changes notification/Focus settings** — macOS has no public API for
  toggling Do Not Disturb. The supported route is the **Shortcuts app**: build
  Shortcuts that set a Focus mode, then invoke them with `shortcuts run "<name>"`
  from Python. This works and is stable across recent macOS versions, but it
  requires Charlie to create the Shortcuts once by hand. Scope that setup step
  explicitly rather than discovering it mid-build.

Reading current Focus state is harder than setting it. Do not promise a status
readout without testing it first.

### 5.3 Image generation, and showing images in chat

**[unverified]** Charlie wants Ted to render things inline the way Claude and
ChatGPT do. Three pieces, and only one of them is hard:

1. **Generation.** Groq does not do images. Options: a hosted API (OpenAI,
   Replicate, fal.ai — all need a key and cost money), or local diffusion on the
   M5's Neural Engine via Core ML / MLX. Local fits the project's direction and
   the 48 GB machine; it is also a multi-day piece of work on its own. **Ask
   Charlie which before building either.**
2. **Hosting.** Mostly solved already. The dashboard Flask app runs on
   `127.0.0.1:5175`. Add a static route serving a `data/generated/` directory and
   every image has a URL. No new server, no cloud, no Vercel.
3. **Display.** `ui/ted_hud.html` is a real web page in pywebview. Rendering
   `<img src="http://127.0.0.1:5175/generated/...">` needs a markdown-image branch
   in the transcript renderer and nothing else.

Do 2 and 3 first — they are small, and they immediately let Ted show *any* local
image (a screenshot, a chart, a PDF page), which is useful before generation
exists at all.

### 5.4 macOS keeps asking for permissions it already has

**[inferred]** Not reproduced, needs eyes on the Mac.

Leading hypothesis: macOS grants Automation (AppleEvents) permission per
**(calling binary → target app)** pair. `hud.py:186` reads
`os.environ.get("TED_NATIVE_HOST") == "1"`, so there is a native Dock host
launching Python as a child process. **The native host and the Python
interpreter are different binaries with different code signatures**, so a grant
earned by one is not inherited by the other. Launching from Terminal vs from
`Ted.app` is a third caller.

Diagnostic sequence:

```bash
# Which grants exist, and for which client
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT service, client, auth_value FROM access WHERE service LIKE '%Apple%'"
# (read-only; may need Full Disk Access for the terminal to read TCC.db)

codesign -dv --entitlements - Ted.app 2>&1 | head -30
```

Ask Charlie to note **which app** the prompt names and **what he was doing**. The
fix differs completely depending on whether it is Calendar, Notes, Chrome, or
Accessibility — and Accessibility (used by `ui_press` / `ui_fill` /
`has_accessible_text`) is a separate permission class from Automation with its
own rules.

---

## PART 6 — Performance and hygiene (Cowork's suggestions, not Charlie's asks)

Charlie invited these. They are ranked; none are urgent.

**6.1 — `core/app.py` is 152 KB.** Was 103 KB when the decomposition was scoped
in June, 126 KB when the master handoff was written on Aug 14, **152 KB today**.
It is growing faster than it is being cleaned. `tests/test_pipeline.py` is the
safety net and it exists. The seam is still the old `_assistant_command` dispatch
chain — one domain per commit, green between each.
**Charlie has repeatedly chosen features over this refactor. That is his call to
make and it is a defensible one. Do not spend a session on it uninvited.**

**6.2 — Surface the rate-limit gauge during the wait, not after.**
`rate_limit_status()` (`core/providers.py:99`) already returns limit, remaining,
and cooldown seconds, read live from Groq's own `x-ratelimit-*` headers. That is
better data than most apps have and it is only visible in diagnostics. Put
remaining-tokens in the HUD chrome. The single most likely cause of Ted feeling
broken should not be invisible while it is happening — that is principle 2
applied to the interface instead of the logs.

**6.3 — Warm the local brain at startup, in the background.**
A one-token request to `LOCAL_TOOL_MODEL` a few seconds after launch, on a daemon
thread, makes the first fallback of the session fast instead of the slowest one.
Costs nothing but RAM on a 48 GB machine. Pairs with the keep-alive change in 1.1.

**6.4 — Dead weight, from `CLAUDE.md`'s own generated status block.**
`goals`, `habit_logs`, `routines` are empty. `patterns` has 135 rows and nothing
reads them. `TED_REFERENCE.txt` is untracked, last touched in June, and describes
fireworks store mode, Neo4j, and the Claude relay — all three deleted. It will
actively mislead the next reader, human or AI. Delete it or rewrite it.

**6.5 — Rotate the API keys.**
`config.py` is correctly gitignored (`.gitignore:9`) and has never been
committed — the repo is clean. But the same keys sit in the `Ted Keys` Google Doc
in plaintext, and this audit read them off disk. Groq, ElevenLabs, and the
Airtable tokens. Ten minutes, and it is the only item in this document with an
unbounded downside.

---

## PART 7 — Order of work

1. **Part 1** — the two provider bugs. Nothing else matters until a conversation
   holds together.
2. **Part 2** — the false-alarm log. One line, and it restores the diagnostic
   channel you need for everything below.
3. **3.3** — the deminiaturize fix. Three lines, once the two preceding questions
   are answered.
4. **3.2** — split `muted`, then the transcribe button.
5. **3.1** — Spotify buttons and playlist editing.
6. **3.4** — the YouTube title.
7. **3.5** — chat archiving.
8. **Part 4** — token reduction, guided by telemetry rather than guesses.
9. **3.6** — diagnose the humor question by pinning providers. Fifteen minutes,
   and it might be a config change rather than a code change.
10. **Part 5** — long-term, one at a time, after asking Charlie the open questions
    flagged in each.

## Before you start

```bash
cd ~/ted-ai
git status && git log --oneline -8 && git branch --show-current
python tools/ted_map.py --markdown        # ground truth, not this document
```

Work happens on `arch/single-call`. Charlie runs ChatGPT on this repo too — read
`docs/AI_WORKFLOW.md`. If `git status` shows modified files you did not write,
say so rather than editing them.

Run the suite before and after every change:

```bash
for t in tests/test_*.py; do printf "%-34s " "$t"; python "$t" | tail -1; done
# 655 checks across 21 suites as of 3ed27ee
```

## What this document does not know

Written from a Linux sandbox with no ability to run Ted, macOS, audio, Ollama, or
the real Groq API. Every fix in Part 1 is reasoning from a traceback and a
timeout constant — plausible, tested by nobody. The permission problem (5.4) has
not been reproduced at all. Where a line number is given it was read from the
tree at `3ed27ee`; where behaviour is described it was inferred.

Correct this file as you go, and say plainly which claims you confirmed and which
turned out to be wrong.
