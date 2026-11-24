"""Settings, all from the environment (compose.yml lists every one with its default)."""
import os
from pathlib import Path

MEDIA = Path(os.environ.get('YTSHELF_MEDIA', '/media/youtube'))      # the folder Jellyfin's YouTube library reads
DATA = Path(os.environ.get('YTSHELF_DATA', '/data'))                 # queue DB, cookies copy, Whisper models
COOKIES = Path(os.environ.get('YTSHELF_COOKIES', '/cookies.txt'))    # Netscape cookies.txt for YouTube (optional)
MAX_HEIGHT = int(os.environ.get('YTSHELF_MAX_HEIGHT', '1080'))
SUB_LANG = os.environ.get('YTSHELF_SUB_LANG', 'ko')                  # real (not auto-generated) subs to download
WHISPER = os.environ.get('YTSHELF_WHISPER', '1') == '1'              # Korean video without Korean subs -> Whisper
WHISPER_MODEL = os.environ.get('YTSHELF_WHISPER_MODEL', 'large-v3')
WHISPER_COMPUTE = os.environ.get('YTSHELF_WHISPER_COMPUTE', 'int8_float16')
WHISPER_VRAM_MB = int(os.environ.get('YTSHELF_WHISPER_VRAM_MB', '3500'))   # free GPU memory needed to start
JELLYFIN_URL = os.environ.get('JELLYFIN_URL', 'http://jellyfin:8096').rstrip('/')
JELLYFIN_API_KEY = os.environ.get('JELLYFIN_API_KEY', '')
