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
ICONSET="$(mktemp -d)/Ted.iconset"

if [ ! -f "$PROJECT/hud.py" ]; then
    echo "error: $PROJECT/hud.py not found — is the project somewhere else?" >&2
    exit 1
fi

echo "Building Ted.app…"

# ── 1. Icon ───────────────────────────────────────────────────────────────────
# Generated with pure stdlib Python (no Pillow) so this has no dependencies:
# a dark squircle with Ted's green orb, matching the HUD sphere.
mkdir -p "$ICONSET"
BASE_PNG="$ICONSET/../base.png"

/usr/bin/python3 - "$BASE_PNG" <<'PY'
import math, struct, sys, zlib

S = 1024
OUT = sys.argv[1]

def smoothstep(e0, e1, x):
    if e1 == e0:
        return 0.0 if x < e0 else 1.0
    t = (x - e0) / (e1 - e0)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return t * t * (3 - 2 * t)

def squircle(dx, dy, half, r):
    """Signed distance to a rounded square centred at 0. Negative = inside."""
    qx, qy = abs(dx) - (half - r), abs(dy) - (half - r)
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ox, oy) + min(max(qx, qy), 0.0) - r

AA   = S / 512.0          # antialias width, scales with size
HALF = S / 2.0
CORNER = S * 0.2237       # macOS-ish corner radius
R_RING = S * 0.300        # orb radius
W_RING = S * 0.026        # ring thickness

# HUD palette
BG_TOP, BG_BOT = (0x12, 0x17, 0x20), (0x07, 0x0A, 0x0F)
GREEN          = (0x3E, 0xCF, 0x8E)

rows = []
for y in range(S):
    row = bytearray([0])                     # PNG filter byte: none
    dy = y - HALF + 0.5
    fy = y / (S - 1)
    bg = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * fy) for i in range(3))
    for x in range(S):
        dx = x - HALF + 0.5

        # Background plate
        inside = 1.0 - smoothstep(-AA, AA, squircle(dx, dy, HALF, CORNER))
        if inside <= 0.001:
            row += b'\x00\x00\x00\x00'
            continue
        r, g, b = bg

        dist = math.hypot(dx, dy)

        # Soft outer glow
        glow = (1.0 - smoothstep(0.0, R_RING * 1.75, dist)) ** 2.4 * 0.42
        # Faint filled orb so it reads as a sphere, not a hoop
        body = (1.0 - smoothstep(0.0, R_RING, dist)) * 0.16
        # Equator ellipse — the "sphere" cue that survives 16px
        ed = (math.hypot(dx / R_RING, dy / (R_RING * 0.34)) - 1.0) * R_RING * 0.34
        equator = 1.0 - smoothstep(W_RING * 0.42, W_RING * 0.42 + AA * 1.6, abs(ed))
        # The ring itself
        ring = 1.0 - smoothstep(W_RING * 0.5, W_RING * 0.5 + AA * 1.4, abs(dist - R_RING))

        lit = min(1.0, glow + body + equator * 0.70 + ring)
        if lit > 0:
            r = int(r + (GREEN[0] - r) * lit)
            g = int(g + (GREEN[1] - g) * lit)
            b = int(b + (GREEN[2] - b) * lit)

        a = int(255 * inside)
        row += bytes((min(r, 255), min(g, 255), min(b, 255), a))
    rows.append(bytes(row))

def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

png = (b'\x89PNG\r\n\x1a\n'
       + chunk(b'IHDR', struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(b''.join(rows), 9))
       + chunk(b'IEND', b''))
with open(OUT, "wb") as f:
    f.write(png)
print(f"  icon rendered ({S}x{S})")
PY

# iconutil wants these exact filenames.
for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" \
            "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" \
            "512 512x512" "1024 512x512@2x"; do
    set -- $spec
    /usr/bin/sips -z "$1" "$1" "$BASE_PNG" --out "$ICONSET/icon_$2.png" >/dev/null 2>&1
done

# ── 2. Bundle skeleton ────────────────────────────────────────────────────────
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
if ! /usr/bin/iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Ted.icns"; then
    echo "  warning: macOS rejected the generated icon; building Ted.app without it"
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

# Make Finder pick up the new icon immediately instead of caching the generic one.
touch "$APP"
rm -rf "$(dirname "$ICONSET")"

echo
echo "Built $APP"
echo
echo "Next:"
echo "  • Double-click Ted.app, or search 'Ted' in Spotlight."
echo "  • Drag it onto the Dock to keep it there."
echo "  • First launch will ask for Microphone access — allow it."
echo "  • If it starts and immediately quits, read: $PROJECT/data/ted_launch.log"
