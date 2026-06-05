# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi>=0.110",
#   "uvicorn>=0.27",
#   "yt-dlp>=2024.10",
#   "httpx>=0.27",
#   "mutagen>=1.47",
#   "pytest>=8",
# ]
# ///
"""Offline tests for the Spotify backend: no real network, httpx is patched."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest import mock

import main


def _events(sse_chunks):
    return [json.loads(c[len("data: "):].strip()) for c in sse_chunks]


def test_parse_spotify_url():
    assert main.parse_spotify_url("https://open.spotify.com/track/abc123") == ("track", "abc123")
    assert main.parse_spotify_url("https://open.spotify.com/playlist/XYZ") == ("playlist", "XYZ")
    assert main.parse_spotify_url("https://open.spotify.com/intl-de/track/de9") == ("track", "de9")
    assert main.parse_spotify_url("spotify:playlist:uri1") == ("playlist", "uri1")
    assert main.parse_spotify_url("https://open.spotify.com/album/x") is None
    assert main.parse_spotify_url("https://soundcloud.com/a/b") is None


def test_spotify_entry():
    assert main._spotify_entry(
        {"type": "track", "name": "Song", "artists": [{"name": "A"}, {"name": "B"}]}
    ) == ("A, B", "Song")
    assert main._spotify_entry({"type": "episode", "name": "Pod"}) is None
    assert main._spotify_entry(None) is None
    assert main._spotify_entry({"type": "track", "name": "", "artists": []}) is None


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_token_rejects_bad_creds():
    async def go():
        client = mock.AsyncMock()
        client.post.return_value = FakeResp(400, {"error": "invalid_client"})
        try:
            await main._spotify_access_token(client, "id", "secret")
        except main.SpotifyError as e:
            return str(e)
        return None
    msg = asyncio.run(go())
    assert msg and "rejected" in msg.lower()


def test_fetch_playlist_paginates():
    async def go():
        client = mock.AsyncMock()
        page1 = FakeResp(200, {
            "name": "My List",
            "tracks": {
                "items": [{"track": {"type": "track", "name": "T1", "artists": [{"name": "A1"}]}}],
                "next": "https://api.spotify.com/v1/playlists/x/tracks?offset=100",
            },
        })
        page2 = FakeResp(200, {
            "items": [
                {"track": {"type": "track", "name": "T2", "artists": [{"name": "A2"}]}},
                {"track": None},  # removed track, skipped
                {"track": {"type": "episode", "name": "Pod"}},  # episode, skipped
            ],
            "next": None,
        })
        client.get.side_effect = [page1, page2]
        return await main._fetch_spotify_entries(client, {}, "playlist", "x")
    title, entries = asyncio.run(go())
    assert title == "Playlist: My List"
    assert entries == [("A1", "T1"), ("A2", "T2")]


def test_stream_spotify_no_creds(tmp_path=None):
    import tempfile
    from pathlib import Path
    async def collect(gen):
        return [e async for e in gen]
    with mock.patch.object(main, "_read_spotify_creds", return_value=None):
        req = main.DownloadRequest(url="https://open.spotify.com/track/x")
        gen = main.stream_spotify(req, ("track", "x"), Path(tempfile.mkdtemp()), False)
        evs = _events(asyncio.run(collect(gen)))
    assert any(e["type"] == "error" and "credentials" in e["msg"] for e in evs)


def test_stream_spotify_track_uses_youtube():
    import tempfile
    from pathlib import Path

    async def fake_yt(user, title, output_dir, label, idx=None, status=None):
        if status is not None:
            status["ok"] = True
        yield main.sse({"type": "info", "msg": f"YT {user} {title}"})
        yield main.sse({"type": "saved", "token": "tok", "filename": "f.mp3", "id": idx})

    async def collect(gen):
        return [e async for e in gen]

    fake_client = mock.AsyncMock()
    # context manager
    cm = mock.AsyncMock()
    cm.__aenter__.return_value = fake_client
    cm.__aexit__.return_value = False

    with mock.patch.object(main, "_read_spotify_creds", return_value=("id", "sec")), \
         mock.patch.object(main, "_spotify_access_token", new=mock.AsyncMock(return_value="bearer")), \
         mock.patch.object(main, "_fetch_spotify_entries",
                           new=mock.AsyncMock(return_value=("Artist — Song", [("Artist", "Song")]))), \
         mock.patch.object(main, "_youtube_fallback", new=fake_yt), \
         mock.patch("httpx.AsyncClient", return_value=cm):
        req = main.DownloadRequest(url="https://open.spotify.com/track/x")
        gen = main.stream_spotify(req, ("track", "x"), Path(tempfile.mkdtemp()), False)
        evs = _events(asyncio.run(collect(gen)))

    types = [e["type"] for e in evs]
    assert "title" in types
    assert any(e["type"] == "track" and e["state"] == "yt" for e in evs)
    assert any(e["type"] == "saved" for e in evs)
    assert any(e["type"] == "track" and e["state"] == "done" for e in evs)
    assert evs[-1]["type"] == "done"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
