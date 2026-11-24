# yt-shelf: download YouTube videos into Jellyfin's YouTube folder. State in /data, videos in /media/youtube.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv
# yt-dlp needs a JavaScript runtime for YouTube's player challenges
COPY --from=denoland/deno:bin-2.8.3 /deno /usr/local/bin/deno

WORKDIR /app
ENV LD_LIBRARY_PATH=/app/.venv/lib/python3.12/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cudnn/lib
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never \
    YTSHELF_DATA=/data PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# YouTube breaks old yt-dlp versions often: always the newest one (deploy.sh changes this arg once a day)
ARG YTDLP_REFRESH=0
RUN uv pip install --upgrade "yt-dlp[default]" && yt-dlp --version
COPY ytshelf ./ytshelf

RUN useradd -u 1000 -m ytshelf && mkdir -p /data && chown ytshelf /data
USER ytshelf

EXPOSE 8780
VOLUME /data
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/health', timeout=4)"
CMD ["python", "-m", "ytshelf.web", "--host", "0.0.0.0", "--port", "8780"]
