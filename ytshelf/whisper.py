"""Korean subtitles with Whisper for Korean videos that have no real Korean subs: <video stem>.ko.srt.

faster-whisper large-v3, int8_float16 on the GPU (the settings kr-miner benchmarked on this GTX 1660: ~17 min per
90 min of audio). The GPU is shared with kr-miner, first come first served: the model is only loaded when enough
GPU memory is free (else wait), and unloaded as soon as the queue is empty.
"""
import gc
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .config import DATA, WHISPER_COMPUTE, WHISPER_MODEL, WHISPER_VRAM_MB

MODELS = DATA / 'models'
SENTENCE_END = re.compile(r'[.?!…~]$')
HALLUCINATIONS = ('시청해 주셔서', '시청해주셔서', '구독과 좋아요', '구독 좋아요', '좋아요와 구독', '자막 제공',
                  '자막 by', 'MBC 뉴스', '다음 영상에서', '영상 편집', 'Thank you for watching')
MAX_CUE_S, MAX_CUE_CHARS, MAX_GAP = 6.0, 32, 0.8


def gpu_free_mb():
    """Free GPU memory in MiB, or None when there's no NVIDIA GPU (CPU mode: no waiting)."""
    try:
        out = subprocess.run(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.split()[0])
    except Exception:
        return None


def extract_audio(video, out):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(video), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
                    '-f', 's16le', '-c:a', 'pcm_s16le', str(out)], check=True, capture_output=True)


def is_junk(seg):
    text = seg['text'].strip()
    if not text or any(h in text for h in HALLUCINATIONS):
        return True
    if seg['compression_ratio'] > 2.4:
        return True
    return seg['no_speech_prob'] > 0.6 and seg['avg_logprob'] < -1.0


def cues(segments):
    """Segments with word timestamps -> [(start, end, text)], split at sentence ends, pauses and length."""
    out, cur, prev = [], [], None

    def flush():
        if cur:
            text = ''.join(w['word'] for w in cur).strip()
            if text:
                out.append((cur[0]['start'], cur[-1]['end'], text))
            cur.clear()

    for seg in segments:
        if is_junk(seg) or seg['text'].strip() == prev:
            continue
        prev = seg['text'].strip()
        for w in seg['words']:
            if cur and (w['start'] - cur[-1]['end'] > MAX_GAP or w['end'] - cur[0]['start'] > MAX_CUE_S
                        or len(''.join(x['word'] for x in cur).strip()) >= MAX_CUE_CHARS):
                flush()
            cur.append(w)
            if SENTENCE_END.search(w['word'].strip()):
                flush()
        flush()
    return out


def ts(t):
    ms = int(round(t * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def srt(cue_list):
    return ''.join(f'{i}\n{ts(s)} --> {ts(max(e, s + 0.5))}\n{t}\n\n' for i, (s, e, t) in enumerate(cue_list, 1))


class Transcriber:
    def __init__(self):
        self.model = None

    def load(self, wait):
        if self.model is None:
            from faster_whisper import WhisperModel
            import ctranslate2
            cuda = ctranslate2.get_cuda_device_count() > 0
            while cuda:
                free = gpu_free_mb()
                if free is None or free >= WHISPER_VRAM_MB:
                    break
                wait(f'waiting for the GPU (in use, {free} MB free)')
                time.sleep(30)
            MODELS.mkdir(parents=True, exist_ok=True)
            self.model = WhisperModel(WHISPER_MODEL, device='cuda' if cuda else 'cpu',
                                      compute_type=WHISPER_COMPUTE if cuda else 'int8', download_root=str(MODELS))
        return self.model

    def unload(self):
        self.model = None
        gc.collect()

    def subtitles(self, video, stage=lambda s: None, progress=lambda p: None):
        """Write <stem>.ko.srt next to the video. -> its path."""
        import numpy as np
        video = Path(video)
        with tempfile.TemporaryDirectory(dir=DATA) as tmp:
            pcm = Path(tmp) / 'audio.pcm'
            stage('extracting audio')
            extract_audio(video, pcm)
            stage('loading Whisper')
            model = self.load(stage)
            stage('transcribing')
            audio = np.fromfile(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            segs, info = model.transcribe(audio, language='ko', beam_size=5, word_timestamps=True, vad_filter=True,
                                          condition_on_previous_text=False)
            out = []
            for s in segs:
                out.append({'text': s.text, 'compression_ratio': s.compression_ratio,
                            'no_speech_prob': s.no_speech_prob, 'avg_logprob': s.avg_logprob,
                            'words': [{'start': w.start, 'end': w.end, 'word': w.word} for w in s.words or []]})
                if info.duration:
                    progress(min(99.0, 100 * s.end / info.duration))
        dest = video.with_name(video.stem + '.ko.srt')
        tmp_dest = dest.with_name(dest.name + '.tmp')
        tmp_dest.write_text(srt(cues(out)), encoding='utf-8')
        tmp_dest.replace(dest)
        return dest
