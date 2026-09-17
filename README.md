# scdl — SoundCloud Archiver

Tiny self-hosted web app for grabbing SoundCloud tracks as MP3. It picks the
highest-quality stream SoundCloud will serve and tags the result with artwork,
artist, title, album, genre and year. Streams that are already MP3 are copied
byte-for-byte; anything else is encoded at 320 kbps CBR — constant bitrate, so
DJ software beatgrids don't drift. DRM-locked tracks transparently fall back to
a YouTube search via `yt-dlp`. A glowing "Download" button lights up when each
file is ready; clicking it streams the file to your browser and deletes it from
the server.

## Deploying via Docker Compose

CI publishes the image to GHCR on every merge to `main` — multi-arch
(`linux/amd64`, `linux/arm64`), so the same tag works on x86_64 and on a Pi
4/5:

```
ghcr.io/<your-gh-username>/scdl:latest
```

Drop something like this into your stack:

```yaml
services:
  scdl:
    image: ghcr.io/<your-gh-username>/scdl:latest
    ports:
      - "8765:8765"
    environment:
      - SCDL_DATA_DIR=/data
      # Credentials are read from the environment, not the UI. Supply the
      # SoundCloud oauth_token, and optionally a Netscape cookies.txt for the
      # YouTube fallback. Keep these out of the compose file in practice — use
      # an .env file or your orchestrator's secret store.
      - SCDL_SC_TOKEN=${SCDL_SC_TOKEN}
      - SCDL_YT_COOKIES=${SCDL_YT_COOKIES}
    volumes:
      # Staging dir for in-flight downloads. Files are deleted from disk once
      # the browser fetches them, so this mostly stays empty.
      - ./downloads:/app/downloads
      # Session secret for the login gate. Credentials no longer live here.
      - scdl-data:/data
    restart: unless-stopped

volumes:
  scdl-data:
```

The image tags published are:

- `latest` — head of `main`
- `sha-<short>` — every build, for pinning

If the GHCR package is private (the default for a new package), log in once
on the host with a PAT that has `read:packages`:

```sh
echo "$GHCR_PAT" | docker login ghcr.io -u <user> --password-stdin
```

Or flip the package to public from its Settings page on GitHub if you'd
rather not bother with login.

## Credentials

Both credentials are read from environment variables — there's no in-app
configuration. On Fly, set them as secrets:

```sh
flyctl secrets set SCDL_SC_TOKEN="<oauth_token value>"
# optional, for the YouTube DRM fallback:
flyctl secrets set SCDL_YT_COOKIES="$(cat cookies.txt)"
```

- **`SCDL_SC_TOKEN`** — the `oauth_token` cookie value from a logged-in
  SoundCloud session. In DevTools: Application → Cookies → `https://soundcloud.com`
  → copy the **Value** of the `oauth_token` row (not the name). The SC token
  typically lasts weeks-to-months; re-set the secret only when SoundCloud
  starts rejecting it.
- **`SCDL_YT_COOKIES`** (optional) — the full contents of a Netscape-format
  `cookies.txt` export. With it, the DRM fallback dodges YouTube's bot
  challenge; without it the fallback works for many tracks but not all. The
  app spills this to a temp file at startup for `yt-dlp --cookies`.

## Checks

```sh
make check    # lint, format-check, and import main.py on the image's Python
make format   # apply ruff formatting and autofixes
```

CI runs `make check` on every PR and before any build, so a broken import is
caught before merge rather than by Fly's smoke check after deploy.

Dependencies are declared once, in `pyproject.toml`, and resolved into
`uv.lock`. `make check`, local runs and the Docker image all install from that
lock, so they get the same versions. `make check` fails if the lock is out of
date; run `uv lock` after editing the dependency list.

The import step matters more than it looks. It runs against the Python version
parsed out of the `Dockerfile`, so the two can't drift apart. When local and
deployed disagreed on the version, a bad annotation passed locally and crashed
the container on boot.

Lint rules are pinned in `pyproject.toml` rather than inherited from ruff's
defaults, which move between releases — a laptop and CI should never disagree
about what passes.

## YouTube cookies

The DRM fallback searches YouTube, which challenges unauthenticated requests.
Cookies get past that. Google invalidates them fairly aggressively, so expect
to re-run this periodically — **the UI shows a banner** when they expire, are
about to, or get rejected mid-download.

```sh
make yt-cookies          # re-export from Firefox, scope, push to Fly
make yt-cookies-check    # is the secret set?
make yt-cookies-revoke   # remove it (the fallback then runs unauthenticated)
```

`BROWSER=chrome make yt-cookies` if you don't use Firefox.

Two things worth knowing:

- **Use a throwaway Google account.** Even scoped to youtube.com these are
  Google account credentials, not YouTube-only tokens, and they end up in a
  Fly secret that is spilled to a temp file inside the container.
- **`--cookies-from-browser` dumps your entire browser profile** — thousands of
  cookies across hundreds of hosts, live Gmail and cloud-console sessions
  included. `scripts/yt-cookies.py` filters it to youtube.com before anything
  leaves the machine, and pipes the result straight into `flyctl` so the
  credentials never touch a file you have to remember to delete. Don't
  shortcut it by piping `yt-dlp --cookies` output to `flyctl` yourself.

`make yt-cookies-revoke` only removes the Fly secret. If you think the cookies
leaked, sign the session out at
[myaccount.google.com](https://myaccount.google.com/device-activity), which
invalidates them everywhere.

## Logs

Two levels. `INFO` (the default) is a few lines per track — what it is, which
stream won and why, what got written, how long it took:

```
09:21:59 INFO  [3/12] SKILAH — Earthquake
09:22:00 INFO  [3/12] transcoded aac_160k -> mp3 320 CBR -> [SKILAH] Earthquake.mp3 (665,325 bytes in 0.9s)
09:22:00 INFO  [3/12] tagged: artist='SKILAH' title='Earthquake' genre='Techhouse' year='2025' art=88KB image/jpeg
09:22:00 INFO  [3/12] done from SoundCloud in 1.0s
```

`SCDL_LOG_LEVEL=DEBUG` adds the full transcoding ranking (so a surprising
choice can be traced to its inputs), raw ffmpeg and yt-dlp output, and timings:

```
DEBUG [3/12] 3 transcoding(s) advertised, 2 plaintext; ranked:
DEBUG [3/12]   1. preset=aac_160k  score=160  proto=hls         mime=audio/mp4
DEBUG [3/12]   2. preset=mp3_1_0   score=128  proto=progressive mime=audio/mpeg
DEBUG [3/12]   (1 encrypted variant(s) filtered out before ranking)
```

Records go to stdout (`docker compose logs -f`) and to a fixed-size in-memory
ring buffer, readable at **`/api/logs`** (`?n=` for how many lines, newest
last). The ring buffer is why DEBUG is safe to leave on for a while: it holds
`SCDL_LOG_BUFFER` lines (default 500) and evicts the oldest, so nothing grows
without bound.

Signed CDN URLs are logged with their query string stripped, and the
SoundCloud OAuth token is never logged.

| Variable | Default | Effect |
|---|---|---|
| `SCDL_LOG_LEVEL` | `INFO` | `DEBUG` for per-transcoding detail |
| `SCDL_LOG_BUFFER` | `500` | Lines held in memory for `/api/logs` |

## Local dev

```sh
make run    # uv run --locked main.py
```

Boots on `http://127.0.0.1:8765`. uv creates `.venv` from `uv.lock` on first
run. Data dir defaults to `./data` (gitignored).

The bundled `docker-compose.yml` builds from source rather than pulling
GHCR — handy when iterating on the Dockerfile itself:

```sh
docker compose up --build
```

## Caveats

- Credentials come from the environment (Fly secrets / env vars), so they're
  never exposed or editable through the UI. The YT cookies are spilled to a
  `chmod 600` temp file at startup so `yt-dlp` can read them.
- The image uses `python:3.14-slim` because `bun` (the JS runtime yt-dlp
  needs for YouTube extraction) requires glibc. Don't swap the base for
  Alpine without also swapping the JS runtime.
