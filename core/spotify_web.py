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

import json
import os
import re
import time

HOME = os.path.expanduser("~/ted-ai")
CACHE = os.path.join(HOME, "data", ".spotify_cache")

# Permissions requested during OAuth. Covers reading playback state, controlling
# playback, reading AND editing private/collaborative playlists, and seeing
# currently playing.
#
# The two playlist-modify scopes were added when playlist editing was. A token
# granted before that lacks them, which is what can_edit_playlists() is for.
SCOPE = ("user-read-playback-state user-modify-playback-state "
         "playlist-read-private playlist-read-collaborative "
         "playlist-modify-private playlist-modify-public "
         "user-read-currently-playing")

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


def _cached_scopes():
    """The permissions the cached token actually carries, as a set."""
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return set((json.load(fh).get("scope") or "").split())
    except Exception:
        return set()


def missing_scopes():
    """Permissions SCOPE asks for that the cached token does not have."""
    if not (os.path.exists(CACHE) and os.path.getsize(CACHE) > 0):
        return set()
    return set(SCOPE.split()) - _cached_scopes()


def _authorized():
    """Return True when a non-empty OAuth token cache file exists on disk."""
    return os.path.exists(CACHE) and os.path.getsize(CACHE) > 0


def can_edit_playlists():
    """True when the cached token actually carries the playlist-modify scopes.

    Deliberately separate from _authorized(). Adding playlist editing widened
    SCOPE, and Charlie's existing token predates it — but that token is still
    perfectly good for everything it was granted for. Gating all of Spotify on
    the new scopes would break working playback to add an unrelated feature,
    so only the four editing functions ask this question.
    """
    return _authorized() and not missing_scopes()


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
        # Ask for everything when we are allowed to prompt; otherwise ask only
        # for what the cached token already has.
        #
        # spotipy rejects a cached token whose scope does not cover the
        # requested one and falls through to an interactive flow — which, with
        # open_browser=False, ends at input() on stdin inside one of Ted's
        # daemon threads. That is a silent hang, not an error. Requesting the
        # granted set keeps every previously-working call working; the four
        # editing functions check can_edit_playlists() instead.
        granted = _cached_scopes()
        scope = SCOPE if (interactive or not granted) else " ".join(sorted(granted))
        auth = SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope=scope,
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
            confirmed = _confirm_playing(sp)
            if confirmed is True:
                return "Playing."
            if confirmed is False:
                return ("I sent resume to Spotify, but nothing is playing. "
                        "The playback device may be asleep.")
            return "I sent resume to Spotify, but couldn't confirm that audio started."
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


# ---- Playlist editing ----
#
# Every function below re-reads the playlist after writing to it. A 200 from
# playlist_add_items means Spotify accepted the request, not that the track is
# in the playlist — same distinction _confirm_playing exists for, and the same
# honesty rule (README §5.3). Ted must not report a change it has not seen.

def _edit_blocked():
    """Why playlist editing can't run right now, or None if it can."""
    if not configured():
        return "Spotify isn't set up — add the client ID and secret to config.py."
    if not _authorized():
        return "Spotify isn't connected yet — run: python authorize_spotify.py"
    missing = missing_scopes()
    if missing:
        return ("Spotify hasn't given me permission to edit playlists yet "
                f"({', '.join(sorted(missing))}). Playback still works. "
                "Run: python authorize_spotify.py")
    return None


def _write_failed(exc, prefix):
    """Turn a write failure into something that says what to do about it."""
    text = str(exc)
    if "403" in text:
        return (f"{prefix} — Spotify refused it. That usually means the playlist "
                "belongs to someone else, or the token predates playlist editing "
                "(run: python authorize_spotify.py).")
    return f"{prefix}: {text[:120]}"


def _playlist_id(uri):
    """spotify:playlist:37i9dQ… → 37i9dQ…"""
    return (uri or "").rsplit(":", 1)[-1]


def _resolve_playlist(sp, name):
    """Match a spoken playlist name, re-fetching once in case it is new."""
    match = match_playlist(name, _get_playlists(sp))
    if match is None:
        match = match_playlist(name, _get_playlists(sp, refresh=True))
    return match


def _track_label(item):
    names = ", ".join(a["name"] for a in (item or {}).get("artists", []))
    return (item or {}).get("name", "that track") + (f" by {names}" if names else "")


def _resolve_track(sp, query=None):
    """The track to act on. Returns (uri, label) or (None, message-to-say).

    With no query this is the CURRENTLY PLAYING track, because "add this to my
    running playlist" is the common case and it never names the song.
    """
    if not query:
        try:
            cur = sp.current_playback()
        except Exception as e:
            print("[spotify] current track:", e)
            return None, "I couldn't reach Spotify to see what's playing."
        item = (cur or {}).get("item")
        if not item or not item.get("uri"):
            return None, "Nothing's playing, so I don't know which track you mean."
        return item["uri"], _track_label(item)
    try:
        res = sp.search(q=query, type="track", limit=8)
        items = res.get("tracks", {}).get("items", [])
    except Exception as e:
        print("[spotify] search:", e)
        return None, f"I couldn't search Spotify for '{query}'."
    if not items:
        return None, f"Couldn't find a track for '{query}'."
    # Same popularity sort as play_track, for the same reason: a bare title
    # should resolve to the song a person means.
    items = sorted(items, key=lambda t: t.get("popularity", 0), reverse=True)
    return items[0]["uri"], _track_label(items[0])


def _playlist_track_uris(sp, playlist_id):
    """Every track URI in a playlist, or None if it could not be read."""
    uris = []
    try:
        res = sp.playlist_items(playlist_id, fields="items(track(uri)),next",
                                limit=100, additional_types=("track",))
        while res:
            for row in res.get("items", []):
                track = (row or {}).get("track") or {}
                if track.get("uri"):
                    uris.append(track["uri"])
            res = sp.next(res) if res.get("next") else None
    except Exception as e:
        print("[spotify] playlist items:", e)
        return None
    return uris


def add_to_playlist(playlist, track_query=None):
    """Add a track (default: the one playing) to a playlist, and verify it."""
    blocked = _edit_blocked()
    if blocked:
        return blocked
    sp = _client()
    if sp is None:
        return "Spotify isn't connected yet — run: python authorize_spotify.py"
    match = _resolve_playlist(sp, playlist)
    if match is None:
        return f"I couldn't find a playlist called '{playlist}'."
    pl_name, pl_uri = match
    uri, label = _resolve_track(sp, track_query)
    if uri is None:
        return label
    pid = _playlist_id(pl_uri)
    before = _playlist_track_uris(sp, pid)
    if before is not None and uri in before:
        return f"{label} is already in {pl_name}."
    try:
        sp.playlist_add_items(pid, [uri])
    except Exception as e:
        print("[spotify] add:", e)
        return _write_failed(e, f"I couldn't add {label} to {pl_name}")
    after = _playlist_track_uris(sp, pid)
    if after is None:
        return f"I sent {label} to {pl_name}, but couldn't read the playlist back to confirm."
    if uri in after:
        return f"Added {label} to {pl_name}."
    return f"Spotify accepted that, but {label} is not in {pl_name}."


def remove_from_playlist(playlist, track_query=None):
    """Remove a track (default: the one playing) from a playlist, and verify it."""
    blocked = _edit_blocked()
    if blocked:
        return blocked
    sp = _client()
    if sp is None:
        return "Spotify isn't connected yet — run: python authorize_spotify.py"
    match = _resolve_playlist(sp, playlist)
    if match is None:
        return f"I couldn't find a playlist called '{playlist}'."
    pl_name, pl_uri = match
    uri, label = _resolve_track(sp, track_query)
    if uri is None:
        return label
    pid = _playlist_id(pl_uri)
    before = _playlist_track_uris(sp, pid)
    if before is not None and uri not in before:
        return f"{label} isn't in {pl_name}."
    try:
        sp.playlist_remove_all_occurrences_of_items(pid, [uri])
    except Exception as e:
        print("[spotify] remove:", e)
        return _write_failed(e, f"I couldn't remove {label} from {pl_name}")
    after = _playlist_track_uris(sp, pid)
    if after is None:
        return f"I sent that removal to {pl_name}, but couldn't read the playlist back to confirm."
    if uri not in after:
        return f"Removed {label} from {pl_name}."
    return f"Spotify accepted that, but {label} is still in {pl_name}."


def create_playlist(name, public=False, description=""):
    """Create a playlist on the user's account, and verify it exists after."""
    blocked = _edit_blocked()
    if blocked:
        return blocked
    sp = _client()
    if sp is None:
        return "Spotify isn't connected yet — run: python authorize_spotify.py"
    name = (name or "").strip()
    if not name:
        return "I need a name for the playlist."
    try:
        me = sp.current_user()
        created = sp.user_playlist_create(
            me["id"], name, public=bool(public), description=description or "")
    except Exception as e:
        print("[spotify] create:", e)
        return _write_failed(e, f"I couldn't create '{name}'")
    new_uri = (created or {}).get("uri")
    # Verify by URI, not by name: match_playlist is fuzzy and would happily
    # confirm a create by finding some OTHER playlist with a similar name.
    found = [n for n, u in _get_playlists(sp, refresh=True) if u == new_uri]
    if not found:
        return f"Spotify accepted the request but '{name}' isn't in your playlists."
    return f"Created {found[0]} ({'public' if public else 'private'})."


def delete_playlist(name):
    """Unfollow a playlist — the only 'delete' Spotify has. Verified after."""
    blocked = _edit_blocked()
    if blocked:
        return blocked
    sp = _client()
    if sp is None:
        return "Spotify isn't connected yet — run: python authorize_spotify.py"
    match = _resolve_playlist(sp, name)
    if match is None:
        return f"I couldn't find a playlist called '{name}'."
    pl_name, pl_uri = match
    try:
        sp.current_user_unfollow_playlist(_playlist_id(pl_uri))
    except Exception as e:
        print("[spotify] unfollow:", e)
        return _write_failed(e, f"I couldn't remove '{pl_name}'")
    still_there = any(u == pl_uri for _, u in _get_playlists(sp, refresh=True))
    if still_there:
        return f"Spotify accepted that, but {pl_name} is still in your library."
    # Say what actually happened. There is no delete endpoint; unfollowing is
    # what the Spotify app's own "Delete playlist" button does, and it is
    # recoverable from spotify.com for a while afterwards.
    return (f"Removed {pl_name} from your library. Spotify has no true delete — "
            "this unfollows it, the same thing the Spotify app does, and it can "
            "be recovered from spotify.com for a while.")


def now_playing():
    """Structured currently-playing info for the HUD strip. Never raises.

    Separate from transport("current") because that returns a sentence for Ted
    to say. The UI needs fields, and it needs them cheaply — this runs on a
    HUD poll, not on a model turn.
    """
    blank = {"playing": False, "title": "", "artist": ""}
    sp = _client()
    if sp is None:
        return blank
    try:
        cur = sp.current_playback()
    except Exception as e:
        print("[spotify] now playing:", e)
        return blank
    item = (cur or {}).get("item")
    if not item:
        return blank
    return {
        "playing": bool(cur.get("is_playing")),
        "title": item.get("name", ""),
        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
    }


def list_playlist_names(limit=10):
    """Return up to limit playlist names from the user's account.

    The n=limit cap prevents Ted from reading out an overwhelming list when
    the user asks 'what playlists do I have?'
    """
    sp = _client()
    if sp is None:
        return []
    return [n for n, _ in _get_playlists(sp)][:limit]
