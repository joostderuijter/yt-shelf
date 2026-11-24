"""Tell Jellyfin to rescan its YouTube library after a download or delete. That's all this app does in Jellyfin."""
import json
import threading
import urllib.request

from .config import JELLYFIN_API_KEY, JELLYFIN_URL

LIBRARY_PATH = '/media/youtube'        # where Jellyfin sees the folder


def _req(path, method='GET'):
    req = urllib.request.Request(JELLYFIN_URL + path, method=method,
                                 headers={'Authorization': f'MediaBrowser Token="{JELLYFIN_API_KEY}"'})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def refresh_library():
    """Rescan the library whose folder is /media/youtube (whole server if it can't be found)."""
    if not JELLYFIN_API_KEY:
        return 'no JELLYFIN_API_KEY'
    lib = next((f for f in _req('/Library/VirtualFolders') or []
                if any(loc.rstrip('/') == LIBRARY_PATH for loc in f.get('Locations') or [])), None)
    if lib:
        _req(f"/Items/{lib['ItemId']}/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default",
             'POST')
        return f"rescanning {lib['Name']}"
    _req('/Library/Refresh', 'POST')
    return 'rescanning all libraries'


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
