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

# PkgInfo. Eight bytes, technically optional since forever, and its absence is
# the classic reason a structurally-correct bundle is not treated as an
# application — Finder falls back to opening Contents/MacOS/Ted as a document,
# which is why double-clicking Ted.app was launching Script Editor instead of
# Ted. Costs nothing to write and removes the ambiguity.
printf 'APPL????' > "$APP/Contents/PkgInfo"

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
    <key>NSScreenCaptureUsageDescription</key>
        <string>Ted uses screenshots to understand and verify screen actions you request.</string>
</dict>
</plist>
PLIST

# ── 4. Native launcher ────────────────────────────────────────────────────────
# A script that execs framework Python leaves the visible AppKit process named
# Python, so the Dock puts its running dot under Python's icon. Keep a real Ted
# process alive as the app host and run Python as its accessory child instead.
# The same signed executable also owns Accessibility control. macOS permission
# is therefore granted to the Ted.app the user selected, not to an ad-hoc helper
# elsewhere in the repository.
echo "  building native app host…"
swiftc -O -parse-as-library \
    "$PROJECT/native/ted_launcher.swift" \
    "$PROJECT/native/ted_control.swift" \
    -o "$APP/Contents/MacOS/Ted" \
    -framework Foundation -framework AppKit -framework ApplicationServices
# An ordinary ad-hoc signature identifies each rebuild by its changing code
# hash. TCC then leaves Ted visibly checked in Accessibility while rejecting
# the new binary. Give development builds one explicit designated requirement
# so macOS recognizes later rebuilds as the same local app. This Mac has no
# Apple Development signing identity; replace `-` with one if that changes.
codesign --force --sign - --identifier com.charlierowenhorst.ted \
    --requirements '=designated => identifier "com.charlierowenhorst.ted"' \
    "$APP"
codesign --verify --strict "$APP"

# Confirm macOS agrees this is an application before claiming success. Opening
# the bundle in Script Editor is what "not an application" looks like from the
# outside, and the build should notice that rather than the user.
if /usr/bin/mdls -name kMDItemContentType "$APP" 2>/dev/null \
        | grep -q "com.apple.application-bundle"; then
    echo "  macOS recognises Ted.app as an application"
else
    echo "  note: Spotlight has not indexed the bundle yet — if double-clicking"
    echo "        opens the wrong app, log out and back in once."
fi

# ── 5. Make macOS actually show the icon ──────────────────────────────────────
# A `touch` alone is not enough and never was. macOS caches an app's icon per
# bundle, and Ted.app spent days with NO icon — so what is cached is "this app
# has no icon", and that entry survives the file appearing underneath it.
#
# Three steps, cheapest first. All are safe to re-run and none need sudo.
touch "$APP"

# Strip quarantine and any stale Finder metadata. A bundle rebuilt underneath
# a registration macOS already holds is exactly the state that produces
# "opens in the wrong app".
/usr/bin/xattr -cr "$APP" >/dev/null 2>&1 || true

LSREG="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks"
LSREG="$LSREG/LaunchServices.framework/Versions/A/Support/lsregister"
if [ -x "$LSREG" ]; then
    # Unregister BEFORE registering. `-f` alone updates a record that may
    # already describe this path as something other than an application, and
    # updating a wrong record leaves it wrong.
    "$LSREG" -u "$APP" >/dev/null 2>&1 || true
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
