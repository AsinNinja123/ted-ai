"""Live Mac state must override stale conversational claims."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import system_state
from dashboard.app import _neutral_chat_label, _neutral_chat_row


PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


print("— model grounding —")
quiet = {
    "captured_at": time.time(),
    "apps": ["Finder", "Brave Browser"],
    "frontmost": "Brave Browser",
    "front_window": "YouTube",
    "media": None,
    "details": {
        "Brave Browser": {"kind": "browser", "tabs": [
            {"title": "YouTube", "host": "youtube.com", "active": True}]},
        "ChatGPT": {"kind": "terminal", "terminals": [
            {"folder": "ted-ai", "branch": "arch/single-call"}]},
    },
}
prompt = system_state.format_for_prompt(quiet)
check("all visible apps and focused window reach the model",
      "Finder, Brave Browser" in prompt and "Brave Browser — YouTube" in prompt)
check("no playback becomes an explicit anti-hallucination fact",
      "nothing playing" in prompt and "Do not claim" in prompt)
check("silence from every browser is stated as a fact, not a gap",
      "No browser is playing" in prompt and "wake lock" in prompt)
check("browser tabs and terminal Git branches reach the model",
      "YouTube [youtube.com]" in prompt
      and "ted-ai branch arch/single-call" in prompt)

# macOS holds a "Video Wake Lock" power assertion for exactly as long as a
# video plays. That is the evidence; these checks pin how far Ted is allowed to
# read it. A tab may only be named when the evidence names one.
certain = dict(quiet, browser_media={
    "app": "Brave Browser", "kind": "video", "confidence": "certain",
    "title": "South Park retrospective", "host": "youtube.com",
    "url": "https://youtube.com/watch?v=x", "candidates": []})
prompt = system_state.format_for_prompt(certain)
check("a confirmed video in the only open tab is named outright",
      "Brave Browser is playing a video" in prompt
      and "South Park retrospective" in prompt
      and "most likely" not in prompt)

likely = dict(certain, browser_media=dict(certain["browser_media"],
                                          confidence="likely"))
prompt = system_state.format_for_prompt(likely)
check("an inferred tab keeps its hedge in the sentence",
      "South Park retrospective" in prompt and "most likely" in prompt)

vague = dict(quiet, browser_media={
    "app": "Google Chrome", "kind": "audio", "confidence": "browser",
    "title": "", "host": "", "url": "",
    "candidates": ["Lecture 4", "Study beats"]})
prompt = system_state.format_for_prompt(vague)
check("playback with no attributable tab says so and lists candidates",
      "Google Chrome is playing audio" in prompt
      and "not which tab" in prompt
      and "Lecture 4" in prompt)

print("\n— playback assertions —")
# Captured verbatim from `pmset -g assertions` on this Mac while a YouTube
# video played in Brave. If Apple changes this format the parser must fail
# loudly here rather than quietly start reporting silence.
_PMSET = '''Assertion status system-wide:
   PreventUserIdleDisplaySleep    1
Listed by owning process:
   pid 179(coreaudiod): [0x0002253500018ba1] 00:00:18 PreventUserIdleSystemSleep named: "com.apple.audio.ctx"
   pid 65704(Brave Browser): [0x0002253500018c9f] 00:00:18 NoIdleSleepAssertion named: "Playing audio"
   pid 65704(Brave Browser): [0x0002253500058c9e] 00:00:18 NoDisplaySleepAssertion named: "Video Wake Lock"
   pid 36147(Claude): [0x0001e14c000190cc] 06:46:05 NoIdleSleepAssertion named: "Electron"
'''


class _FakeRun:
    returncode = 0
    stdout = _PMSET


_real_run = system_state.subprocess.run
system_state.subprocess.run = lambda *a, **k: _FakeRun()
try:
    found = system_state._media_assertions()
finally:
    system_state.subprocess.run = _real_run

check("a playing browser is picked out of pmset by pid",
      found.get(65704) == {"video": True, "audio": True})
check("an Electron idle assertion is not mistaken for playback",
      36147 not in found)
check("unrelated system assertions are ignored", 179 not in found)

playing = dict(quiet, media={
    "source": "Spotify", "title": "Maine", "artist": "Noah Kahan",
    "device": "Charlie's MacBook"})
prompt = system_state.format_for_prompt(playing)
check("verified track, artist, source, and device are named",
      all(x in prompt for x in ("Maine", "Noah Kahan", "Spotify", "Charlie's MacBook")))


print("\n— neutral chat labels —")
check("Ted helps narration is stripped",
      _neutral_chat_label("Ted helps with digital tasks") == "digital tasks")
check("user asks narration is stripped",
      _neutral_chat_label("The user asks about browser control") == "browser control")
check("ordinary labels remain untouched",
      _neutral_chat_label("Browser and Screen Control") == "Browser and Screen Control")
old = _neutral_chat_row({
    "title": "Digital Task Requests",
    "summary": "Ted helps with digital tasks and browser control"})
check("old narrated sidebar summaries display as a neutral label",
      old["summary"] == "Digital Task Requests")

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
hud_source = open(os.path.join(root, "hud.py"), encoding="utf-8").read()
check("optional shutdown summaries cannot freeze Ted indefinitely",
      'saver.join(timeout=15.0)' in hud_source and 'saver.is_alive()' in hud_source)
launcher = open(os.path.join(root, "native", "ted_launcher.swift"), encoding="utf-8").read()
build = open(os.path.join(root, "tools", "make_app.sh"), encoding="utf-8").read()
ui_source = open(os.path.join(root, "ui", "ted_hud.html"), encoding="utf-8").read()
app_source = open(os.path.join(root, "core", "app.py"), encoding="utf-8").read()
check("a native Ted host owns the Dock identity instead of framework Python",
      'app.setActivationPolicy(.regular)' in launcher
      and 'TED_NATIVE_HOST' in launcher
      and 'native/ted_launcher.swift' in build
      and 'NSApplicationActivationPolicyAccessory' in hud_source)
check("the native Dock tile can raise the accessory Ted window",
      'SIGUSR1' in launcher and 'SIGUSR1' in hud_source
      and 'orderFrontRegardless' in hud_source)
# The Dock host has always sent SIGUSR1 on reopen; hud.py handled it with
# signal.signal(), and Python runs those handlers only when the MAIN thread
# executes bytecode. webview.start() gives the main thread to AppKit's run loop
# and never takes it back, so the signal arrived and the interpreter dropped
# it. Verified against a standalone AppKit run loop: signal.signal never fires,
# a blocked signal consumed by sigwait on its own thread always does.
check("…via a sigwait thread, because a signal.signal handler never runs "
      "under AppKit's run loop",
      'sigwait' in hud_source and 'pthread_sigmask' in hud_source
      and 'signal.signal(signal.SIGUSR1, _raise_ted_window)' not in hud_source)
check("…with SIGUSR1 blocked before any thread can take its default action",
      hud_source.index('pthread_sigmask') < hud_source.index('import webview'))
check("…and a miniaturized window is asked to leave the Dock explicitly",
      'isMiniaturized' in hud_source and 'deminiaturize_' in hud_source)
check("the right rail receives and renders the full computer hierarchy",
      'setComputerState:function(state)' in ui_source
      and "term.branch" in ui_source and "tab.host" in ui_source
      and 'setComputerState' in app_source)


print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
