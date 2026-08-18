"""Checks for Ted reading his own source.

Charlie's rule: Ted can see everything and change nothing without being asked.
Most of what follows is that rule, tested from several directions, because
"read-only" written in a docstring is not read-only.

The containment checks are the important ones. A codebase reader that can be
talked into quoting config.py has leaked Charlie's real Groq and ElevenLabs
keys into a chat log that gets saved to SQLite.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import codebase as cb
from core import routing
from core import tool_handlers as th


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("— what Ted refuses to look at —")
for path, why in (
        ("config.py", "the file holding the real API keys"),
        ("../../.ssh/id_rsa", "an escape with .."),
        ("/etc/passwd", "an absolute path outside the project"),
        ("~/.ssh/id_rsa", "a home-relative path outside the project"),
        ("core/../config.py", "a path that only looks safe before resolving"),
        ("data/memory.db", "Charlie's memory database"),
):
    rel, error = cb.resolve(path)
    check(f"refuses {why}", not rel and bool(error))

check("an ordinary source file resolves",
      cb.resolve("core/routing.py") == ("core/routing.py", ""))
check("…and so does its absolute form",
      cb.resolve(os.path.join(cb.ROOT, "core/routing.py"))[0] == "core/routing.py")
check("an empty path is refused", cb.resolve("")[1] == "no path given")
check("git-ignored files are invisible to the walker",
      not any(cb.is_secret(rel) for rel in cb._walk()))
check("config.py never appears in the tree",
      "config.py" not in cb.tree().replace("config.example.py", ""))

print("\n— reading —")
out = cb.read("core/notebook.py", 1, 5)
check("a line range comes back numbered",
      "core/notebook.py lines 1-5" in out and "    1  " in out)
check("a missing file says so", "no file at" in cb.read("core/nope.py"))
check("a silly range is handled", "only has" in cb.read("core/notebook.py", 99999))

# core/app.py is 3,700 lines. Under the byte ceiling and still ruinous — it
# would fill the whole context window and leave no room for the answer.
whole = cb.read("core/app.py")
check("a huge file is capped by lines, not just bytes",
      whole.count("\n") <= cb.MAX_READ_LINES + 5)
check("…and says how to get the rest", "more lines. Ask for lines" in whole)

print("\n— searching —")
hits = cb.search("classify_brain")
check("a real symbol is found", "core/routing.py:" in hits)
check("an empty query asks for one", "need something" in cb.search(""))
check("a miss is reported honestly",
      "No match" in cb.search("zzz_this_string_is_not_in_the_repo_zzz"))

print("\n— the project describes itself from disk —")
over = cb.overview()
check("the overview names real modules", "core/app.py" in over)
check("…the current branch", "Branch:" in over)
check("…and recent commits", "Recent commits:" in over)
check("history works", "commits" in cb.history(count=3).lower())
check("history can be scoped to one file",
      "core/notebook.py" in cb.history("core/notebook.py", 3)
      or "No commit history" in cb.history("core/notebook.py", 3))

print("\n— writing needs Charlie's yes —")
target = os.path.join(cb.ROOT, "core", "routines.py")
before = hashlib.sha256(open(target, "rb").read()).hexdigest()
refused = cb.write("core/routines.py", "DESTROYED")
after = hashlib.sha256(open(target, "rb").read()).hexdigest()
check("an unconfirmed write is refused", "won't change my own code" in refused)
check("…and the file is byte-for-byte unchanged", before == after)
check("even a CONFIRMED write cannot touch a secret",
      "won't read it" in cb.write("config.py", "x", confirmed=True))
check("even a CONFIRMED write cannot escape the project",
      "outside the project" in cb.write("/etc/hosts", "x", confirmed=True))

probe = "docs/_ted_write_test.md"
probe_full = os.path.join(cb.ROOT, probe)
try:
    created = cb.write(probe, "one\n", confirmed=True)
    check("a confirmed write inside the project works",
          "Created" in created and open(probe_full).read() == "one\n")
    updated = cb.write(probe, "two\n", confirmed=True)
    check("…and rewriting keeps a backup of the old version",
          "backup" in updated
          and open(probe_full + ".ted-backup").read() == "one\n")
finally:
    for p in (probe_full, probe_full + ".ted-backup"):
        if os.path.exists(p):
            os.remove(p)
check("the probe cleaned up after itself", not os.path.exists(probe_full))

print("\n— the write tool is gated like sending a message —")
check("code_write requires confirmation", "code_write" in th.CONFIRMATION_TOOLS)
check("the read tools do not",
      not ({"code_read", "code_search", "code_overview", "code_tree",
            "code_history", "code_diff"} & th.CONFIRMATION_TOOLS))


def names_for(text):
    return {routing.tool_name(s) for s in routing.select_tool_schemas(text)}


print("\n— when the code tools are offered —")
for phrase in ("how are you built", "show me your code",
               "which file handles the memory", "search your source for reflex"):
    check(f"{phrase!r} loads the reading tools",
          "code_search" in names_for(phrase))
check("…and none of them offer the write tool",
      "code_write" not in names_for("show me your code"))
check("asking to change Ted's code does offer it",
      "code_write" in names_for("change your own code to add a routine"))
check("…as does 'edit yourself'", "code_write" in names_for("edit yourself"))
# "the code" is far more often Charlie's homework than it is Ted.
check("someone else's code does not load Ted's source tools",
      "code_search" not in names_for("help me debug my java homework"))
check("ordinary requests are unaffected",
      not any(n.startswith("code_") for n in names_for("play some music")))

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
