# Ted

**Also called:** the Ted project
**Status:** Active; chat-first pivot in progress as of Aug 12, 2026
**Owner:** Charlie Rowenhorst

## What it is

Ted is a from-scratch personal AI chatbot/assistant for Charlie's Mac. It has its own desktop UI, routing, tool layer, and persistent editable memory rather than being a wrapper around ChatGPT or Claude. It can chat, remember Charlie across sessions, and take actions involving the Mac and connected accounts.

## Current direction

The target is “personalized chatbot that also acts.” The former voice-first, always-listening Jarvis framing is legacy. Ted now boots muted, centers the chat transcript and saved sessions, and uses voice as an opt-in secondary mode.

## Architecture snapshot

- Entry point: `hud.py` or `Ted.app`; primary HUD: `ui/ted_hud.html`.
- Python/macOS application with Groq-hosted models for replies/tool calling, live web answers, fact extraction, summaries, and vision.
- SQLite at `data/memory.db` for facts, exchanges, chat turns, session summaries, habits, patterns, goals, and audit history.
- ChromaDB/fastembed knowledge base with document intake from `inbox/`.
- Roughly 30 tools spanning apps, browser, Spotify, calendar, notes, email, reminders, timers, clipboard, computer control, screenshots, habits, and weather.
- Flask memory/chat dashboard on `127.0.0.1:5175`; remote endpoint for iOS Shortcuts on `:5150`.
- Optional voice: Groq Whisper STT, local Kokoro TTS, and native Swift audio; current barge-in path has no real AEC.

## Important invariants

- Action tools must report ground truth verbatim; Ted must not claim success when an action failed.
- Facts supersede contradictory single-valued facts and should not accumulate duplicates.
- Most routine sessions should produce no long-term summary; that is intentional selectivity.
- Before planning, inspect `git status`, `git log --oneline -10`, and `sqlite3 data/memory.db '.tables'`.

## Current work and risks

The chat-first conversion, memory dashboard, decision-flow work, and related HUD/LLM changes were uncommitted in the Aug 12 handoff. Do not discard them with stash/checkout/reset. The largest architectural liabilities identified are regex-heavy command routing and two separate LLM calls per turn; these are future rebuild concerns, not assumptions that they are already fixed.

## Source

`docs/TED_MASTER_HANDOFF.md`, written Aug 12, 2026. Confidence labels in that document distinguish code-verified facts from stated, documented, or unverified plans.
