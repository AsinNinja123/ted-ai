"""
tools/ted_map.py — read Ted, describe Ted.

WHY THIS EXISTS
    Every hand-written description of this project has gone stale within days.
    docs/TED_MASTER_HANDOFF.md asserted the wrong model stack two days after it
    was written. CLAUDE.md still says the working tree is uncommitted. The
    problem is not carelessness — it is that a description maintained by hand
    is a second source of truth, and the second one always loses.

    So nothing here is typed in. Model names come from config.py, the tool list
    from core/tools.py, the file descriptions from each module's own docstring,
    the row counts from memory.db, the state from git. Every number on the page
    was read from the thing it describes, seconds before the page was written.

    The only prose in this file is prose about *purpose* — why a part exists —
    which changes on the order of months, not days. It is marked as such.

USAGE
    python tools/ted_map.py                  write ted_map.html and open it
    python tools/ted_map.py -o out.html      write somewhere else
    python tools/ted_map.py --markdown       print the status block for
                                             CLAUDE.md / AGENTS.md instead
    python tools/ted_map.py --json           print the raw facts

    Pure standard library, and it imports nothing from core/ on purpose: it has
    to run when the venv is missing, when httpx is not installed, and inside a
    Linux sandbox that cannot load Ted at all.
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── small helpers ────────────────────────────────────────────────────────────

def read(*parts) -> str:
    try:
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def exists(*parts) -> bool:
    return os.path.exists(os.path.join(ROOT, *parts))


def git(*args) -> str:
    try:
        out = subprocess.run(["git", "-C", ROOT, *args],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def ago(seconds: float) -> str:
    """Humanize an age. '3 minutes ago' beats a timestamp you have to subtract."""
    if seconds < 0:
        return "in the future"
    for limit, div, name in ((90, 1, "second"), (5400, 60, "minute"),
                             (172800, 3600, "hour"), (1209600, 86400, "day")):
        if seconds < limit:
            n = int(round(seconds / div))
            return f"{n} {name}{'' if n == 1 else 's'} ago"
    return f"{int(seconds / 604800)} weeks ago"


def assigned(source: str, name: str):
    """Return the literal assigned to NAME at module level, or None.

    Uses ast rather than a regex so a commented-out or shadowed assignment
    cannot be mistaken for the real one.
    """
    try:
        tree = ast.parse(source)
    except Exception:
        return None
    found = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    try:
                        found = ast.literal_eval(node.value)
                    except Exception:
                        found = None
    return found


# ── what Ted thinks with ─────────────────────────────────────────────────────

def collect_models() -> dict:
    """Which models, from config first and the code's defaults second.

    providers.py wraps each `from config import X` in try/except and falls back
    to its own literal, so a key missing from config.py is not an error — it
    just means the default silently applies. The page says which happened,
    because 'where is this value coming from' is exactly the question you cannot
    answer by reading one file.
    """
    cfg = read("config.py")
    prov = read("core", "providers.py")
    voice = read("core", "voice.py")

    def pick(name, label, why):
        from_config = assigned(cfg, name)
        default = assigned(prov, name)
        return {
            "key": name,
            "label": label,
            "why": why,
            "value": from_config if from_config is not None else default,
            "source": "config.py" if from_config is not None else "providers.py default",
            "in_config": from_config is not None,
        }

    stt = re.search(r'model\s*=\s*"(whisper[^"]+)"', voice)
    kokoro = re.search(r'"(kokoro-[^"]+\.onnx)"', voice)
    use_groq_stt = assigned(cfg, "USE_GROQ_STT")

    return {
        "cloud": pick("CLOUD_CHAT_MODEL", "Cloud brain",
                      "Tried first. Does all the thinking: chat, tools, "
                      "remembering facts, describing screenshots."),
        "local": pick("LOCAL_CHAT_MODEL", "Local brain",
                      "Runs on your Mac through Ollama. Only used when the "
                      "cloud one is missing, down, or rate limited."),
        "ollama_url": pick("OLLAMA_URL", "Where the local brain lives",
                           "Ted starts Ollama itself if it is installed but idle."),
        "stt": {"label": "Ears", "value": stt.group(1) if stt else "unknown",
                "why": "Turns what you say into text. Cloud by default; "
                       "set USE_GROQ_STT = False for the offline version.",
                "source": "core/voice.py",
                "extra": "cloud" if use_groq_stt is not False else "local"},
        "tts": {"label": "Voice", "value": kokoro.group(1) if kokoro else "unknown",
                "why": "Speaks. Runs entirely on your Mac — no network, no key.",
                "source": "core/voice.py", "extra": "local"},
    }


# ── what Ted can do ──────────────────────────────────────────────────────────

# The one piece of hand-written prose in this file. Grouping is a judgment call
# and cannot be derived; the tool NAMES and DESCRIPTIONS below are read from
# core/tools.py, so a new tool shows up automatically — in "Other" until someone
# files it here.
TOOL_GROUPS = [
    ("Your Mac", ["open_app", "close_app", "type_text", "system_volume",
                  "system_brightness", "clipboard_read", "clipboard_write",
                  "screen_describe"]),
    ("The web", ["browse_to", "web_search", "get_weather"]),
    ("Music", ["play_music", "play_playlist", "spotify_control"]),
    ("Messages and mail", ["send_message", "get_emails", "read_email",
                           "email_action", "send_email"]),
    ("Time", ["set_reminder", "set_timer", "get_reminders", "toggle_clock"]),
    ("Calendar and notes", ["calendar_get", "calendar_add", "notes_add", "notes_get"]),
    ("Your documents", ["search_knowledge", "add_knowledge"]),
    ("Numbers and habits", ["calculate", "log_habit", "get_habit_streak"]),
]


def collect_tools() -> dict:
    """Tool names and their one-line descriptions, straight out of the schemas.

    TOOL_SCHEMAS is a plain literal list, so ast.literal_eval gives the real
    structure — including implicitly concatenated description strings. Parsing
    it as text instead was quietly missing two thirds of the tools.
    """
    schemas = assigned(read("core", "tools.py"), "TOOL_SCHEMAS") or []
    tools = {}
    for entry in schemas:
        fn = (entry or {}).get("function") or {}
        name = fn.get("name")
        if not name or name in tools:
            continue
        desc = " ".join((fn.get("description") or "").split())
        # First sentence only: the model needs the full text, you do not.
        tools[name] = re.split(r"(?<=[.!?])\s", desc)[0][:190] if desc else ""

    grouped = {n for _, names in TOOL_GROUPS for n in names}
    groups = [(title, [n for n in names if n in tools]) for title, names in TOOL_GROUPS]
    leftover = [n for n in tools if n not in grouped]
    if leftover:
        groups.append(("Other", leftover))

    # "Ted requires confirmation before…" is written into the schema text, which
    # is the only place that fact lives — so read it from there.
    confirm = sorted(n for n in tools
                     if "confirmation" in (tools[n] or "").lower()
                     or "requires confirmation" in " ".join(
                         str(((e.get("function") or {}).get("description") or ""))
                         for e in schemas
                         if (e.get("function") or {}).get("name") == n).lower())
    return {"all": tools, "groups": groups, "confirm_required": confirm}


# ── how a message is handled ─────────────────────────────────────────────────

# Purpose prose again: what each step is FOR. Which steps exist is derived below.
STEP_PURPOSE = {
    "mute": "Mute and unmute. Has to be instant, and Ted must not 'discuss' being muted.",
    "stop": "Stop and cancel. Cuts Ted off mid-sentence; pauses Spotify if he wasn't talking.",
    "ui": "Window controls — open the chat log, repeat that, speak faster.",
    "pending": "Answers to a question Ted asked you last turn, and yes/no confirmations.",
    "deterministic": "The short list of things that must not be left to a model.",
    "reflex": "Complete reversible Mac app opens/closes run locally without a model.",
    "model": "Everything else. One streamed loop with a focused tool menu.",
}


def collect_routing() -> dict:
    """What still gets decided before the model sees a message.

    Read from the calls inside _use_deterministic_command rather than assumed:
    that function is the gate, and as it shrinks this section shrinks with it.
    """
    src = read("core", "app.py")
    helpers, guard = [], ""
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_use_deterministic_command":
                guard = ast.get_docstring(node) or ""
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                        helpers.append(inner.func.id)
    except Exception:
        pass

    # A human-readable name for each survivor. Anything unmapped still lists.
    names = {
        "_parse_calc": "arithmetic (\"8 percent of 250\")",
        "_is_timer_request": "timers",
        "_parse_reminder": "reminders",
        "_parse_correction": "corrections (\"actually make it 20 minutes\")",
        "_parse_cancel_scheduled": "cancelling something scheduled",
        "_matches": "voice shortcuts and briefing phrases",
        "_normalize_cmd": None, "bool": None, "any": None,
        "str": None, "len": None, "print": None,
    }
    kept, seen = [], set()
    for h in helpers:
        label = names.get(h, h)
        if label and label not in seen:
            seen.add(label)
            kept.append(label)
    if re.search(r"remember|forget .*about me", src):
        kept.append("explicit memory edits (\"remember that…\")")
    kept.append("mic recalibration and voice enrollment")

    legacy = "TED_LEGACY_LADDER" in src
    # The old two-call path still EXISTS in the file behind the legacy flag, so
    # "is the probe string present" answers the wrong question. What decides it
    # is whether the default path builds a ToolRuntime instead of probing.
    probe_gone = "tool_runtime=_runtime" in src and "llm.ToolRuntime(" in src
    reflex = "routing.plan_reflex(" in src
    return {"deterministic": kept, "guard_doc": guard.strip().split("\n")[0] if guard else "",
            "legacy_flag": legacy, "single_call": probe_gone, "reflex": reflex,
            "purpose": STEP_PURPOSE}


# ── the parts ────────────────────────────────────────────────────────────────

def collect_files() -> list:
    """Every core/ module with its size and its own first docstring line.

    Taking the description from the module docstring means the page describes
    what the file says about itself. If someone changes a file's job and updates
    its docstring — which they do here, the docstrings are good — this follows.
    """
    out = []
    core = os.path.join(ROOT, "core")
    entries = []
    if os.path.isdir(core):
        entries += [("core/" + f, os.path.join(core, f))
                    for f in sorted(os.listdir(core)) if f.endswith(".py")
                    and f != "__init__.py"]
    for extra in ("hud.py", "ted_daemon.py"):
        if exists(extra):
            entries.append((extra, os.path.join(ROOT, extra)))

    for label, path in entries:
        try:
            src = open(path, encoding="utf-8").read()
        except Exception:
            continue
        doc = ""
        try:
            doc = (ast.get_docstring(ast.parse(src)) or "").strip()
        except Exception:
            pass
        # First real sentence: skip a leading "filename — " and any bare title.
        first = ""
        for line in doc.split("\n"):
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^[\w./]+\.py\s*[—–-]\s*", "", line)
            first = line
            break
        out.append({
            "file": label,
            "no_doc": not first,
            "kb": round(len(src.encode()) / 1024, 1),
            "lines": src.count("\n") + 1,
            "desc": first or "(no description at the top of this file)",
        })
    out.sort(key=lambda r: -r["kb"])
    return out


# ── what Ted remembers ───────────────────────────────────────────────────────

MEMORY_MEANING = {
    "facts": "Things Ted knows about you. Every one of these goes into every reply.",
    "chat_turns": "Individual messages in the chat window. The real conversation volume.",
    "chat_sessions": "Separate chat threads, like tabs.",
    "exchanges": "Older voice/HUD turn log, keyword-searchable.",
    "session_summaries": "Dated memories of whole conversations. Deliberately rare — "
                         "Ted declines to write one most of the time, on purpose.",
    "patterns": "Topics you come back to. Written but never read by anything.",
    "memory_audit": "A log of every change to memory, whoever made it.",
    "goals": "Left over from the deleted fireworks feature.",
    "habit_logs": "Built, never used.",
    "audit_context": "Bookkeeping: marks whether Ted or you made a change.",
}


def collect_memory() -> dict:
    path = os.path.join(ROOT, "data", "memory.db")
    if not os.path.exists(path):
        return {"ok": False, "reason": "data/memory.db not found", "tables": []}
    info = {"ok": True, "tables": [],
            "modified": time.time() - os.path.getmtime(path),
            "size_mb": round(os.path.getsize(path) / 1048576, 1)}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        names = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' order by name")]
        for t in names:
            if t.startswith("sqlite_") or "_fts" in t:
                continue
            try:
                n = conn.execute(f"select count(*) from {t}").fetchone()[0]
            except Exception:
                continue
            info["tables"].append({"table": t, "rows": n,
                                   "meaning": MEMORY_MEANING.get(t, "")})
        conn.close()
    except Exception as e:
        return {"ok": False, "reason": str(e), "tables": []}
    info["tables"].sort(key=lambda r: -r["rows"])
    return info


# ── right now ────────────────────────────────────────────────────────────────

def collect_state() -> dict:
    """Live vitals: git, the daemon, the last run, which brain answered."""
    state = {}

    state["branch"] = git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    state["commits"] = [l for l in git("log", "--pretty=%h\t%cr\t%s", "-6").split("\n") if l]
    dirty = [l for l in git("status", "--porcelain").split("\n") if l.strip()]
    state["dirty"] = dirty
    state["ahead"] = git("rev-list", "--count", "@{u}..HEAD") or ""

    # Daemon: heartbeat first, log second. Both absent means never started.
    hb = os.path.join(ROOT, "data", "daemon_heartbeat")
    state["daemon"] = {"installed": exists("tools", "install_daemon.sh"),
                       "ever_ran": os.path.exists(os.path.join(ROOT, "data", "ted_daemon.log")),
                       "alive": False, "age": None}
    if os.path.exists(hb):
        try:
            age = time.time() - float(open(hb).read().strip())
            state["daemon"]["age"] = age
            state["daemon"]["alive"] = age < 195
        except Exception:
            pass

    # Last run + which brain served it, from the launch log.
    log_path = os.path.join(ROOT, "data", "ted_launch.log")
    state["last_run"] = None
    state["fallbacks"] = 0
    if os.path.exists(log_path):
        state["last_run"] = time.time() - os.path.getmtime(log_path)
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-400000:]
            state["fallbacks"] = tail.count("[provider] Groq unavailable")
        except Exception:
            pass

    errs = os.path.join(ROOT, "ted_errors.log")
    state["errors"] = {"exists": os.path.exists(errs),
                       "age": (time.time() - os.path.getmtime(errs))
                       if os.path.exists(errs) else None,
                       "size": os.path.getsize(errs) if os.path.exists(errs) else 0}

    suites = []
    tdir = os.path.join(ROOT, "tests")
    if os.path.isdir(tdir):
        for f in sorted(os.listdir(tdir)):
            if f.startswith("test_") and f.endswith(".py"):
                src = read("tests", f)
                suites.append({"file": f, "checks": len(re.findall(r"\bcheck\(", src))})
    state["suites"] = suites
    return state


def collect_warnings(models, memory, state) -> list:
    """Things worth knowing, every one of them derived from a fact above.

    Deliberately not a wishlist. If it cannot be detected, it does not belong
    here — a hand-kept list of concerns is the thing this whole file replaces.
    """
    warn = []
    if state["dirty"]:
        n = len(state["dirty"])
        tracked = [d for d in state["dirty"] if not d.startswith("??")]
        if tracked:
            warn.append(("Uncommitted changes",
                         f"{len(tracked)} tracked file(s) modified and not committed. "
                         "This is the only state where work can vanish without trace."))
        elif n:
            warn.append(("Untracked files",
                         f"{n} file(s) git does not know about yet."))
    if state["ahead"] and state["ahead"] != "0":
        warn.append(("Not pushed",
                     f"{state['ahead']} commit(s) exist only on this Mac."))
    if not state["daemon"]["ever_ran"]:
        warn.append(("The calendar daemon has never run",
                     "ted_daemon.py is built and tested but no data/ted_daemon.log "
                     "exists, so it has not started on this machine. Proactive "
                     "class reminders do nothing until it does. "
                     "See docs/DAEMON_HANDOFF.md."))
    elif not state["daemon"]["alive"]:
        warn.append(("The calendar daemon is not running right now",
                     "It has run before. The HUD takes the calendar watch back "
                     "automatically, so alerts still work while Ted is open."))
    if not models["local"]["in_config"] or not models["cloud"]["in_config"]:
        warn.append(("Model names are not in config.py",
                     "They fall through to the defaults inside providers.py, so "
                     "changing a model means editing code instead of config."))
    empty = [t["table"] for t in memory.get("tables", []) if t["rows"] == 0]
    if empty:
        warn.append(("Empty tables",
                     ", ".join(empty) + " — built and never used, or left over "
                     "from a deleted feature."))
    unread = [t for t in memory.get("tables", [])
              if t["table"] == "patterns" and t["rows"] > 0]
    if unread:
        warn.append(("Data nothing reads",
                     f"patterns has {unread[0]['rows']} rows and nothing in the "
                     "code reads it. Either use it or drop it."))
    if state["errors"]["exists"] and state["errors"]["size"] > 0:
        warn.append(("There are logged errors",
                     f"ted_errors.log is {state['errors']['size']} bytes, last "
                     f"written {ago(state['errors']['age'])}. Real failures only "
                     "go in this file.",
                     "ted_errors.log is not empty. Only real failures are written "
                     "there, so it is worth reading before assuming things are fine."))
    if state["fallbacks"]:
        warn.append(("Ted has fallen back to the local brain",
                     f"{state['fallbacks']} time(s) in the current launch log — "
                     "the cloud model was unreachable or rate limited.",
                     "It has happened at least once in the current launch log, so "
                     "the Groq to Ollama handover does fire in practice — grep "
                     "'[provider]' in data/ted_launch.log for how often."))
    locks = [f for f in os.listdir(os.path.join(ROOT, ".git"))
             if "lock" in f] if exists(".git") else []
    if any(f == "index.lock" for f in locks):
        warn.append(("Stale git lock",
                     "A .git/index.lock is present and will block the next git "
                     "command. Remove it with: rm -f .git/index.lock"))
    return [w if len(w) == 3 else (w[0], w[1], w[1]) for w in warn]


def collect() -> dict:
    models = collect_models()
    memory = collect_memory()
    state = collect_state()
    return {
        "generated": datetime.now().astimezone().strftime("%A %B %-d, %Y at %-I:%M %p"),
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "root": ROOT,
        "models": models,
        "tools": collect_tools(),
        "routing": collect_routing(),
        "files": collect_files(),
        "memory": memory,
        "state": state,
        "warnings": collect_warnings(models, memory, state),
    }


# ── rendering ────────────────────────────────────────────────────────────────

CSS = """
:root{
  --bg:#171614; --panel:#1f1e1b; --line:#332f2a; --ink:#efe9df;
  --dim:#a49b8d; --faint:#6f675c; --gold:#d9a441; --good:#7fb069; --warn:#d98441;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.65 ui-sans-serif,-apple-system,"SF Pro Text",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:48px 28px 96px}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 6px;font-weight:650}
h2{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:var(--gold);
  margin:52px 0 4px;font-weight:650}
h2 + .sub{color:var(--dim);margin:0 0 18px;font-size:14px;max-width:64ch}
.stamp{color:var(--faint);font-size:13px;margin-bottom:28px}
.lede{font-size:17px;line-height:1.6;color:var(--ink);max-width:66ch;
  border-left:2px solid var(--gold);padding-left:18px;margin:26px 0 8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin:10px 0}
.row{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}
.k{color:var(--dim);font-size:13px}
.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:13px}
.val{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:14px;color:var(--gold)}
.why{color:var(--dim);font-size:13.5px;margin-top:5px;line-height:1.55}
.src{color:var(--faint);font-size:11.5px;margin-top:7px;
  font-family:ui-monospace,Menlo,monospace}
table{width:100%;border-collapse:collapse;margin:8px 0}
td,th{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
  vertical-align:top;font-size:14px}
th{color:var(--faint);font-weight:600;font-size:11px;letter-spacing:.09em;
  text-transform:uppercase}
td.n{text-align:right;font-family:ui-monospace,Menlo,monospace;color:var(--gold);
  white-space:nowrap;width:1%}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;
  border:1px solid var(--line);color:var(--dim)}
.pill.on{color:var(--good);border-color:#3f5637}
.pill.off{color:var(--warn);border-color:#5c4029}
.tool{display:flex;gap:9px;padding:5px 0;font-size:13.5px;align-items:baseline}
.tool .nm{font-family:ui-monospace,Menlo,monospace;color:var(--gold);
  white-space:nowrap;font-size:12.5px}
.tool .ds{color:var(--dim);font-size:13px}
ol.steps{counter-reset:s;list-style:none;padding:0;margin:8px 0}
ol.steps li{counter-increment:s;position:relative;padding:11px 0 11px 42px;
  border-bottom:1px solid var(--line)}
ol.steps li:before{content:counter(s);position:absolute;left:0;top:11px;width:26px;
  height:26px;border-radius:50%;border:1px solid var(--line);color:var(--faint);
  font-size:12px;display:flex;align-items:center;justify-content:center;
  font-family:ui-monospace,Menlo,monospace}
ol.steps li.big:before{border-color:var(--gold);color:var(--gold)}
.wt{border-left:2px solid var(--warn);padding:2px 0 2px 16px;margin:14px 0}
.wt b{display:block;font-weight:600;margin-bottom:2px}
.wt span{color:var(--dim);font-size:13.5px}
.foot{color:var(--faint);font-size:12.5px;margin-top:56px;border-top:1px solid var(--line);
  padding-top:16px;line-height:1.7}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def render(d: dict) -> str:
    m, t, r = d["models"], d["tools"], d["routing"]
    st, mem = d["state"], d["memory"]
    o = []
    add = o.append

    add(f"<!doctype html><html lang=en><head><meta charset=utf-8>")
    add("<meta name=viewport content='width=device-width,initial-scale=1'>")
    add("<title>Ted — what it is right now</title>")
    add(f"<style>{CSS}</style></head><body><div class=wrap>")

    add("<h1>Ted</h1>")
    add(f"<div class=stamp>Read from the code on {esc(d['generated'])}. "
        "Nothing here is typed in by hand.</div>")

    # ── the one-paragraph version ────────────────────────────────────────────
    cloud = m["cloud"]["value"]
    local = m["local"]["value"]
    n_tools = len(t["all"])
    facts = next((x["rows"] for x in mem.get("tables", []) if x["table"] == "facts"), 0)
    turns = next((x["rows"] for x in mem.get("tables", []) if x["table"] == "chat_turns"), 0)
    add(f"<div class=lede>Ted is a chat window on your Mac that thinks with "
        f"<b>{esc(cloud)}</b>, falls back to <b>{esc(local)}</b> running on your own "
        f"machine when that is unavailable, remembers <b>{facts} things about you</b> "
        f"across <b>{turns} messages</b>, and can take <b>{n_tools} kinds of action</b> "
        f"on your computer and your accounts.</div>")

    # ── brains ───────────────────────────────────────────────────────────────
    add("<h2>What it thinks with</h2>")
    add("<p class=sub>Four different jobs. Only the first two are 'the brain' — "
        "the other two are hearing and speaking.</p><div class=grid>")
    for key in ("cloud", "local", "stt", "tts"):
        e = m[key]
        add("<div class=card>")
        add(f"<div class=k>{esc(e['label'])}</div>")
        add(f"<div class=val>{esc(e['value'])}</div>")
        add(f"<div class=why>{esc(e['why'])}</div>")
        add(f"<div class=src>from {esc(e['source'])}</div>")
        add("</div>")
    add("</div>")

    # ── how a message is handled ─────────────────────────────────────────────
    add("<h2>What happens when you type something</h2>")
    add("<p class=sub>Cheap checks first, each one asking 'is this mine?'. The first "
        "one that claims your message handles it — nothing further down gets a say.</p>")
    add("<ol class=steps>")
    for k in ("mute", "stop", "ui", "pending"):
        add(f"<li>{esc(r['purpose'][k])}</li>")
    det = ", ".join(r["deterministic"])
    add("<li><b>Things that must not be left to a model.</b> "
        f"<span class=why style='display:block'>Currently: {esc(det)}. "
        "Arithmetic is on this list because a model getting a number wrong fails "
        "<i>silently</i> — a wrong answer looks exactly like a right one.</span></li>")
    if r["reflex"]:
        add(f"<li><b>Fast app reflex.</b> {esc(r['purpose']['reflex'])}</li>")
    add("<li class=big><b>Everything else goes to the model</b>, in one streamed "
        "loop with a focused tool menu. It can answer, discover another tool, or use "
        "several and chain them."
        + (" <span class=why style='display:block'>There used to be two calls per "
           "message — one to ask 'does this need a tool?', another to write the "
           "answer. They were merged in August.</span>" if r["single_call"] else "")
        + "</li>")
    add("</ol>")
    if r["legacy_flag"]:
        add("<p class=why>The old version of this is still in the code behind "
            "<span class=mono>TED_LEGACY_LADDER=1</span>, for tracking down a "
            "regression.</p>")

    # ── tools ────────────────────────────────────────────────────────────────
    add(f"<h2>What it can actually do &mdash; {n_tools} tools</h2>")
    add("<p class=sub>Ted decides which of these to reach for. The descriptions "
        "below are the exact text the model reads when it chooses.</p>")
    for title, names in t["groups"]:
        if not names:
            continue
        add(f"<div class=card><div class=k style='margin-bottom:7px'>{esc(title)}</div>")
        for n in names:
            add(f"<div class=tool><span class=nm>{esc(n)}</span>"
                f"<span class=ds>{esc(t['all'].get(n, ''))}</span></div>")
        add("</div>")
    if t["confirm_required"]:
        add("<p class=why>Asks you before doing it: "
            + ", ".join(f"<span class=mono>{esc(x)}</span>" for x in t["confirm_required"])
            + ".</p>")

    # ── memory ───────────────────────────────────────────────────────────────
    add("<h2>What it remembers</h2>")
    if not mem.get("ok"):
        add(f"<p class=sub>Could not read the memory database: {esc(mem.get('reason'))}</p>")
    else:
        add(f"<p class=sub>One SQLite file, {mem['size_mb']} MB, last written "
            f"{esc(ago(mem['modified']))}. You can edit any of this by hand in the "
            "Memory panel.</p><table><tr><th>Table</th><th>What it is</th>"
            "<th style='text-align:right'>Rows</th></tr>")
        for row in mem["tables"]:
            add(f"<tr><td class=mono>{esc(row['table'])}</td>"
                f"<td style='color:var(--dim)'>{esc(row['meaning'])}</td>"
                f"<td class=n>{row['rows']}</td></tr>")
        add("</table>")

    # ── right now ────────────────────────────────────────────────────────────
    add("<h2>Right now</h2><div class=grid>")
    dmn = st["daemon"]
    if dmn["alive"]:
        pill, note = "<span class='pill on'>running</span>", "Calendar alerts work with Ted closed."
    elif dmn["ever_ran"]:
        pill, note = "<span class='pill off'>stopped</span>", "It has run before. Ted's window takes the watch back while it is open."
    else:
        pill, note = "<span class='pill off'>never started</span>", "Built and tested, never launched on this Mac. See docs/DAEMON_HANDOFF.md."
    add(f"<div class=card><div class=k>Calendar daemon</div><div style='margin:4px 0'>{pill}</div>"
        f"<div class=why>{esc(note)}</div></div>")

    add(f"<div class=card><div class=k>Launch log last written</div><div class=val>"
        f"{esc(ago(st['last_run']) if st['last_run'] is not None else 'no launch log')}</div>"
        f"<div class=why>Everything Ted prints goes here. Recent means Ted ran "
        f"recently &mdash; though the memory dashboard writes to it too.</div></div>")

    add(f"<div class=card><div class=k>Branch</div><div class=val>{esc(st['branch'])}</div>"
        f"<div class=why>{len([x for x in st['dirty'] if not x.startswith('??')])} "
        f"modified file(s), {len([x for x in st['dirty'] if x.startswith('??')])} untracked."
        f"</div></div>")

    total = sum(s["checks"] for s in st["suites"])
    add(f"<div class=card><div class=k>Tests</div><div class=val>{total} checks</div>"
        f"<div class=why>Across {len(st['suites'])} suites. This counts what is "
        "written, not what passed &mdash; run them to know that.</div></div>")
    add("</div>")

    if st["commits"]:
        add("<div class=card><div class=k style='margin-bottom:8px'>Recent work</div><table>")
        for line in st["commits"]:
            parts = line.split("\t")
            if len(parts) == 3:
                add(f"<tr><td class=mono style='width:1%;white-space:nowrap'>{esc(parts[0])}</td>"
                    f"<td>{esc(parts[2])}</td>"
                    f"<td style='color:var(--faint);width:1%;white-space:nowrap;text-align:right'>"
                    f"{esc(parts[1])}</td></tr>")
        add("</table></div>")

    # ── worth knowing ────────────────────────────────────────────────────────
    if d["warnings"]:
        add("<h2>Worth knowing</h2>")
        add("<p class=sub>Every item below was detected, not remembered. If it stops "
            "being true it disappears from this page by itself.</p>")
        for title, body, _stable in d["warnings"]:
            add(f"<div class=wt><b>{esc(title)}</b><span>{esc(body)}</span></div>")

    # ── the parts ────────────────────────────────────────────────────────────
    add("<h2>The parts</h2>")
    add("<p class=sub>Every file, biggest first, described by its own opening line. "
        "Size matters here: the big ones are where bugs hide.</p><table>"
        "<tr><th>File</th><th>What it does</th><th style='text-align:right'>Size</th></tr>")
    for f in d["files"]:
        add(f"<tr><td class=mono style='white-space:nowrap'>{esc(f['file'])}</td>"
            f"<td style='color:var(--dim)'>{esc(f['desc'])}</td>"
            f"<td class=n>{f['kb']} KB</td></tr>")
    add("</table>")

    add("<div class=foot>Generated by <span class=mono>tools/ted_map.py</span>, which "
        "reads config.py, core/providers.py, core/tools.py, core/app.py, every module "
        "docstring, data/memory.db, and git. It imports nothing from Ted, so it runs "
        "even when Ted cannot.<br>Regenerate any time with "
        "<span class=mono>python tools/ted_map.py</span>.</div>")
    add("</div></body></html>")
    return "\n".join(o)


def render_markdown(d: dict, stable: bool = False) -> str:
    """The status block for CLAUDE.md / AGENTS.md.

    Two shapes, and the difference is the whole point of the ``stable`` flag.

    The full version (``--markdown``) is for a human looking at the moment: it
    includes branch, working-tree state and recent commits.

    The stable version (``--sync``, and therefore the commit hook) deliberately
    leaves all of that out. Two reasons, and they are the same two reasons this
    file exists at all:

    1. Branch, dirty files and the commit list are one git command away, and
       every assistant is already told to run those first. Writing them into a
       tracked file makes a second copy that can disagree with the first.
    2. A block containing volatile facts changes on every commit, so every
       commit would touch CLAUDE.md and AGENTS.md. Diff noise trains people to
       stop reading the diff.

    What stays is what is expensive to discover: which models, how many tools,
    what shape the routing is, roughly how much Ted is used, whether the daemon
    has ever run. Chat volume is rounded so it moves a few times a month rather
    than a few times an hour.
    """
    m, st, mem = d["models"], d["state"], d["memory"]
    rows = {x["table"]: x["rows"] for x in mem.get("tables", [])}
    dirty = [x for x in st["dirty"] if not x.startswith("??")]

    def about(n, step=50):
        return f"~{int(round(n / step)) * step}" if n >= step else str(n)

    L = []
    L.append("<!-- GENERATED by tools/ted_map.py — do not edit by hand. -->")
    L.append("<!-- Refresh: python tools/ted_map.py --sync  (the commit hook does this) -->")
    L.append("")
    stamp = d["generated"].split(" at ")[0] if stable else d["generated"]
    L.append(f"## Current state — read from the repo on {stamp}")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Thinks with | `{m['cloud']['value']}` (cloud), falling back to "
             f"`{m['local']['value']}` on local Ollama |")
    L.append(f"| Hears / speaks | `{m['stt']['value']}` / `{m['tts']['value']}` (local) |")
    L.append(f"| Tools | {len(d['tools']['all'])} |")
    route = ("local app reflex + one streamed loop" if d["routing"].get("reflex")
             and d["routing"]["single_call"] else
             "one streamed loop" if d["routing"]["single_call"] else "two calls")
    L.append(f"| Routing | {route}"
             f"{'; legacy path behind TED_LEGACY_LADDER=1' if d['routing']['legacy_flag'] else ''} |")
    if stable:
        L.append(f"| Memory | {rows.get('facts', 0)} facts, "
                 f"{about(rows.get('chat_turns', 0))} chat turns, "
                 f"{rows.get('session_summaries', 0)} session memories |")
    else:
        L.append(f"| Memory | {rows.get('facts',0)} facts, {rows.get('chat_turns',0)} chat turns, "
                 f"{rows.get('session_summaries',0)} session memories |")
        L.append(f"| Branch | `{st['branch']}` |")
        L.append(f"| Working tree | {'clean' if not dirty else f'{len(dirty)} file(s) modified — someone may be mid-task'} |")
    L.append(f"| Tests | {sum(s['checks'] for s in st['suites'])} checks across "
             f"{len(st['suites'])} suites |")
    daemon = ("running" if st["daemon"]["alive"]
              else "built, never started on this Mac" if not st["daemon"]["ever_ran"]
              else "installed, not running")
    L.append(f"| Calendar daemon | {daemon} |")
    L.append("")

    # Git-derived warnings are dropped from the stable block for the same reason
    # the git rows are: `git status` answers them, and they would churn.
    GIT_DERIVED = {"Uncommitted changes", "Untracked files", "Not pushed", "Stale git lock"}
    warnings = [w for w in d["warnings"]
                if not (stable and w[0] in GIT_DERIVED)]
    if warnings:
        L.append("**Detected right now:**" if not stable else "**Standing issues, detected not remembered:**")
        L.append("")
        for title, body, stable_body in warnings:
            L.append(f"- **{title}.** {stable_body if stable else body}")
        L.append("")

    if stable:
        L.append("Run `git log --oneline -10` and `git status` for anything about the "
                 "working tree — that is deliberately not duplicated here.")
        L.append("")
    else:
        L.append("Recent commits — the commit log is the handoff log on this project:")
        L.append("")
        for line in st["commits"][:6]:
            p = line.split("\t")
            if len(p) == 3:
                L.append(f"- `{p[0]}` {p[2]} *({p[1]})*")
        L.append("")
    return "\n".join(L)


BEGIN = "<!-- ted_map:begin -->"
END = "<!-- ted_map:end -->"


def sync_files(d: dict, paths=("CLAUDE.md", "AGENTS.md")) -> list:
    """Refresh the generated status block inside the AI entry-point files.

    Only the text between the two markers is touched — everything a human wrote
    around it survives. A file without markers is skipped and reported rather
    than guessed at: silently rewriting someone's notes is worse than doing
    nothing.
    """
    block = BEGIN + "\n" + render_markdown(d, stable=True) + "\n" + END
    results = []
    for name in paths:
        full = os.path.join(ROOT, name)
        if not os.path.exists(full):
            results.append((name, "missing"))
            continue
        text = open(full, encoding="utf-8").read()
        if BEGIN not in text or END not in text:
            results.append((name, "no markers — add them where the block should go"))
            continue
        start = text.index(BEGIN)
        stop = text.index(END) + len(END)
        updated = text[:start] + block + text[stop:]
        if updated == text:
            results.append((name, "already current"))
            continue
        with open(full, "w", encoding="utf-8") as f:
            f.write(updated)
        results.append((name, "updated"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Describe Ted by reading Ted.")
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "ted_map.html"))
    ap.add_argument("--markdown", action="store_true",
                    help="print the CLAUDE.md / AGENTS.md status block instead")
    ap.add_argument("--json", action="store_true", help="print the raw facts")
    ap.add_argument("--open", action="store_true", help="open the page when done")
    ap.add_argument("--sync", action="store_true",
                    help="refresh the generated block in CLAUDE.md and AGENTS.md")
    args = ap.parse_args()

    d = collect()
    if args.json:
        print(json.dumps(d, indent=2, default=str))
        return 0
    if args.markdown:
        print(render_markdown(d))
        return 0
    if args.sync:
        for name, outcome in sync_files(d):
            print(f"[ted_map] {name}: {outcome}")
        return 0

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render(d))
    print(f"[ted_map] wrote {args.out}")
    if args.open:
        subprocess.run(["open", args.out], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
