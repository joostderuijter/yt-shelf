"""yt-shelf web app: library of downloaded YouTube videos, add links, delete, queue. No auth: Tailscale only.

  uv run python -m ytshelf.web --port 8780
"""
import argparse
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import ytdl
from .config import DATA, MEDIA
from .db import DB
from .jellyfin import Refresher
from .library import Library, disk
from .worker import Worker

DATA.mkdir(parents=True, exist_ok=True)
db = DB(DATA / 'ytshelf.db')
lib = Library(MEDIA)
refresher = Refresher()
worker = Worker(db, lib, refresher)
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    worker.start()
    yield


app = FastAPI(lifespan=lifespan)


def whisper_ids():
    return {r['video_id'] for r in db.all('SELECT video_id FROM whisper_subs')}


def video_json(v, wids):
    return {'id': v['id'], 'channel': v['channel'], 'file': v['file'], 'title': v['title'], 'season': v['season'],
            'number': v['number'], 'size': v['size'], 'mtime': v['mtime'], 'thumb': bool(v['thumb']),
            'subs': v['subs'], 'whisper': v['id'] in wids}


@app.get('/api/overview')
def overview():
    chans = lib.scan()
    return {'disk': disk(MEDIA), 'size': sum(c['size'] for c in chans.values()),
            'videos': sum(len(c['videos']) for c in chans.values()),
            'channels': sorted(({'folder': c['folder'], 'videos': len(c['videos']), 'size': c['size'],
                                 'latest': c['latest'],
                                 'thumb': next(({'file': v['file']} for v in c['videos'] if v['thumb']), None)}
                                for c in chans.values()), key=lambda c: -c['latest'])}


@app.get('/api/channel/{folder}')
def channel(folder: str):
    c = lib.scan().get(folder)
    if c is None:
        raise HTTPException(404)
    wids = whisper_ids()
    return {'folder': folder, 'size': c['size'], 'videos': [video_json(v, wids) for v in c['videos']]}


@app.get('/api/search')
def search(q: str):
    q = q.strip().lower()
    wids = whisper_ids()
    hits = [v for c in lib.scan().values() for v in c['videos'] if q and q in v['title'].lower()]
    return {'videos': [video_json(v, wids) for v in hits[:200]], 'more': max(0, len(hits) - 200)}


@app.get('/api/thumb/{folder}/{file}')
def thumb(folder: str, file: str):
    v = lib.find(folder, file)
    if not v or not v['thumb']:
        raise HTTPException(404)
    return FileResponse(Path(v['dir']) / v['thumb'], headers={'Cache-Control': 'max-age=604800'})


class Link(BaseModel):
    url: str


@app.post('/api/resolve')
def resolve(body: Link):
    """What's behind a link, and how much of it is new."""
    try:
        r = ytdl.resolve(body.url)
    except ytdl.YtError as e:
        raise HTTPException(400, str(e))
    have, queued = lib.ids(), db.active_ids()
    for e in r['entries']:
        e['have'] = e['id'] in have
        e['queued'] = e['id'] in queued
    r['new'] = sum(not e['have'] and not e['queued'] for e in r['entries'])
    return r


class Add(BaseModel):
    entries: list[dict]


@app.post('/api/add')
def add(body: Add):
    entries = [e for e in body.entries if ytdl.VIDEO_ID.match(str(e.get('id', '')))]
    for e in entries:
        e['url'] = f"https://www.youtube.com/watch?v={e['id']}"
    return {'queued': worker.add(entries)}


@app.post('/api/quick')
def quick(body: Link):
    """One call for the iPhone share sheet: queue everything new behind a link."""
    r = resolve(body)
    return {'title': r['title'], 'queued': worker.add([e for e in r['entries'] if not e['have']])}


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
