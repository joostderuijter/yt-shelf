# yt-shelf: YouTube downloads for Jellyfin

## Goal
A small web app on the home server (https://yt.joost.im, Tailscale only, no auth by design) to download YouTube
videos, playlists or whole channels into Jellyfin's YouTube library, see what's there, and delete videos.
Before this app the user ran yt-dlp by hand (see "Folder layout").

## Decisions (user, 2026-11-24)
- Writes to `/mnt/content1/youtube` (mounted at `/media/youtube`, the path Jellyfin sees): the only place on the
  server this app may change. Explicitly approved by the user. Delete = immediately (no trash).
- Subtitles: only real Korean subs (`ko`, `ko-*`), never YouTube's auto-generated ones. Other content is English.
- Korean video without real Korean subs -> Whisper makes `<video>.ko.srt` (large-v3, int8_float16, GPU).
  GPU shared with kr-miner: first come, first served (waits until YTSHELF_WHISPER_VRAM_MB is free).
- Max 1080p. No subscriptions (for now). No kr-miner integration needed (it just benefits from the .ko.srt files).
- Show disk space (total/used/free + YouTube's share) on the page.
- Stale `.part` leftovers older than 2 days are removed at startup.

## Folder layout (= the user's old yt-dlp commands)
`<channel>/Season 1/S01E0443 - <title> [<videoId>].mkv` + `.webp` thumbnail + `.ko.srt`, subs and thumbnail also
embedded. Old command: `yt-dlp --cookies ~/cookies.txt --playlist-reverse --write-subs --sub-langs ko --sub-format srt
--embed-subs --write-thumbnail --embed-thumbnail --merge-output-format mkv -o ".../S01E%(playlist_autonumber)04d - %(title)s [%(id)s].%(ext)s" <channel>/videos`
- The number makes Jellyfin show a channel as one season in order. Existing folders differ (checked 2026-11-24):
  workman = oldest first (1 = oldest), living-alone = NEWEST first (1 = newest at download time), syuka-world =
  oldest first but shifted ~300-400. So `Worker.number` follows each folder's own pattern from the nearest existing
  video in upload order; new channels: 1 = oldest. A number that's taken or < 1 -> next free number at the end.
- Shorts/streams (not in the channel's /videos list) -> `Season 0` (Jellyfin: Specials).
- New channel folder = its @handle (`@jachinam` -> `jachinam`). Existing folders are matched to their channel once
  by looking up one of their videos (table `folders_checked`).

## Code
- `ytshelf/library.py` scan/delete (sidecars = same name stem), `ytdl.py` yt-dlp as a command (+ Deno in the image
  for YouTube's JS challenges), `worker.py` queue threads (1 download + 1 Whisper at a time), `whisper.py`,
  `jellyfin.py` (debounced library rescan, the only Jellyfin call), `web.py` + `index.html`.
- Tests: `uv run pytest -q tests`. Local run: `YTSHELF_DATA=/tmp/x YTSHELF_MEDIA=/tmp/m uv run python -m ytshelf.web`.
- iPhone: share sheet Shortcut -> `POST /api/quick {"url": ...}` queues everything new, or open `/?url=<link>`.

## Deployment
`/srv/yt-shelf` (container `yt-shelf`, caddy label yt.joost.im, `tls internal`). `scripts/deploy.sh` (rsync +
`docker compose up -d --build`; refuses while a job runs; never copies `data/` or `.env`). yt-dlp is upgraded to the
newest version on each day's first build. Same server rules as kr-miner: read-only first, only `/srv/yt-shelf`, the
`yt-shelf` container and the YouTube folder; never touch Jellyfin/Caddy/other containers; no sudo without asking.
