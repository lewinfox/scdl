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

# Use the image's Python rather than letting uv download its own, and compile
# bytecode up front for a faster boot.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Dependencies first, in their own layer, so editing main.py doesn't
# reinstall them. --locked fails the build if uv.lock is out of date with
# pyproject.toml rather than quietly resolving something new.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

COPY main.py index.html login.html ./

ENV PATH=/app/.venv/bin:$PATH

EXPOSE 8765

# main.py's __main__ binds 127.0.0.1; in a container we need 0.0.0.0.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
