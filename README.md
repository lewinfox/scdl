# scdl — SoundCloud & Spotify Archiver

Tiny self-hosted web app for grabbing SoundCloud and Spotify tracks/playlists
as MP3. DRM-locked tracks transparently fall back to a YouTube search via
`yt-dlp`. A glowing "Download" button lights up when each file is ready;
clicking it streams the file to your browser and deletes it from the server.

Paste a `soundcloud.com/…` or `open.spotify.com/…` link and the app figures
out which backend to use.

> **Note on Spotify:** Spotify's audio is DRM-encrypted, so — unlike
> SoundCloud — there's no cookie that unlocks a downloadable file. The app
> uses the Spotify API purely to read each track/playlist's artist and title,
> then fetches the actual audio from YouTube (the same fallback used for
> DRM-locked SoundCloud tracks). Quality is whatever YouTube has.

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
    volumes:
      # Staging dir for in-flight downloads. Files are deleted from disk once
      # the browser fetches them, so this mostly stays empty.
      - ./downloads:/app/downloads
      # Persistent auth: SC oauth_token + (optional) YouTube cookies.txt.
      # Set via the in-app Auth panel.
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

## First-time auth

Open `http://<host>:8765`. The **Auth** panel walks you through:

- **SoundCloud oauth_token** — paste the cookie value from your logged-in
  browser. The server validates it against SoundCloud's `/me` and shows the
  username so you know you copied the right cell. Per-browser steps are
  inside the panel (Chromium / Firefox / Safari).
- **Spotify client ID + secret** (only needed for Spotify links) — create a
  free app at
  [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
  and paste the Client ID and Client secret. These read public catalogue
  metadata only (Client Credentials flow) — no account access, no private
  playlists. The server validates them by fetching a token before saving.
- **YouTube cookies.txt** (optional) — paste a Netscape-format export if you
  want the DRM fallback to dodge YouTube's bot challenge. Otherwise the
  fallback works for many tracks but not all.

Both are stored under `SCDL_DATA_DIR` (`/data` in the container). The SC
token typically lasts weeks-to-months; re-paste only when SoundCloud starts
rejecting it.

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

- **No auth on the web app itself.** Anyone who can reach `:8765` can save
  or replace the stored SC token. Fine on a home LAN; if you expose it past
  that, put it behind a reverse proxy, VPN, or Tailscale.
- Token + cookies live in plaintext on the volume (`chmod 600` best-effort,
  silently ignored on some bind-mounted filesystems).
- The image uses `python:3.13-slim` because `bun` (the JS runtime yt-dlp
  needs for YouTube extraction) requires glibc. Don't swap the base for
  Alpine without also swapping the JS runtime.
