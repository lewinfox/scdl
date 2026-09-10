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
import hashlib
import hmac
import json
import os
import re
import secrets as secretslib
import shlex
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel
from starlette.background import BackgroundTask

app = FastAPI()

DEFAULT_DOWNLOAD_DIR = Path(__file__).parent / "downloads"
INDEX_PATH = Path(__file__).parent / "index.html"
SC_API = "https://api-v2.soundcloud.com"

# Approximate effective bitrate per SoundCloud transcoding preset. Used only to
# order the variants, so the numbers just need to rank correctly relative to
# each other. Deliberately NOT derived from the sibling `quality` field
# ("hq"/"sq"/"lq"): aac_160k reports "sq", the same as the 128 kbps mp3_1_0, so
# that field carries no ordering information.
_PRESET_KBPS = {
    "aac_256k": 256,
    "aac_160k": 160,
    "mp3_1_0": 128,   # SC's MP3, served both progressive and over HLS
    "abr_sq": 128,    # adaptive HLS master playlist, tops out in the same tier
    "aac_96k": 96,
    "opus_0_0": 110,  # ~64 kbps Opus: perceptually near mp3 128, worse to re-encode
}


def _preset_score(preset: Optional[str]) -> int:
    """Rank a transcoding preset by rough audio quality, best = highest."""
    if preset in _PRESET_KBPS:
        return _PRESET_KBPS[preset]
    m = re.search(r"(\d{2,4})k", preset or "")
    # An unrecognised preset sorts just below mp3_1_0 — prefer the devil we know.
    return int(m.group(1)) if m else 100


# How many playlist tracks to download at once. Most SC tracks are progressive
# MP3 (network-bound direct copies), so parallelism is a clear win; the cap keeps
# concurrent ffmpeg transcodes from swamping a small VM. Override via env.
try:
    DOWNLOAD_CONCURRENCY = max(1, int(os.environ.get("SCDL_CONCURRENCY", "4")))
except ValueError:
    DOWNLOAD_CONCURRENCY = 4

# Persistent storage dir (session secret etc.). Overridable via SCDL_DATA_DIR —
# in Docker this points at a mounted volume.
DATA_DIR = Path(os.environ.get("SCDL_DATA_DIR") or (Path(__file__).parent / "data"))

# Credentials are provided as secrets via the environment (Fly secrets / env
# vars), never entered or stored through the UI.
#   SCDL_SC_TOKEN   — SoundCloud oauth_token cookie value
#   SCDL_YT_COOKIES — optional Netscape cookies.txt contents for the YT fallback
SC_TOKEN = (os.environ.get("SCDL_SC_TOKEN") or "").strip() or None


def _materialize_yt_cookies() -> Optional[Path]:
    """yt-dlp's --cookies wants a file path, so spill SCDL_YT_COOKIES to a
    temp file at startup. Returns None when no cookies are configured."""
    content = os.environ.get("SCDL_YT_COOKIES")
    if not content or not content.strip():
        return None
    path = Path(tempfile.gettempdir()) / "scdl-yt-cookies.txt"
    path.write_text(content if content.endswith("\n") else content + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


YT_COOKIES_FILE = _materialize_yt_cookies()

# --- Auth ----------------------------------------------------------------
# A single shared password gates the whole app. The gate is only active when
# SCDL_PASSWORD is set; leave it unset for local/dev and the app stays open.
LOGIN_PATH = Path(__file__).parent / "login.html"
APP_PASSWORD = os.environ.get("SCDL_PASSWORD") or None
SESSION_COOKIE = "scdl_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days
SESSION_SECRET_PATH = DATA_DIR / "session_secret"
# Mark the cookie Secure only where we actually serve HTTPS. On fly (which sets
# FLY_APP_NAME and forces TLS) that's always; locally over http://localhost a
# Secure cookie would be silently dropped by the browser, breaking login.
COOKIE_SECURE = bool(os.environ.get("FLY_APP_NAME"))
# Clients from these IPs skip the login page and are logged in automatically.
TRUSTED_IPS = {"170.64.251.115"}
# Endpoints reachable without a session; everything else requires auth.
PUBLIC_PATHS = {"/login", "/api/login", "/api/ping"}

# Login throttling. A fixed-capacity LRU of recent IPs acts as a ring buffer:
# once it's full the least-recently-seen IP is evicted. LOGIN_FAIL_LIMIT wrong
# guesses from one IP trip a LOGIN_BLOCK_SECONDS lockout. Best-effort only —
# in-memory, per-process, and an attacker rotating IPs can churn the buffer.
LOGIN_FAIL_LIMIT = 3
LOGIN_BLOCK_SECONDS = 15 * 60
LOGIN_TRACKER_SIZE = 1024
# ip -> [fail_count, blocked_until_epoch]
_login_attempts: "OrderedDict[str, list]" = OrderedDict()

if APP_PASSWORD is None:
    print("WARNING: SCDL_PASSWORD is not set — the app is UNAUTHENTICATED "
          "and open to anyone who can reach it.", flush=True)


def _session_secret() -> bytes:
    """Persistent random key for signing session cookies. Generated once and
    stored on the data volume so sessions survive restarts and redeploys."""
    try:
        return SESSION_SECRET_PATH.read_bytes()
    except FileNotFoundError:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = secretslib.token_bytes(32)
        SESSION_SECRET_PATH.write_bytes(key)
        try:
            SESSION_SECRET_PATH.chmod(0o600)
        except OSError:
            pass
        return key


def _make_session_cookie() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    sig = hmac.new(_session_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _session_valid(cookie: Optional[str]) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp, _, sig = cookie.partition(".")
    expected = hmac.new(_session_secret(), exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    # Behind fly's proxy the socket peer is the proxy itself; the real client
    # IP is in Fly-Client-IP (which fly sets and clients cannot spoof). Fall
    # back to the socket address for direct/local connections.
    return (request.headers.get("fly-client-ip")
            or (request.client.host if request.client else ""))


def _is_authed(request: Request) -> bool:
    if APP_PASSWORD is None:
        return True
    if _client_ip(request) in TRUSTED_IPS:
        return True
    return _session_valid(request.cookies.get(SESSION_COOKIE))


def _login_block_remaining(ip: str) -> int:
    """Seconds left on an active lockout for this IP, or 0 if not blocked."""
    rec = _login_attempts.get(ip)
    if not rec:
        return 0
    remaining = int(rec[1] - time.time())
    return remaining if remaining > 0 else 0


def _record_login_failure(ip: str) -> int:
    """Tally a failed attempt and return the lockout seconds if the limit is
    now hit (0 otherwise). Maintains the ring buffer as a most-recent LRU."""
    now = time.time()
    rec = _login_attempts.get(ip)
    if rec is None or (rec[1] and rec[1] <= now):
        # New IP, or a previous block that has since expired: start fresh.
        rec = [0, 0.0]
    rec[0] += 1
    if rec[0] >= LOGIN_FAIL_LIMIT:
        rec[1] = now + LOGIN_BLOCK_SECONDS
    _login_attempts[ip] = rec
    _login_attempts.move_to_end(ip)
    while len(_login_attempts) > LOGIN_TRACKER_SIZE:
        _login_attempts.popitem(last=False)  # evict least-recently-seen IP
    return _login_block_remaining(ip)


def _clear_login_failures(ip: str) -> None:
    _login_attempts.pop(ip, None)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or _is_authed(request):
        return await call_next(request)
    # Unauthenticated: send browsers to the login page, APIs a JSON 401.
    accepts_html = "text/html" in request.headers.get("accept", "")
    if request.method == "GET" and accepts_html:
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "authentication required"}, status_code=401)


class LoginRequest(BaseModel):
    password: str


@app.get("/login")
async def login_page(request: Request):
    # Already authed (e.g. a trusted IP) — no point showing the form.
    if _is_authed(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(LOGIN_PATH)


@app.post("/api/login")
async def do_login(req: LoginRequest, request: Request):
    if APP_PASSWORD is None:
        raise HTTPException(503, "no password configured on the server")

    ip = _client_ip(request)
    blocked = _login_block_remaining(ip)
    if blocked:
        mins = max(1, round(blocked / 60))
        raise HTTPException(429, f"too many attempts — try again in ~{mins} min",
                            headers={"Retry-After": str(blocked)})

    if not hmac.compare_digest(req.password, APP_PASSWORD):
        blocked = _record_login_failure(ip)
        if blocked:
            mins = max(1, round(blocked / 60))
            raise HTTPException(429, f"too many attempts — locked out for ~{mins} min",
                                headers={"Retry-After": str(blocked)})
        raise HTTPException(401, "incorrect password")

    _clear_login_failures(ip)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE,
        _make_session_cookie(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return resp


@app.post("/api/logout")
async def do_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp

# Maps short tokens to absolute paths of files we've just saved. The browser
# fetches /api/file/<token> to download them. Tokens keep this endpoint from
# becoming an arbitrary-path read sink.
_file_tokens: dict[str, Path] = {}


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


def _saved_event(path: Path, track_id: Optional[int] = None) -> str:
    token = uuid.uuid4().hex
    _file_tokens[token] = path
    payload = {"type": "saved", "token": token, "filename": path.name}
    if track_id is not None:
        payload["id"] = track_id
    return sse(payload)


def _track_event(track_id: int, total: int, name: str, state: str) -> str:
    """Per-track status for the UI card. state: sc | yt | done | failed."""
    return sse({"type": "track", "id": track_id, "total": total,
                "name": name, "state": state})


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


async def _hydrate_track(client, client_id: str, stub: dict) -> Optional[dict]:
    """Playlist entries past the first few come back as stubs — typically just
    an id, with no media/title/user. Fetch the full track object by id (falling
    back to resolving its permalink_url) so both the SoundCloud download and the
    YouTube fallback get the real artist/title metadata."""
    track_id = stub.get("id")
    if track_id is not None:
        try:
            r = await client.get(f"{SC_API}/tracks/{track_id}",
                                 params={"client_id": client_id})
            r.raise_for_status()
            return r.json()
        except Exception:
            pass
    permalink = stub.get("permalink_url")
    if permalink:
        try:
            r = await client.get(f"{SC_API}/resolve",
                                 params={"url": permalink, "client_id": client_id})
            r.raise_for_status()
            return r.json()
        except Exception:
            pass
    return None


async def _process_track(
    client, client_id: str, t: dict, output_dir: Path, idx: int, total: int,
) -> AsyncGenerator[str, None]:
    """Hydrate one (possibly stub) entry, download it from SoundCloud, and fall
    back to YouTube if nothing playable comes back. Yields SSE events, including
    `track` status updates that drive the per-track card in the UI."""
    label = f"[{idx}/{total}]" if total > 1 else ""
    if "media" not in t:
        hydrated = await _hydrate_track(client, client_id, t)
        if hydrated is None:
            yield sse({"type": "info", "msg": f"{label} could not fetch full track metadata"})
            yield _track_event(idx, total, "unknown track", "failed")
            return
        t = hydrated

    title = t.get("title") or "untitled"
    user = (t.get("user") or {}).get("username") or "unknown"
    name = f"{user} — {title}"
    # Tags come from SoundCloud even when the audio ends up coming from YouTube,
    # so build this once and hand it to both paths.
    meta = _extract_meta(t)
    # Blue: downloading from SoundCloud.
    yield _track_event(idx, total, name, "sc")

    status = {"ok": False}
    async for ev in _download_track(client, client_id, t, output_dir, user, title, label,
                                    status, idx, meta=meta):
        yield ev
    if status["ok"]:
        yield _track_event(idx, total, name, "done")
        return

    # Nothing playable from SC — yellow while we try YouTube.
    yield _track_event(idx, total, name, "yt")
    yt_status = {"ok": False}
    async for ev in _youtube_fallback(user, title, output_dir, label, idx, yt_status,
                                      client=client, meta=meta):
        yield ev
    yield _track_event(idx, total, name, "done" if yt_status["ok"] else "failed")


async def stream_direct_api(
    req: DownloadRequest, output_dir: Path, url_changed: bool,
) -> AsyncGenerator[str, None]:
    """Direct SoundCloud API backend: read OAuth token from the environment,
    resolve the URL, iterate transcodings until one delivers a working stream URL."""
    import httpx

    if url_changed:
        yield sse({"type": "info", "msg": f"stripped query params -> {req.url}"})
    yield sse({"type": "info", "msg": f"saving to: {output_dir}"})

    token = SC_TOKEN
    if not token:
        yield sse({"type": "error",
                   "msg": "no SoundCloud token configured — set the SCDL_SC_TOKEN secret"})
        return
    yield sse({"type": "info", "msg": f"using configured oauth_token (...{token[-6:]})"})

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
            t_user = (root.get("user") or {}).get("username") or "unknown"
            t_title = root.get("title") or "untitled"
            yield sse({"type": "title", "title": f"{t_user} — {t_title}"})
        elif kind == "playlist":
            tracks = root.get("tracks", [])
            p_title = root.get("title", "?")
            yield sse({"type": "title",
                       "title": f"Playlist: {p_title} ({len(tracks)} tracks)"})
            yield sse({"type": "info",
                       "msg": f"playlist '{p_title}': {len(tracks)} tracks"})
        else:
            yield sse({"type": "error",
                       "msg": f"unsupported resource kind {kind!r} (need a track or playlist URL)"})
            return

        # Download tracks concurrently, multiplexing each worker's SSE events
        # onto the response through a queue. A semaphore bounds how many run at
        # once; per-track errors are caught so one bad track can't abort the rest.
        total = len(tracks)
        queue: asyncio.Queue = asyncio.Queue()
        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

        async def _run_track(i: int, t: dict) -> None:
            label = f"[{i}/{total}]" if total > 1 else ""
            async with sem:
                try:
                    async for ev in _process_track(client, client_id, t, output_dir, i, total):
                        await queue.put(ev)
                except Exception as e:
                    await queue.put(sse({"type": "info",
                                         "msg": f"{label} unexpected error, skipping: {e}"}))
                    await queue.put(_track_event(i, total, "", "failed"))

        tasks = [asyncio.create_task(_run_track(i, t)) for i, t in enumerate(tracks, 1)]

        async def _signal_done() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
            await queue.put(None)  # sentinel: every worker has finished

        signal_task = asyncio.create_task(_signal_done())
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    break
                yield ev
        finally:
            # Client disconnected or we're unwinding — stop any in-flight work.
            for tsk in tasks:
                tsk.cancel()
            signal_task.cancel()

        yield sse({"type": "done", "msg": "complete"})


async def _download_track(
    client, client_id: str, track: dict, output_dir: Path,
    user: str, title: str, label: str, status: dict, track_id: Optional[int] = None,
    *, meta: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """Attempt each transcoding for a single track until one succeeds."""
    import httpx

    if meta is None:
        meta = _extract_meta(track)

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

    # Best audio first. Progressive only wins ties (it's a single GET with no
    # ffmpeg), so when nothing outranks the 128 kbps mp3_1_0 we still take the
    # cheap byte-copy path below — but a plaintext AAC 256 no longer loses to it.
    plaintext.sort(key=lambda tc: (
        -_preset_score(tc.get("preset")),
        0 if (tc.get("format") or {}).get("protocol") == "progressive" else 1,
    ))

    art_cache: list = []   # one-slot memo so a retried transcoding doesn't refetch

    async def _tag(path: Path) -> None:
        """Fetch cover art (once per track) and write tags."""
        if not art_cache:
            art_cache.append(await _fetch_artwork(client, meta.get("artwork_url", "")))
        await asyncio.to_thread(_write_mp3_tags, path, meta, art_cache[0])

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
                size = out_path.stat().st_size
                if size == 0:
                    raise RuntimeError("empty response body")
                yield sse({"type": "info", "msg": f"{label}   saved {size:,} bytes"})
                try:
                    await _tag(out_path)
                    art_note = " + artwork" if art_cache[0] else ""
                    yield sse({"type": "info", "msg": f"{label}   tagged{art_note}"})
                except Exception as e:
                    yield sse({"type": "info", "msg": f"{label}   tag write failed: {e}"})
                yield _saved_event(out_path, track_id)
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
            yield sse({"type": "info", "msg": f"{label}   {proto} -> mp3 (libmp3lame 320 CBR) -> {out_path.name}"})
            # 320k CBR, not VBR: variable-bitrate MP3 can drift beatgrids and cue
            # points in DJ software. No -ar/-ac — resampling or downmixing would
            # only add a generation of loss, and every DJ app reads 44.1 and 48k.
            # -map_metadata -1 keeps ffmpeg out of the tags; mutagen owns them.
            ff_cmd = ["ffmpeg", "-y", "-loglevel", "warning",
                      "-i", stream_url,
                      "-vn", "-sn", "-dn",
                      "-map", "0:a:0",
                      "-map_metadata", "-1",
                      "-c:a", "libmp3lame", "-b:a", "320k", "-write_xing", "1",
                      str(out_path)]
            proc = None
            try:
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
            except Exception as e:
                # ffmpeg failed to spawn, or its input stream errored mid-read
                # (e.g. "Error in input stream"). Clean up and fall through to the
                # next transcoding instead of letting it crash the whole stream.
                yield sse({"type": "info", "msg": f"{label}   transcode failed: {e}"})
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                if out_path.exists():
                    out_path.unlink()
                continue
            if rc == 0 and out_path.exists() and out_path.stat().st_size > 0:
                size = out_path.stat().st_size
                yield sse({"type": "info", "msg": f"{label}   saved {size:,} bytes"})
                try:
                    await _tag(out_path)
                    art_note = " + artwork" if art_cache[0] else ""
                    yield sse({"type": "info", "msg": f"{label}   tagged{art_note}"})
                except Exception as e:
                    yield sse({"type": "info", "msg": f"{label}   tag write failed: {e}"})
                yield _saved_event(out_path, track_id)
                status["ok"] = True
                return
            else:
                yield sse({"type": "info", "msg": f"{label}   ffmpeg exit {rc} (no usable output)"})
                if out_path.exists():
                    out_path.unlink()
                continue

    yield sse({"type": "error",
               "msg": f"{label} every plaintext transcoding errored "
                      "(network/CDN/ffmpeg failure, not DRM)"})


def _make_output_path(output_dir: Path, user: str, title: str, ext: str) -> Path:
    return output_dir / f"[{sanitize_filename(user)}] {sanitize_filename(title)}.{ext}"


def _read_yt_path(path_file: Path, output_dir: Path) -> Optional[Path]:
    """Read the real output path yt-dlp wrote to its --print-to-file sidecar.

    Beats reconstructing the name ourselves: the download is only linkable if we
    can find it, and the filename depends on yt-dlp's own post-processing. The
    sidecar is removed either way.
    """
    try:
        lines = [ln.strip() for ln in path_file.read_text().splitlines() if ln.strip()]
    except OSError:
        return None
    finally:
        path_file.unlink(missing_ok=True)
    if not lines:
        return None
    path = Path(lines[-1])
    # yt-dlp is ours to trust here, but the file must still land where we asked —
    # /api/file only serves what we hand it a token for.
    try:
        path.resolve().relative_to(output_dir.resolve())
    except ValueError:
        return None
    return path


def _extract_meta(track: dict) -> dict:
    """Pull the tag-worthy fields out of a SoundCloud v2 track object.

    Only fields SoundCloud actually publishes — nothing is inferred or scraped
    out of free text. Note there is no BPM and no key field in the v2 track
    object (they're absent, not null), so we write neither; DJ software detects
    both on import anyway.
    """
    pub = track.get("publisher_metadata") or {}
    user = (track.get("user") or {}).get("username") or "unknown"

    # publisher_metadata.artist is the real artist on label uploads; the
    # uploader username is a poor stand-in ("MCG" for a Supermode track). Tags
    # only — _make_output_path deliberately stays on the username so filenames
    # don't shift under existing users.
    artist = (pub.get("artist") or "").strip() or user

    genre = (track.get("genre") or "").strip()
    if not genre:
        # tag_list is space-separated with multi-word entries quoted, e.g.
        # 'Techno House "Deep House"'. shlex handles the quoting; str.split
        # would shred it.
        try:
            tags = shlex.split(track.get("tag_list") or "")
        except ValueError:
            tags = []
        genre = tags[0] if tags else ""

    date = (track.get("release_date") or track.get("created_at") or "")
    year = date[:4] if len(date) >= 4 and date[:4].isdigit() else ""

    return {
        "title": track.get("title") or "untitled",
        "artist": artist,
        "uploader": user,
        "album": (pub.get("album_title") or "").strip(),
        "genre": genre,
        "year": year,
        "url": track.get("permalink_url") or "",
        "artwork_url": track.get("artwork_url") or (track.get("user") or {}).get("avatar_url") or "",
    }


# SoundCloud's artwork_url is a 100x100 "-large.jpg". The -t500x500 variant is
# 50-120 KB and the right size to embed; -original can be several MB.
_ART_SIZE_RE = re.compile(r"-(large|t\d+x\d+|original)\.(jpg|png)$", re.I)
_ART_MAX_BYTES = 2 * 1024 * 1024


async def _fetch_artwork(client, url: str) -> Optional[tuple[bytes, str]]:
    """Fetch cover art as (bytes, mime). Returns None on any failure — artwork
    is a nice-to-have and must never fail a download."""
    if not url:
        return None
    big = _ART_SIZE_RE.sub(lambda m: f"-t500x500.{m.group(2)}", url)
    try:
        # Reuse the connection pool but drop the OAuth header — the artwork CDN
        # serves these publicly, so there's no reason to hand it the token.
        request = client.build_request("GET", big, timeout=8)
        request.headers.pop("Authorization", None)
        r = await client.send(request)
        r.raise_for_status()
        data = r.content
    except Exception:
        return None
    if not data or len(data) > _ART_MAX_BYTES:
        return None
    if data[:2] == b"\xff\xd8":
        return data, "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    return None


def _write_mp3_tags(path: Path, meta: dict,
                    art: Optional[tuple[bytes, str]] = None) -> None:
    """Write ID3v2.3 tags. Only frames we have a value for are touched, so a
    byte-copied progressive MP3 keeps whatever the uploader already tagged it
    with (BPM and key included)."""
    from mutagen.id3 import (
        ID3, ID3NoHeaderError, APIC, COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2,
    )

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    uploader = meta.get("uploader")
    for frame, value in (
        (TIT2, meta.get("title")),
        (TPE1, meta.get("artist")),
        # Album-artist doubles as "who uploaded this", but only when it adds
        # something the artist frame doesn't already say.
        (TPE2, uploader if uploader != meta.get("artist") else None),
        (TALB, meta.get("album")),
        (TCON, meta.get("genre")),
        (TDRC, meta.get("year")),
    ):
        if value:
            tags.add(frame(encoding=3, text=value))

    if meta.get("url"):
        tags.add(COMM(encoding=3, lang="eng", desc="", text=meta["url"]))

    if art:
        data, mime = art
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))

    # v2.3 rather than v2.4: Rekordbox and Serato read it most reliably, and
    # mutagen downgrades TDRC to TYER/TDAT for us on save.
    tags.save(path, v2_version=3)


async def _youtube_fallback(
    user: str, title: str, output_dir: Path, label: str,
    track_id: Optional[int] = None, status: Optional[dict] = None,
    *, client=None, meta: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """When SC returns nothing playable, try yt-dlp ytsearch1 against YouTube.
    Output filename is prefixed [YouTube] so the source is unambiguous. Sets
    status["ok"] on success so the caller knows whether a file was produced."""
    if shutil.which("yt-dlp") is None:
        yield sse({"type": "error", "msg": f"{label} YT fallback: yt-dlp not on PATH"})
        return

    query = f"{user} {title}"
    # The literal part of -o is a yt-dlp output template, so any '%' the track
    # title carries would be read as a field reference: "Mix %(id)s Vol 2" gets
    # expanded to the YouTube id, and a bare "%(" aborts the run outright with
    # "incomplete format key". Double them so they survive as literal percents;
    # only our own trailing %(ext)s stays live.
    stem = f"[YouTube] [{sanitize_filename(user)}] {sanitize_filename(title)}".replace("%", "%%")
    out_template = output_dir / f"{stem}.%(ext)s"
    # yt-dlp reports where it actually put the file rather than us reconstructing
    # the name. Sidecar rather than --print because --print implies --quiet,
    # which would swallow the progress lines we stream to the client below.
    path_file = output_dir / f".ytpath-{uuid.uuid4().hex}"
    yield sse({"type": "info", "msg": f"{label} YT fallback: ytsearch1 {query!r}"})

    cmd = [
        "yt-dlp",
        f"ytsearch1:{query}",
        # 320K makes yt-dlp pass -b:a 320k to ffmpeg. Without it --audio-format
        # mp3 defaults to -q:a 5 VBR, the same beatgrid-drift trap we avoid on
        # the SoundCloud path. --embed-metadata is gone because we now write a
        # full tag set from the SoundCloud track below, overwriting it anyway.
        "-x", "--audio-format", "mp3", "--audio-quality", "320K",
        "--no-playlist",
        "--newline",
        "--js-runtimes", "bun",
        "-o", str(out_template),
        "--print-to-file", "after_move:filepath", str(path_file),
    ]
    if YT_COOKIES_FILE is not None:
        cmd += ["--cookies", str(YT_COOKIES_FILE)]
    else:
        yield sse({"type": "info",
                   "msg": f"{label} YT fallback: no SCDL_YT_COOKIES configured — may hit YouTube's bot wall"})
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
    produced = _read_yt_path(path_file, output_dir)
    if rc == 0:
        yield sse({"type": "info", "msg": f"{label} YT fallback: saved"})
        if produced is not None and produced.exists():
            # Replace yt-dlp's embedded YouTube metadata with the SoundCloud
            # metadata — that's still the right metadata for this track, even
            # though the audio came from YouTube.
            tag_meta = meta or {"title": title, "artist": user, "uploader": user}
            try:
                art = None
                if client is not None:
                    art = await _fetch_artwork(client, tag_meta.get("artwork_url", ""))
                await asyncio.to_thread(_write_mp3_tags, produced, tag_meta, art)
                yield sse({"type": "info",
                           "msg": f"{label} YT  tagged from SoundCloud"
                                  + (" + artwork" if art else "")})
            except Exception as e:
                yield sse({"type": "info", "msg": f"{label} YT  tag write failed: {e}"})
            if status is not None:
                status["ok"] = True
            yield _saved_event(produced, track_id)
        else:
            yield sse({"type": "info",
                       "msg": f"{label} YT fallback: couldn't locate output for download link"})
    else:
        yield sse({"type": "error", "msg": f"{label} YT fallback: yt-dlp exit {rc}"})


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
