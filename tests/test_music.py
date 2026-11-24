from ytshelf import music
from ytshelf.db import DB


def test_meta_from_youtube_music():
    m = music.meta({'artist': 'IU', 'artists': ['IU'], 'track': 'Love wins all', 'album': 'The Winning',
                    'release_year': 2024, 'title': 'Love wins all', 'channel': 'IU - Topic'}, track=2)
    assert m == {'artist': 'IU', 'album': 'The Winning', 'title': 'Love wins all', 'year': 2024, 'track': 2,
                 'all_artists': 'IU'}


def test_meta_from_plain_video():
    m = music.meta({'title': 'NewJeans (뉴진스) - Supernatural (Official MV)', 'channel': 'HYBE LABELS',
                    'upload_date': '20240621'})
    assert (m['artist'], m['title'], m['album'], m['year'], m['track']) == \
        ('NewJeans (뉴진스)', 'Supernatural', 'Singles', 2024, None)
    m = music.meta({'title': 'Some Song [MV]', 'channel': 'Some Band - Topic'}, track=4)
    assert (m['artist'], m['title'], m['track']) == ('Some Band', 'Some Song', None)   # no album -> no track no.
    m = music.meta({'title': '[MV] IU(아이유) _ Through the Night(밤편지)', 'channel': '1theK (원더케이)'})
    assert (m['artist'], m['title']) == ('IU(아이유)', 'Through the Night(밤편지)')
    m = music.meta({'title': "IU 'Love wins all' MV", 'channel': 'Rough Cut'})
    assert (m['artist'], m['title']) == ('IU', 'Love wins all')
    m = music.meta({'title': 'Just a title', 'channel': 'Artist VEVO'})
    assert (m['artist'], m['title']) == ('Artist', 'Just a title')
    m = music.meta({'artist': 'A, B', 'artists': ['A', 'B'], 'track': 'Duet', 'title': 'Duet'})
    assert (m['artist'], m['all_artists']) == ('A', 'A, B')


def test_clean_title():
    assert music.clean_title('Hype Boy (Official Music Video)') == 'Hype Boy'
    assert music.clean_title('Song (Lyrics)') == 'Song'
    assert music.clean_title('Song ㅣ Live Clip') == 'Song'
    assert music.clean_title('[MV] IU _ Song') == 'IU _ Song'


def test_out_template(tmp_path):
    d, name = music.out_template(tmp_path, {'artist': 'AC/DC', 'album': 'Back: in Black', 'title': 'Hells "Bells"',
                                            'track': 1})
    assert d == tmp_path / 'ACDC' / 'Back in Black' and name == '01 - Hells Bells [%(id)s].%(ext)s'
    d, name = music.out_template(tmp_path, {'artist': 'IU', 'album': 'Singles', 'title': '밤편지', 'track': None})
    assert d == tmp_path / 'IU' / 'Singles' and name == '밤편지 [%(id)s].%(ext)s'


def test_library_scan_and_delete(tmp_path):
    root = tmp_path / 'music'
    alb = root / 'IU' / 'The Winning'
    alb.mkdir(parents=True)
    (alb / '02 - Love wins all [aaaaaaaaaaa].m4a').write_bytes(b'x' * 10)
    (alb / 'folder.jpg').write_bytes(b'j')
    (root / 'IU' / 'Singles').mkdir()
    (root / 'IU' / 'Singles' / '밤편지 [bbbbbbbbbbb].m4a').write_bytes(b'x')
    lib = music.MusicLibrary(root)
    a = lib.scan()['IU']
    assert [(s['album'], s['track'], s['title']) for s in a['songs']] == \
        [('The Winning', 2, 'Love wins all'), ('Singles', None, '밤편지')]
    assert lib.ids() == {'aaaaaaaaaaa', 'bbbbbbbbbbb'}
    lib.delete(lib.find('IU/The Winning/02 - Love wins all [aaaaaaaaaaa].m4a'))
    assert not alb.exists()                          # only cover art was left -> album folder removed
    lib.delete(lib.find('IU/Singles/밤편지 [bbbbbbbbbbb].m4a'))
    assert not (root / 'IU').exists() and root.exists()


def test_modes_dedupe_separately(tmp_path):
    db = DB(tmp_path / 'db.sqlite')
    assert db.add_job('download', 'aaaaaaaaaaa', 'u')
    assert db.add_job('download', 'aaaaaaaaaaa', 'u', mode='music')          # same video, other library: fine
    assert db.add_job('download', 'aaaaaaaaaaa', 'u', mode='music') is None
    assert db.active_ids(mode='music') == {'aaaaaaaaaaa'}
