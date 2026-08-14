#!/bin/bash
# Install (or reinstall) Ted's calendar daemon as a launchd user agent.
#
#   bash tools/install_daemon.sh            install / reinstall
#   bash tools/install_daemon.sh --uninstall  remove it
#
# A user agent, not a system daemon: it runs as you, when you are logged in,
# which is what reading YOUR Calendar.app requires.

set -euo pipefail

LABEL="com.charlie.ted-daemon"
TED_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Removed $LABEL."
    exit 0
fi

if [[ ! -x "$TED_HOME/venv/bin/python" ]]; then
    echo "No venv at $TED_HOME/venv — create it before installing." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$TED_HOME/data"
sed "s|__PLACEHOLDER_HOME__|$TED_HOME|g" \
    "$TED_HOME/tools/$LABEL.plist" > "$TARGET"

# bootout first so a reinstall replaces the running job instead of failing on
# "service already loaded".
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/$LABEL"

echo "Installed $LABEL."
echo "  status:  launchctl print $DOMAIN/$LABEL | head -20"
echo "  log:     tail -f $TED_HOME/data/ted_daemon.log"
echo "  remove:  bash tools/install_daemon.sh --uninstall"
echo
echo "macOS will ask for permission the first time it reads Calendar and the"
echo "first time it posts a notification. If no prompt appears and the log"
echo "shows an osascript error, grant it by hand in System Settings →"
echo "Privacy & Security → Automation, and → Notifications."
