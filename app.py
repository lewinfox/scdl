# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi>=0.110",
#   "uvicorn>=0.27",
#   "yt-dlp>=2024.10",
#   "httpx>=0.27",
# ]
# ///
import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI()

DEFAULT_DOWNLOAD_DIR = Path(__file__).parent / "downloads"
SC_API = "https://api-v2.soundcloud.com"


class DownloadRequest(BaseModel):
    url: str
    output_dir: Optional[str] = None
    browser: str = "firefox"          # browser to read cookies from, e.g. "firefox" or "firefox:profile-name"
    yt_fallback: bool = False         # try YouTube via yt-dlp when SC returns DRM-only


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


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
    req: DownloadRequest, output_dir: Path, url_changed: bool
) -> AsyncGenerator[str, None]:
    """Direct SoundCloud API backend: read OAuth token from browser cookies,
    resolve the URL, iterate transcodings until one delivers a working stream URL."""
    import httpx

    if url_changed:
        yield sse({"type": "info", "msg": f"stripped query params -> {req.url}"})
    yield sse({"type": "info", "msg": f"saving to: {output_dir}"})

    browser_name = req.browser or "firefox"
    try:
        token = await asyncio.to_thread(read_browser_oauth_token, browser_name)
    except Exception as e:
        yield sse({"type": "error", "msg": f"could not read {browser_name} cookies: {e}"})
        return
    if not token:
        yield sse({"type": "error",
                   "msg": f"no oauth_token cookie found for soundcloud.com in {browser_name}. "
                          "Log in to soundcloud.com in that browser first."})
        return
    yield sse({"type": "info", "msg": f"got oauth_token from {browser_name} (...{token[-6:]})"})

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
            if not status["ok"] and req.yt_fallback:
                async for ev in _youtube_fallback(user, title, output_dir, label, req.browser):
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
        yield sse({"type": "error",
                   "msg": (f"{label} DRM-locked. SoundCloud only offers encrypted variants "
                           f"for this track ({', '.join(protos)}); they require a Widevine/"
                           "PlayReady CDM to decrypt. GO+ doesn't change this — labels mark "
                           "their catalog DRM-only regardless of subscription tier. "
                           "Enable 'Fall back to YouTube' to grab the same title via yt-dlp.")})
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


async def _youtube_fallback(
    user: str, title: str, output_dir: Path, label: str, browser: str,
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
        "--no-playlist",
        "--newline",
        "--cookies-from-browser", browser,
        "--js-runtimes", "bun",
        "-o", str(out_template),
    ]
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
    else:
        yield sse({"type": "error", "msg": f"{label} YT fallback: yt-dlp exit {rc}"})


def read_browser_oauth_token(browser_spec: str) -> Optional[str]:
    """Read the soundcloud.com `oauth_token` cookie from the named browser.

    `browser_spec` is yt-dlp's `--cookies-from-browser` syntax: 'firefox',
    'chrome:Profile 1', etc. Returns the token value or None if not present.
    """
    from yt_dlp.cookies import extract_cookies_from_browser

    class _Quiet:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass

    name, _, profile = browser_spec.partition(":")
    jar = extract_cookies_from_browser(name, profile=profile or None, logger=_Quiet())
    for c in jar:
        if c.name == "oauth_token" and "soundcloud.com" in (c.domain or ""):
            return c.value
    return None


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


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SoundCloud Archiver</title>
<style>
  :root { color-scheme: dark; --bg:#16181d; --panel:#1b1f26; --panel-2:#1f242c;
          --text:#e6e8eb; --muted:#aab1bd; --border:#2c333d; --accent:#4f8cff;
          --good:#6ee7b7; --bad:#fca5a5; --info:#93c5fd; }
  * { box-sizing: border-box; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
         background: var(--bg); color: var(--text); margin: 0;
         padding: 2.5rem 1.5rem; }
  .wrap { max-width: 720px; margin-inline: auto; }
  header { margin-bottom: 1.5rem; }
  h1 { margin: 0; font-size: 1.5rem; letter-spacing: -0.01em; }
  .tag { color: var(--muted); font-size: 0.9rem; margin-top: 0.25rem; }
  .howto { background: var(--panel); border: 1px solid var(--border);
           border-radius: 10px; padding: 1.1rem 1.3rem; margin-bottom: 1.5rem; }
  .howto h2 { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.08em;
              color: var(--muted); margin: 0 0 0.6rem; font-weight: 600; }
  .howto ol { margin: 0; padding-left: 1.2rem; }
  .howto ol li { margin: 0.3rem 0; font-size: 0.92rem; line-height: 1.45; }
  .howto .note { color: var(--muted); font-size: 0.83rem; margin-top: 0.7rem;
                 line-height: 1.5; }
  .howto code { background: var(--panel-2); padding: 0.05em 0.4em; border-radius: 4px;
                font-size: 0.85em; }
  form { display: grid; gap: 0.85rem; background: var(--panel);
         border: 1px solid var(--border); border-radius: 10px; padding: 1.3rem; }
  label { display: grid; gap: 0.3rem; font-size: 0.85rem; color: var(--muted); }
  label.inline { display: flex; flex-direction: row; align-items: center;
                 gap: 0.55rem; cursor: pointer; }
  label.inline input { width: auto; }
  input[type=text], input[type=url] { background: var(--panel-2); color: inherit;
          border: 1px solid var(--border); border-radius: 6px;
          padding: 0.55rem 0.75rem; font: inherit; }
  input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  button { background: var(--accent); color: white; border: 0; border-radius: 6px;
           padding: 0.7rem 1.2rem; font: inherit; font-weight: 600; cursor: pointer;
           justify-self: start; transition: filter 120ms; }
  button:hover:not(:disabled) { filter: brightness(1.1); }
  button:disabled { background: #2c333d; cursor: not-allowed; }
  .status-row { display: flex; align-items: center; gap: 0.6rem; margin: 1rem 0 0.4rem;
                font-size: 0.85rem; color: var(--muted); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: var(--muted); }
  .dot.info { background: var(--info); animation: pulse 1.4s ease-in-out infinite; }
  .dot.done { background: var(--good); }
  .dot.error { background: var(--bad); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  pre#log { background: #0f1115; border: 1px solid var(--border); border-radius: 8px;
            padding: 1rem; min-height: 200px; max-height: 60vh; overflow: auto;
            white-space: pre-wrap; font-family: ui-monospace, "SF Mono", Menlo, monospace;
            font-size: 0.8rem; line-height: 1.5; margin: 0.5rem 0 0; }
  .status-done { color: var(--good); }
  .status-error { color: var(--bad); }
  .status-info { color: var(--info); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>SoundCloud Archiver</h1>
    <div class="tag">Archive your own SoundCloud uploads as MP3, locally.</div>
  </header>

  <section class="howto">
    <h2>How to use</h2>
    <ol>
      <li>Log into <a href="https://soundcloud.com" style="color:var(--accent);">soundcloud.com</a> in your browser (Firefox by default).</li>
      <li>Paste a track or playlist URL below.</li>
      <li>Hit <strong>Download</strong>. Files save as <code>[Artist] Title.mp3</code> in <code>./downloads</code>.</li>
    </ol>
    <p class="note">
      The tool reads your <code>oauth_token</code> cookie from the named browser to
      authenticate. It works for any track you can play in your browser <em>that isn't
      DRM-locked</em> (most major-label catalog is). For DRM-locked tracks, enable the
      YouTube fallback to grab the same song via <code>yt-dlp</code> instead.
    </p>
  </section>

  <form id="f">
    <label>SoundCloud URL
      <input id="url" type="url" required
             placeholder="https://soundcloud.com/you/your-track">
    </label>
    <label>Browser to read cookies from
      <input id="browser" type="text" value="firefox"
             placeholder="firefox | chrome | brave | firefox:profile-name">
    </label>
    <label>Output directory <span style="opacity:0.6;">(optional)</span>
      <input id="output_dir" type="text" placeholder="leave blank for ./downloads">
    </label>
    <label class="inline">
      <input id="yt_fallback" type="checkbox">
      <span>If the track is DRM-locked, fall back to YouTube
        <span style="color:var(--muted); font-size: 0.8em;">
          (filename will be prefixed <code>[YouTube]</code>)
        </span>
      </span>
    </label>
    <button id="go" type="submit">Download</button>
  </form>

  <div class="status-row">
    <span class="dot" id="dot"></span>
    <span id="status">idle</span>
  </div>
  <pre id="log"></pre>
</div>

<script>
const form = document.getElementById('f');
const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const dotEl = document.getElementById('dot');
const goBtn = document.getElementById('go');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  logEl.replaceChildren();
  setStatus('starting...', 'info');
  goBtn.disabled = true;

  const body = {
    url: document.getElementById('url').value,
    output_dir: document.getElementById('output_dir').value || null,
    browser: document.getElementById('browser').value || 'firefox',
    yt_fallback: document.getElementById('yt_fallback').checked,
  };

  try {
    const resp = await fetch('/api/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      setStatus('http ' + resp.status, 'error');
      logEl.textContent = await resp.text();
      goBtn.disabled = false;
      return;
    }
    await readStream(resp.body.getReader());
  } catch (err) {
    setStatus('network error', 'error');
    appendLine(String(err));
  } finally {
    goBtn.disabled = false;
  }
});

async function readStream(reader) {
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream: true});
    let idx;
    while ((idx = buf.indexOf('\\n\\n')) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split('\\n')) {
        if (line.startsWith('data: ')) handleEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}

function handleEvent(ev) {
  if (ev.type === 'log' || ev.type === 'info') appendLine(ev.msg);
  if (ev.type === 'done') { appendLine('-- done --'); setStatus('done', 'done'); }
  if (ev.type === 'error') { appendLine('-- error: ' + ev.msg + ' --'); setStatus('error', 'error'); }
  if (ev.type === 'info') setStatus('running...', 'info');
}

const LOG_MAX_LINES = 2000;
let _scrollPending = false;
function appendLine(line) {
  logEl.appendChild(document.createTextNode(line + '\\n'));
  while (logEl.childNodes.length > LOG_MAX_LINES) {
    logEl.removeChild(logEl.firstChild);
  }
  if (!_scrollPending) {
    _scrollPending = true;
    requestAnimationFrame(() => {
      logEl.scrollTop = logEl.scrollHeight;
      _scrollPending = false;
    });
  }
}

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = 'status-' + kind;
  dotEl.className = 'dot ' + kind;
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
