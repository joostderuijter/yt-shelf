"""Music: audio-only downloads into /media/music as <Artist>/<Album>/<NN - Title [id]>.m4a, tagged for Jellyfin.

The audio is YouTube's own AAC track (m4a, no re-encoding). Tags (artist, album, title, year, track, cover) come from
YouTube Music's metadata when the video has it, else from the channel name and an "Artist - Title" video title.
Jellyfin's music library goes by these tags; the folders are just tidy.
"""
import os
import re
import subprocess
import threading
import time
from pathlib import Path

AUDIO_EXT = {'.m4a', '.mp3', '.opus', '.flac', '.ogg', '.aac', '.wav'}
SINGLES = 'Singles'
VID = re.compile(r'\[([\w-]{11})\]')
TRACK = re.compile(r'^(\d{1,3}) - ')
# "(Official Video)", "[MV]", "(Lyrics)", "| 4K" ... noise in music video titles
NOISE = re.compile(r'\s*[\(\[【]\s*(?:official\s*)?(?:music\s*)?(?:video|audio|mv|m/v|lyrics?(?:\s*video)?|visuali[sz]er|'
                   r'live|hd|4k|color coded lyrics[^\)\]]*|performance video|dance practice)\s*[\)\]】]', re.I)
NOISE_TAIL = re.compile(r'\s+(?:ㅣ)\s*.*$|\s+(?:official\s+)?(?:mv|m/v|music video)$|^(?:mv|m/v)\s+', re.I)
CHANNEL_NOISE = re.compile(r'\s*(?:- topic|vevo|official(?: youtube channel)?|공식(?: 채널)?)\s*$', re.I)


def safe(name, fallback='Unknown'):
    """A folder/file name: no path separators or characters Windows/SMB clients choke on."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', name or '').strip().strip('.')
    return s[:120] or fallback


def clean_title(t):
    t = NOISE.sub('', t or '')
    t = NOISE_TAIL.sub('', t)
    return t.strip(' -–—') or (t or '').strip()


def split_title(raw):
    """'IU - 밤편지', 'IU _ 밤편지', "IU 'Love wins all'", 'IU「밤편지」' -> (artist, title), else (None, None)."""
    m = re.match(r'^(.+?)\s+[-–—_|]\s+(.+)$', raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"""^(.+?)\s*['‘"“「『](.+?)['’"”」』]\s*(.*)$""", raw)          # K-pop style: Artist 'Title'
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def meta(info, track=None):
    """-> {'artist', 'album', 'title', 'year', 'track'} for a video's metadata."""
    artist = info.get('artist') or ', '.join(info.get('artists') or []) or None
    title = info.get('track')
    if not artist or not title:
        raw = clean_title(info.get('title') or '')
        channel = CHANNEL_NOISE.sub('', info.get('channel') or info.get('uploader') or '').strip()
        a, t = split_title(raw)
        if a and not artist:                                   # "Artist - Title (Official Video)"
            artist, title = a, title or t
        artist = artist or channel or 'Unknown artist'
        title = title or raw
    artist = artist.split(',')[0].strip() if info.get('artists') and len(info['artists']) > 1 else artist
    year = info.get('release_year') or (int(info['upload_date'][:4]) if info.get('upload_date') else None)
    return {'artist': artist, 'album': info.get('album') or SINGLES, 'title': clean_title(title), 'year': year,
            'track': track if info.get('album') else None,
            'all_artists': ', '.join(info.get('artists') or []) or artist}


def out_template(root, m):
    d = Path(root) / safe(m['artist'], 'Unknown artist') / safe(m['album'], SINGLES)
    prefix = f"{m['track']:02d} - " if m.get('track') else ''
    return d, f"{prefix}{safe(m['title'], 'Untitled')} [%(id)s].%(ext)s"


def square_cover(src, dest):
    """Thumbnail -> centred square jpg (YouTube thumbnails are 16:9; album art is square)."""
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-vf',
                    "crop='min(iw,ih)':'min(iw,ih)',scale='min(600,iw)':-2", '-q:v', '3', str(dest)],
                   check=True, capture_output=True)


def tag(path, m, cover=None, video_id=None):
    from mutagen.mp4 import MP4, MP4Cover
    f = MP4(path)
    f['\xa9nam'] = [m['title']]
    f['\xa9ART'] = [m['all_artists']]
    f['aART'] = [m['artist']]
    f['\xa9alb'] = [m['album']]
    if m.get('year'):
        f['\xa9day'] = [str(m['year'])]
    if m.get('track'):
        f['trkn'] = [(m['track'], 0)]
    if video_id:
        f['\xa9cmt'] = [f'https://www.youtube.com/watch?v={video_id}']
    if cover and Path(cover).exists():
        f['covr'] = [MP4Cover(Path(cover).read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
    f.save()


def cover_bytes(path):
    from mutagen.mp4 import MP4
    try:
        c = MP4(path).get('covr')
        return bytes(c[0]) if c else None
    except Exception:
        return None


class MusicLibrary:
    """<root>/<artist>/<album>/<song>. Cached scan like the video library."""

    def __init__(self, root, ttl=30):
        self.root, self.ttl = Path(root), ttl
        self._cache, self._at, self._lock = None, 0, threading.Lock()

    def invalidate(self):
        self._at = 0

    def scan(self):
        with self._lock:
            if self._cache is None or time.time() - self._at > self.ttl:
                self._cache, self._at = self._scan(), time.time()
            return self._cache

    def _scan(self):
        artists = {}
        if not self.root.is_dir():
            return artists
        for a in sorted(os.scandir(self.root), key=lambda e: e.name.lower()):
            if not a.is_dir() or a.name.startswith('.'):
                continue
            songs = []
            for dirpath, _, files in os.walk(a.path):
                d = Path(dirpath)
                album = d.name if d != Path(a.path) else ''
                for f in files:
                    p = d / f
                    if p.suffix.lower() not in AUDIO_EXT or f.startswith('.'):
                        continue
                    st = p.stat()
                    stem = p.stem
                    v = VID.search(stem)
                    t = TRACK.match(stem)
                    title = VID.sub('', TRACK.sub('', stem)).strip()
                    songs.append({'id': v.group(1) if v else None, 'artist': a.name, 'album': album,
                                  'track': int(t.group(1)) if t else None, 'title': title, 'file': f,
                                  'dir': str(d), 'rel': str(p.relative_to(self.root)), 'size': st.st_size,
                                  'mtime': st.st_mtime})
            songs.sort(key=lambda s: (s['album'] == SINGLES, s['album'].lower(), s['track'] or 0, s['title'].lower()))
            artists[a.name] = {'artist': a.name, 'songs': songs, 'size': sum(s['size'] for s in songs),
                               'latest': max((s['mtime'] for s in songs), default=0)}
        return artists

    def ids(self):
        return {s['id'] for a in self.scan().values() for s in a['songs'] if s['id']}

    def find(self, rel):
        return next((s for a in self.scan().values() for s in a['songs'] if s['rel'] == rel), None)

    def delete(self, song):
        """Delete one song; an album/artist folder left with nothing but cover art goes too."""
        root = self.root.resolve()
        p = (Path(song['dir']) / song['file']).resolve()
        if root not in p.parents:
            raise ValueError(f'refusing to delete outside {root}: {p}')
        p.unlink()
        for d in (p.parent, p.parent.parent):
            if d != root and root in d.parents and d.is_dir():
                left = list(d.iterdir())
                if all(x.is_file() and x.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') for x in left):
                    for x in left:
                        x.unlink()
                    d.rmdir()
        self.invalidate()
        return [song['file']]
