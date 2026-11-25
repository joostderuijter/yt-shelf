"""yt-shelf web app: library of downloaded YouTube videos, add links, delete, queue. No auth: Tailscale only.

  uv run python -m ytshelf.web --port 8780
"""
import argparse
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from . import music, ytdl
from .config import DATA, MEDIA, MUSIC
from .db import DB
from .jellyfin import Refresher, watch_states
from .library import Library, disk
from .worker import Worker

DATA.mkdir(parents=True, exist_ok=True)
db = DB(DATA / 'ytshelf.db')
lib = Library(MEDIA)
mlib = music.MusicLibrary(MUSIC)
refresher = Refresher()
worker = Worker(db, lib, refresher, mlib=mlib)
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    worker.start()
    yield


app = FastAPI(lifespan=lifespan)


def whisper_ids():
    return {r['video_id'] for r in db.all('SELECT video_id FROM whisper_subs')}


def video_json(v, wids, watched=None):
    w = (watched or {}).get(str(Path(v['dir']) / v['file']), {})
    return {'id': v['id'], 'channel': v['channel'], 'file': v['file'], 'title': v['title'], 'season': v['season'],
            'number': v['number'], 'size': v['size'], 'mtime': v['mtime'], 'thumb': bool(v['thumb']),
            'subs': v['subs'], 'whisper': v['id'] in wids, 'played': w.get('played', False), 'pct': w.get('pct', 0)}


@app.get('/api/overview')
def overview():
    chans, arts, watched = lib.scan(), mlib.scan(), watch_states()
    return {'disk': disk(MEDIA), 'size': sum(c['size'] for c in chans.values()),
            'music_size': sum(a['size'] for a in arts.values()),
            'songs': sum(len(a['songs']) for a in arts.values()), 'artists': len(arts),
            'videos': sum(len(c['videos']) for c in chans.values()),
            'channels': sorted(({'folder': c['folder'], 'videos': len(c['videos']), 'size': c['size'],
                                 'watched': sum(watched.get(str(Path(v['dir']) / v['file']), {}).get('played', False)
                                                for v in c['videos']),
                                 'latest': c['latest'],
                                 'thumb': next(({'file': v['file']} for v in c['videos'] if v['thumb']), None)}
                                for c in chans.values()), key=lambda c: -c['latest'])}


@app.get('/api/channel/{folder}')
def channel(folder: str):
    c = lib.scan().get(folder)
    if c is None:
        raise HTTPException(404)
    wids, watched = whisper_ids(), watch_states()
    return {'folder': folder, 'size': c['size'], 'videos': [video_json(v, wids, watched) for v in c['videos']]}


@app.get('/api/search')
def search(q: str):
    q = q.strip().lower()
    wids = whisper_ids()
    hits = [v for c in lib.scan().values() for v in c['videos'] if q and q in v['title'].lower()]
    songs = [x for a in mlib.scan().values() for x in a['songs']
             if q and (q in x['title'].lower() or q in x['artist'].lower() or q in x['album'].lower())]
    watched = watch_states()
    return {'videos': [video_json(v, wids, watched) for v in hits[:200]], 'more': max(0, len(hits) - 200),
            'songs': [song_json(x) for x in songs[:200]]}


@app.get('/api/thumb/{folder}/{file}')
def thumb(folder: str, file: str):
    v = lib.find(folder, file)
    if not v or not v['thumb']:
        raise HTTPException(404)
    return FileResponse(Path(v['dir']) / v['thumb'], headers={'Cache-Control': 'max-age=604800'})


def song_json(x):
    return {k: x[k] for k in ('id', 'artist', 'album', 'track', 'title', 'rel', 'size', 'mtime')}


@app.get('/api/music')
def music_artists():
    return {'artists': sorted(({'artist': a['artist'], 'songs': len(a['songs']), 'size': a['size'], 'latest': a['latest'],
                                'cover': a['songs'][0]['rel'] if a['songs'] else None}
                               for a in mlib.scan().values()), key=lambda a: a['artist'].lower())}


@app.get('/api/music/artist/{artist}')
def music_artist(artist: str):
    a = mlib.scan().get(artist)
    if a is None:
        raise HTTPException(404)
    return {'artist': artist, 'size': a['size'], 'songs': [song_json(x) for x in a['songs']]}


_covers = {}


@app.get('/api/cover')
def cover(rel: str):
    """Cover art embedded in a song (cached in memory)."""
    s = mlib.find(rel)
    if s is None:
        raise HTTPException(404)
    if rel not in _covers:
        if len(_covers) > 500:
            _covers.clear()
        _covers[rel] = music.cover_bytes(Path(s['dir']) / s['file'])
    if not _covers[rel]:
        raise HTTPException(404)
    return Response(_covers[rel], media_type='image/jpeg', headers={'Cache-Control': 'max-age=604800'})


class Song(BaseModel):
    rel: str


@app.post('/api/music/delete')
def music_delete(body: Song):
    s = mlib.find(body.rel)
    if s is None:
        raise HTTPException(404)
    gone = mlib.delete(s)
    _covers.pop(body.rel, None)
    refresher.soon()
    return {'deleted': gone}


class Link(BaseModel):
    url: str
    mode: str = 'video'


@app.post('/api/resolve')
def resolve(body: Link):
    """What's behind a link, and how much of it is new."""
    try:
        r = ytdl.resolve(body.url)
    except ytdl.YtError as e:
        raise HTTPException(400, str(e))
    music_mode = body.mode == 'music'
    have = mlib.ids() if music_mode else lib.ids()
    queued = db.active_ids(mode='music' if music_mode else 'video')
    album = r['kind'] == 'list' and (r['title'] or '').startswith('Album - ')     # a YouTube Music album
    for i, e in enumerate(r['entries']):
        e['have'] = e['id'] in have
        e['queued'] = e['id'] in queued
        if album:
            e['track'] = i + 1
    r['mode'] = 'music' if music_mode else 'video'
    r['music_link'] = 'music.youtube.com' in body.url
    r['new'] = sum(not e['have'] and not e['queued'] for e in r['entries'])
    return r


class Add(BaseModel):
    entries: list[dict]
    mode: str = 'video'


@app.post('/api/add')
def add(body: Add):
    entries = [e for e in body.entries if ytdl.VIDEO_ID.match(str(e.get('id', '')))]
    for e in entries:
        e['url'] = f"https://www.youtube.com/watch?v={e['id']}"
        e['track'] = e.get('track') if isinstance(e.get('track'), int) else None
    return {'queued': worker.add(entries, 'music' if body.mode == 'music' else 'video')}


@app.post('/api/quick')
def quick(body: Link):
    """One call for the iPhone share sheet: queue everything new behind a link (music for music.youtube.com links,
    unless "mode" says otherwise)."""
    if 'mode' not in body.model_fields_set and 'music.youtube.com' in body.url:
        body.mode = 'music'
    r = resolve(body)
    return {'title': r['title'], 'mode': r['mode'],
            'queued': worker.add([e for e in r['entries'] if not e['have']], r['mode'])}


@app.get('/api/jobs')
def jobs():
    active = db.all("SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY state='queued', id")
    recent = db.all("SELECT * FROM jobs WHERE state NOT IN ('queued','running') AND updated_at>? "
                    'ORDER BY updated_at DESC LIMIT 30', time.time() - 3 * 86400)
    return {'active': active, 'recent': recent}


@app.post('/api/jobs/{jid}/cancel')
def cancel(jid: int):
    if not worker.cancel(jid):
        raise HTTPException(409, "can't cancel that")
    return {'ok': True}


@app.post('/api/jobs/{jid}/retry')
def retry(jid: int):
    j = db.one('SELECT * FROM jobs WHERE id=?', jid)
    if not j or j['state'] not in ('error', 'canceled'):
        raise HTTPException(409, "can't retry that")
    db.set_job(jid, state='queued', progress=0, detail=None)
    worker.wake[j['kind']].set()
    return {'ok': True}


@app.post('/api/jobs/clear')
def clear():
    db.q("DELETE FROM jobs WHERE state NOT IN ('queued','running')")
    return {'ok': True}


class Ref(BaseModel):
    channel: str
    file: str


@app.post('/api/delete')
def delete(body: Ref):
    v = lib.find(body.channel, body.file)
    if v is None:
        raise HTTPException(404)
    gone = lib.delete(v)
    if v['id']:
        db.q('DELETE FROM whisper_subs WHERE video_id=?', v['id'])
    refresher.soon()
    return {'deleted': gone}


@app.post('/api/subs')
def make_subs(body: Ref):
    v = lib.find(body.channel, body.file)
    if v is None:
        raise HTTPException(404)
    if not worker.add_subs(v):
        raise HTTPException(409, 'already queued')
    return {'ok': True}


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/', response_class=HTMLResponse)
def index():
    return (HERE / 'index.html').read_text(encoding='utf-8')


def main():
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8780)
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == '__main__':
    main()
