import json
import time

import pytest

from ytshelf import library, whisper, ytdl
from ytshelf.db import DB
from ytshelf.worker import Worker, slug


def touch(p, data=b'x'):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def media(tmp_path):
    s1 = tmp_path / 'media' / 'workman' / 'Season 1'
    touch(s1 / 'S01E0001 - first [aaaaaaaaaaa].mkv', b'x' * 100)
    touch(s1 / 'S01E0001 - first [aaaaaaaaaaa].webp')
    touch(s1 / 'S01E0001 - first [aaaaaaaaaaa].ko.srt')
    touch(s1 / 'S01E0002 - second ＂q＂ [bbbbbbbbbbb].mkv', b'x' * 50)
    touch(s1 / 'S01E0003 - third [ccccccccccc].f251.webm.part')
    return tmp_path / 'media'


def test_scan(media):
    lib = library.Library(media)
    ch = lib.scan()['workman']
    assert [v['number'] for v in ch['videos']] == [2, 1]               # newest first
    first = ch['videos'][1]
    assert first['id'] == 'aaaaaaaaaaa' and first['thumb'].endswith('.webp') and first['subs'] == ['ko']
    assert ch['videos'][0]['subs'] == [] and ch['videos'][0]['title'] == 'second ＂q＂'
    assert ch['size'] == 150
    assert lib.ids() == {'aaaaaaaaaaa', 'bbbbbbbbbbb'}                  # .part is not a video
    assert lib.numbers('workman', 1) == {1, 2}


def test_delete_takes_sidecars_and_empty_folders(media):
    lib = library.Library(media)
    v = lib.by_id('aaaaaaaaaaa')
    assert sorted(lib.delete(v)) == ['S01E0001 - first [aaaaaaaaaaa].ko.srt', 'S01E0001 - first [aaaaaaaaaaa].mkv',
                                     'S01E0001 - first [aaaaaaaaaaa].webp']
    assert lib.ids() == {'bbbbbbbbbbb'}
    lib.delete(lib.by_id('bbbbbbbbbbb'))
    # the stale .part keeps the season folder alive; the media root itself is never removed
    assert media.is_dir()


def test_delete_refuses_outside_root(media, tmp_path):
    lib = library.Library(media)
    outside = touch(tmp_path / 'other' / 'x [ddddddddddd].mkv')
    with pytest.raises(ValueError):
        lib.delete({'dir': str(outside.parent), 'file': outside.name})
    assert outside.exists()


def test_stale_partials(media):
    lib = library.Library(media)
    assert lib.stale_partials() == []
    assert len(lib.stale_partials(older_than=-1)) == 1


def test_normalize():
    assert ytdl.normalize('https://www.youtube.com/@workman') == 'https://www.youtube.com/@workman/videos'
    assert ytdl.normalize('https://youtube.com/@workman/') == 'https://youtube.com/@workman/videos'
    assert ytdl.normalize('https://www.youtube.com/@workman/shorts') == 'https://www.youtube.com/@workman/shorts'
    assert ytdl.normalize('jeZOJfHWE60') == 'https://www.youtube.com/watch?v=jeZOJfHWE60'
    assert ytdl.normalize('https://youtu.be/jeZOJfHWE60') == 'https://youtu.be/jeZOJfHWE60'


def test_korean_and_subs():
    assert ytdl.is_korean({'language': 'ko'})
    assert not ytdl.is_korean({'language': 'en', 'title': '재활용 달인의 집'})
    assert ytdl.is_korean({'title': '재활용 달인의 집 l 성남 월세'})
    assert not ytdl.is_korean({'title': 'How to cook rice'})
    assert ytdl.has_real_subs({'subtitles': {'ko': []}})
    assert ytdl.has_real_subs({'subtitles': {'ko-KR': []}})
    assert not ytdl.has_real_subs({'subtitles': {'en': []}, 'automatic_captions': {'ko': []}})


def test_cues_and_srt():
    w = lambda s, e, t: {'start': s, 'end': e, 'word': t}
    seg = lambda words: {'text': ''.join(x['word'] for x in words), 'compression_ratio': 1.2, 'no_speech_prob': 0.1,
                         'avg_logprob': -0.3, 'words': words}
    segs = [seg([w(0, 0.5, '안녕'), w(0.5, 1, '하세요.'), w(1.1, 1.5, ' 오늘은'), w(3, 3.5, ' 날씨')]),
            seg([w(4, 5, '구독과 좋아요')])]                                      # hallucination dropped
    c = whisper.cues(segs)
    assert [t for _, _, t in c] == ['안녕하세요.', '오늘은', '날씨']               # sentence end, then a pause
    out = whisper.srt(c)
    assert out.startswith('1\n00:00:00,000 --> 00:00:01,000\n안녕하세요.\n\n2\n')
    assert whisper.ts(3725.5) == '01:02:05,500'


def test_slug():
    assert slug('@Workman') == 'workman'
    assert slug('슈카월드 Syuka') == '슈카월드-syuka'
    assert slug('') == 'other'


class FakeRefresher:
    def soon(self):
        pass


def test_place_numbers_by_upload_order(media, tmp_path, monkeypatch):
    db = DB(tmp_path / 'db.sqlite')
    lib = library.Library(media)
    w = Worker(db, lib, FakeRefresher(), log=lambda *a: None)
    infos = {k: {'id': k, 'channel_id': 'UCwork', 'channel': 'Workman'} for k in ('aaaaaaaaaaa', 'bbbbbbbbbbb')}
    monkeypatch.setattr(ytdl, 'video_info', lambda url: infos[url[-11:]])
    monkeypatch.setattr(ytdl, 'uploads', lambda cid: {'UCwork': ['aaaaaaaaaaa', 'eeeeeeeeeee', 'bbbbbbbbbbb', 'hhhhhhhhhhh'],
                                                       'UCnew': ['fffffffffff']}[cid])
    # existing folder is recognised as its channel, a new upload gets its place in the upload list
    assert w.place({'id': 'hhhhhhhhhhh', 'channel_id': 'UCwork'}) == ('workman', 1, 3)
    # its place (2) is already used by another file -> next free number
    assert w.place({'id': 'eeeeeeeeeee', 'channel_id': 'UCwork'}) == ('workman', 1, 3)
    # a short (not in the uploads list) -> Season 0
    assert w.place({'id': 'ggggggggggg', 'channel_id': 'UCwork'}) == ('workman', 0, 1)
    # a new channel gets a folder named after its handle
    assert w.place({'id': 'fffffffffff', 'channel_id': 'UCnew', 'uploader_id': '@NewOne', 'channel': 'New'}) == \
        ('newone', 1, 1)


def test_jobs_dedupe(tmp_path):
    db = DB(tmp_path / 'db.sqlite')
    assert db.add_job('download', 'aaaaaaaaaaa', 'u')
    assert db.add_job('download', 'aaaaaaaaaaa', 'u') is None
    assert db.active_ids() == {'aaaaaaaaaaa'}


def test_number_follows_folder_direction(tmp_path):
    def lib_with(names):
        root = tmp_path / ('m' + str(len(list(tmp_path.iterdir()))))
        for n in names:
            touch(root / 'ch' / 'Season 1' / n)
        return library.Library(root)
    ids = [c * 11 for c in 'abcdefgh']                                   # oldest first
    # oldest-first folder, shifted by 300 (old uploads missing)
    w = Worker(DB(tmp_path / 'a.db'), lib_with(['S01E0301 - a [aaaaaaaaaaa].mkv', 'S01E0303 - c [ccccccccccc].mkv']),
               FakeRefresher(), log=lambda *a: None)
    assert w.number('ch', ids, ids.index('eeeeeeeeeee')) == 305
    # newest-first folder: an older video slots in after, a newer one (would be < 1) goes to the end
    w = Worker(DB(tmp_path / 'b.db'), lib_with(['S01E0001 - h [hhhhhhhhhhh].mkv', 'S01E0003 - f [fffffffffff].mkv']),
               FakeRefresher(), log=lambda *a: None)
    assert w.number('ch', ids, ids.index('ddddddddddd')) == 5
    assert w.number('ch', ids + ['iiiiiiiiiii'], 8) == 4
    # nothing known: oldest = 1
    w = Worker(DB(tmp_path / 'c.db'), lib_with([]), FakeRefresher(), log=lambda *a: None)
    assert w.number('ch', ids, 2) == 3
