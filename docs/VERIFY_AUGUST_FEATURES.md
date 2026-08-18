# Verify: the August 16 feature batch

**Written by:** Claude Code, running on `charlies-macbook-pro-local` — so unlike the
Cowork handoffs, most of this **was** executed against the real Mac, the real Groq
API, and the real Ollama models. Each item below says which.

Eight commits, `4bf2566` through `52d1693`. Suite: **1163 checks across 33 files**,
all passing (`866` before this batch).

> **Aug 18, 2026.** Three features described below were removed at Charlie's
> request: the pet / Ted Bear, the news watcher, and voice ID (`core/speaker.py`).
> Their rows are struck from the tables and their steps deleted, so this file
> matches the code again. The suite is now **32 files**.

```bash
cd ~/ted-ai
for t in tests/test_*.py; do printf '%-34s ' "$t"; venv/bin/python "$t" | tail -1; done
```

---

## Already verified here, on this Mac

| What | How |
|---|---|
| Browser video detection | A real YouTube video playing in Brave was detected by pid, named from its tab; Chrome (open, silent) correctly reported nothing |
| Contacts resolution | `_resolve_known_app("contacts")` → `contacts`; audited against `/Applications` and `/System/Applications` |
| Panel tabs | Headless render; widths measured mid-transition (side 250→0, apps 0→320) |
| Brain router | 0.10–0.13 s per tiebreak on `llama3.2:3b` after warmup; discrimination probed on 8 inputs |
| Memory speed | Extraction measured at 6.84 s on the local brain — that is the lag that moved off the critical path |
| Memory quality | Real extraction produced `calc 2 exam on Thursday August 20, 2026` at importance 3 |
| Importance ordering | Verified in an isolated DB; the real DB migrated cleanly, all 58 facts defaulting to 2 |
| Attachments | A PNG sent to Groq came back described, so the vision path genuinely works |
| Chat rendering | `mdlite` executed in Node over 8 cases; links inert, digits intact, broken images collapse |
| Codebase reader | Every containment refusal exercised; a confirmed write created, rewrote, kept a backup, and cleaned up |
| Read-only Messages access | `chat.db` mtime unchanged; every connection uses `mode=ro` |

---

## What you need to do

### 1. The bouncer needs Full Disk Access — it does not have it

This is the only feature that is **inert until you act**. Verified: `chat.db` exists
(25 MB) and the read is refused.

1. **System Settings → Privacy & Security → Full Disk Access**
2. Add whichever binary launches Ted — `Ted.app` if you use the Dock icon, or
   **Terminal** if you run `python hud.py` from a shell. If you use both, add both.
3. Restart Ted.
4. Ask Ted *"who are you watching for"* — it should stop saying it cannot read your
   messages.
5. `tell me when Gavin texts me`, then have someone text you.

The bouncer also ships **off**, deliberately. Adding the first name turns it on.

**Unverified until you do this:** the chat.db query itself, `attributedBody`
decoding against real messages, and contact-name reverse lookup. The decoder is
unit-tested against synthesised blobs of both length encodings, but no real Apple
blob has passed through it. If texts announce as *"Someone sent you a text"* with no
name, the Contacts AppleScript is the thing to look at; if the preview is blank but
the announcement works, it is the decoder.

### 2. Run the app once and watch the log

```bash
cd ~/ted-ai && venv/bin/python hud.py
```

Look for `[router] local — …` / `[router] cloud — …` on the first few turns. The router prints its verdict and reason every turn.

### 3. Things worth trying, in rough order of how likely they are to be wrong

- *"what video is playing"* while something plays in Brave
- Drag a PDF onto the window, then ask about it
- Paste a screenshot (Cmd+Shift+4, then Cmd+V in the chat box) and ask what it shows
- *"show me a red panda"* — pictures should land in the chat
- *"how are you built"* / *"what's in core/routing.py"*
- *"change your own code"* — it must ask before writing, and name the file and size

---

## Known limits, stated rather than discovered later

- **DuckDuckGo image quality varies.** No API key, no key to lose, and usually
  relevant — but "eiffel tower" returned Paris catacombs once during testing. If it
  becomes annoying the fix is a keyed image API, not a prompt tweak.
- **The router biases toward the cloud.** A wrong LOCAL costs answer quality; a wrong
  CLOUD costs tokens that refill every minute. If you want more aggressive saving,
  the thresholds are in `classify_brain` in `core/routing.py`.
- **`code_write` takes whole files, not patches.** Fine for small modules, wasteful
  for `core/app.py` at 173 KB. Ted can read a range but must rewrite the whole file,
  so treat self-editing as suitable for small files only.
- **`core/memory.py` now honours `TED_DB`.** It was the only store that did not,
  which meant a harness pointed at a scratch file redirected everything except the
  facts table. Found the hard way — three invented facts were written into the real
  database during testing and deleted again; the count is back to 58, and there is a
  backup at `scratchpad/memory.db.backup` from that session if you ever want it.

---

## New files

| File | What it owns |
|---|---|
| `core/attachments.py` | Resolving a dropped/picked/pasted file into what the model receives |
| `core/codebase.py` | Ted reading his own source; all the containment rules |
| `core/messages.py` | Reading `chat.db`, decoding bodies, contact lookup |
| `core/bouncer.py` | Who gets announced, who is ignored |

New tests: `test_attachments`, `test_codebase`, `test_bouncer`, `test_rich_chat`.
