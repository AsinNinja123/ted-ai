#!/bin/bash
# tools/make_app.sh — build Ted.app, a double-clickable launcher for Ted.
#
# Run once:   bash tools/make_app.sh
# Rebuild any time; it overwrites cleanly.
#
# What you get: ~/ted-ai/Ted.app — double-click it (or Spotlight "Ted") instead
# of opening a terminal. Drag it to the Dock or /Applications if you want.
#
# The bundle is a thin wrapper: it activates the venv and runs hud.py, with
# stdout/stderr going to data/ted_launch.log so a crash isn't invisible.

set -euo pipefail

PROJECT="$HOME/ted-ai"
APP="$PROJECT/Ted.app"
ICON_TMP="$(mktemp -d)"

if [ ! -f "$PROJECT/hud.py" ]; then
    echo "error: $PROJECT/hud.py not found — is the project somewhere else?" >&2
    exit 1
fi

echo "Building Ted.app…"

# ── 1. Icon ───────────────────────────────────────────────────────────────────
# Built by tools/make_icon.py, which writes the .icns itself. This used to
# generate a PNG, resample it with `sips`, and package it with `iconutil` —
# that chain broke, and because the failure was caught and reduced to a one-line
# warning inside an otherwise-successful build, Ted.app ran for days wearing the
# generic blank-page icon with Contents/Resources/ completely empty.
#
# Nothing external is involved now, so there is nothing to be missing. If it
# fails anyway the build stops and says why, because a warning nobody reads is
# the same as no warning at all.

echo "  building icon…"
if ! /usr/bin/python3 "$PROJECT/tools/make_icon.py" "$ICON_TMP/Ted.icns"; then
    echo "error: could not build the icon — see the output above." >&2
    exit 1
fi

# ── 2. Bundle skeleton ────────────────────────────────────────────────────────
# Replace Contents, NOT the .app directory itself. `rm -rf "$APP"` gave the
# bundle a new inode on every build, which breaks anything holding a reference
# to the old one — a Dock tile, a Spotlight entry, a Login Item. Since Charlie
# launches Ted from the Dock, a rebuild was silently invalidating the very icon
# this script exists to produce. Keeping the outer directory keeps those
# references pointing at something real.
mkdir -p "$APP"
rm -rf "$APP/Contents"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ICON_TMP/Ted.icns" "$APP/Contents/Resources/Ted.icns"

# Trust nothing: confirm the icon is actually in the bundle rather than assuming
# the copy worked. This is the exact check whose absence hid the original bug.
if [ ! -s "$APP/Contents/Resources/Ted.icns" ]; then
    echo "error: Ted.icns did not land in the bundle." >&2
    exit 1
fi

# Ask macOS ITSELF whether it can read the file. Everything up to here was our
# own code agreeing with our own code; sips uses the system image decoders, so
# this is the first opinion that counts. If macOS can read it and Finder still
# shows a blank page, the file is fine and the problem is the icon cache —
# which is a completely different fix, and worth not guessing about.
if /usr/bin/sips -g pixelWidth -g pixelHeight \
        "$APP/Contents/Resources/Ted.icns" >/dev/null 2>&1; then
    echo "  icon installed and readable by macOS \
($(wc -c < "$APP/Contents/Resources/Ted.icns" | tr -d ' ') bytes)"
else
    echo "  warning: macOS could not read the generated .icns." >&2
    echo "           The bundle is still usable; the icon will be blank." >&2
fi

# ── 3. Info.plist ─────────────────────────────────────────────────────────────
# The usage-description strings matter: without NSMicrophoneUsageDescription
# macOS kills the process the moment it opens the mic, and without the Apple
# Events string the Calendar/Notes/Spotify/Messages AppleScript calls fail.
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                  <string>Ted</string>
    <key>CFBundleDisplayName</key>           <string>Ted</string>
    <key>CFBundleExecutable</key>            <string>Ted</string>
    <key>CFBundleIdentifier</key>            <string>com.charlierowenhorst.ted</string>
    <key>CFBundleIconFile</key>              <string>Ted</string>
    <key>CFBundlePackageType</key>           <string>APPL</string>
    <key>CFBundleShortVersionString</key>    <string>4.0</string>
    <key>CFBundleVersion</key>               <string>4</string>
    <key>NSHighResolutionCapable</key>       <true/>
    <key>LSMinimumSystemVersion</key>        <string>12.0</string>
    <key>NSMicrophoneUsageDescription</key>
        <string>Ted listens for your voice commands.</string>
    <key>NSSpeechRecognitionUsageDescription</key>
        <string>Ted transcribes what you say so he can respond.</string>
    <key>NSAppleEventsUsageDescription</key>
        <string>Ted controls Calendar, Notes, Messages and Spotify on your behalf.</string>
    <key>NSCalendarsUsageDescription</key>
        <string>Ted reads and creates calendar events when you ask.</string>
    <key>NSRemindersUsageDescription</key>
        <string>Ted reads and creates reminders when you ask.</string>
    <key>NSDesktopFolderUsageDescription</key>
        <string>Ted can take a screenshot to see what you are looking at.</string>
</dict>
</plist>
PLIST

# ── 4. Launcher ───────────────────────────────────────────────────────────────
cat > "$APP/Contents/MacOS/Ted" <<'LAUNCH'
#!/bin/bash
# Ted.app launcher — activates the venv and starts hud.py.
PROJECT="$HOME/ted-ai"
PY="$PROJECT/venv/bin/python"
LOG="$PROJECT/data/ted_launch.log"

die() {  # no terminal to print to, so use a real dialog
    /usr/bin/osascript -e "display alert \"Ted couldn't start\" message \"$1\" as critical" >/dev/null 2>&1
    exit 1
}

[ -x "$PY" ] || die "No virtualenv at ~/ted-ai/venv. Run: python3 -m venv venv && pip install -r requirements.txt"
[ -f "$PROJECT/config.py" ] || die "config.py is missing. Copy config.example.py to config.py and add your GROQ_API_KEY."

# Already running? Don't start a second one — two Teds fight over the microphone.
if /usr/bin/pgrep -f "$PROJECT/hud.py" >/dev/null 2>&1; then
    /usr/bin/osascript -e 'display notification "Ted is already running." with title "Ted"' >/dev/null 2>&1
    exit 0
fi

mkdir -p "$PROJECT/data"
# Keep the log from growing forever — last ~2000 lines is plenty to debug a crash.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exec >> "$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — launching Ted from Ted.app ==="

cd "$PROJECT" || die "Can't open ~/ted-ai"
"$PY" -u "$PROJECT/hud.py"
status=$?

# A non-zero exit with no window means something broke at import time; surface it.
if [ $status -ne 0 ]; then
    last=$(tail -n 12 "$LOG" | tr '"' "'" | tr '\n' ' ')
    /usr/bin/osascript -e "display alert \"Ted exited unexpectedly\" message \"$last\" as critical" >/dev/null 2>&1
fi
exit $status
LAUNCH

chmod +x "$APP/Contents/MacOS/Ted"

# ── 5. Make macOS actually show the icon ──────────────────────────────────────
# A `touch` alone is not enough and never was. macOS caches an app's icon per
# bundle, and Ted.app spent days with NO icon — so what is cached is "this app
# has no icon", and that entry survives the file appearing underneath it.
#
# Three steps, cheapest first. All are safe to re-run and none need sudo.
touch "$APP"

LSREG="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks"
LSREG="$LSREG/LaunchServices.framework/Versions/A/Support/lsregister"
if [ -x "$LSREG" ]; then
    "$LSREG" -f "$APP" >/dev/null 2>&1 || true
    echo "  re-registered with LaunchServices"
fi

# The Dock holds its own copy of the icon for anything pinned to it, and Finder
# holds another. Both relaunch immediately; this looks like a half-second
# flicker and is the step that actually makes the icon appear.
killall Dock   >/dev/null 2>&1 || true
killall Finder >/dev/null 2>&1 || true
echo "  refreshed Dock and Finder"

rm -rf "$ICON_TMP"

echo
echo "Built $APP"
echo
echo "Next:"
echo "  • Double-click Ted.app, or search 'Ted' in Spotlight."
echo "  • Drag it onto the Dock to keep it there."
echo "  • First launch will ask for Microphone access — allow it."
echo "  • If it starts and immediately quits, read: $PROJECT/data/ted_launch.log"
echo
echo "If the Dock tile is still blank, drag it off the Dock and drag Ted.app on"
echo "again — a tile pinned before this build points at the old bundle."
