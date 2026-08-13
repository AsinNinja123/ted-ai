# Working on Ted with more than one AI

Charlie runs Claude and ChatGPT on this repo, sometimes the same day. They cannot
see each other. Git has no locking — if both edit a file, the second write wins
silently and the first one's work is gone with no error and no record.

That has already happened once: the muted guards in `core/voice.py` were deleted
and only caught because `git status` happened to show the file.

Three rules. They are short because a long protocol does not get followed.

---

## 1. Read before you write

First thing, every session, before touching anything:

```bash
cd ~/ted-ai
git log --oneline -10        # what happened since last time, and why
git status                   # is someone mid-edit right now?
```

`git log` IS the handoff log. Commit messages on this project explain the
reasoning, not just the change — that is deliberate, and it is why there is no
separate worklog file to keep in sync. A parallel log rots; a commit message is
welded to the code it describes.

If `git status` shows modified files you did not write, **someone is mid-task.**
Do not edit those files. Say so and ask.

## 2. Commit before handing off

Before Charlie switches to the other assistant, the work in progress gets
committed. Not pushed necessarily — committed. An uncommitted working tree is
the only state where work can vanish without trace.

This is the whole rule. Most collisions die here.

## 3. Never revert what you did not write

If a change looks wrong and you did not make it, **say so — do not fix it.** It
is probably the other assistant's, and it probably had a reason. `git log` and
`git diff` will tell you what and why.

Reverting someone else's uncommitted work destroys it permanently. Git cannot
recover a change that was never committed.

---

## Who does what

Split by what each one can physically reach, not by preference.

| | Claude (Cowork) | Claude Code / ChatGPT on the Mac |
|---|---|---|
| Where it runs | Linux VM, `~/ted-ai` mounted | Natively on macOS |
| Can run Ted | **No** | Yes |
| Mic, audio, CoreAudio, Swift | **No** | Yes |
| AppleScript, screencapture, macOS permissions | **No** | Yes |
| Real Groq API calls | **No** | Yes |
| Reads/edits the repo | Yes | Yes |
| Runs pure-Python tests | Yes (with a stubbed `groq`) | Yes, for real |

**On the Mac owns:** audio and barge-in, the Swift engine, macOS permissions,
checking which Groq models are still alive, and running any verification
checklist. Anything whose answer is "run it and see."

**Cowork owns:** architecture, the reasoning path in `llm.py` and `app.py`,
tests, refactors, and research.

Neither owns "reviewing the other's diff" — both should, and both have caught
real bugs in the other's work doing it.

## One extra hazard for the Cowork side

Its view of the repo is a network mount and it can go stale. Writes have silently
failed mid-session, and `git checkout` has silently not taken. After any edit,
re-read the file or `grep` for the change to confirm it landed. Do not assume a
write succeeded because the tool returned cleanly.

It also has **no internet** in that shell, so it cannot `git push`, `pip install`,
or call any API. Pushing is always Charlie or the Mac-side assistant.

---

## When something is unverified, say so

Cowork cannot run Ted. Any claim from that side about runtime behavior, audio,
latency, or a live API is **unverified** and must be labelled that way. The
project has been burned by this before; `docs/BARGE_IN_HANDOFF.md` is the pattern
that worked — diagnose in one tool, hand off to the other, and state plainly
which claims were never observed running.
