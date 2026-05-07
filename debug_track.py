# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27",
#   "yt-dlp>=2024.10",
# ]
# ///
"""Walk SoundCloud's API for a given track URL and dump everything to ./debug/.

Usage: uv run debug_track.py <track_url> [--browser firefox]

Produces:
  debug/track.json           full /resolve response
  debug/<preset>.m3u8        each successfully-resolved HLS manifest
  debug/<preset>.key         AES key (if m3u8 declares one), or 'FAILED'
  debug/<preset>.seg00.bin   first segment bytes for each variant
"""
import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx

SC_API = "https://api-v2.soundcloud.com"
DEBUG_DIR = Path(__file__).parent / "debug"


class _Quiet:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def read_oauth_token(spec: str) -> str | None:
    from yt_dlp.cookies import extract_cookies_from_browser
    name, _, profile = spec.partition(":")
    jar = extract_cookies_from_browser(name, profile=profile or None, logger=_Quiet())
    for c in jar:
        if c.name == "oauth_token" and "soundcloud.com" in (c.domain or ""):
            return c.value
    return None


async def fetch_client_id(client: httpx.AsyncClient) -> str:
    home = await client.get("https://soundcloud.com/")
    home.raise_for_status()
    scripts = re.findall(r'<script[^>]+src="(https://[^"]+\.js)"', home.text)
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
    raise RuntimeError("client_id not found")


def hr(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


async def main(url: str, browser: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)

    token = read_oauth_token(browser)
    if not token:
        print(f"no oauth_token cookie for soundcloud.com in {browser}")
        sys.exit(1)
    print(f"token: ...{token[-6:]} (len={len(token)})")

    headers = {
        "Authorization": f"OAuth {token}",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    }
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        client_id = await fetch_client_id(client)
        print(f"client_id: {client_id}")

        r = await client.get(f"{SC_API}/resolve",
                             params={"url": url, "client_id": client_id})
        r.raise_for_status()
        track = r.json()
        (DEBUG_DIR / "track.json").write_text(json.dumps(track, indent=2))
        print(f"track.json saved -> {DEBUG_DIR}/track.json")

        hr("track summary")
        print(json.dumps({
            "id": track.get("id"),
            "kind": track.get("kind"),
            "title": track.get("title"),
            "user": (track.get("user") or {}).get("username"),
            "duration_ms": track.get("duration"),
            "policy": track.get("policy"),       # 'ALLOW', 'BLOCK', 'SNIP', 'MONETIZE'
            "monetization_model": track.get("monetization_model"),
            "sub_high_tier_only": track.get("sub_high_tier_only"),
            "downloadable": track.get("downloadable"),
            "has_track_auth": bool(track.get("track_authorization")),
        }, indent=2))

        transcodings = (track.get("media") or {}).get("transcodings") or []
        hr(f"{len(transcodings)} transcodings advertised")
        print(json.dumps([
            {"preset": t.get("preset"), "format": t.get("format"), "quality": t.get("quality")}
            for t in transcodings
        ], indent=2))

        track_auth = track.get("track_authorization")

        for tc in transcodings:
            preset = tc.get("preset", "unknown")
            fmt = tc.get("format") or {}
            proto = fmt.get("protocol", "?")
            tc_url = tc.get("url")
            if not tc_url:
                continue

            hr(f"format-resolve: {preset} / {proto}")
            params = {"client_id": client_id}
            if track_auth:
                params["track_authorization"] = track_auth
            r = await client.get(tc_url, params=params)
            print(f"status: {r.status_code}")
            if r.status_code != 200:
                print(f"body: {r.text[:300]}")
                continue
            body = r.json()
            stream_url = body.get("url")
            print(f"stream_url: {stream_url}")

            if not stream_url:
                continue

            if proto != "hls":
                # progressive — just fetch a few KB and identify
                head = await client.get(stream_url, headers={"Range": "bytes=0-65535"})
                seg_path = DEBUG_DIR / f"{preset}.head.bin"
                seg_path.write_bytes(head.content)
                print(f"first 64KB saved -> {seg_path} ({len(head.content)} bytes, status {head.status_code})")
                print(f"  first 32 bytes hex: {head.content[:32].hex()}")
                continue

            m3u8 = await client.get(stream_url)
            print(f"m3u8 status: {m3u8.status_code}")
            m3u8_path = DEBUG_DIR / f"{preset}.m3u8"
            m3u8_path.write_text(m3u8.text)
            print(f"m3u8 saved -> {m3u8_path}")
            print(f"--- m3u8 contents ---\n{m3u8.text}")

            # Look for AES key directive
            key_match = re.search(
                r'#EXT-X-KEY:[^\n]*URI="([^"]+)"', m3u8.text)
            if key_match:
                key_url = key_match.group(1)
                if not key_url.startswith("http"):
                    key_url = urljoin(stream_url, key_url)
                print(f"AES key URL: {key_url}")
                key_resp = await client.get(key_url)
                key_path = DEBUG_DIR / f"{preset}.key"
                if key_resp.status_code == 200:
                    key_path.write_bytes(key_resp.content)
                    print(f"key fetched ({len(key_resp.content)} bytes) -> {key_path}")
                else:
                    key_path.write_text(f"FAILED status={key_resp.status_code}\n{key_resp.text[:300]}")
                    print(f"key fetch FAILED status={key_resp.status_code}")

            # Pull the first segment
            seg_urls = [ln.strip() for ln in m3u8.text.splitlines()
                        if ln.strip() and not ln.startswith("#")]
            if seg_urls:
                seg_url = seg_urls[0]
                if not seg_url.startswith("http"):
                    seg_url = urljoin(stream_url, seg_url)
                seg_resp = await client.get(seg_url)
                seg_path = DEBUG_DIR / f"{preset}.seg00.bin"
                seg_path.write_bytes(seg_resp.content)
                print(f"segment 0 saved -> {seg_path} ({len(seg_resp.content)} bytes, status {seg_resp.status_code})")
                print(f"  first 32 bytes hex: {seg_resp.content[:32].hex()}")
                file_out = subprocess.run(
                    ["file", str(seg_path)], capture_output=True, text=True
                ).stdout.strip()
                print(f"  file: {file_out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--browser", default="firefox")
    args = ap.parse_args()
    asyncio.run(main(args.url, args.browser))
