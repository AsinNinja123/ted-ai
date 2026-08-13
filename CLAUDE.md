# Project Memory

## Person

| Who | What to remember |
|---|---|
| **Charlie** | Charlie Rowenhorst; CS sophomore at Northwest Christian College (NWC), Iowa. Building Ted as his own daily-driver AI and portfolio project. |

## Ted

| Topic | Current understanding |
|---|---|
| Identity | Charlie's personalized, chat-first AI chatbot that can also act on his Mac and accounts; voice is secondary. |
| Main goal | Eventually replace Charlie's everyday use of Claude/ChatGPT, on Mac and phone, with persistent personal memory and tools. |
| Current status | As of Aug 12, 2026, the chat-first pivot is active but uncommitted. Treat the working tree as valuable user work. |
| Stack | Python/macOS; pywebview HUD; Groq-hosted inference; SQLite memory; ChromaDB knowledge base; Flask dashboard; optional local Kokoro TTS and cloud Whisper STT. |
| Warning | Re-check code and runtime state before planning: `git status`, recent `git log`, and SQLite tables. Plans rot faster than code. |

## Preferences and context

- Charlie values an assistant that genuinely knows him, is proactive, and reports action ground truth honestly.
- Voice should not be assumed to be the primary interface; Charlie is back at college and usually needs quiet chat.
- Ted and Charlie's separate AI automation agency are different projects. Do not conflate their stacks.

## Working alongside another AI

Charlie runs Claude and ChatGPT on this repo, sometimes the same day. They cannot
see each other, and git has no locking — the second write to a file wins silently.

**Read `docs/AI_WORKFLOW.md` before editing anything.** The short version:

1. `git log --oneline -10` and `git status` first. The commit history is the
   handoff log; there is no separate worklog to read.
2. If `git status` shows modified files you did not write, someone is mid-task.
   Do not edit them — say so.
3. Never revert a change you did not make. Uncommitted work cannot be recovered.
4. State plainly when a claim is unverified. Cowork cannot run Ted, macOS, audio,
   or the real Groq API.

→ Deep memory: `memory/people/`, `memory/projects/`, `memory/context/`
→ Source handoff: `docs/TED_MASTER_HANDOFF.md`

