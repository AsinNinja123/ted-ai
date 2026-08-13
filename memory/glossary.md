# Glossary

## Project terms

| Term | Meaning |
|---|---|
| **Ted** | Charlie's personal AI chatbot/assistant project. |
| **chat-first pivot** | The Aug 2026 shift from always-listening voice assistant to quiet desktop chat with optional voice. |
| **HUD** | Ted's pywebview desktop interface, currently centered on `ui/ted_hud.html`. |
| **decision ladder** | The ordered routing gates in `TedApi._respond()`; the first gate that claims a message wins. |
| **truth/ground-truth rule** | Tool results are reported as returned; Ted cannot turn an action failure into a success claim. |
| **agency** | Charlie's separate AI automation work, not Ted; do not merge their architecture or stack. |

## Important corrections

- Ted's current stack is not “n8n + Airtable + Claude API + Twilio + Vercel”; that describes the separate automation agency.
- The current source of truth is the code/runtime state, especially because project plans have gone stale quickly.
