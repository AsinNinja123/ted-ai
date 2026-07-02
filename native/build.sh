#!/bin/bash
# Build the native AEC audio engine (ted_audio).
# Requires Xcode command line tools:  xcode-select --install
set -e
cd "$(dirname "$0")"

echo "Building ted_audio (this needs Apple's Swift toolchain)…"

# The embedded Info.plist (with NSMicrophoneUsageDescription) is REQUIRED — a
# bare command-line binary cannot get a microphone permission prompt without it,
# so macOS would silently deny mic access and you'd get no audio / no orange dot.
swiftc -O ted_audio.swift -o ted_audio \
    -framework Foundation \
    -framework AVFoundation \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist

# Ad-hoc code-sign so macOS remembers the microphone grant across rebuilds.
codesign --force --sign - --identifier com.ted.audio ./ted_audio 2>/dev/null \
    && echo "(signed)" || echo "(codesign skipped — not fatal)"

echo "✅ Built: $(pwd)/ted_audio"
echo
echo "Next launch will ask for microphone permission — click Allow."
echo "If no prompt appears, open System Settings → Privacy & Security →"
echo "Microphone and enable it for your terminal app, then relaunch."
