"""core/codebase.py — Ted reading his own source.

Charlie's rule, and the only one that matters here: **Ted can see everything and
change nothing without being asked.** Reading is free. Writing exists, but it is
a single narrow function that the tool layer routes through the same yes/no
confirmation as sending a message, and it refuses to run without it.

Three defences, because "read-only" written in a docstring is not read-only:

1. Every path is resolved with realpath and must land inside the repository.
   That is what stops ``../../.ssh/id_rsa`` and a symlink pointing out of the
   tree — checking the string before resolving catches neither.
2. Files git ignores are invisible. config.py holds Charlie's real API keys and
   is gitignored precisely because it is secret; a codebase reader that helpfully
   quotes it back into a chat log has leaked it. .env, keys and data are
   excluded the same way.
3. Writes are refused unless ``confirmed=True`` is passed by the caller, and the
   only caller that can pass it is the confirmation resolver.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 27 (§27.3)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Ted reading his own source code. The rule that governs this entire file:
#      Ted can see everything and change nothing without being asked.
#
#      Reading is free. Writing exists as one narrow function that goes through the
#      same yes/no confirmation as sending a message, and refuses to run without it.
#
#  THE THREE DEFENCES, AND WHY EACH IS NEEDED
#      1. Every path is resolved with realpath and must land inside the repository.
#         Checking the string BEFORE resolving catches neither `../../.ssh/id_rsa`
#         nor a symlink pointing out of the tree. Resolve first, then check.
#      2. Files git ignores are invisible — that is what keeps config.py, which
#         holds your API keys, out of reach.
#      3. Writing requires an explicit confirmation from you, every time.
#
# =============================================================================

from __future__ import annotations

import os
import re
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories that are never worth reading and are large enough to make a
# search slow if they were.
SKIP_DIRS = {".git", "venv", "__pycache__", "node_modules", ".pytest_cache",
             "data", ".DS_Store", "Ted.app", "inbox", ".claude"}

# Secrets, and things that merely look like source. Checked by name, in
# addition to the gitignore rule below, so a repo whose ignore file changes
# does not silently start exposing keys.
SECRET_NAMES = {"config.py", ".env", ".env.local", "credentials.json",
                "token.json", ".ted_email_config.json", "shortcuts.json"}
SECRET_PATTERNS = (re.compile(r"\.(pem|key|p12|keychain|sqlite|db)$", re.I),
                   re.compile(r"(^|[._-])secret", re.I))

READABLE_EXT = {".py", ".js", ".html", ".css", ".md", ".txt", ".json", ".sh",
                ".swift", ".plist", ".toml", ".yaml", ".yml", ".cfg", ".ini", ""}

MAX_READ_BYTES = 240_000
MAX_MATCHES = 60
# A whole-file read of core/app.py is 3,700 lines. Under the byte ceiling and
# still ruinous: it would consume the entire context window and leave no room
# for the answer. Reads are capped by LINES RETURNED, and a longer file comes
# back as its first page with the range to ask for next.
MAX_READ_LINES = 400

_ignored_cache = {"at": 0.0, "paths": frozenset()}
_IGNORE_TTL = 60.0


def _git(*args, timeout=8):
    try:
        result = subprocess.run(["git", "-C", ROOT, *args],
                                capture_output=True, text=True, timeout=timeout)
        return result.stdout if result.returncode == 0 else ""
    except Exception as exc:
        print(f"[codebase] git {' '.join(args)}: {exc}")
        return ""


def _ignored_paths():
    """Repo-relative paths git is ignoring. Cached — this shells out."""
    now = time.time()
    if now - _ignored_cache["at"] < _IGNORE_TTL:
        return _ignored_cache["paths"]
    raw = _git("status", "--porcelain", "--ignored=matching", "--untracked-files=all")
    paths = set()
    for line in raw.splitlines():
        if line.startswith("!! "):
            paths.add(line[3:].strip().rstrip("/"))
    _ignored_cache.update(at=now, paths=frozenset(paths))
    return _ignored_cache["paths"]


def is_secret(rel):
    """True when this path must never be shown, whatever asked for it."""
    name = os.path.basename(rel)
    if name in SECRET_NAMES:
        return True
    if any(p.search(name) for p in SECRET_PATTERNS):
        return True
    ignored = _ignored_paths()
    if rel in ignored or name in ignored:
        return True
    # An ignored directory hides everything under it.
    return any(rel.startswith(ig + "/") for ig in ignored if ig)


def resolve(path):
    """Turn a user- or model-supplied path into a safe repo-relative one.

    Returns (relative_path, error). realpath runs BEFORE the containment check,
    so '../../.ssh/id_rsa' and a symlink out of the tree are both caught; a
    string comparison done first would catch neither.
    """
    raw = str(path or "").strip()
    if not raw:
        return "", "no path given"
    # Expanded before anything else, so "~/.ssh/id_rsa" is refused as the
    # outside-the-project path it obviously is, rather than being treated as a
    # literal directory named "~" and answered with "no such file".
    raw = os.path.expanduser(raw)
    root = os.path.realpath(ROOT)
    if os.path.isabs(raw):
        # An absolute path is answered as itself or refused. Quietly stripping
        # the leading slash and re-rooting it would turn "/etc/passwd" into
        # "<repo>/etc/passwd" and report "no such file" — an answer about a
        # different question, which is the kind of near-miss that reads as Ted
        # having looked when he has not.
        candidate = os.path.realpath(raw)
        if candidate != root and not candidate.startswith(root + os.sep):
            return "", "that path is outside the project"
        full = candidate
    else:
        full = os.path.realpath(os.path.join(ROOT, raw))
    if full != root and not full.startswith(root + os.sep):
        return "", "that path is outside the project"
    rel = os.path.relpath(full, root)
    if is_secret(rel):
        return "", (f"{os.path.basename(rel)} holds credentials or is git-ignored, "
                    f"so I won't read it")
    return rel, ""


def _walk():
    """Yield repo-relative paths of every readable source file."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
            if os.path.splitext(name)[1].lower() not in READABLE_EXT:
                continue
            if is_secret(rel):
                continue
            yield rel


def tree(subdir="", limit=200):
    """List the project's files with their sizes."""
    rel_base, error = (resolve(subdir) if subdir else ("", ""))
    if error:
        return error
    rows = []
    for rel in sorted(_walk()):
        if rel_base and not rel.startswith(rel_base):
            continue
        try:
            size = os.path.getsize(os.path.join(ROOT, rel))
        except OSError:
            continue
        rows.append((rel, size))
    if not rows:
        return f"Nothing readable under {subdir or 'the project'}."
    shown = rows[:limit]
    lines = [f"{rel}  ({size / 1024:.0f} KB)" for rel, size in shown]
    header = f"{len(rows)} files in {subdir or 'ted-ai'}"
    if len(rows) > limit:
        header += f" (showing {limit})"
    return header + ":\n" + "\n".join(lines)


def read(path, start=1, end=0):
    """Read a file, or a line range of it."""
    rel, error = resolve(path)
    if error:
        return error
    full = os.path.join(ROOT, rel)
    if not os.path.isfile(full):
        return f"There's no file at {rel}."
    try:
        if os.path.getsize(full) > MAX_READ_BYTES:
            return (f"{rel} is too big to read whole "
                    f"({os.path.getsize(full) / 1024:.0f} KB) — ask for a line range.")
        with open(full, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception as exc:
        return f"I couldn't read {rel}: {exc}"
    try:
        start = max(1, int(start or 1))
        end = int(end or 0)
    except (TypeError, ValueError):
        start, end = 1, 0
    total = len(lines)
    end = total if end <= 0 else min(end, total)
    if start > total:
        return f"{rel} only has {total} lines."
    truncated = False
    if end - start + 1 > MAX_READ_LINES:
        end = start + MAX_READ_LINES - 1
        truncated = True
    chunk = lines[start - 1:end]
    # Numbered, because the next question is almost always "change line N" and
    # an unnumbered dump makes that a counting exercise.
    body = "".join(f"{start + i:5d}  {line}" for i, line in enumerate(chunk))
    note = ""
    if truncated:
        note = (f"\n… {total - end} more lines. Ask for "
                f"lines {end + 1}-{min(total, end + MAX_READ_LINES)} to continue.")
    return f"{rel} lines {start}-{end} of {total}:\n{body}{note}"


def search(query, limit=MAX_MATCHES):
    """Find where something appears in the source. Literal, not regex."""
    needle = str(query or "").strip()
    if not needle:
        return "I need something to search for."
    low = needle.lower()
    hits, scanned = [], 0
    for rel in sorted(_walk()):
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8",
                      errors="replace") as fh:
                for number, line in enumerate(fh, 1):
                    if low in line.lower():
                        hits.append(f"{rel}:{number}: {line.strip()[:180]}")
                        if len(hits) >= limit:
                            break
        except Exception:
            continue
        scanned += 1
        if len(hits) >= limit:
            break
    if not hits:
        return f"No match for {needle!r} in {scanned} files."
    head = f"{len(hits)} match(es) for {needle!r}"
    if len(hits) >= limit:
        head += " (stopped at the limit)"
    return head + ":\n" + "\n".join(hits)


def overview():
    """A short factual description of the project, read fresh from disk.

    Deliberately computed rather than remembered: this project's own CLAUDE.md
    warns that plans rot faster than code, and a hand-written summary of a
    codebase is wrong within a week.
    """
    files = list(_walk())
    by_ext = {}
    total = 0
    for rel in files:
        ext = os.path.splitext(rel)[1].lower() or "(none)"
        try:
            size = os.path.getsize(os.path.join(ROOT, rel))
        except OSError:
            continue
        by_ext.setdefault(ext, [0, 0])
        by_ext[ext][0] += 1
        by_ext[ext][1] += size
        total += size
    ranked = sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:6]
    parts = [f"{n} {ext} ({s / 1024:.0f} KB)" for ext, (n, s) in ranked]
    biggest = sorted(
        ((rel, os.path.getsize(os.path.join(ROOT, rel)))
         for rel in files if os.path.exists(os.path.join(ROOT, rel))),
        key=lambda kv: -kv[1])[:6]
    branch = _git("branch", "--show-current").strip()
    recent = [l for l in _git("log", "--oneline", "-5").splitlines() if l]
    dirty = [l for l in _git("status", "--porcelain").splitlines() if l][:8]
    out = [
        f"Ted's own source at {ROOT}.",
        f"{len(files)} readable files, {total / 1024:.0f} KB: " + ", ".join(parts),
        "Largest: " + ", ".join(f"{r} ({s / 1024:.0f} KB)" for r, s in biggest),
        f"Branch: {branch or 'unknown'}",
    ]
    if recent:
        out.append("Recent commits:\n  " + "\n  ".join(recent))
    out.append("Uncommitted changes:\n  " + "\n  ".join(dirty) if dirty
               else "Working tree is clean.")
    return "\n".join(out)


def history(path="", count=8):
    """Recent commits, optionally only those touching one file."""
    try:
        count = max(1, min(int(count or 8), 30))
    except (TypeError, ValueError):
        count = 8
    if path:
        rel, error = resolve(path)
        if error:
            return error
        raw = _git("log", "--oneline", f"-{count}", "--", rel)
        label = f"Last {count} commits touching {rel}"
    else:
        raw = _git("log", "--oneline", f"-{count}")
        label = f"Last {count} commits"
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return f"No commit history found{' for ' + path if path else ''}."
    return label + ":\n" + "\n".join(lines)


def write(path, content, confirmed=False):
    """Change a file. Refuses outright unless the caller passes confirmed=True.

    The only caller allowed to pass it is the confirmation resolver in
    core/app.py, which gets there only after Charlie has said yes to a preview
    of the change. This function does not ask; it just will not act unasked.
    """
    if not confirmed:
        return ("I won't change my own code without your say-so. "
                "Nothing was written.")
    rel, error = resolve(path)
    if error:
        return error
    full = os.path.join(ROOT, rel)
    existed = os.path.isfile(full)
    # A backup beside the file, because the working tree is the only copy of
    # uncommitted work and this project has already lost some that way.
    backup = ""
    try:
        if existed:
            backup = full + ".ted-backup"
            with open(full, encoding="utf-8", errors="replace") as fh:
                previous = fh.read()
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(previous)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception as exc:
        return f"I couldn't write {rel}: {exc}"
    verb = "Updated" if existed else "Created"
    note = f" The previous version is at {os.path.basename(backup)}." if backup else ""
    return f"{verb} {rel} ({len(content)} characters).{note}"


def diff(path=""):
    """What has changed in the working tree, so Ted can report it honestly."""
    if path:
        rel, error = resolve(path)
        if error:
            return error
        raw = _git("diff", "--", rel)
        label = f"Uncommitted changes in {rel}"
    else:
        raw = _git("diff", "--stat")
        label = "Uncommitted changes"
    raw = raw.strip()
    if not raw:
        return "Nothing has changed in the working tree."
    if len(raw) > 8000:
        raw = raw[:8000] + "\n… (truncated)"
    return f"{label}:\n{raw}"
