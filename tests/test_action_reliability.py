"""Safe checks for verified app/browser action ground truth."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import actions, tool_handlers as th
from core.tools import TOOL_SCHEMAS


PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


print("— browser verification —")
original_run = th.subprocess.run
original_popen = th.subprocess.Popen
original_exists = th.os.path.exists
original_window_count = th._browser_window_count
original_tool_sleep = th.time.sleep
th.time.sleep = lambda _n: None
th.os.path.exists = lambda _path: True
calls = []


def successful_browser(argv, **kwargs):
    calls.append(argv)
    return SimpleNamespace(returncode=0, stdout="", stderr="")


th.subprocess.run = successful_browser
th.subprocess.Popen = lambda argv, **kwargs: calls.append(argv)
window_counts = iter([1, 1])
th._browser_window_count = lambda _app: next(window_counts, 1)
result = th.tool_browse_to("youtube")
check("YouTube automatically uses Charlie's Brave preference",
      calls and calls[0] == ["open", "-a", "Brave Browser", "https://youtube.com"])
check("default navigation reuses the existing window as a new tab",
      len(calls) == 1 and "--new-window" not in calls[0])
check("success is reported only after a real Brave window appears",
      result == "Opened Youtube in Brave Browser.")

calls.clear()
window_counts = iter([1, 1])
th._browser_window_count = lambda _app: next(window_counts, 1)
result = th.tool_browse_to("google docs")
check("every non-YouTube site defaults to Google Chrome",
      calls and calls[0] == ["open", "-a", "Google Chrome", "https://docs.google.com"]
      and result == "Opened Google Docs in Google Chrome.")

calls.clear()
window_counts = iter([1, 2])
th._browser_window_count = lambda _app: next(window_counts, 2)
result = th.tool_browse_to("youtube", "Brave", new_window=True)
check("an explicit new-window request uses exactly one Chromium launch",
      calls == [["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                 "--new-window", "https://youtube.com"]]
      and result == "Opened Youtube in Brave Browser.")

calls.clear()
window_counts = iter([0, 2])
th._browser_window_count = lambda _app: next(window_counts, 2)
result = th.tool_browse_to("youtube", "Brave")
check("a cold launch never calls two Brave windows success",
      "unexpectedly created 2 windows" in result and th.looks_like_failure(result))


th.subprocess.run = successful_browser
th.subprocess.Popen = lambda argv, **kwargs: None
th._browser_window_count = lambda _app: 0
result = th.tool_browse_to("youtube", "Brave")
check("background-only browser process is an honest, actionable failure",
      "couldn't verify" in result.lower() and "quit and reopen" in result.lower()
      and th.looks_like_failure(result))
th.subprocess.run = original_run
th.subprocess.Popen = original_popen
th.os.path.exists = original_exists
th._browser_window_count = original_window_count
th.time.sleep = original_tool_sleep

original_browse = th.tool_browse_to
web_routes = []
th.tool_browse_to = lambda site, browser=None: (
    web_routes.append((site, browser)), "verified website"
)[1]
check("open_app model mistakes for web services still use verified browsing",
      th.tool_open_app("youtube") == "verified website" and web_routes == [("youtube", None)])
th.tool_browse_to = original_browse


print("\n— YouTube outcome tool —")
check("YouTube playback has its own action contract",
      "play_youtube" in {s["function"]["name"] for s in TOOL_SCHEMAS}
      and "play_youtube" in th.ACTION_TOOLS)
real_find_video = th._youtube_first_video_id
real_browse = th.tool_browse_to
real_video_state = th._browser_video_state
real_sleep = th.time.sleep
try:
    th._youtube_first_video_id = lambda _query: ("dQw4w9WgXcQ", "")
    opened_urls = []
    th.tool_browse_to = lambda site, browser=None, new_window=False: (
        opened_urls.append((site, browser)), "Opened youtube.com in Brave Browser."
    )[1]
    th._browser_video_state = lambda _app: "playing"
    th.time.sleep = lambda _n: None
    result = th.tool_play_youtube("test video")
    check("YouTube tool opens a concrete autoplay watch URL",
          opened_urls == [("https://www.youtube.com/watch?v=dQw4w9WgXcQ&autoplay=1", "Brave")]
          and result == "Playing test video on YouTube in Brave Browser.")
finally:
    th._youtube_first_video_id = real_find_video
    th.tool_browse_to = real_browse
    th._browser_video_state = real_video_state
    th.time.sleep = real_sleep


print("\n— app launch verification —")
original_action_run = actions.subprocess.run
original_running = actions.get_running_apps
original_sleep = actions.time.sleep
actions.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0)
actions.time.sleep = lambda _n: None
actions.get_running_apps = lambda: []
result = actions.open_app("spotify")
check("Launch Services acknowledgement alone is not called success",
      "couldn't verify" in result.lower())
actions.get_running_apps = lambda: ["Spotify"]
check("a running process permits a verified success",
      actions.open_app("spotify") == "Opened Spotify.")
actions.subprocess.run = original_action_run
actions.get_running_apps = original_running
actions.time.sleep = original_sleep

print(f"\n{'=' * 50}\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
