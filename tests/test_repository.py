from meloshelf.domain.models import Track
from meloshelf.infrastructure.repository import LibraryRepository


def make_track(path='/tmp/001.mp3', title='001', artist='', album='', status='unidentified'):
    return Track(path=path, size=12, extension='.mp3', mtime_ns=123, title=title, artist=artist, album=album, year='', status=status)


def test_upsert_is_idempotent_and_counts_are_correct(tmp_path):
    repo = LibraryRepository(tmp_path / 'library.sqlite3')
    repo.save_many([make_track()])
    repo.save_many([make_track(title='Hello', artist='Adele', album='25', status='tagged')])
    assert repo.counts() == {'total':1,'tagged':1,'review':0,'unidentified':0}
    assert repo.list_tracks(limit=5)[0]['title'] == 'Hello'
    repo.close()


def test_uses_pagination_not_full_table_load(tmp_path):
    repo = LibraryRepository(tmp_path / 'library.sqlite3')
    repo.save_many([make_track(path=f'/tmp/{i}.mp3') for i in range(150)])
    assert len(repo.list_tracks(limit=20)) == 20
    assert len(repo.list_tracks(limit=20, offset=140)) == 10
    repo.close()


def test_source_files_not_moved_by_repository(tmp_path):
    song = tmp_path / 'song.mp3'
    song.write_bytes(b'original-file')
    repo = LibraryRepository(tmp_path / 'library.sqlite3')
    repo.save_many([make_track(path=str(song))])
    assert song.read_bytes() == b'original-file'
    repo.close()
