#!/usr/bin/env python3
"""
One-time Spotify authorization for Ted.

Run this ONCE in your terminal after putting your Client ID and Secret in
config.py:

    python authorize_spotify.py

A browser window opens — log in to Spotify and click Agree. That caches a token
to data/.spotify_cache, which Ted then refreshes automatically. You shouldn't
need to run this again unless you revoke access or delete the cache.
"""
import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/ted-ai"))

from core import spotify_web

if not spotify_web.configured():
    print("✗ Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to config.py first.")
    sys.exit(1)

# A token granted before playlist editing existed does not carry the two
# playlist-modify scopes, and Spotify will not add them to it. Move the old
# cache aside rather than deleting it, so a failed re-auth is recoverable.
missing = spotify_web.missing_scopes()
if missing:
    backup = spotify_web.CACHE + ".pre-" + time.strftime("%Y%m%d-%H%M%S")
    os.replace(spotify_web.CACHE, backup)
    print("Your cached token predates these permissions:")
    for scope in sorted(missing):
        print(f"    • {scope}")
    print(f"  Old token moved to {os.path.basename(backup)} — re-authorizing.\n")

print("Opening Spotify login in your browser…")
sp = spotify_web._client(interactive=True)
if sp is None:
    print("✗ Could not start Spotify auth. Is spotipy installed?  pip install spotipy")
    sys.exit(1)

try:
    me = sp.current_user()
except Exception as e:
    print(f"✗ Authorization failed: {e}")
    sys.exit(1)

who = me.get("display_name") or me.get("id")
print(f"✓ Authorized as {who}. Token cached — you're set.")

still_missing = spotify_web.missing_scopes()
if still_missing:
    print("✗ But the new token is STILL missing: " + ", ".join(sorted(still_missing)))
    print("  Playlist editing will not work. Remove Ted from")
    print("  https://www.spotify.com/account/apps/ and run this again.")
    sys.exit(1)
print("  Playlist editing is authorized (add, remove, create, unfollow).")

names = spotify_web.list_playlist_names(25)
if names:
    print(f"  Found {len(names)} playlists, e.g.: " + ", ".join(names[:8]))
else:
    print("  (No playlists found — that's fine, song search still works.)")
