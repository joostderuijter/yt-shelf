"""What's on disk: <channel>/Season N/SnnEnnnn - title [videoId].mkv plus sidecars (thumbnail, .ko.srt).

Jellyfin reads the folder as a TV library: a channel = a show, the number = the video's place in the channel's
upload list (oldest = 1), so Jellyfin keeps everything of a channel in one season in upload order.
"""
import os
import re
import shutil
import threading
import time
from pathlib import Path

VIDEO_EXT = {'.mkv', '.mp4', '.webm', '.m4v', '.mov'}
THUMB_EXT = {'.webp', '.jpg', '.jpeg', '.png'}
SUB_EXT = {'.srt', '.vtt', '.ass'}
NAME = re.compile(r'^S(\d+)E(\d+) - (.*?)(?: \[([\w-]{11})\])?$')
VID = re.compile(r'\[([\w-]{11})\]')
PARTIAL = ('.part', '.ytdl', '.temp', '.tmp')


def is_partial(name):
    return name.endswith(PARTIAL) or '.part-Frag' in name or '.temp.' in name


def parse(stem):
    """'S01E0443 - title [abcdefghijk]' -> (season, number, title, id); other names -> (None, None, stem, id?)."""
    m = NAME.match(stem)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    v = VID.search(stem)
    return None, None, stem, v.group(1) if v else None


def sub_lang(name, stem):
    """'x.ko.srt' -> 'ko'; 'x.srt' -> ''."""
    rest = name[len(stem):]
    parts = rest.split('.')
    return parts[1] if len(parts) == 3 else ''


class Library:
    """Scans the media folder (cached briefly; ~2,500 files take well under a second)."""

    def __init__(self, root, ttl=30):
        self.root, self.ttl = Path(root), ttl
        self._cache, self._at = None, 0
        self._lock = threading.Lock()

    def invalidate(self):
        self._at = 0

    def scan(self):
        with self._lock:
            if self._cache is None or time.time() - self._at > self.ttl:
                self._cache, self._at = self._scan(), time.time()
            return self._cache

    def _scan(self):
        channels = {}
        if not self.root.is_dir():
            return channels
        for ch in sorted(os.scandir(self.root), key=lambda e: e.name.lower()):
            if not ch.is_dir() or ch.name.startswith('.'):
                continue
            videos = []
            for dirpath, _, files in os.walk(ch.path):
                videos += self._videos(Path(dirpath), files, ch.name)
            videos.sort(key=lambda v: (v['season'] or 0, v['number'] or 0, v['mtime']), reverse=True)
            channels[ch.name] = {'folder': ch.name, 'videos': videos, 'size': sum(v['size'] for v in videos),
                                 'latest': max((v['mtime'] for v in videos), default=0)}
        return channels

    def _videos(self, d, files, channel):
        files = [f for f in files if not f.startswith('.') and not is_partial(f)]
        vids = [f for f in files if Path(f).suffix.lower() in VIDEO_EXT]
        by_id = {}
        for g in files:
            m = VID.search(g)
            if m:
                by_id.setdefault(m.group(1), []).append(g)
        out = []
        for f in vids:
            stem = Path(f).stem
            season, number, title, vid = parse(stem)
            p = d / f
            st = p.stat()
            side = [g for g in (by_id.get(vid, []) if vid else files) if g != f and g.startswith(stem + '.')]
            thumb = next((g for g in side if Path(g).suffix.lower() in THUMB_EXT), None)
            subs = sorted({sub_lang(g, stem) for g in side if Path(g).suffix.lower() in SUB_EXT})
            out.append({'id': vid, 'channel': channel, 'season': season, 'number': number, 'title': title,
                        'file': f, 'dir': str(d), 'size': st.st_size, 'mtime': st.st_mtime,
                        'thumb': thumb, 'subs': subs})
        return out

    # ------------------------------------------------------------ lookups
    def ids(self):
        return {v['id'] for ch in self.scan().values() for v in ch['videos'] if v['id']}

    def find(self, channel, file):
        ch = self.scan().get(channel)
        return next((v for v in ch['videos'] if v['file'] == file), None) if ch else None

    def by_id(self, vid):
        return next((v for ch in self.scan().values() for v in ch['videos'] if v['id'] == vid), None)

    def numbers(self, channel, season):
        ch = self.scan().get(channel)
        return {v['number'] for v in ch['videos'] if v['season'] == season} if ch else set()

    # ------------------------------------------------------------ changes
    def delete(self, video):
        """Delete a video and its sidecars (same name stem). Empty season/channel folders go too. -> [deleted names]"""
        d = Path(video['dir']).resolve()
        root = self.root.resolve()
        if root not in d.parents:
            raise ValueError(f'refusing to delete outside {root}: {d}')
        stem = Path(video['file']).stem
        gone = []
        for f in os.listdir(d):
            if f == video['file'] or f.startswith(stem + '.'):
                (d / f).unlink()
                gone.append(f)
        for p in (d, d.parent):
            if p != root and root in p.parents and p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        self.invalidate()
        return gone

    def stale_partials(self, older_than=2 * 86400):
        """Leftovers of interrupted downloads (.part etc.) not touched for `older_than` seconds."""
        now, out = time.time(), []
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                p = Path(dirpath) / f
                if is_partial(f) and now - p.stat().st_mtime > older_than:
                    out.append(p)
        return out


def disk(root):
    u = shutil.disk_usage(root)
    return {'total': u.total, 'used': u.used, 'free': u.free}
