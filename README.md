# scdl — SoundCloud Archiver

Tiny self-hosted web app for grabbing SoundCloud tracks as MP3. DRM-locked
tracks transparently fall back to a YouTube search via `yt-dlp`. A glowing
"Download" button lights up when each file is ready; clicking it streams the
file to your browser and deletes it from the server.

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

## Local dev

```sh
uv run main.py
```

Boots on `http://127.0.0.1:8765`. Inline PEP 723 metadata in `main.py`
resolves deps automatically. Data dir defaults to `./data` (gitignored).

The bundled `docker-compose.yml` builds from source rather than pulling
GHCR — handy when iterating on the Dockerfile itself:

```sh
docker compose up --build
```

## Caveats

- Credentials come from the environment (Fly secrets / env vars), so they're
  never exposed or editable through the UI. The YT cookies are spilled to a
  `chmod 600` temp file at startup so `yt-dlp` can read them.
- The image uses `python:3.13-slim` because `bun` (the JS runtime yt-dlp
  needs for YouTube extraction) requires glibc. Don't swap the base for
  Alpine without also swapping the JS runtime.
