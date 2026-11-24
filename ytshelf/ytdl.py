"""yt-dlp, run as a command (same flags the user used by hand before this app existed)."""
import json
import re
import subprocess

from .config import DATA, MAX_HEIGHT, SUB_LANG

CHANNEL_ROOT = re.compile(r'^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?'
                          r'(?:featured)?/?$')
VIDEO_ID = re.compile(r'^[\w-]{11}$')
HANGUL = re.compile(r'[가-힣]')
LETTER = re.compile(r'[^\W\d_]')


class YtError(Exception):
    pass


def base():
    args = ['yt-dlp', '--ignore-config', '--no-warnings']
    cookies = DATA / 'cookies.txt'
    if cookies.exists():
        args += ['--cookies', str(cookies)]
    return args


def _error(text):
    lines = [l for l in text.strip().splitlines() if l.startswith('ERROR')] or text.strip().splitlines() or ['failed']
    return lines[-1].removeprefix('ERROR: ')[:300]


def _json(args, timeout=180):
    try:
        p = subprocess.run(base() + ['--no-progress'] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise YtError('YouTube took too long to answer')
    if p.returncode != 0 or not p.stdout.strip():
        raise YtError(_error(p.stderr))
    return json.loads(p.stdout)


def normalize(url):
    url = url.strip()
    if VIDEO_ID.match(url):
        return f'https://www.youtube.com/watch?v={url}'
    m = CHANNEL_ROOT.match(url)
    return m.group(1) + '/videos' if m else url        # a channel = its uploads (not shorts/live)


def resolve(url):
    """Look at a link without downloading. -> {'kind': 'video'|'list', 'title', 'channel', 'entries': [...]}
    entries: {'id', 'title', 'url'}"""
    url = normalize(url)
    info = _json(['-J', '--flat-playlist', url])
    if info.get('_type') in ('playlist', 'multi_video'):
        entries = []
        for e in info.get('entries') or []:
            if e is None:
                continue
            if e.get('_type') == 'playlist' or (e.get('ie_key') == 'YoutubeTab' and not VIDEO_ID.match(e.get('id') or '')):
                entries += resolve(e['url'])['entries']             # a channel's tabs
            elif VIDEO_ID.match(e.get('id') or ''):
                entries.append({'id': e['id'], 'title': e.get('title') or e['id'],
                                'url': f"https://www.youtube.com/watch?v={e['id']}"})
        return {'kind': 'list', 'title': info.get('title') or url, 'channel': info.get('channel') or info.get('uploader'),
                'entries': entries}
    return {'kind': 'video', 'title': info.get('title'), 'channel': info.get('channel') or info.get('uploader'),
            'entries': [{'id': info['id'], 'title': info.get('title'), 'url': info.get('webpage_url') or url}]}


def video_info(url):
    """Full metadata of one video (channel, language, subtitle tracks)."""
    return _json(['-J', '--no-playlist', normalize(url)])


def uploads(channel_id):
    """The channel's videos, oldest first (shorts and streams are separate tabs, not in here)."""
    info = _json(['-J', '--flat-playlist', f'https://www.youtube.com/channel/{channel_id}/videos'], timeout=600)
    ids = [e['id'] for e in info.get('entries') or [] if e and VIDEO_ID.match(e.get('id') or '')]
    return ids[::-1]


def is_korean(info):
    lang = (info.get('language') or '').lower()
    if lang:
        return lang.startswith('ko')
    text = (info.get('title') or '') + ' ' + (info.get('description') or '')[:300]
    letters = LETTER.findall(text)
    return bool(letters) and len(HANGUL.findall(text)) / len(letters) > 0.3


def has_real_subs(info, lang=SUB_LANG):
    return any(k == lang or k.startswith(lang + '-') for k in (info.get('subtitles') or {}))


PROG = re.compile(r'^PROG (\S+) (\S+) (\S+) (\S+) (\S+)$')


def download(url, outtmpl, on_progress=None, proc_ref=None):
    """Download one video. -> final file path. on_progress(pct, speed_bytes, eta_s); proc_ref(list) gets the Popen."""
    fmt = f'bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/bv*+ba/b'
    cmd = base() + ['--newline', '--progress', '--no-playlist', '-f', fmt, '--merge-output-format', 'mkv',
                    '--write-subs', '--sub-langs', f'{SUB_LANG},{SUB_LANG}-.*', '--convert-subs', 'srt',
                    '--embed-subs', '--write-thumbnail', '--embed-thumbnail',
                    '--progress-template', 'download:PROG %(progress.downloaded_bytes)s %(progress.total_bytes)s '
                                           '%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s',
                    '--print', 'after_move:FINAL %(filepath)s', '-o', outtmpl, url]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    if proc_ref is not None:
        proc_ref.append(p)
    final, tail = None, []
    for line in p.stdout:
        line = line.rstrip()
        m = PROG.match(line)
        if m:
            num = lambda s: float(s) if s not in ('NA', 'None') else None
            done, total, est, speed, eta = (num(g) for g in m.groups())
            total = total or est
            if on_progress and done is not None and total:
                on_progress(min(100.0, 100 * done / total), speed, eta)
        elif line.startswith('FINAL '):
            final = line[6:]
        elif line:
            tail = (tail + [line])[-20:]
    p.wait()
    if p.returncode != 0 or not final:
        raise YtError(_error('\n'.join(tail)) if p.returncode != -15 else 'canceled')
    return final
