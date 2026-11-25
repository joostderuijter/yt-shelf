"""Jellyfin: rescan our libraries after a download or delete, and read (never change) what the user has watched."""
import json
import threading
import urllib.request

import os
import time

from .config import JELLYFIN_API_KEY, JELLYFIN_URL

LIBRARY_PATHS = ('/media/youtube', '/media/music')        # where Jellyfin sees our folders


def _req(path, method='GET'):
    req = urllib.request.Request(JELLYFIN_URL + path, method=method,
                                 headers={'Authorization': f'MediaBrowser Token="{JELLYFIN_API_KEY}"'})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def refresh_library():
    """Rescan the libraries that hold our folders (the whole server if none can be found)."""
    if not JELLYFIN_API_KEY:
        return 'no JELLYFIN_API_KEY'
    libs = [f for f in _req('/Library/VirtualFolders') or []
            if any(loc.rstrip('/') in LIBRARY_PATHS for loc in f.get('Locations') or [])]
    for lib in libs:
        _req(f"/Items/{lib['ItemId']}/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default",
             'POST')
    if libs:
        return 'rescanning ' + ', '.join(l['Name'] for l in libs)
    _req('/Library/Refresh', 'POST')
    return 'rescanning all libraries'


_watch = {'at': 0, 'data': {}}


def watch_states(ttl=30):
    """{file path as Jellyfin sees it: {'played': bool, 'pct': 0-100}} for the YouTube library, for JELLYFIN_USER
    (default: the first admin). Cached `ttl` seconds; {} when Jellyfin can't be reached."""
    if time.time() - _watch['at'] < ttl:
        return _watch['data']
    data = {}
    try:
        users = _req('/Users') or []
        name = os.environ.get('JELLYFIN_USER', '')
        user = next((u for u in users if u['Name'] == name), None) or \
            next((u for u in users if u.get('Policy', {}).get('IsAdministrator')), None) or (users[0] if users else None)
        lib = next((f for f in _req('/Library/VirtualFolders') or []
                    if any(loc.rstrip('/') == LIBRARY_PATHS[0] for loc in f.get('Locations') or [])), None)
        if user and lib:
            r = _req(f"/Users/{user['Id']}/Items?ParentId={lib['ItemId']}&Recursive=true&IncludeItemTypes=Episode"
                     '&Fields=Path&EnableUserData=true&EnableImages=false')
            for it in r.get('Items') or []:
                ud = it.get('UserData') or {}
                if it.get('Path') and (ud.get('Played') or ud.get('PlayedPercentage')):
                    data[it['Path']] = {'played': bool(ud.get('Played')), 'pct': round(ud.get('PlayedPercentage') or 0)}
    except Exception as e:
        print(f'jellyfin watch states: {e!r}')
        return _watch['data']
    _watch.update(at=time.time(), data=data)
    return data


class Refresher:
    """Debounced: many changes in a row -> one rescan `delay` seconds after the last one."""

    def __init__(self, delay=30, log=print):
        self.delay, self.log, self.timer = delay, log, None
        self.lock = threading.Lock()

    def soon(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self._run)
            self.timer.daemon = True
            self.timer.start()

    def _run(self):
        try:
            self.log(f'jellyfin: {refresh_library()}')
        except Exception as e:
            self.log(f'jellyfin rescan failed: {e!r}')
