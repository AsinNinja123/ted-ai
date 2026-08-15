"""Verified hierarchy of what is happening on Charlie's Mac.

This module deliberately reports only things macOS or a media API confirmed.
Conversation history is never treated as computer state: an old "Playing."
tool result must not make Ted claim that music is still playing five minutes
later.
"""

import json
import os
import subprocess
import time
from urllib.parse import urlsplit

from core.actions import get_frontmost_app, get_running_apps, _osa


_remote_cache = {"ts": 0.0, "media": None, "checked": False}
_REMOTE_TTL = 8.0


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
    media = _local_spotify(apps) or _apple_music(apps)
    remote_checked = False
    if media is None and include_remote:
        media, remote_checked = _remote_spotify()
    return {
        "captured_at": time.time(),
        "apps": apps,
        "frontmost": frontmost,
        "front_window": _front_window(frontmost),
        "details": _app_details(apps),
        "media": media,
        "media_scope": "Spotify and Apple Music",
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
        media_line = (
            "Verified media: nothing playing in Spotify or Apple Music. "
            "Browser audio is unknown until the browser accessibility tree is inspected. "
            "Do not claim a song is playing from conversation history.")
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
