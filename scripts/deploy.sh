#!/usr/bin/env bash
# Deploy to the home server. Refuses while something is downloading or transcribing (a restart would cut it off;
# downloads resume, Whisper starts over). Usage: scripts/deploy.sh [--force]
set -euo pipefail
HOST=joost@192.168.1.100
DIR=/srv/yt-shelf
cd "$(dirname "$0")/.."
busy=$(ssh "$HOST" 'curl -sk -m 10 https://youtube.joost.im/api/jobs' 2>/dev/null || echo '{}')
if [[ "${1:-}" != "--force" ]] && grep -q '"state":"running"' <<<"$busy"; then
  echo "Not deploying: a job is running (use --force to cut it off)" >&2
  exit 1
fi
rsync -a --exclude .git --exclude data --exclude .env --exclude .venv --exclude .pytest_cache --exclude __pycache__ \
  --exclude .claude ./ "$HOST:$DIR/"
ssh "$HOST" "mkdir -p $DIR/data && cd $DIR && YTDLP_REFRESH=$(date +%F) docker compose up -d --build 2>&1 | tail -1 &&
  for i in \$(seq 60); do curl -skf -m 3 -o /dev/null https://youtube.joost.im/health && echo up && exit 0; sleep 2; done; echo NOT UP; docker logs --tail 20 yt-shelf; exit 1"
