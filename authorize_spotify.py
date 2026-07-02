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

sys.path.insert(0, os.path.expanduser("~/ted-ai"))

from core import spotify_web

if not spotify_web.configured():
    print("✗ Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to config.py first.")
    sys.exit(1)

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

names = spotify_web.list_playlist_names(25)
if names:
    print(f"  Found {len(names)} playlists, e.g.: " + ", ".join(names[:8]))
else:
    print("  (No playlists found — that's fine, song search still works.)")
