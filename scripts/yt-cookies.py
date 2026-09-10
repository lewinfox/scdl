#!/usr/bin/env python3
"""Export YouTube cookies from a local browser, scoped to youtube.com.

`--cookies-from-browser` hands back the *entire* browser profile — several
thousand cookies across hundreds of hosts, including live Gmail, Drive and
cloud-console sessions. Shipping that to Fly would put far more in an
environment variable than the YouTube fallback needs, so this filters the jar
down to youtube.com before it goes anywhere.

Usage is via the Makefile: `make yt-cookies`.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Any host in the youtube.com family. The "#HttpOnly_" prefix is part of the
# Netscape format, not a comment marker — filtering on a leading "#" alone
# would smuggle in every other site's httponly cookies.
YOUTUBE = re.compile(r"^(#HttpOnly_)?\.?([a-z0-9-]+\.)*youtube\.com$", re.I)

# Without at least one of these the jar carries no session and is not worth
# deploying.
AUTH_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"}

# Any URL will do — yt-dlp writes the cookie jar out regardless of whether the
# extraction itself succeeds, and we skip the download.
PROBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def extract(browser: str, raw: Path) -> None:
    """Dump the browser's whole cookie jar to `raw` via yt-dlp."""
    cmd = [
        "yt-dlp",
        "--cookies-from-browser", browser,
        "--cookies", str(raw),
        "--skip-download",
        "--no-warnings",
        "--js-runtimes", "bun,node,deno",
        PROBE_URL,
    ]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    # A non-zero exit is expected and fine: YouTube's anti-bot challenge often
    # fails here, but the jar is written on the way out either way.
    subprocess.run(cmd, check=False)
    if not raw.exists():
        sys.exit(
            f"yt-dlp wrote no cookie file. Is {browser} installed and logged "
            "in to YouTube? Close it and retry if the database is locked."
        )


def scope(raw: Path, out: Path) -> tuple[int, int]:
    """Copy only the youtube.com cookies from `raw` into `out`."""
    kept, dropped = [], 0
    for line in raw.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue  # a real header comment; we write our own below
        domain = line.split("\t", 1)[0]
        if YOUTUBE.match(domain):
            kept.append(line)
        else:
            dropped += 1
    out.write_text("# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n",
                   encoding="utf-8")
    out.chmod(0o600)
    return len(kept), dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="firefox",
                    help="browser to read cookies from (default: firefox)")
    ap.add_argument("--out", type=Path,
                    help="write the scoped jar here instead of a temp file")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="scdl-cookies-"))
    tmp.chmod(0o700)
    raw, scoped = tmp / "raw.txt", args.out or (tmp / "youtube.txt")
    try:
        extract(args.browser, raw)
        kept, dropped = scope(raw, scoped)
        names = {
            line.split("\t")[5]
            for line in scoped.read_text(encoding="utf-8").splitlines()
            if line.count("\t") >= 6
        }
        print(f"\nkept {kept} youtube.com cookies, dropped {dropped} others",
              file=sys.stderr)
        if not names & AUTH_COOKIES:
            sys.exit(
                "None of the session cookies are present, so this jar would "
                "not authenticate anything.\nLog in to YouTube in "
                f"{args.browser} and try again."
            )
        if args.out:
            print(f"wrote {scoped}", file=sys.stderr)
        else:
            # Straight to stdout so the caller can pipe it into a secret
            # without the credentials ever touching a file it has to remember
            # to delete.
            sys.stdout.write(scoped.read_text(encoding="utf-8"))
    finally:
        for f in (raw, scoped):
            if f.exists() and f != args.out:
                # Overwrite before unlinking; these are live credentials.
                with open(f, "r+b") as fh:
                    length = fh.seek(0, os.SEEK_END)
                    fh.seek(0)
                    fh.write(b"\0" * length)
                f.unlink()
        if tmp.exists():
            tmp.rmdir()


if __name__ == "__main__":
    main()
