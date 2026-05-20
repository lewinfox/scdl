FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to extract YouTube formats. Bun is the smallest
# option that yt-dlp supports out of the box.
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH=/root/.bun/bin:$PATH

WORKDIR /app

RUN pip install --no-cache-dir \
        "fastapi>=0.110" \
        "uvicorn>=0.27" \
        "yt-dlp>=2024.10" \
        "httpx>=0.27" \
        "mutagen>=1.47"

COPY main.py index.html ./

EXPOSE 8765

# main.py's __main__ binds 127.0.0.1; in a container we need 0.0.0.0.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
