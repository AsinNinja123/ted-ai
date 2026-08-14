#!/bin/bash
# Point git at the repo's versioned hooks.
#
#   bash tools/install_hooks.sh              install
#   bash tools/install_hooks.sh --uninstall  remove
#
# Uses core.hooksPath rather than copying files into .git/hooks, so the hook
# stays visible in the repo and in diffs instead of becoming a thing that
# invisibly edits your commits. One config line, easy to see and easy to undo:
#
#   git config --get core.hooksPath
#
# This is per-clone. A fresh clone needs it run once.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
    git -C "$ROOT" config --unset core.hooksPath 2>/dev/null || true
    echo "Hooks disabled. git is back to .git/hooks."
    exit 0
fi

chmod +x "$ROOT/tools/githooks/"* 2>/dev/null || true
git -C "$ROOT" config core.hooksPath tools/githooks

echo "Hooks installed → tools/githooks"
echo
echo "Every commit now refreshes the generated block in CLAUDE.md and AGENTS.md"
echo "so the next assistant — Claude Code, Codex, or Cowork — reads current"
echo "facts instead of whatever was true the last time someone updated them."
echo
echo "  skip once:  git commit --no-verify"
echo "  remove:     bash tools/install_hooks.sh --uninstall"
echo "  check:      git config --get core.hooksPath"
