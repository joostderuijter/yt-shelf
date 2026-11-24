"""Background work: one download at a time, one Whisper job at a time (they run side by side)."""
import json
import re
import shutil
import threading
import time
from pathlib import Path

from . import ytdl
from .config import COOKIES, DATA, MEDIA, WHISPER
from .whisper import Transcriber

UPLOADS_TTL = 600           # re-read a channel's upload list when a video isn't in a copy older than this


class Canceled(Exception):
    pass


def slug(text):
    s = re.sub(r'[^\w-]+', '-', (text or '').lstrip('@')).strip('-').lower()
    return s or 'other'


class Worker:
    def __init__(self, db, lib, refresher, log=print):
        self.db, self.lib, self.refresher, self.log = db, lib, refresher, log
        self.wake = {'download': threading.Event(), 'whisper': threading.Event()}
        self.procs, self.canceled = {}, set()
        self.map_lock = threading.Lock()
        self.tx = Transcriber()

    # ------------------------------------------------------------ setup
    def start(self):
        self.db.q("UPDATE jobs SET state='queued', detail='restarted' WHERE state='running'")
        self.copy_cookies()
        for p in self.lib.stale_partials():
            p.unlink(missing_ok=True)
            self.log(f'removed leftover {p}')
        threading.Thread(target=self._loop, args=('download', self._download), daemon=True).start()
        threading.Thread(target=self._loop, args=('whisper', self._whisper), daemon=True).start()
        threading.Thread(target=self._map_quietly, daemon=True).start()

    def copy_cookies(self):
        """yt-dlp writes cookies back, so it gets its own copy (the mounted file is read-only)."""
        dest = DATA / 'cookies.txt'
        if COOKIES.is_file() and (not dest.exists() or COOKIES.stat().st_mtime > dest.stat().st_mtime):
            shutil.copyfile(COOKIES, dest)

    def _map_quietly(self):
        try:
            self.map_existing()
        except Exception as e:
            self.log(f'channel lookup failed: {e!r}')

    def map_existing(self):
        """Which YouTube channel each existing folder is (looks up one video per folder, once)."""
        with self.map_lock:
            checked = {r['folder'] for r in self.db.all('SELECT folder FROM folders_checked')}
            for folder, ch in self.lib.scan().items():
                if folder in checked:
                    continue
                cid = None
                for v in [v for v in ch['videos'] if v['id']][:3]:
                    try:
                        info = ytdl.video_info(v['id'])
                    except ytdl.YtError:
                        continue                         # removed from YouTube: try another one
                    cid = info.get('channel_id')
                    if cid and not self.db.one('SELECT 1 FROM channels WHERE channel_id=?', cid):
                        self.db.q('INSERT INTO channels (channel_id, folder, title) VALUES (?,?,?)',
                                  cid, folder, info.get('channel'))
                    break
                self.db.q('INSERT OR REPLACE INTO folders_checked VALUES (?,?)', folder, cid)
                self.log(f'folder {folder} = channel {cid}')

    # ------------------------------------------------------------ queue
    def add(self, entries):
        """entries: [{'id', 'title', 'url'}]. Skips videos already on disk or queued. -> number queued"""
        have, queued, n = self.lib.ids(), self.db.active_ids(), 0
        for e in entries:
            if e['id'] in have or e['id'] in queued:
                continue
            if self.db.add_job('download', e['id'], e['url'], e.get('title')):
                n += 1
        self.wake['download'].set()
        return n

    def add_subs(self, video):
        jid = self.db.add_job('whisper', video['id'], None, video['title'], video['channel'],
                              str(Path(video['dir']) / video['file']))
        self.wake['whisper'].set()
        return jid

    def cancel(self, jid):
        job = self.db.one('SELECT * FROM jobs WHERE id=?', jid)
        if not job or job['state'] not in ('queued', 'running'):
            return False
        if job['kind'] == 'whisper' and job['state'] == 'running':
            return False                                 # can't stop Whisper halfway
        self.canceled.add(jid)
        if job['state'] == 'queued':
            self.db.set_job(jid, state='canceled', detail='canceled')
        for p in self.procs.get(jid, []):
            p.terminate()
        return True

    def _loop(self, kind, fn):
        while True:
            job = self.db.next_job(kind)
            if job is None:
                if kind == 'whisper':
                    self.tx.unload()                     # free the GPU for kr-miner / Jellyfin
                self.wake[kind].wait(60)
                self.wake[kind].clear()
                continue
            jid = job['id']
            self.db.set_job(jid, state='running', progress=0, detail='starting')
            try:
                fn(job)
            except Exception as e:
                if jid in self.canceled:
                    self.db.set_job(jid, state='canceled', detail='canceled')
                else:
                    self.db.set_job(jid, state='error', detail=str(e)[:300])
                    self.log(f'{kind} job {jid} failed: {e!r}')
            finally:
                self.procs.pop(jid, None)
                self.canceled.discard(jid)

    # ------------------------------------------------------------ download
    def place(self, info):
        """-> (folder, season, number). Number = place in the channel's uploads (oldest = 1), like the user's
        existing files; videos that aren't regular uploads (shorts, streams) go to Season 0 = Specials."""
        self.map_existing()
        cid = info.get('channel_id')
        if not cid:
            return 'other', 0, max(self.lib.numbers('other', 0) | {0}) + 1
        row = self.db.one('SELECT * FROM channels WHERE channel_id=?', cid)
        if row is None:
            folder = self._new_folder(info)
            self.db.q('INSERT INTO channels (channel_id, folder, title) VALUES (?,?,?)', cid, folder, info.get('channel'))
            row = self.db.one('SELECT * FROM channels WHERE channel_id=?', cid)
        ids = json.loads(row['uploads']) if row['uploads'] else None
        if ids is None or (info['id'] not in ids and time.time() - (row['uploads_at'] or 0) > UPLOADS_TTL):
            ids = ytdl.uploads(cid)
            self.db.q('UPDATE channels SET uploads=?, uploads_at=? WHERE channel_id=?', json.dumps(ids), time.time(), cid)
        folder = row['folder']
        if info['id'] in ids:
            return folder, 1, self.number(folder, ids, ids.index(info['id']))
        return folder, 0, max(self.lib.numbers(folder, 0) | {0}) + 1

    def number(self, folder, ids, pos):
        """Episode number for the upload at `pos` (0 = oldest), following the folder's own numbering: the user's
        older downloads count oldest-first (workman), newest-first (living-alone) or shifted (syuka-world). Take the
        nearest existing video in upload order and count from its number, in the folder's direction."""
        ch = self.lib.scan().get(folder)
        known = {v['id']: v['number'] for v in (ch['videos'] if ch else []) if v['season'] == 1 and v['id']}
        at = {vid: i for i, vid in enumerate(ids)}
        pairs = sorted((at[v], n) for v, n in known.items() if v in at)
        taken = set(known.values())
        if not pairs:
            n = pos + 1
        else:
            step = -1 if len(pairs) > 1 and pairs[-1][1] < pairs[0][1] else 1
            p0, n0 = min(pairs, key=lambda t: abs(t[0] - pos))
            n = n0 + step * (pos - p0)
        # below 1 (newer than everything in a newest-first folder) or already used: next free number at the end
        return n if n >= 1 and n not in taken else max(taken | {0}) + 1

    def _new_folder(self, info):
        base = slug(info.get('uploader_id') if (info.get('uploader_id') or '').startswith('@') else info.get('channel'))
        taken = {r['folder'] for r in self.db.all('SELECT folder FROM channels')}
        folder, i = base, 2
        while folder in taken:
            folder, i = f'{base}-{i}', i + 1
        return folder

    def _download(self, job):
        jid = job['id']
        set_ = lambda **kw: self.db.set_job(jid, **kw)
        set_(detail='looking up')
        info = ytdl.video_info(job['url'])
        if self.lib.by_id(info['id']):
            return set_(state='done', progress=100, detail='already on the server', video_id=info['id'])
        folder, season, number = self.place(info)
        set_(video_id=info['id'], title=info.get('title'), channel=folder, detail='downloading')
        out = MEDIA / folder / f'Season {season}'
        out.mkdir(parents=True, exist_ok=True)
        last = [0.0]

        def progress(pct, speed, eta):
            if pct - last[0] >= 1 or pct >= 100:
                last[0] = pct
                bits = [f'{pct:.0f}%']
                if speed:
                    bits.append(f'{speed / 1e6:.1f} MB/s')
                if eta:
                    bits.append(f'{int(eta) // 60}:{int(eta) % 60:02d} left')
                set_(progress=pct, detail='downloading ' + ' · '.join(bits))

        self.procs[jid] = []
        if jid in self.canceled:
            raise Canceled()
        path = ytdl.download(job['url'], str(out / f'S{season:02d}E{number:04d} - %(title)s [%(id)s].%(ext)s'),
                             progress, self.procs[jid])
        self.lib.invalidate()
        self.refresher.soon()
        p = Path(path)
        has_subs = any(f.name.startswith(p.stem + '.') and f.suffix == '.srt' for f in p.parent.iterdir())
        note = 'Korean subs from YouTube' if has_subs else ''
        if WHISPER and not has_subs and ytdl.is_korean(info):
            self.db.add_job('whisper', info['id'], job['url'], info.get('title'), folder, path)
            self.wake['whisper'].set()
            note = 'Korean subs: Whisper queued'
        set_(state='done', progress=100, path=path, detail=note or 'done')

    # ------------------------------------------------------------ whisper
    def _whisper(self, job):
        jid = job['id']
        set_ = lambda **kw: self.db.set_job(jid, **kw)
        path = Path(job['path'] or '')
        if not path.exists() and job['video_id']:
            v = self.lib.by_id(job['video_id'])
            path = Path(v['dir']) / v['file'] if v else path
        if not path.exists():
            raise FileNotFoundError('video is gone')
        last = [-1]

        def progress(pct):
            if int(pct) != last[0]:
                last[0] = int(pct)
                set_(progress=pct, detail=f'making Korean subs {int(pct)}%')

        dest = self.tx.subtitles(path, stage=lambda s: set_(detail=s), progress=progress)
        self.db.q('INSERT OR REPLACE INTO whisper_subs VALUES (?,?,?)', job['video_id'], str(dest), time.time())
        self.lib.invalidate()
        self.refresher.soon()
        set_(state='done', progress=100, detail='Korean subs made (Whisper)')
