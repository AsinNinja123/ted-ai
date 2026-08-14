# Daemon handoff — calendar alerts that survive the window closing

**Written:** August 14, 2026, in Cowork (Linux sandbox)
**Verified:** logic only. **Nothing in this document has been run on macOS.**

Following the `BARGE_IN_HANDOFF.md` pattern: diagnose and build in one place,
verify on the Mac in the other, and say plainly which claims are which.

---

## The problem

`core/proactive.py` worked, but `ProactiveScheduler` was started as a thread
from `TedApi.start()`. It died with the HUD window. Proactive alerts were only
available while Ted was already on screen — which is exactly when you do not
need to be told about your next class.

## What was built

| File | What it is |
|---|---|
| `ted_daemon.py` | Standalone calendar watch. Polls `Calendar.app` every 60 s, posts a macOS notification for events ≤16 min out. |
| `tools/com.charlie.ted-daemon.plist` | launchd **user agent** template. `__PLACEHOLDER_HOME__` is rewritten at install. |
| `tools/install_daemon.sh` | Install / reinstall / `--uninstall`. |
| `core/proactive.py` | Gained `daemon_alive()`; `_check_calendar` now no-ops while the daemon holds the watch. |
| `tests/test_daemon.py` | 15 checks. AppleScript is stubbed, so this proves the logic and says nothing about macOS permissions. |

## Scope — what it deliberately does NOT do

**It does not fire user-defined triggers.** Those carry action text like "give
me the rundown" that only means something when `TedApi` is loaded and can
speak. Executing them headlessly would either post useless notifications or
create a second, divergent command path — the exact bug class the single-call
rewrite exists to remove. Triggers stay with the in-process scheduler.

**It notifies, it does not speak.** Ted is used at college now. A laptop
announcing your schedule out loud is the failure mode, not the feature.

## How the two watchers stay out of each other's way

The daemon writes `data/daemon_heartbeat` every poll. `daemon_alive()` returns
true if that file is under 195 seconds old (three missed polls). While it is
true, the HUD's `_check_calendar` returns immediately, so an event never fires
twice. Every failure mode — no file, unparseable file, stale file — resolves
toward the HUD keeping the watch, so alerts degrade to the old behavior rather
than stopping.

De-dupe state lives in `data/daemon_alerted.json`, not memory, because launchd
restarts this process on crash, logout, and reboot. An in-memory set would
re-announce every upcoming event on each restart.

---

## Verification checklist — run these on the Mac

Nothing below has been done. Work top to bottom; stop at the first failure.

**1. It runs at all.**

```bash
cd ~/ted-ai && venv/bin/python ted_daemon.py --once
```

Expect a line per upcoming event, or `0 event(s) in the next 16 min`. This is
also where a missing-permission failure will first show as an `osascript`
error.

**2. It can actually read Calendar.**

Put a test event on your calendar ~10 minutes out, then rerun `--once`. It
should find it and post a notification. **This is the step most likely to
fail** — macOS gates AppleEvents per calling binary, and a launchd-spawned
python is a different caller from your terminal. Expect a permission prompt;
if none appears and it fails silently, grant it in **System Settings → Privacy
& Security → Automation** (and check **Notifications** for the same).

**3. It survives the window closing — the whole point.**

```bash
bash tools/install_daemon.sh
launchctl print gui/$(id -u)/com.charlie.ted-daemon | head -20
tail -f ~/ted-ai/data/ted_daemon.log
```

Quit Ted entirely. Set an event ~5 minutes out. The notification should arrive
with no HUD open. If that works, the item that has blocked every proactive
feature since June is closed.

**4. It does not double-fire.**

With the daemon installed, open the HUD and watch for an event. You should get
exactly one notification and **no** spoken alert. The log line to look for on
the HUD side is the absence of a calendar alert, not a message.

**5. It comes back.**

`kill` the daemon process. launchd should restart it within ~60 s
(`ThrottleInterval`). Confirm in the log.

**6. Regression.**

```bash
cd ~/ted-ai && for t in tests/test_*.py; do printf "%-34s " "$t"; venv/bin/python "$t" | tail -1; done
```

353 checks across 11 suites were green in the Linux sandbox with `groq` stubbed
and `osascript` mocked. On the Mac they should be green for real.

---

## Known limits, stated rather than hidden

- **A user agent runs only while you are logged in.** Log out and alerts stop.
  A true system daemon would survive that but could not read your calendar,
  which is the wrong trade.
- **A sleeping laptop does not poll.** An event that starts while the lid is
  shut alerts late, when the machine wakes. Not fixable by polling; it needs a
  scheduled wake or a push channel.
- **`core/proactive.py` still truncates minutes where the daemon rounds.** The
  daemon says "in 12 minutes" for an event 11.9 minutes out; the HUD path says
  11. They never both run, so it is cosmetic — but it is two places computing
  one number differently, and it should collapse when the HUD path retires.
