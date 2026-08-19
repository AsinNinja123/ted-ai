"""Verified hierarchy of what is happening on Charlie's Mac.

This module deliberately reports only things macOS or a media API confirmed.
Conversation history is never treated as computer state: an old "Playing."
tool result must not make Ted claim that music is still playing five minutes
later.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 24 (§24.3)
# =============================================================================
#
#  WHAT THIS FILE IS
#      A verified picture of what is happening on your Mac right now: which apps are
#      open, what is playing, which browser tab is in front.
#
#      The word doing the work in that sentence is VERIFIED. This module reports only
#      things macOS or a media API confirmed just now.
#
#  THE RULE THIS FILE ENFORCES
#      Conversation history is never treated as computer state. An old "Playing."
#      tool result from five minutes ago must not let Ted claim music is still
#      playing. What Ted said happened and what is happening are two different
#      questions, and only one of them is answered by looking.
#
# =============================================================================

import json
import os
import re
import subprocess
import time
from urllib.parse import urlsplit

from core.actions import get_frontmost_app, get_running_apps, _osa


_remote_cache = {"ts": 0.0, "media": None, "checked": False}
_REMOTE_TTL = 8.0

# Sites whose pages are a video player, used only to attribute a confirmed
# browser playback to one tab. Being absent from this list never suppresses a
# playback report — it only downgrades it from "this tab" to "this browser".
_VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "netflix.com", "twitch.tv", "vimeo.com",
    "hulu.com", "disneyplus.com", "max.com", "primevideo.com", "peacocktv.com",
    "crunchyroll.com", "paramountplus.com", "tv.apple.com", "espn.com",
    "kick.com", "dailymotion.com", "plex.tv", "tiktok.com",
)


def _jxa_json(source, timeout=4):
    """Run read-only JavaScript for Automation and decode its JSON result."""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", source],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as exc:
        print("[state] app detail check:", exc)
    return []


def _browser_tabs(app_name):
    """Read tab titles/URLs from a scriptable browser without page screenshots."""
    app = json.dumps(app_name)
    if app_name == "Safari":
        source = f'''var a=Application({app}); JSON.stringify(a.windows().map((w,wi)=>{{
          var current=""; try{{ current=w.currentTab().url(); }}catch(e){{}}
          return {{window:wi+1,tabs:w.tabs().map((t,ti)=>({{
            title:t.name(),url:t.url(),active:t.url()===current
          }}))}};
        }}))'''
    else:
        source = f'''var a=Application({app}); JSON.stringify(a.windows().map((w,wi)=>{{
          var active=0; try{{ active=w.activeTabIndex(); }}catch(e){{}}
          return {{window:wi+1,tabs:w.tabs().map((t,ti)=>({{
            title:t.title(),url:t.url(),active:ti+1===active
          }}))}};
        }}))'''
    # Browsers can be "running" with no responsive UI process (especially
    # Safari after its last window closes). Keep the entire live-state refresh
    # inside the five-second HUD cadence instead of waiting four seconds for
    # one stale browser.
    windows = _jxa_json(source, timeout=2)
    tabs = []
    for window in windows or []:
        for tab in window.get("tabs") or []:
            url = tab.get("url") or ""
            host = urlsplit(url).hostname or ""
            tabs.append({
                "title": (tab.get("title") or host or "Untitled tab")[:180],
                "url": url[:500],
                "host": host.removeprefix("www."),
                "active": bool(tab.get("active")),
                "window": int(window.get("window", 1) or 1),
            })
    return tabs[:80], len(windows or [])


def _visible_windows(apps):
    """Return real layer-zero window titles grouped by owning application."""
    grouped = {app: [] for app in apps}
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
        for window in windows:
            owner = window.get(Quartz.kCGWindowOwnerName) or ""
            if owner not in grouped:
                continue
            bounds = window.get(Quartz.kCGWindowBounds) or {}
            if (int(window.get(Quartz.kCGWindowLayer, 0) or 0) != 0
                    or not bool(window.get(Quartz.kCGWindowIsOnscreen, False))
                    or float(window.get(Quartz.kCGWindowAlpha, 1) or 0) <= 0
                    or float(bounds.get("Width", 0) or 0) < 180
                    or float(bounds.get("Height", 0) or 0) < 100):
                continue
            title = (window.get(Quartz.kCGWindowName) or "").strip()
            item = {"title": title[:220] or f"{owner} window"}
            if item not in grouped[owner]:
                grouped[owner].append(item)
    except Exception as exc:
        print("[state] window check:", exc)
    return grouped


def _process_table():
    rows = {}
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,tty=,state=,comm="],
            capture_output=True, text=True, timeout=4)
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 4)
            if len(parts) == 5:
                pid, ppid, tty, state, command = parts
                rows[int(pid)] = {
                    "pid": int(pid), "ppid": int(ppid), "tty": tty,
                    "state": state, "command": command,
                }
    except Exception:
        pass
    return rows


def _gui_pids(apps):
    mapping = {}
    try:
        from AppKit import NSWorkspace
        for running in NSWorkspace.sharedWorkspace().runningApplications():
            name = running.localizedName()
            if name in apps:
                mapping[int(running.processIdentifier())] = name
    except Exception:
        pass
    return mapping


def _owner_app(pid, processes, gui_pids):
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        if pid in gui_pids:
            return gui_pids[pid]
        pid = (processes.get(pid) or {}).get("ppid", 0)
    return ""


def _cwd_for(pid):
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=3)
        for line in result.stdout.splitlines():
            if line.startswith("n/"):
                return line[1:]
    except Exception:
        pass
    return ""


def _git_branch(cwd):
    if not cwd:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=2)
        return result.stdout.strip()[:120] if result.returncode == 0 else ""
    except Exception:
        return ""


def _terminal_sessions(apps):
    """Find interactive shells, including terminals embedded in ChatGPT/Codex."""
    processes = _process_table()
    gui_pids = _gui_pids(apps)
    shell_names = {"zsh", "bash", "fish", "sh", "nu"}
    by_tty = {}
    for proc in processes.values():
        tty = proc["tty"]
        command = os.path.basename(proc["command"])
        if tty in {"??", "?"} or command not in shell_names:
            continue
        # Keep the shell nearest the owning GUI app; nested shells on the same
        # tty are implementation detail, not additional terminal tabs.
        current = by_tty.get(tty)
        if current is None or proc["pid"] < current["pid"]:
            by_tty[tty] = proc
    sessions = {}
    for tty, proc in by_tty.items():
        owner = _owner_app(proc["pid"], processes, gui_pids)
        if not owner:
            continue
        cwd = _cwd_for(proc["pid"])
        sessions.setdefault(owner, []).append({
            "tty": tty,
            "shell": os.path.basename(proc["command"]),
            "cwd": cwd,
            "folder": os.path.basename(cwd.rstrip("/")) if cwd else "",
            "branch": _git_branch(cwd),
            "busy": not proc["state"].startswith("S"),
        })
    return sessions


def _app_details(apps):
    windows = _visible_windows(apps)
    terminals = _terminal_sessions(apps)
    details = {}
    for app in apps:
        detail = {"kind": "app", "windows": windows.get(app, [])}
        if app in {"Google Chrome", "Brave Browser", "Safari"}:
            tabs, window_count = _browser_tabs(app)
            detail.update(kind="browser", tabs=tabs, window_count=window_count)
        if terminals.get(app):
            detail["terminals"] = terminals[app]
            if detail["kind"] == "app":
                detail["kind"] = "terminal"
        details[app] = detail
    return details


_ASSERTION_LINE = re.compile(
    r"^\s*pid (\d+)\((.+?)\):\s*\[0x[0-9a-fA-F]+\]\s+\S+\s+(\w+)\s+named:\s*\"(.*?)\"")


def _media_assertions():
    """Ask macOS which processes are currently holding playback awake.

    A Chromium or WebKit browser takes a NoDisplaySleepAssertion named
    "Video Wake Lock" for exactly as long as a <video> element is playing, and
    a NoIdleSleepAssertion named "Playing audio" for anything audible. Both
    disappear the moment playback pauses.

    This is the operating system's own record rather than a guess read off a
    tab title. It costs one cheap subprocess, needs no permission, and needs no
    browser security setting relaxed — unlike executing JavaScript through
    Apple Events, which Chromium disables by default and which is why Ted could
    not answer this question before.

    The record is per-process, not per-tab: it settles "is this browser playing
    a video" exactly, and "which tab" only in combination with the tab list.
    """
    playing = {}
    try:
        result = subprocess.run(
            ["pmset", "-g", "assertions"],
            capture_output=True, text=True, timeout=4)
    except Exception as exc:
        print("[state] playback assertion check:", exc)
        return playing
    if result.returncode != 0:
        return playing
    for line in result.stdout.splitlines():
        match = _ASSERTION_LINE.match(line)
        if not match:
            continue
        pid, _owner, kind, name = match.groups()
        label = name.strip().lower()
        entry = playing.setdefault(int(pid), {"video": False, "audio": False})
        if kind == "NoDisplaySleepAssertion" and "video wake lock" in label:
            entry["video"] = True
        elif kind == "NoIdleSleepAssertion" and "playing audio" in label:
            entry["audio"] = True
    return {pid: flags for pid, flags in playing.items()
            if flags["video"] or flags["audio"]}


def _browser_playback(apps, details):
    """Name what a browser is confirmed to be playing, and how sure that is.

    Returns None unless macOS confirmed playback. When it did, a tab is named
    only where the evidence supports naming one — a single open tab is certain,
    an active tab on a video site is likely, and anything else names the browser
    and offers the video tabs as candidates. Reporting the shape of the evidence
    is the point: Ted may say "Brave is playing something" without inventing
    which of eleven tabs it is.
    """
    assertions = _media_assertions()
    if not assertions:
        return None
    by_pid = _gui_pids(apps)
    for pid, flags in assertions.items():
        app = by_pid.get(pid)
        if not app:
            continue
        detail = details.get(app) or {}
        if detail.get("kind") != "browser":
            continue
        tabs = detail.get("tabs") or []
        active = next((t for t in tabs if t.get("active")), None)
        candidates = [t for t in tabs
                      if any(host in (t.get("host") or "") for host in _VIDEO_HOSTS)]
        tab, confidence = None, "browser"
        if len(tabs) == 1:
            tab, confidence = tabs[0], "certain"
        elif active and any(host in (active.get("host") or "")
                            for host in _VIDEO_HOSTS):
            tab, confidence = active, "likely"
        elif len(candidates) == 1:
            tab, confidence = candidates[0], "likely"
        return {
            "app": app,
            "kind": "video" if flags["video"] else "audio",
            "title": (tab or {}).get("title", ""),
            "host": (tab or {}).get("host", ""),
            "url": (tab or {}).get("url", ""),
            "confidence": confidence,
            "candidates": [t.get("title", "") for t in candidates[:4]],
        }
    return None


def _apple_music(apps):
    if "Music" not in apps:
        return None
    if _osa('tell application "Music" to player state') != "playing":
        return None
    title = _osa('tell application "Music" to name of current track')
    artist = _osa('tell application "Music" to artist of current track')
    if not title:
        return None
    return {"source": "Apple Music", "title": title, "artist": artist}


def _local_spotify(apps):
    if "Spotify" not in apps:
        return None
    if _osa('tell application "Spotify" to player state') != "playing":
        return None
    title = _osa('tell application "Spotify" to name of current track')
    artist = _osa('tell application "Spotify" to artist of current track')
    if not title:
        return None
    return {"source": "Spotify on this Mac", "title": title, "artist": artist}


def _remote_spotify():
    """Return confirmed Spotify playback on another device, with a short cache."""
    now = time.time()
    if now - _remote_cache["ts"] < _REMOTE_TTL:
        return _remote_cache["media"], _remote_cache["checked"]
    media, checked = None, False
    try:
        from core import spotify_web
        if spotify_web.enabled():
            sp = spotify_web._client()
            if sp is not None:
                state = sp.current_playback()
                checked = True
                if state and state.get("is_playing") and state.get("item"):
                    item = state["item"]
                    artists = ", ".join(
                        a.get("name", "") for a in item.get("artists", [])
                        if a.get("name"))
                    media = {
                        "source": "Spotify",
                        "title": item.get("name", "Unknown track"),
                        "artist": artists,
                        "device": (state.get("device") or {}).get("name", ""),
                    }
    except Exception as exc:
        print("[state] Spotify playback check:", exc)
    _remote_cache.update(ts=now, media=media, checked=checked)
    return media, checked


def _front_window(app):
    if not app:
        return ""
    safe = app.replace('"', '\\"')
    return _osa(
        f'tell application "System Events" to tell process "{safe}" '
        'to get name of front window')


def collect(apps=None, include_remote=True):
    """Collect visible apps, their children, focus, and verified media."""
    apps = list(apps if apps is not None else get_running_apps())
    frontmost = get_frontmost_app()
    details = _app_details(apps)
    media = _local_spotify(apps) or _apple_music(apps)
    remote_checked = False
    if media is None and include_remote:
        media, remote_checked = _remote_spotify()
    return {
        "captured_at": time.time(),
        "apps": apps,
        "frontmost": frontmost,
        "front_window": _front_window(frontmost),
        "details": details,
        "media": media,
        "media_scope": "Spotify, Apple Music, and browser video",
        "browser_media": _browser_playback(apps, details),
        "remote_spotify_checked": remote_checked,
    }


def format_for_prompt(state):
    """Compact, explicit grounding text injected into the current model turn."""
    state = state or {}
    apps = state.get("apps") or []
    frontmost = state.get("frontmost") or "unknown"
    window = state.get("front_window") or ""
    age = max(0, int(time.time() - state.get("captured_at", 0)))
    focus = frontmost + (f" — {window}" if window and window != frontmost else "")
    media = state.get("media")
    if media:
        label = media.get("title", "Unknown track")
        if media.get("artist"):
            label += " — " + media["artist"]
        if media.get("device"):
            label += " on " + media["device"]
        media_line = f"Verified playing: {label} ({media.get('source', 'media')})."
    else:
        media_line = ("Verified media: nothing playing in Spotify or Apple Music. "
                      "Do not claim a song is playing from conversation history.")
    # Browser playback is a separate, independently verified fact: Spotify can be
    # silent while a YouTube tab plays. macOS confirms the browser; the tab name
    # is attributed only as far as the evidence goes, and the confidence word is
    # kept in the sentence so Ted repeats the uncertainty instead of dropping it.
    browser = state.get("browser_media")
    if browser:
        what = "a video" if browser.get("kind") == "video" else "audio"
        app = browser.get("app", "A browser")
        if browser.get("confidence") in ("certain", "likely") and browser.get("title"):
            hedge = "" if browser["confidence"] == "certain" else " (its active tab, most likely this one)"
            media_line += (f" macOS confirms {app} is playing {what}: "
                           f"\"{browser['title']}\"{hedge}.")
        else:
            extra = ""
            if browser.get("candidates"):
                extra = " Video tabs open: " + " | ".join(browser["candidates"]) + "."
            media_line += (f" macOS confirms {app} is playing {what}, but not which "
                           f"tab.{extra}")
    else:
        media_line += (" No browser is playing video or audio either — macOS "
                       "reports no playback wake lock from any browser.")
    app_line = ", ".join(apps) if apps else "none detected"
    detail_parts = []
    for app, detail in (state.get("details") or {}).items():
        tabs = detail.get("tabs") or []
        if tabs:
            labels = []
            for tab in tabs[:16]:
                marker = "active: " if tab.get("active") else ""
                host = f" [{tab['host']}]" if tab.get("host") else ""
                labels.append(f"{marker}{tab.get('title', 'Untitled')}{host}")
            detail_parts.append(f"{app} tabs ({len(tabs)}): " + " | ".join(labels))
        sessions = detail.get("terminals") or []
        if sessions:
            labels = []
            for session in sessions[:10]:
                label = session.get("folder") or session.get("tty") or "shell"
                if session.get("branch"):
                    label += f" branch {session['branch']}"
                labels.append(label)
            detail_parts.append(f"{app} terminal sessions ({len(sessions)}): "
                                + " | ".join(labels))
        if not tabs and not sessions:
            titles = [w.get("title", "") for w in detail.get("windows", [])
                      if w.get("title")]
            if titles:
                detail_parts.append(f"{app} windows: " + " | ".join(titles[:5]))
    hierarchy = (" Open hierarchy: " + " || ".join(detail_parts)) if detail_parts else ""
    return (
        f"LIVE MAC STATE ({age}s old): visible open apps: {app_line}. "
        f"Frontmost: {focus}. {media_line}{hierarchy}")
