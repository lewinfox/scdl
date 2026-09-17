FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to extract YouTube formats. Bun is the smallest
# option that yt-dlp supports out of the box. Pinned because yt-dlp lags bun
# releases and warns that newer versions are unsupported.
RUN curl -fsSL https://bun.sh/install | bash -s "bun-v1.3.14"
ENV PATH=/root/.bun/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /bin/uv

WORKDIR /app

RUN uv pip install --system --no-cache \
        "fastapi>=0.110" \
        "uvicorn>=0.27" \
        # [default] pulls in yt-dlp-ejs, the script bun runs to solve
        # YouTube's JS challenges. Without it every YouTube download fails
        # with "The page needs to be reloaded".
        "yt-dlp[default]>=2025.11" \
        "httpx>=0.27" \
        "mutagen>=1.47"

COPY main.py index.html login.html ./

EXPOSE 8765

# main.py's __main__ binds 127.0.0.1; in a container we need 0.0.0.0.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
