"""SQLite state in /data/ytshelf.db: the job queue and which folder belongs to which YouTube channel."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,              -- download | whisper
    video_id   TEXT,
    url        TEXT,
    title      TEXT,
    channel    TEXT,                       -- folder name, once known
    state      TEXT NOT NULL,              -- queued | running | done | error | canceled
    progress   REAL,
    detail     TEXT,
    path       TEXT,                       -- final video file
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    folder     TEXT UNIQUE NOT NULL,
    title      TEXT,
    uploads    TEXT,                       -- JSON list of video ids, oldest first
    uploads_at REAL
);
CREATE TABLE IF NOT EXISTS folders_checked (  -- existing folders whose channel was looked up (also failures)
    folder TEXT PRIMARY KEY,
    channel_id TEXT
);
CREATE TABLE IF NOT EXISTS whisper_subs (     -- .ko.srt files made by Whisper (not YouTube's)
    video_id TEXT PRIMARY KEY,
    path     TEXT,
    made_at  REAL
);
"""

ACTIVE = ('queued', 'running')


class DB:
    def __init__(self, path):
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA journal_mode=WAL')
        self.lock = threading.RLock()
        with self.lock:
            self.con.executescript(SCHEMA)

    def q(self, sql, *args):
        with self.lock:
            return self.con.execute(sql, args)

    def all(self, sql, *args):
        return [dict(r) for r in self.q(sql, *args).fetchall()]

    def one(self, sql, *args):
        r = self.q(sql, *args).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ jobs
    def add_job(self, kind, video_id, url, title=None, channel=None, path=None):
        """-> job id, or None when this video already has an active job of this kind."""
        with self.lock:
            if video_id and self.one(f"SELECT id FROM jobs WHERE kind=? AND video_id=? AND state IN {ACTIVE}",
                                     kind, video_id):
                return None
            now = time.time()
            return self.q('INSERT INTO jobs (kind, video_id, url, title, channel, state, path, created_at, updated_at) '
                          "VALUES (?,?,?,?,?, 'queued', ?,?,?)", kind, video_id, url, title, channel, path,
                          now, now).lastrowid

    def set_job(self, jid, **kw):
        kw['updated_at'] = time.time()
        cols = ', '.join(f'{k}=?' for k in kw)
        self.q(f'UPDATE jobs SET {cols} WHERE id=?', *kw.values(), jid)

    def next_job(self, kind):
        return self.one("SELECT * FROM jobs WHERE kind=? AND state='queued' ORDER BY id LIMIT 1", kind)

    def active_ids(self, kind='download'):
        return {r['video_id'] for r in self.all(f"SELECT video_id FROM jobs WHERE kind=? AND state IN {ACTIVE}", kind)}
