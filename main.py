# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi>=0.110",
#   "uvicorn>=0.27",
#   "yt-dlp>=2024.10",
#   "httpx>=0.27",
#   "mutagen>=1.47",
# ]
# ///
import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

app = FastAPI()

DEFAULT_DOWNLOAD_DIR = Path(__file__).parent / "downloads"
INDEX_PATH = Path(__file__).parent / "index.html"
SC_API = "https://api-v2.soundcloud.com"

# Persistent storage for the SC oauth_token and (optional) YT cookies.txt.
# Overridable via SCDL_DATA_DIR — in Docker this points at a mounted volume.
DATA_DIR = Path(os.environ.get("SCDL_DATA_DIR") or (Path(__file__).parent / "data"))
SC_TOKEN_PATH = DATA_DIR / "sc_token"
YT_COOKIES_PATH = DATA_DIR / "yt-cookies.txt"

# Maps short tokens to absolute paths of files we've just saved. The browser
# fetches /api/file/<token> to download them. Tokens keep this endpoint from
# becoming an arbitrary-path read sink.
_file_tokens: dict[str, Path] = {}


def _read_sc_token() -> Optional[str]:
    try:
        return SC_TOKEN_PATH.read_text().strip() or None
    except FileNotFoundError:
        return None


def _write_secret(path: Path, content: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    # chmod is best-effort: some bind-mounted volumes silently ignore it.
    try:
        path.chmod(0o600)
    except OSError:
        pass


class DownloadRequest(BaseModel):
    url: str
    output_dir: Optional[str] = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/api/ping")
async def ping() -> dict:
    return {"ok": True}


@app.get("/api/file/{token}")
async def get_file(token: str) -> FileResponse:
    path = _file_tokens.get(token)
    if path is None or not path.exists():
        raise HTTPException(status_code=404)

    def cleanup() -> None:
        _file_tokens.pop(token, None)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        background=BackgroundTask(cleanup),
    )


def _saved_event(path: Path) -> str:
    token = uuid.uuid4().hex
    _file_tokens[token] = path
    return sse({"type": "saved", "token": token, "filename": path.name})


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    output_dir = Path(req.output_dir).expanduser() if req.output_dir else DEFAULT_DOWNLOAD_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    cleaned = strip_url(req.url)
    url_changed = cleaned != req.url
    req = req.model_copy(update={"url": cleaned})

    return StreamingResponse(
        stream_direct_api(req, output_dir, url_changed),
        media_type="text/event-stream",
    )


async def stream_direct_api(
    req: DownloadRequest, output_dir: Path, url_changed: bool,
) -> AsyncGenerator[str, None]:
    """Direct SoundCloud API backend: read OAuth token from the stored config,
    resolve the URL, iterate transcodings until one delivers a working stream URL."""
    import httpx

    if url_changed:
        yield sse({"type": "info", "msg": f"stripped query params -> {req.url}"})
    yield sse({"type": "info", "msg": f"saving to: {output_dir}"})

    token = _read_sc_token()
    if not token:
        yield sse({"type": "error",
                   "msg": "no SoundCloud token configured — set one in the Auth panel above"})
        return
    yield sse({"type": "info", "msg": f"using stored oauth_token (...{token[-6:]})"})

    headers = {
        "Authorization": f"OAuth {token}",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    }
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        try:
            client_id = await fetch_client_id(client)
        except Exception as e:
            yield sse({"type": "error", "msg": f"could not get client_id: {e}"})
            return
        yield sse({"type": "info", "msg": f"client_id: {client_id}"})

        try:
            r = await client.get(f"{SC_API}/resolve",
                                 params={"url": req.url, "client_id": client_id})
            r.raise_for_status()
            root = r.json()
        except Exception as e:
            yield sse({"type": "error", "msg": f"resolve failed: {e}"})
            return

        kind = root.get("kind")
        if kind == "track":
            tracks = [root]
        elif kind == "playlist":
            tracks = root.get("tracks", [])
            yield sse({"type": "info",
                       "msg": f"playlist '{root.get('title', '?')}': {len(tracks)} tracks"})
        else:
            yield sse({"type": "error",
                       "msg": f"unsupported resource kind {kind!r} (need a track or playlist URL)"})
            return

        for i, t in enumerate(tracks, 1):
            label = f"[{i}/{len(tracks)}]" if len(tracks) > 1 else ""
            # Playlist entries are abbreviated — re-resolve for the full media block.
            if "media" not in t and t.get("permalink_url"):
                try:
                    rr = await client.get(f"{SC_API}/resolve",
                                          params={"url": t["permalink_url"], "client_id": client_id})
                    rr.raise_for_status()
                    t = rr.json()
                except Exception as e:
                    yield sse({"type": "error", "msg": f"{label} could not fetch full track: {e}"})
                    continue

            title = t.get("title") or "untitled"
            user = (t.get("user") or {}).get("username") or "unknown"
            yield sse({"type": "info", "msg": f"{label} {user} — {title}"})

            status = {"ok": False}
            async for ev in _download_track(client, client_id, t, output_dir, user, title, label, status):
                yield ev
            if not status["ok"]:
                async for ev in _youtube_fallback(user, title, output_dir, label):
                    yield ev
        yield sse({"type": "done", "msg": "complete"})


async def _download_track(
    client, client_id: str, track: dict, output_dir: Path,
    user: str, title: str, label: str, status: dict,
) -> AsyncGenerator[str, None]:
    """Attempt each transcoding for a single track until one succeeds."""
    import httpx

    transcodings = (track.get("media") or {}).get("transcodings") or []
    if not transcodings:
        yield sse({"type": "error", "msg": f"{label} no transcodings advertised"})
        return

    # SoundCloud's player passes a per-track authorization token alongside client_id
    # when resolving stream URLs. Without it, encrypted-HLS transcodings 404.
    track_auth = track.get("track_authorization")

    # Skip encrypted variants up front — they use Widevine/PlayReady DRM and
    # there's no point downloading bytes we can't decrypt without a CDM. If
    # *every* variant is encrypted, surface one clear error and bail.
    plaintext = [tc for tc in transcodings
                 if (tc.get("format") or {}).get("protocol") in ("progressive", "hls")]
    if not plaintext:
        protos = sorted({(tc.get("format") or {}).get("protocol", "?") for tc in transcodings})
        yield sse({"type": "info",
                   "msg": (f"{label} DRM-locked. SoundCloud only offers encrypted variants "
                           f"for this track ({', '.join(protos)}); they require a Widevine/"
                           "PlayReady CDM to decrypt. GO+ doesn't change this — labels mark "
                           "their catalog DRM-only regardless of subscription tier. "
                           "Falling back to YouTube via yt-dlp.")})
        return

    # Prefer progressive (single GET, no muxing); fall back to HLS via ffmpeg.
    plaintext.sort(key=lambda tc: 0 if (tc.get("format") or {}).get("protocol") == "progressive" else 1)

    for tc in plaintext:
        fmt = tc.get("format") or {}
        proto = fmt.get("protocol", "?")
        mime = fmt.get("mime_type", "")
        preset = tc.get("preset", "?")
        tc_url = tc.get("url")
        if not tc_url:
            continue

        yield sse({"type": "info", "msg": f"{label}   trying {preset} / {proto}"})
        params = {"client_id": client_id}
        if track_auth:
            params["track_authorization"] = track_auth
        try:
            r = await client.get(tc_url, params=params)
            r.raise_for_status()
            stream_url = (r.json() or {}).get("url")
        except httpx.HTTPStatusError as e:
            yield sse({"type": "info", "msg": f"{label}   skip ({e.response.status_code})"})
            continue
        except Exception as e:
            yield sse({"type": "info", "msg": f"{label}   skip ({e})"})
            continue
        if not stream_url:
            continue

        out_path = _make_output_path(output_dir, user, title, "mp3")

        if proto == "progressive" and "mpeg" in mime:
            # SC progressive is already MP3 — save bytes directly, no transcode.
            yield sse({"type": "info", "msg": f"{label}   GET -> {out_path.name}"})
            try:
                async with client.stream("GET", stream_url) as resp:
                    resp.raise_for_status()
                    with out_path.open("wb") as f:
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            f.write(chunk)
                yield sse({"type": "info",
                           "msg": f"{label}   saved {out_path.stat().st_size:,} bytes"})
                try:
                    await asyncio.to_thread(_write_mp3_tags, out_path, user, title)
                    yield sse({"type": "info", "msg": f"{label}   tagged artist/title"})
                except Exception as e:
                    yield sse({"type": "info", "msg": f"{label}   tag write failed: {e}"})
                yield _saved_event(out_path)
                status["ok"] = True
                return
            except Exception as e:
                yield sse({"type": "info", "msg": f"{label}   download failed: {e}"})
                if out_path.exists():
                    out_path.unlink()
                continue
        else:
            # HLS (or any non-MP3 source): re-encode to MP3 via libmp3lame.
            # We avoid `-c copy` here because the HLS-fMP4 -> MP4/MKV mux drops
            # codec config across segment boundaries and produces silent files.
            if shutil.which("ffmpeg") is None:
                yield sse({"type": "info",
                           "msg": f"{label}   need ffmpeg to transcode {proto}/{mime} but it's not on PATH"})
                continue
            yield sse({"type": "info", "msg": f"{label}   {proto} -> mp3 (libmp3lame) -> {out_path.name}"})
            ff_cmd = ["ffmpeg", "-y", "-loglevel", "warning",
                      "-i", stream_url,
                      "-vn",
                      "-c:a", "libmp3lame", "-q:a", "2",
                      str(out_path)]
            proc = await asyncio.create_subprocess_exec(
                *ff_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    yield sse({"type": "log", "msg": f"{label}   {line}"})
            rc = await proc.wait()
            if rc == 0:
                yield sse({"type": "info",
                           "msg": f"{label}   saved {out_path.stat().st_size:,} bytes"})
                try:
                    await asyncio.to_thread(_write_mp3_tags, out_path, user, title)
                    yield sse({"type": "info", "msg": f"{label}   tagged artist/title"})
                except Exception as e:
                    yield sse({"type": "info", "msg": f"{label}   tag write failed: {e}"})
                yield _saved_event(out_path)
                status["ok"] = True
                return
            else:
                yield sse({"type": "info", "msg": f"{label}   ffmpeg exit {rc}"})
                if out_path.exists():
                    out_path.unlink()
                continue

    yield sse({"type": "error",
               "msg": f"{label} every plaintext transcoding errored "
                      "(network/CDN/ffmpeg failure, not DRM)"})


def _make_output_path(output_dir: Path, user: str, title: str, ext: str) -> Path:
    return output_dir / f"[{sanitize_filename(user)}] {sanitize_filename(title)}.{ext}"


def _write_mp3_tags(path: Path, artist: str, title: str) -> None:
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags.save(path, v2_version=3)


async def _youtube_fallback(
    user: str, title: str, output_dir: Path, label: str,
) -> AsyncGenerator[str, None]:
    """When SC returns nothing playable, try yt-dlp ytsearch1 against YouTube.
    Output filename is prefixed [YouTube] so the source is unambiguous."""
    if shutil.which("yt-dlp") is None:
        yield sse({"type": "error", "msg": f"{label} YT fallback: yt-dlp not on PATH"})
        return

    query = f"{user} {title}"
    out_template = output_dir / f"[YouTube] [{sanitize_filename(user)}] {sanitize_filename(title)}.%(ext)s"
    yield sse({"type": "info", "msg": f"{label} YT fallback: ytsearch1 {query!r}"})

    cmd = [
        "yt-dlp",
        f"ytsearch1:{query}",
        "-x", "--audio-format", "mp3",
        "--embed-metadata",
        "--no-playlist",
        "--newline",
        "--js-runtimes", "bun",
        "-o", str(out_template),
    ]
    if YT_COOKIES_PATH.exists():
        cmd += ["--cookies", str(YT_COOKIES_PATH)]
    else:
        yield sse({"type": "info",
                   "msg": f"{label} YT fallback: no cookies.txt configured — may hit YouTube's bot wall"})
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if line:
            yield sse({"type": "log", "msg": f"{label} YT  {line}"})
    rc = await proc.wait()
    if rc == 0:
        yield sse({"type": "info", "msg": f"{label} YT fallback: saved"})
        # The template ends in .%(ext)s and -x converts to mp3, so the post-
        # extraction filename is deterministic. If yt-dlp's filename sanitizer
        # diverges from ours and the path doesn't match, skip the saved event
        # rather than guessing.
        expected_mp3 = output_dir / f"[YouTube] [{sanitize_filename(user)}] {sanitize_filename(title)}.mp3"
        if expected_mp3.exists():
            yield _saved_event(expected_mp3)
        else:
            yield sse({"type": "info",
                       "msg": f"{label} YT fallback: couldn't locate output for download link"})
    else:
        yield sse({"type": "error", "msg": f"{label} YT fallback: yt-dlp exit {rc}"})


class SCAuthRequest(BaseModel):
    token: str


class YTAuthRequest(BaseModel):
    cookies: str


@app.get("/api/auth")
async def auth_status() -> dict:
    tok = _read_sc_token()
    return {
        "sc_token_set": tok is not None,
        "sc_token_hint": f"…{tok[-6:]}" if tok else None,
        "yt_cookies_set": YT_COOKIES_PATH.exists(),
    }


@app.post("/api/auth/sc")
async def save_sc_token(req: SCAuthRequest) -> dict:
    """Validate the pasted oauth_token against SoundCloud's /me endpoint
    before persisting it. The returned username gives the user immediate
    confidence that they pasted the right cookie."""
    import httpx

    token = req.token.strip()
    if not token:
        raise HTTPException(400, "token is empty")

    headers = {
        "Authorization": f"OAuth {token}",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    }
    async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
        try:
            client_id = await fetch_client_id(client)
        except Exception as e:
            raise HTTPException(502, f"could not get SoundCloud client_id: {e}")
        try:
            r = await client.get(f"{SC_API}/me", params={"client_id": client_id})
        except Exception as e:
            raise HTTPException(502, f"SoundCloud /me failed: {e}")

    if r.status_code == 401:
        raise HTTPException(401, "SoundCloud rejected this token — make sure you copied the value, not the name")
    if r.status_code != 200:
        raise HTTPException(502, f"SoundCloud /me returned HTTP {r.status_code}")

    me = r.json() or {}
    _write_secret(SC_TOKEN_PATH, token)
    return {"username": me.get("username") or me.get("permalink") or "unknown"}


@app.delete("/api/auth/sc")
async def clear_sc_token() -> dict:
    SC_TOKEN_PATH.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/auth/yt")
async def save_yt_cookies(req: YTAuthRequest) -> dict:
    content = req.cookies.strip()
    if not content:
        raise HTTPException(400, "cookies content is empty")
    if "# Netscape HTTP Cookie File" not in content.splitlines()[0]:
        # Soft-validate: yt-dlp will reject bad files anyway, but a heads-up
        # at paste time saves a confused fallback run later.
        raise HTTPException(400,
            "doesn't look like a Netscape cookies.txt — first line should be "
            "'# Netscape HTTP Cookie File'. Use a cookies.txt exporter extension.")
    _write_secret(YT_COOKIES_PATH, content + "\n")
    return {"ok": True}


@app.delete("/api/auth/yt")
async def clear_yt_cookies() -> dict:
    YT_COOKIES_PATH.unlink(missing_ok=True)
    return {"ok": True}


async def fetch_client_id(client) -> str:
    """Scrape soundcloud.com's JS bundles to find the public client_id."""
    home = await client.get("https://soundcloud.com/")
    home.raise_for_status()
    scripts = re.findall(r'<script[^>]+src="(https://[^"]+\.js)"', home.text)
    # client_id lives in one of the bundles — the later ones are most likely.
    for src in reversed(scripts):
        try:
            r = await client.get(src)
            if r.status_code != 200:
                continue
            m = re.search(r'client_id\s*[:=]\s*"([0-9a-zA-Z]{20,})"', r.text)
            if m:
                return m.group(1)
        except Exception:
            continue
    raise RuntimeError("client_id not found in any of soundcloud.com's bundled scripts")


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip()[:200]


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def strip_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
