#!/bin/bash
# Sets Finder comments on everything in ted-ai, so the Comments column in
# List view explains what each item is.
#
#   bash docs/set_finder_comments.sh
#
# Then: Cmd-2 (List view) -> Cmd-J (View Options) -> tick "Comments".
#
# Comments live in each file's extended attributes. They are LOCAL to this Mac:
# they are not in git, so a fresh clone will not have them. Re-run this script
# after cloning. Safe to run repeatedly; it overwrites rather than appends.
#
# To wipe them all again:
#   xattr -r -d com.apple.metadata:kMDItemFinderComment ~/ted-ai

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

set_comment() {
  local target="$ROOT/$1"
  local note="$2"
  if [ ! -e "$target" ]; then
    echo "  skip (missing): $1"
    return
  fi
  osascript - "$target" "$note" >/dev/null <<'APPLESCRIPT'
on run argv
  set p to POSIX file (item 1 of argv) as alias
  tell application "Finder" to set comment of p to (item 2 of argv)
end run
APPLESCRIPT
  echo "  set: $1"
}

echo "Tagging $ROOT ..."

# --- tier 1: opened constantly ---
set_comment "hud.py"              "START HERE. Entry point - run 'python hud.py' to launch Ted."
set_comment "core"                "All 25 modules. This is the actual codebase."
set_comment "config.py"           "Your API keys and settings. Gitignored - never committed."
set_comment "README.md"           "Setup, architecture, and the full file-by-file layout."

# --- tier 2: opened occasionally ---
set_comment "ui"                  "The HUD window. ted_hud.html is live; _legacy is the old one."
set_comment "tests"               "Run before committing: source venv/bin/activate && python -m pytest tests/"
set_comment "docs"                "Design notes and debugging handoffs. Not needed to run Ted."
set_comment "native"              "Swift echo-cancel engine. Run ./build.sh once, then ignore."
set_comment "inbox"               "Drop PDFs here, then say 'index my documents' to make them searchable."
set_comment "shortcuts.json"      "Your custom voice shortcuts - edit to add new spoken commands."
set_comment "requirements.txt"    "pip dependencies. Update when you add a library."
set_comment "config.example.py"   "Template with placeholder keys. This is the one that goes to GitHub."

# --- tier 3: never touched ---
set_comment "data"                "340 MB of voice models and local DBs. Generated - do not edit by hand."
set_comment "venv"                "1.7 GB Python environment. Rebuildable from requirements.txt."

echo
echo "Done. Now: Cmd-2 for List view, Cmd-J for View Options, tick 'Comments'."
