# Verify: session memories + Ted.app

**Written by:** Claude in Cowork, which can read and edit this repo but runs in a
Linux sandbox — so it **cannot run Ted, use CoreAudio, call Groq, or build a `.app`.**
Everything below was written by reading and unit-testing; none of it has been
observed working on the Mac. That's what this checklist is for.

## What was already verified from the sandbox

- `py_compile` clean on `hud.py`, `core/app.py`, `core/llm.py`, `core/memory.py`
- `bash -n` clean on `tools/make_app.sh`
- `tests/test_memory.py` — **37 passed** (12 new, covering upsert/search/migration)
- `tests/test_session_memory.py` — **28 passed** (new file, the "worth remembering" filter)
- `tests/test_intents.py` 63 · `test_capture_gates.py` 32 · `test_barge.py` 19 — all pass
- The icon generator produces a valid 1024×1024 PNG

`tests/test_pipeline.py` needs the real `groq` package, which isn't in the sandbox.
With a stubbed client it reaches exactly the same point as it does on the unmodified
`HEAD` (checked against `git archive HEAD`), so there's no regression — but **run it
for real in the venv**, it's the characterization suite for the migration:

```bash
cd ~/ted-ai && source venv/bin/activate
for t in test_memory test_session_memory test_intents test_capture_gates test_barge test_pipeline; do
    echo "— $t"; python tests/$t.py | tail -1
done
```

---

## 1. Session memories

The bug: `session_summaries` had 0 rows since the table was created. The only write
paths were a 30-minute idle timer and a signal handler, and **closing the window
fires neither** — pywebview just returns from `start()` and the process exits
normally. Now `_teardown()` runs from the pywebview closing event, `atexit`, and both
signals, guarded so it only happens once.

### 1a. A dull session should be remembered as *nothing*

- [ ] Launch Ted. Say only routine things: "what time is it", "set a timer for one
      minute", "play something", "pause", "thanks".
- [ ] Close the window.
- [ ] Console/log should show: `[memory] shutdown: nothing worth remembering this session`
- [ ] Confirm nothing was stored:
      ```bash
      sqlite3 -box ~/ted-ai/data/memory.db "SELECT id, created, text FROM session_summaries"
      ```
      **Expected: still empty.** This is the feature working, not failing.

### 1b. A real conversation should be remembered

- [ ] Launch Ted. Have an actual back-and-forth — three or more real turns about
      something specific (a project, a decision, something going on).
- [ ] Close the window with the red button (not Ctrl-C — that path already worked).
- [ ] Console/log shows `[memory] shutdown: remembered — …`
- [ ] ```bash
      sqlite3 -box ~/ted-ai/data/memory.db "SELECT id, exchanges, topics, text FROM session_summaries"
      ```
      **Expected: exactly one row.** Read the text — is it specific enough that you'd
      recognise the conversation from it a week later? If it's vague ("discussed
      various topics"), the prompt in `core/llm.py` `_MEMORY_SYSTEM` needs tightening.

### 1c. One session must not leave several rows

The periodic flush (every 12 exchanges) and the shutdown write upsert the same row id.

- [ ] Have a long conversation — 15+ exchanges — then close.
- [ ] Row count should still be **1** for that session, with `exchanges` reflecting
      the full length. Several near-duplicate rows means the upsert isn't matching.

### 1d. Ctrl-C and force-quit

- [ ] Ctrl-C in the terminal → memory written, no traceback.
- [ ] `kill <pid>` → memory written.
- [ ] Force-quit (`kill -9`) → **memory is lost for turns since the last flush.**
      Expected and accepted; that's what the 12-exchange flush limits.

### 1e. The callback — the actual point of all this

- [ ] With at least one memory stored, wait 4+ hours (or fake it:
      `sqlite3 ~/ted-ai/data/memory.db "UPDATE session_summaries SET created = datetime('now','-6 hours')"`).
- [ ] Relaunch. Ted should open by referring to that conversation with a human date
      ("Yesterday — you were …").
- [ ] **The more important test:** mid-conversation, mention the topic sideways and
      see whether he connects it himself, without being asked. That's the injected
      memory working, not the greeting.
- [ ] Ask "what were we talking about last time?" — he should answer from memory.

### 1f. Things that could go wrong

- **He recites memories unprompted or lists them.** The prompt says at most one,
  only when it fits. If he over-does it, tighten the wording in `core/llm.py` where
  `past_sessions` is added to `context_parts`.
- **Everything gets remembered.** Raise `MIN_MEMORY_SUBSTANTIVE_WORDS` (currently 15)
  or add openers to `_ROUTINE_OPENERS` in `core/llm.py`.
- **Nothing ever gets remembered.** Check the log for
  `[memory] session summary returned non-JSON` — that means `FAST_MODEL`
  (`openai/gpt-oss-120b`) is ignoring JSON mode, same failure shape as the fact
  extractor bug. The salvage parser should catch it; if it doesn't, log the raw reply.
- **Startup got slower.** `format_memories_for_prompt()` runs in the existing parallel
  context-load block with a 4 s join, so it shouldn't — but it is one more SQLite read
  per reply.

---

## 2. Ted.app

```bash
cd ~/ted-ai && bash tools/make_app.sh
```

- [ ] Script completes and prints `Built /Users/charlierowenhorst/ted-ai/Ted.app`
- [ ] The icon in Finder is the green orb, not a generic blank page.
      (If Finder caches the old one: `touch ~/ted-ai/Ted.app`, or log out and back in.)
- [ ] Double-click → the Ted window opens and he greets you.
- [ ] **macOS asks for Microphone permission on first launch — allow it.**
      This is the piece most likely to misbehave: the mic is opened by the venv
      python and the native `ted_audio` binary, not by the bundle itself, so the
      permission may be attributed to the wrong process. If Ted launches but hears
      nothing, check **System Settings → Privacy & Security → Microphone** and make
      sure **Ted** is listed and enabled. If it isn't there at all, that's the known
      TCC-attribution problem — say so and it can be fixed by having the launcher
      re-exec through the bundle.
- [ ] Ask him to add a calendar event → macOS should prompt for Automation access once.
- [ ] Double-click again while running → notification "Ted is already running",
      no second instance.
- [ ] Spotlight "Ted" finds it.
- [ ] Drag to Dock, launch from there.
- [ ] Break it on purpose: `mv ~/ted-ai/config.py /tmp/` then launch → you should get
      a red alert dialog naming the missing config, not a silent failure. Move it back.
- [ ] `cat ~/ted-ai/data/ted_launch.log` shows the launch banner and Ted's output.

### If you want it in /Applications

```bash
cp -R ~/ted-ai/Ted.app /Applications/
```
The launcher hardcodes `$HOME/ted-ai`, so it works from anywhere. But it's a copy —
re-run `make_app.sh` and you'll need to re-copy.

---

## 3. Then commit

Nothing here is committed yet. Once 1b, 1c and 2 pass:

```bash
cd ~/ted-ai
git add -A
git commit -m "Session memories: reliable write paths, selective recall, Ted.app launcher"
```

If something's wrong and you want out: `git checkout -- .` restores everything, since
the last commit (`9fa57bc`) is clean.
