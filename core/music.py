"""core/music.py — Route spoken music phrases to the right Spotify backend.

Local AppleScript (core/actions.py) for instant transport, Spotify Web API
(core/spotify_web.py) for playlists and named songs. Both self-heal by
launching the desktop app when it isn't running.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 26 (§26.2)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Sixty lines that route a spoken music phrase to one of two backends.
#
#      The split is the interesting part. Transport — play, pause, skip — goes
#      through local AppleScript because it must be instant. Selection — a named
#      song, a playlist — goes through the Spotify Web API because it needs your
#      account. Sending everything through the Web API would make pausing take a
#      network round trip.
#
# =============================================================================

import re

from core import features, intents
from core.actions import spotify_command


def spotify_web_ready():
    return features.HAS_SPOTIFY_WEB and features.spotify_web.enabled()


def transport(action):
    """Transport command with device fallback: try the local desktop app first,
    and when it isn't running, control whichever device IS playing via the
    Web API (music started by Ted can land on a phone or speaker)."""
    r = spotify_command(action)
    if r == "Spotify isn't open right now." and spotify_web_ready():
        web = features.spotify_web.transport(action)
        if web:
            return web
    return r


def handle_spoken(text):
    """Map a spoken phrase to a Spotify action. Returns spoken text or None
    (None = not a music command; fall through to other handlers)."""
    if intents._matches(text, intents._SPOT_NEXT):  return transport("next")
    if intents._matches(text, intents._SPOT_PREV):  return transport("previous")
    if intents._matches(text, intents._SPOT_PAUSE): return transport("pause")
    if intents._matches(text, intents._SPOT_NOW):   return transport("current")
    if intents._matches(text, intents._SPOT_UP):    return spotify_command("up")
    if intents._matches(text, intents._SPOT_DOWN):  return spotify_command("down")

    # "what playlists do I have"
    if re.search(r"\b(?:what|which|list|name)\b.{0,20}\bplaylists?\b", text, re.I):
        if spotify_web_ready():
            names = features.spotify_web.list_playlist_names()
            return ("Your playlists include: " + ", ".join(names) + ".") if names \
                else "I don't see any playlists on your account."
        return "I need the Spotify Web API connected to see your playlists."

    pl = intents._parse_playlist(text)
    if pl:
        if spotify_web_ready():
            return features.spotify_web.play_playlist(pl[0], shuffle=pl[1])
        return ("To play your playlists I need the Spotify Web API connected — it's a quick "
                "one-time setup. Want me to walk you through it?")

    if intents._matches(text, intents._SPOT_PLAY):
        return spotify_command("play")

    sg = intents._parse_song(text)
    if sg:
        if spotify_web_ready():
            return features.spotify_web.play_track(sg[0], sg[1])
        return ("Playing a song by name needs the Spotify Web API connected, which isn't "
                "set up yet. For now I can play, pause, skip, or tell you what's on.")
    return None
