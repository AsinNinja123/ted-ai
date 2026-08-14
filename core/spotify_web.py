"""
core/spotify_web.py — Spotify Web API control (playlists + song search).

Local AppleScript (core/actions.py) handles instant transport — play / pause /
skip. THIS module handles the things that need your account: starting a specific
playlist or searching for a song. It uses spotipy for OAuth + automatic token
refresh, and degrades to a no-op (never crashes Ted) when it isn't set up.

ONE-TIME SETUP
  1. pip install spotipy
  2. Create a free app at https://developer.spotify.com/dashboard
       • Add this Redirect URI exactly:  http://localhost:8888/callback
       • Copy the Client ID and Client Secret into config.py
  3. Run:  python authorize_spotify.py
       • A browser opens; log in and click Agree. A token is cached to
         data/.spotify_cache and refreshed automatically from then on.
  4. Launch Ted. "Play my workout playlist" now works.

Needs Spotify Premium (you have it) and the Spotify app open on some device.
"""

import os
import re
import time

HOME = os.path.expanduser("~/ted-ai")
CACHE = os.path.join(HOME, "data", ".spotify_cache")

# Permissions requested during OAuth. Covers reading playback state, controlling
# playback, reading private/collaborative playlists, and seeing currently playing.
SCOPE = ("user-read-playback-state user-modify-playback-state "
         "playlist-read-private playlist-read-collaborative user-read-currently-playing")

try:
    from config import (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
                        SPOTIFY_REDIRECT_URI)
except Exception:
    SPOTIFY_CLIENT_ID = SPOTIFY_CLIENT_SECRET = ""
    SPOTIFY_REDIRECT_URI = "http://localhost:8888/callback"

# Module-level singleton so every call within a session reuses the same
# authenticated client instead of re-initialising spotipy from scratch.
_sp = None
_playlists = None        # cached [(name, uri), ...] for the session
_last_auth_check = 0.0
_AUTH_CHECK_INTERVAL = 60.0  # only ping /me once per minute, not on every command


def configured():
    """Return True when Spotify credentials are present in config.py."""
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)


def _authorized():
    """Return True when a non-empty OAuth token cache file exists on disk."""
    return os.path.exists(CACHE) and os.path.getsize(CACHE) > 0


def enabled():
    """True only when credentials are set AND a token has been cached."""
    return configured() and _authorized()


# ---- Client ----

def _client(interactive=False):
    """Return the module-level spotipy client, creating it on first call.

    Auth-check rate limiting: /me is called at most once per 60 s to verify
    the token is still valid. Calling it on every command would add noticeable
    latency. If auth has expired and refresh failed, _sp is reset to None so
    the next call retries cleanly instead of staying stuck on a broken client.

    With interactive=False (default) the function refuses to open a browser —
    it only succeeds if a token is already cached.
    """
    global _sp, _last_auth_check
    if _sp is not None:
        now = time.time()
        if now - _last_auth_check >= _AUTH_CHECK_INTERVAL:
            try:
                import spotipy
                _sp.current_user()          # lightweight auth probe
                _last_auth_check = now
            except spotipy.SpotifyException:
                print("[spotify] auth expired — resetting client for re-auth")
                _sp = None
                _last_auth_check = 0.0
            except Exception:
                pass   # network blip — keep the client, let the actual call fail
    if _sp is not None:
        return _sp
    if not configured():
        return None
    if not interactive and not _authorized():
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
        auth = SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope=SCOPE,
            cache_path=CACHE,
            open_browser=interactive,
        )
        _sp = spotipy.Spotify(auth_manager=auth, requests_timeout=10)
        return _sp
    except Exception as e:
        print("[spotify] client init failed:", e)
        return None


# ---- Helpers ----

def _device_id(sp):
    """Pick a playback device — the active one if any, else the first available."""
    try:
        devices = sp.devices().get("devices", [])
    except Exception as e:
        print("[spotify] devices:", e)
        return None
    if not devices:
        return None
    for d in devices:
        if d.get("is_active"):
            return d["id"]
    return devices[0]["id"]


def _device_id_or_launch(sp, timeout=12.0):
    """Return a playback device id, launching the local Spotify app if none exists.

    'Play shake it off' with Spotify closed used to fail with 'open Spotify
    first' — instead, open the desktop app ourselves and poll until it registers
    as a Web API device (usually 3-8 s after launch)."""
    dev = _device_id(sp)
    if dev:
        return dev
    try:
        from core.actions import ensure_spotify_open
        if not ensure_spotify_open():
            return None
    except Exception as e:
        print("[spotify] auto-launch failed:", e)
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev = _device_id(sp)
        if dev:
            return dev
        time.sleep(1.0)
    return None


def _get_playlists(sp, refresh=False):
    """Return the user's playlists as [(name, uri), ...], paging through all results.

    Results are cached in _playlists for the session to avoid repeated API calls.
    Pass refresh=True to force a re-fetch (used when a playlist isn't found on
    first match, in case it was recently created).
    """
    global _playlists
    if _playlists is not None and not refresh:
        return _playlists
    items = []
    try:
        res = sp.current_user_playlists(limit=50)
        while res:                                   # follow pagination cursors
            for p in res.get("items", []):
                if p and p.get("name") and p.get("uri"):
                    items.append((p["name"], p["uri"]))
            res = sp.next(res) if res.get("next") else None
    except Exception as e:
        print("[spotify] playlist fetch:", e)
    _playlists = items
    return items


def match_playlist(name, playlists):
    """Fuzzy-match a spoken playlist name to one the user owns.

    Returns the first (name, uri) tuple that matches, or None.
    """
    q = (name or "").strip().lower()
    if not q:
        return None
    for n, u in playlists:                       # exact
        if n.lower() == q:
            return (n, u)
    for n, u in playlists:                        # contains
        if q in n.lower() or n.lower() in q:
            return (n, u)
    q_tokens = set(re.findall(r"\w+", q))         # word overlap
    best, best_score = None, 0
    for n, u in playlists:
        score = len(q_tokens & set(re.findall(r"\w+", n.lower())))
        if score > best_score:
            best, best_score = (n, u), score
    return best if best_score > 0 else None


# ---- Actions ----

def _confirm_playing(sp, expect_uri=None, timeout=3.0):
    """Ask Spotify whether audio is ACTUALLY playing before claiming it is.

    start_playback returning without an exception does not mean anything is
    coming out of the speakers. The Web API accepts the call and returns 202 in
    plenty of situations where nothing plays: the device went idle between
    listing it and using it, another device grabbed the session, or the account
    can't stream right now. Ted then said "Playing Maine by Noah Kahan" to a
    silent room, which is the exact cheerful lie the honesty rule exists to
    stop — see README section 5.3.

    Returns True (confirmed playing), False (confirmed not playing), or None
    (could not tell — do not claim either way).
    """
    deadline = time.time() + timeout
    saw_state = False
    while time.time() < deadline:
        try:
            state = sp.current_playback()
        except Exception as e:
            print("[spotify] playback check:", e)
            return None
        if state:
            saw_state = True
            if state.get("is_playing"):
                if not expect_uri:
                    return True
                item = state.get("item") or {}
                # Right track, or at least the right context (a playlist starts
                # on whichever track Spotify picks, which is not ours to predict).
                if item.get("uri") == expect_uri:
                    return True
                ctx = (state.get("context") or {}).get("uri")
                if ctx and ctx == expect_uri:
                    return True
        time.sleep(0.4)
    return False if saw_state else None


def play_playlist(name, shuffle=False):
    """Start playing the named playlist via the Spotify Web API.

    Unlike play_track, this uses a context_uri so Spotify queues the whole
    playlist rather than a single track. Returns a human-readable status string.
    If the playlist isn't found in cache, fetches fresh from the API once before
    giving up.
    """
    sp = _client()
    if sp is None:
        return "Spotify isn't connected yet — but I can still control playback if the app's open."
    playlists = _get_playlists(sp)
    if not playlists:
        return "I couldn't find any playlists on your account."
    match = match_playlist(name, playlists)
    if not match:                                 # maybe it's brand new — refresh once
        match = match_playlist(name, _get_playlists(sp, refresh=True))
    if not match:
        return f"I couldn't find a playlist called {name}."
    dev = _device_id_or_launch(sp)
    if dev is None:
        return "I opened Spotify but it hasn't come online yet — give it a second and ask again."
    shuffle_ok = True
    if shuffle:
        try:
            sp.shuffle(True, device_id=dev)
        except Exception as e:
            print("[spotify] shuffle:", e)
            shuffle_ok = False   # continue — start playback anyway, but tell the user
    try:
        sp.start_playback(device_id=dev, context_uri=match[1])
    except Exception as e:
        print("[spotify] playback:", e)
        return "I found it but couldn't start playback. Make sure Spotify's open."
    verb = "Shuffling" if shuffle else "Playing"
    confirmed = _confirm_playing(sp, expect_uri=match[1])
    if confirmed is False:
        return (f"I sent your {match[0]} playlist to Spotify but nothing is "
                "playing. Spotify may need to be open and awake on the device "
                "you want.")
    tail = ""
    if confirmed is None:
        tail = " — though I couldn't confirm it's actually playing"
    elif shuffle and not shuffle_ok:
        tail = " — shuffle isn't available on your account or device right now"
    return f"{verb} your {match[0]} playlist{tail}."


def play_track(query, artist=None):
    """Search Spotify for a track and play it.

    Tries a strict field search first (track:"X" artist:"Y") which works best for
    exact titles. Falls back to free-text search so mood/genre phrases like
    "a happy song" or "something chill" also return a real result.
    """
    sp = _client()
    if sp is None:
        return "Spotify isn't connected yet."

    searches = [
        f"track:{query}" + (f" artist:{artist}" if artist else ""),
        query + (f" {artist}" if artist else ""),
    ]
    items = []
    for q in searches:
        try:
            # limit=1 took whatever Spotify's relevance ranking put first, which
            # answered "Let It Go" with a Jordan Davis country song instead of
            # the Frozen one. Asking for several and keeping the most popular
            # picks the track a person means by a bare title.
            #
            # This applies WITH an artist too. Asked to play a song from The
            # Little Mermaid, the model sent artist="The Little Mermaid" — a
            # film, not an artist — and the strict search happily returned a
            # Royal Philharmonic Orchestra cover as its top relevance hit.
            # Sorting by popularity inside the artist-filtered results costs
            # nothing when the artist is real and rescues the case where the
            # model put a soundtrack, a genre or a mood in that field.
            res = sp.search(q=q, type="track", limit=8)
            items = res.get("tracks", {}).get("items", [])
            if items:
                if len(items) > 1:
                    items = sorted(
                        items, key=lambda t: t.get("popularity", 0), reverse=True)
                break
        except Exception as e:
            print("[spotify] search:", e)

    if not items:
        return f"Couldn't find anything for '{query}'" + (f" by {artist}" if artist else "") + "."
    track = items[0]
    dev = _device_id_or_launch(sp)
    if dev is None:
        return "I opened Spotify but it hasn't come online yet — give it a second and ask again."
    try:
        sp.start_playback(device_id=dev, uris=[track["uri"]])
    except Exception as e:
        print("[spotify] playback:", e)
        return "I found it but couldn't start playback."
    names = ", ".join(a["name"] for a in track.get("artists", []))
    label = track["name"] + (f" by {names}" if names else "")
    confirmed = _confirm_playing(sp, expect_uri=track["uri"])
    if confirmed is True:
        return f"Playing {label}."
    if confirmed is False:
        return (f"I sent {label} to Spotify but it isn't playing. "
                "Spotify may need to be open and awake on the device you want.")
    return f"I started {label} on Spotify, but couldn't confirm it's actually playing."


def transport(action):
    """Transport control via the Web API — reaches whichever device is actually
    playing (phone, Mac, speaker), not just the local desktop app.

    Used as a fallback when the desktop app isn't running but music is playing
    somewhere on the account. Returns a spoken string, or None when there's no
    active playback to control (caller keeps its honest local-app message)."""
    sp = _client()
    if sp is None:
        return None
    try:
        cur = sp.current_playback()
        if not cur or not cur.get("device"):
            return None
        dev = cur["device"]["id"]
        if action == "pause":
            if not cur.get("is_playing"):
                return "Nothing's playing."
            sp.pause_playback(device_id=dev)
            return "Paused."
        if action in ("play", "resume"):
            sp.start_playback(device_id=dev)
            return "Playing."
        if action == "next":
            sp.next_track(device_id=dev)
            return "Skipping ahead."
        if action == "previous":
            sp.previous_track(device_id=dev)
            return "Going back."
        if action == "current":
            if not cur.get("is_playing") or not cur.get("item"):
                return "Nothing's playing right now."
            name = cur["item"]["name"]
            artists = ", ".join(a["name"] for a in cur["item"].get("artists", []))
            return f"{name} by {artists}." if artists else f"{name}."
    except Exception as e:
        print("[spotify] web transport:", e)
    return None


def list_playlist_names(limit=10):
    """Return up to limit playlist names from the user's account.

    The n=limit cap prevents Ted from reading out an overwhelming list when
    the user asks 'what playlists do I have?'
    """
    sp = _client()
    if sp is None:
        return []
    return [n for n, _ in _get_playlists(sp)][:limit]
