"""Track and disc numbers: read from the tags (never written), parsed strictly, stored with the track, backfilled
gradually for files indexed before the feature existed, and used to order the tracks of an album."""
import json
import os
import threading
from pathlib import Path

import pytest

from meloshelf.application.scanner import LibraryScanner
from meloshelf.application.webapp import create_server
from meloshelf.domain.models import Track
from meloshelf.domain.track_numbers import parse_position
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from meloshelf.infrastructure.tags import read_tags
from audio_fixtures import write_mp3
from test_jobs import make_service, run_scan
from test_qa_api import request


# ── parsing ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,expected', [
    ('3', (3, None)), ('03', (3, None)), (' 7 ', (7, None)), ('3/12', (3, 12)), ('03/12', (3, 12)), ('3 / 12', (3, 12)),
    ('3 of 12', (3, 12)), ('1/1', (1, 1)), ('12/', (12, None)), ('/12', (None, 12)), ('999', (999, None)),
])
def test_good_positions(text, expected):
    assert parse_position(text) == expected


@pytest.mark.parametrize('text', ['', None, 'x', '0', '0/0', '-3', '3/-1', '1000', '3/1000', 'a/b', '3.5', '١٢', '3/0', '1e3',
                                  'Disc 2 of 3 extra', '7' * 40])
def test_unusable_positions_give_nothing(text):
    assert parse_position(text) == (None, None)


def test_a_number_above_its_total_is_kept_but_the_total_is_dropped():
    assert parse_position('9/3') == (9, None)


# ── reading tags ─────────────────────────────────────────────────────────────────

def tagged_mp3(path: Path, **frames):
    """A real MP3 (valid MPEG frames) with easy-ID3 tags; written only into pytest's temporary folder."""
    names = {'track': 'tracknumber', 'disc': 'discnumber'}
    return write_mp3(path, tags={names.get(key, key): value for key, value in frames.items()})


def test_the_tag_reader_returns_the_raw_position_text(tmp_path):
    path = tagged_mp3(tmp_path / 'a.mp3', title='Song', track='3/12', disc='2/2')
    tags = read_tags(path)
    assert tags['tracknumber'] == '3/12' and tags['discnumber'] == '2/2' and tags['title'] == 'Song'


def test_a_file_without_positions_has_none_in_its_tags(tmp_path):
    tags = read_tags(tagged_mp3(tmp_path / 'a.mp3', title='Song'))
    assert 'tracknumber' not in tags and 'discnumber' not in tags


def test_reading_numbers_never_changes_the_file(tmp_path):
    path = tagged_mp3(tmp_path / 'a.mp3', title='Song', track='3/12')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    read_tags(path)
    LibraryScanner().read_track(path, path.stat())
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_the_scanner_turns_tag_text_into_numbers(tmp_path):
    path = tmp_path / 'a.mp3'
    path.write_bytes(b'x')
    track = LibraryScanner(tag_reader=lambda _: {'title': 'S', 'tracknumber': '03/12', 'discnumber': '2'}).read_track(path, path.stat())
    assert (track.track_no, track.track_total, track.disc_no, track.disc_total, track.numbers_indexed) == (3, 12, 2, None, True)


def test_garbage_positions_leave_the_numbers_empty_but_the_file_counts_as_read(tmp_path):
    path = tmp_path / 'a.mp3'
    path.write_bytes(b'x')
    track = LibraryScanner(tag_reader=lambda _: {'tracknumber': 'zero', 'discnumber': '0'}).read_track(path, path.stat())
    assert (track.track_no, track.disc_no, track.numbers_indexed) == (None, None, True)


# ── storage ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    return db


def track(path, **kw):
    base = dict(path=path, size=1, extension='.mp3', mtime_ns=1, title='T', artist='A', album='Alb', year='', status='tagged')
    return Track(**{**base, **kw})


def test_numbers_are_stored_and_updated_with_the_track(database):
    repo = LibraryRepository(database)
    repo.save_many([track('/a.mp3', track_no=3, track_total=12, disc_no=1, disc_total=2, numbers_indexed=True)])
    row = repo.get_many(['/a.mp3'])['/a.mp3']
    assert (row['track_no'], row['track_total'], row['disc_no'], row['disc_total'], row['numbers_indexed']) == (3, 12, 1, 2, 1)
    repo.save_many([track('/a.mp3', track_no=4, numbers_indexed=True)])
    assert repo.get_many(['/a.mp3'])['/a.mp3']['track_no'] == 4 and repo.get_many(['/a.mp3'])['/a.mp3']['disc_no'] is None


def test_a_track_saved_without_numbers_is_marked_not_yet_read(database):
    repo = LibraryRepository(database)
    repo.save_many([track('/a.mp3')])
    row = repo.get_many(['/a.mp3'])['/a.mp3']
    assert row['track_no'] is None and row['numbers_indexed'] == 0


def test_the_migration_is_safe_to_run_on_a_database_that_already_has_the_columns(database):
    from meloshelf.infrastructure.database import _add_track_numbers
    with database.write() as db:
        _add_track_numbers(db)
        _add_track_numbers(db)
    assert LibraryRepository(database).get_many(['/none']) == {}


# ── the scan: new files, changed files, and the gradual backfill ─────────────────

class NumberReader:
    """Tag reader that counts reads and takes the track number from the file name (3.mp3 -> track 3 of 9)."""

    def __init__(self):
        self.reads = 0

    def __call__(self, path):
        self.reads += 1
        stem = Path(path).stem
        return {'title': f'Song {stem}', 'artist': 'Band', 'album': 'Album', 'tracknumber': f'{stem}/9', 'discnumber': '1/2'}


def numbered_library(tmp_path, count):
    folder = tmp_path / 'music'
    folder.mkdir()
    for number in range(1, count + 1):
        (folder / f'{number}.mp3').write_bytes(b'x')
    return folder


def rows_of(database):
    with database.read() as db:
        return {Path(r['path']).stem: dict(r) for r in db.execute('SELECT * FROM tracks')}


def test_a_scan_indexes_the_numbers_of_new_files(tmp_path, database):
    folder = numbered_library(tmp_path, 3)
    run_scan(make_service(database, reader=NumberReader()), folder)
    rows = rows_of(database)
    assert [(rows[k]['track_no'], rows[k]['track_total'], rows[k]['disc_no'], rows[k]['disc_total'], rows[k]['numbers_indexed'])
            for k in ('1', '2', '3')] == [(1, 9, 1, 2, 1), (2, 9, 1, 2, 1), (3, 9, 1, 2, 1)]


def test_files_whose_numbers_are_already_indexed_are_not_read_again(tmp_path, database):
    folder = numbered_library(tmp_path, 3)
    reader = NumberReader()
    service = make_service(database, reader=reader)
    run_scan(service, folder)
    reads = reader.reads
    run_scan(service, folder)
    assert reader.reads == reads


def test_a_changed_file_is_read_again(tmp_path, database):
    folder = numbered_library(tmp_path, 2)
    reader = NumberReader()
    service = make_service(database, reader=reader)
    run_scan(service, folder)
    target = folder / '2.mp3'
    target.write_bytes(b'changed')
    os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 5_000_000_000))
    reader.reads = 0
    run_scan(service, folder)
    assert reader.reads == 1


def forget_numbers(database):
    with database.write() as db:
        db.execute('UPDATE tracks SET numbers_indexed=0, track_no=NULL, track_total=NULL, disc_no=NULL, disc_total=NULL')


def test_files_indexed_before_the_feature_are_backfilled_a_bounded_number_per_scan(tmp_path, database, monkeypatch):
    import meloshelf.application.jobs as jobs
    folder = numbered_library(tmp_path, 5)
    reader = NumberReader()
    service = make_service(database, reader=reader)
    run_scan(service, folder)
    forget_numbers(database)
    monkeypatch.setattr(jobs, 'NUMBERS_PER_SCAN', 2)
    done = []
    for _ in range(4):
        reader.reads = 0
        run_scan(service, folder)
        done.append((reader.reads, sum(r['numbers_indexed'] for r in rows_of(database).values())))
    assert done == [(2, 2), (2, 4), (1, 5), (0, 5)]
    assert all(r['track_no'] is not None for r in rows_of(database).values())


def test_a_file_with_no_numbers_is_marked_read_and_not_read_again(tmp_path, database):
    folder = tmp_path / 'music'
    folder.mkdir()
    (folder / 'plain.mp3').write_bytes(b'x')
    reads = []
    service = make_service(database, reader=lambda p: reads.append(p) or {'title': 'T', 'artist': 'A', 'album': 'B'})
    run_scan(service, folder)
    run_scan(service, folder)
    row = rows_of(database)['plain']
    assert len(reads) == 1 and row['numbers_indexed'] == 1 and row['track_no'] is None


# ── ordering and the API ─────────────────────────────────────────────────────────

def test_the_tracks_of_an_album_are_ordered_by_disc_then_track_then_path(database):
    repo = LibraryRepository(database)
    repo.save_many([
        track('/d2t1.mp3', disc_no=2, track_no=1, numbers_indexed=True), track('/d1t2.mp3', disc_no=1, track_no=2, numbers_indexed=True),
        track('/d1t10.mp3', disc_no=1, track_no=10, numbers_indexed=True), track('/d1t1.mp3', disc_no=1, track_no=1, numbers_indexed=True),
        track('/nonum_b.mp3'), track('/nonum_a.mp3'), track('/d0t3.mp3', track_no=3, numbers_indexed=True)])
    ordered = [t['path'] for t in repo.list_tracks(album='Alb', artist='A', limit=50)]
    assert ordered == ['/d0t3.mp3', '/d1t1.mp3', '/d1t2.mp3', '/d1t10.mp3', '/d2t1.mp3', '/nonum_a.mp3', '/nonum_b.mp3']


def test_without_an_album_the_listing_order_is_unchanged(database):
    repo = LibraryRepository(database)
    repo.save_many([track('/b.mp3', track_no=1, numbers_indexed=True), track('/a.mp3', track_no=9, numbers_indexed=True)])
    assert [t['path'] for t in repo.list_tracks(limit=50)] == ['/a.mp3', '/b.mp3']


@pytest.fixture
def served(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    LibraryRepository(running.database).save_many([
        track('/b.mp3', track_no=2, track_total=2, disc_no=1, disc_total=1, numbers_indexed=True),
        track('/a.mp3', track_no=1, track_total=2, disc_no=1, disc_total=1, numbers_indexed=True),
        track('/z.mp3', album='Other')])
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def test_the_tracks_api_carries_the_numbers_and_can_list_one_album_in_order(served):
    _, base = served
    page = json.loads(request(base, 'GET', '/api/tracks?album=Alb&artist=A')[2])
    assert [t['path'] for t in page['tracks']] == ['/a.mp3', '/b.mp3'] and page['total'] == 2
    first = page['tracks'][0]
    assert (first['track_no'], first['track_total'], first['disc_no'], first['disc_total']) == (1, 2, 1, 1)
    assert 'numbers_indexed' not in first


def test_the_page_shows_the_position_next_to_a_song():
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'positionLabel' in page
