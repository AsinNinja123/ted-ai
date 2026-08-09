# Handoff: voice barge-in stopped working

**For:** Claude Code, running locally on Charlie's Mac in `~/ted-ai`
**Written by:** Claude in Cowork, which can read/edit the repo but runs in a Linux
sandbox and therefore **cannot run Ted, use CoreAudio, or hear anything**. The
diagnosis below is from reading code, not from reproducing. You can actually run
it — please verify before and after.

---

## The symptom

Charlie used to be able to talk over Ted mid-reply and have him stop. Now Ted
talks straight through. **Typing and the mute button still interrupt him fine** —
only *voice* barge-in is dead.

That asymmetry is the biggest clue and any fix should preserve it as a test:
typing sets `api.interrupt_speech`, which is checked through the `should_stop`
callback — a completely separate path from voice detection. So the playback-stop
machinery works. What's broken is *detecting the voice*.

---

## How barge-in is supposed to work

- `core/audio.py:163 _ingest_frame()` runs on every 20 ms mic frame.
- Line 171: `if self._playing and (self.mode == "aec" or self._fallback_bargein):`
- Inside, if `rms > self._threshold * BARGE_MARGIN` for `BARGE_FRAMES`
  consecutive frames, it sets `self.barge_in = True`.
- `core/audio.py:371 play_samples()` polls `self.barge_in` each 1024-sample
  chunk (line 390), breaks, and calls `stop_playback()`.
- `core/voice.py:459` reports `barged_by_voice` up to `core/app.py:412`, which
  returns it to the loop at `app.py:2134/2162` so the next capture is `prearmed`.

Constants: `BARGE_MARGIN = 3.0`, `BARGE_FRAMES = 6` (~120 ms) — `audio.py:52-53`.

---

## Root cause 1 — Ted is deaf at every sentence boundary

`speak_streaming` (`core/voice.py:390`) speaks a reply **one sentence at a
time**. Its inner `say()` (line 411) calls `engine.play_samples(...)` once per
sentence (line 421).

`play_samples` opens with (`audio.py:386-387`):

```python
self._set_playing(True)
self.reset_barge_in()
```

and closes in its `finally` with (`audio.py:408`):

```python
self._set_playing(False)
```

So per sentence:

1. `_playing` goes False the moment a sentence's audio ends.
2. `synth()` for the next sentence runs (Kokoro TTS — real, non-trivial time).
   Throughout that gap `_ingest_frame`'s barge check at line 171 is skipped
   entirely, because it's gated on `self._playing`. **Anything Charlie says in
   that window is not examined at all.**
3. The next sentence calls `reset_barge_in()` (`audio.py:481`), zeroing both
   `barge_in` and `_barge_run`.

Two consequences:

- There is a dead window at every sentence boundary.
- The 6-consecutive-frame run must complete **inside a single sentence's
  playback**, because the counter is reset at each boundary. `_set_playing(True)`
  also zeroes `_barge_run` independently (`audio.py:441-442`).

A sentence boundary is exactly where Ted pauses, which is exactly where a human
naturally interrupts. So the most natural barge-in is the one most likely to be
thrown away.

## Root cause 2 (suspected) — the threshold drifted up

`audio.py:300`:

```python
self._threshold = max(0.006, min(0.025, float(np.mean(levels)) * 2.5))
```

Barge-in needs `rms > threshold * 3.0`, so the bar to interrupt ranges from
**0.018** (quiet room, threshold at the floor) to **0.075** (noisy room,
threshold pinned at the cap).

For scale: `MIN_CAPTURE_RMS = 0.011` in `core/voice.py` is the "too quiet to be
speech" cutoff, and normal speech RMS is roughly 0.03–0.10. **0.018 is easy to
clear; 0.075 needs a shout.**

Older logs from a session when barge-in worked showed calibration landing at
0.006 — the floor. Charlie may still be on a 9-channel virtual audio input
rather than the built-in mic, which carries more ambient noise and would push
calibration toward the cap. This fails silently: everything else keeps working.

**First thing to check.** Start Ted and read the startup line printed at
`core/voice.py:179`:

```
Mic calibrated (silence threshold ≈ 0.0XXX)
```

- **≈ 0.006** → threshold is fine; root cause 1 alone.
- **≈ 0.025** → both problems. Also check macOS Sound → Input; if it isn't
  "MacBook Pro Microphone", switch it and re-measure before changing code.

---

## Suggested fix

Roughly 20 lines across `core/audio.py` and `core/voice.py`. Confirm the
diagnosis by observation first — don't take the above on faith.

1. **Keep detection alive for the whole reply, not per sentence.** Add an
   explicit "Ted is delivering a reply" flag (e.g. `_in_reply`) set once when
   `speak_streaming` starts and cleared when it returns. Gate the barge check at
   `audio.py:171` on `self._playing or self._in_reply` so the inter-sentence gap
   is still monitored. Watch for self-interruption in fallback (non-AEC) mode —
   Ted's own voice leaks into the mic there, which is what
   `_fallback_bargein` exists to guard.

2. **Reset barge state once per reply, not once per sentence.** Move
   `reset_barge_in()` out of `play_samples` (`audio.py:387`) up to the start of
   `speak_streaming`, and stop `_set_playing(True)` from clearing `_barge_run`
   (`audio.py:441-442`). Keep a reset somewhere for the `speak()` one-shot path
   (`voice.py:379`) so it isn't left armed.

3. **Give barge-in its own threshold** instead of inheriting the capped ambient
   calibration. A dedicated `BARGE_THRESHOLD_MAX` (~0.030 absolute) applied as
   `min(self._threshold * BARGE_MARGIN, BARGE_THRESHOLD_MAX)` keeps a noisy
   calibration from making interruption impossible. Consider `BARGE_FRAMES = 4`
   (~80 ms) if 6 proves too strict once the boundary gap is closed — but change
   one thing at a time.

4. **Make it observable.** Print barge RMS vs. the effective threshold while Ted
   is speaking (behind a `DEBUG_BARGE` flag). This class of bug went unnoticed
   because nothing ever reported it. Same reasoning as the memory bug below.

**Manual test, since none of this is unit-testable:**

- Start Ted, ask something with a long multi-sentence answer.
- Talk over him **mid-sentence** → should cut off.
- Talk over him **right at a sentence pause** → this is the case that's broken now.
- Confirm typing still interrupts (regression check on the `should_stop` path).
- On speakers, confirm Ted does **not** interrupt himself. If he does, the
  `_in_reply` widening is too aggressive in fallback mode — check which mode is
  active from the startup banner (`🎧 native engine` vs `🔉 sounddevice fallback`).
  The AEC binary at `native/ted_audio` is present and executable, so it should
  be `aec`.

---

## Repo state you're inheriting

Uncommitted, nothing pushed. `git status` before you start.

**Modified earlier today (memory + transcription fixes, all tested):**

- `core/memory.py` — `save_fact` now normalizes relationships, supersedes
  single-valued facts, and collapses near-duplicates; added `forget_fact`,
  `list_facts`, and a cap on facts injected into the prompt.
- `core/llm.py` — `extract_and_save_facts` was silently failing for five weeks
  (asked an 8B model for JSON, got prose, `json.loads` threw, exception died in
  a `print`). Now uses Groq JSON mode plus a salvage parser; real failures go to
  `ted_errors.log`.
- `core/app.py` — the `remember` command only matched "remember **this**" and
  only wrote to ChromaDB, never the facts table. Now handles more phrasings and
  both word orders, and routes personal statements to facts. Added "what do you
  know about me" / "forget everything about me".
- `core/voice.py` — added `_is_junk_fragment` and wired it into `capture()` as
  gate 3.5. Short junk transcriptions ("Tep.", "Start.") were clearing every
  existing filter and being executed as commands.
- `tests/test_capture_gates.py` (new), `tests/test_memory.py` (extended).

**`core/intents.py` was already modified before any of that** — Charlie's own
uncommitted work. Left untouched. Don't assume it's mine.

**Test suites** (all passing, run with the venv python):

```bash
cd ~/ted-ai && source venv/bin/activate
python tests/test_memory.py         # 22 passed
python tests/test_intents.py        # 63 passed
python tests/test_capture_gates.py  # 32 passed
```

**Run Ted:**

```bash
cd ~/ted-ai && source venv/bin/activate && python hud.py
```

---

## Also open, lower priority

- **Session summaries never get written.** `data/memory.db` has 0 rows in
  `session_summaries`. Both write paths require either 30 minutes idle with the
  process alive (`app.py:2111 session_summary_watch`) or a clean shutdown
  through pywebview's closing hook (`hud.py:52`). Closing the window normally or
  force-quitting fires neither, so Ted starts every session with no memory of
  the last one. Worth confirming whether that hook runs at all before rewriting.
- **Dead `goals` table** in `memory.db`, left over from the fireworks-store
  features removed in `3dd744c`. Harmless, but it's cruft.
- **`config.py` holds live API keys in plaintext** (Groq, ElevenLabs, Anthropic,
  Spotify secret, and a Neo4j password for a backend that no longer exists).
  It's gitignored, so this is not urgent — but Charlie has declined to move
  these to a `.env` twice, so don't re-litigate it unless he brings it up.
