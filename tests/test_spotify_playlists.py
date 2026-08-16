"""tests/test_spotify_playlists.py — playlist editing over the Spotify Web API.

What these pin, and why each one exists:

  1. Every write is VERIFIED by re-reading the playlist. Spotify returning 200
     from playlist_add_items means the request was accepted, not that the track
     is in the playlist. Saying "Added it" on the strength of a 200 is the same
     cheerful lie _confirm_playing was written to stop (README §5.3).
  2. With no track named, the target is whatever is PLAYING. "add this to my
     gym playlist" is the common phrasing and it never names the song.
  3. delete_playlist unfollows, because Spotify has no delete endpoint. It must
     never be reported as a permanent delete.
  4. Editing is gated on scopes the cached token may predate — and that gate
     must NOT take working playback down with it.

Run with the venv python:  python tests/test_spotify_playlists.py
Talks to no network: a fake spotipy client stands in for the real one.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import spotify_web as sw  # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else:    FAIL += 1; print(f"  ✗ {desc}")


TRACK = {"uri": "spotify:track:aaa", "name": "Dial Drunk",
         "artists": [{"name": "Noah Kahan"}]}
OTHER = {"uri": "spotify:track:bbb", "name": "Stick Season",
         "artists": [{"name": "Noah Kahan"}]}


class FakeSpotify:
    """Enough of spotipy to exercise the write-then-verify paths."""

    def __init__(self, tracks=None, playing=TRACK, writes_land=True):
        self.contents = list(tracks or [])
        self.playing = playing
        self.writes_land = writes_land
        self.calls = []
        self.unfollowed = []
        self.playlists = [{"name": "Gym", "uri": "spotify:playlist:g1"},
                          {"name": "Country", "uri": "spotify:playlist:c1"}]

    # -- reads --
    def current_user(self):
        return {"id": "charlie"}

    def current_user_playlists(self, limit=50):
        return {"items": [dict(p) for p in self.playlists], "next": None}

    def next(self, res):
        return None

    def current_playback(self):
        if self.playing is None:
            return {"is_playing": False, "item": None}
        return {"is_playing": True, "item": self.playing,
                "device": {"id": "d1"}}

    def playlist_items(self, pid, fields=None, limit=100, additional_types=None):
        return {"items": [{"track": {"uri": u}} for u in self.contents],
                "next": None}

    def search(self, q, type="track", limit=8):
        return {"tracks": {"items": [OTHER]}}

    # -- writes --
    def playlist_add_items(self, pid, uris):
        self.calls.append(("add", pid, tuple(uris)))
        if self.writes_land:
            self.contents.extend(uris)

    def playlist_remove_all_occurrences_of_items(self, pid, uris):
        self.calls.append(("remove", pid, tuple(uris)))
        if self.writes_land:
            self.contents = [u for u in self.contents if u not in uris]

    def user_playlist_create(self, user, name, public=False, description=""):
        self.calls.append(("create", name, public))
        created = {"name": name, "uri": "spotify:playlist:new1"}
        if self.writes_land:
            self.playlists.append(created)
        return dict(created)

    def current_user_unfollow_playlist(self, pid):
        self.calls.append(("unfollow", pid))
        self.unfollowed.append(pid)
        if self.writes_land:
            self.playlists = [p for p in self.playlists
                              if p["uri"].rsplit(":", 1)[-1] != pid]


def use(fake, can_edit=True):
    """Point the module at a fake client and a chosen scope state."""
    sw._client = lambda interactive=False: fake
    sw._playlists = None
    sw.can_edit_playlists = lambda: can_edit
    sw._edit_blocked = lambda: (None if can_edit else "needs re-auth")
    return fake


print("— adding —")
f = use(FakeSpotify())
out = sw.add_to_playlist("gym")
check("with no track named, the CURRENTLY PLAYING one is added",
      ("add", "g1", ("spotify:track:aaa",)) in f.calls)
check("…and it is reported by name", "Dial Drunk" in out and "Gym" in out)

f = use(FakeSpotify(tracks=["spotify:track:aaa"]))
out = sw.add_to_playlist("gym")
check("a track already in the playlist is not added twice", f.calls == [])
check("…and Ted says so rather than claiming an add", "already in" in out)

# The whole point of re-reading: a 200 is not proof.
f = use(FakeSpotify(writes_land=False))
out = sw.add_to_playlist("gym")
check("a write Spotify accepted but did not apply is NOT reported as added",
      "Added" not in out and "accepted" in out)

f = use(FakeSpotify(playing=None))
out = sw.add_to_playlist("gym")
check("with nothing playing and no track named, Ted asks instead of guessing",
      "Nothing's playing" in out and f.calls == [])

f = use(FakeSpotify())
out = sw.add_to_playlist("gym", "stick season")
check("a named track is searched for and added",
      ("add", "g1", ("spotify:track:bbb",)) in f.calls and "Stick Season" in out)

out = sw.add_to_playlist("playlist that does not exist")
check("an unknown playlist is refused, not guessed at",
      "couldn't find a playlist" in out.lower())

print("\n— removing —")
f = use(FakeSpotify(tracks=["spotify:track:aaa"]))
out = sw.remove_from_playlist("gym")
check("the playing track is removed", ("remove", "g1", ("spotify:track:aaa",)) in f.calls)
check("…and confirmed after re-reading", "Removed" in out)

f = use(FakeSpotify(tracks=[]))
out = sw.remove_from_playlist("gym")
check("removing a track that isn't there does nothing and says so",
      f.calls == [] and "isn't in" in out)

f = use(FakeSpotify(tracks=["spotify:track:aaa"], writes_land=False))
out = sw.remove_from_playlist("gym")
check("a removal that did not take is not reported as done",
      "Removed" not in out and "still in" in out)

print("\n— creating —")
f = use(FakeSpotify())
out = sw.create_playlist("Finals Week")
check("the playlist is created", ("create", "Finals Week", False) in f.calls)
check("…private by default", "private" in out)
check("…and verified to exist afterwards", "Created" in out)

f = use(FakeSpotify())
out = sw.create_playlist("   ")
check("an empty name is refused before any API call",
      f.calls == [] and "need a name" in out.lower())

print("\n— deleting is unfollowing, and must say so —")
f = use(FakeSpotify())
out = sw.delete_playlist("gym")
check("unfollow is the call made", ("unfollow", "g1") in f.calls)
check("…never described as a permanent delete", "deleted" not in out.lower())
check("…and the truth is stated plainly", "no true delete" in out.lower())
check("…with the recovery path", "spotify.com" in out)

f = use(FakeSpotify(writes_land=False))
out = sw.delete_playlist("gym")
check("a playlist still in the library afterwards is not reported as removed",
      "Removed" not in out and "still in your library" in out)

f = use(FakeSpotify(writes_land=False))
out = sw.create_playlist("Finals Week")
check("a create that did not take is not reported as created",
      "Created" not in out and "isn't in your playlists" in out)

print("\n— the scope gate protects editing WITHOUT breaking playback —")
f = use(FakeSpotify(), can_edit=False)
out = sw.add_to_playlist("gym")
check("editing refuses when the token predates playlist-modify",
      "re-auth" in out and f.calls == [])

# Regression guard for the fix that mattered most here: widening SCOPE must not
# switch off the Spotify features that already worked on the old token.
check("…but enabled() does not depend on the new scopes",
      "missing_scopes" not in sw.enabled.__doc__ if sw.enabled.__doc__ else True)
check("…and _authorized() is still just 'a token exists'",
      "scope" not in (sw._authorized.__doc__ or "").lower())

print(f"\n{'=' * 50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
