"""Embedded-artwork availability is recorded when a file is read, so the tracks listing reports real availability and
never opens audio files. A thumbnail request that finds no usable picture corrects the record, so it is asked once."""
import json
import threading

import pytest

from meloshelf.application.scanner import LibraryScanner
from meloshelf.application.webapp import create_server
from meloshelf.domain.models import Track
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from meloshelf.infrastructure.tags import has_picture_frame
from test_qa_api import request

from audio_fixtures import write_mp3
from test_song_artwork import JPEG, PNG, with_picture


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / 'music'
    with_art = write_mp3(folder / 'with_art.mp3', tags={'title': 'With Art', 'artist': 'A', 'album': 'Loose'})
    with_picture(with_art, PNG, 'image/png')
    no_art = write_mp3(folder / 'no_art.mp3', tags={'title': 'No Art', 'artist': 'A', 'album': 'Plain'})
    gif = write_mp3(folder / 'gif_art.mp3', tags={'title': 'Gif', 'artist': 'A', 'album': 'Gif'})
    with_picture(gif, b'GIF89a' + b'\x00' * 40, 'image/gif')
    junk = write_mp3(folder / 'junk_art.mp3', tags={'title': 'Junk', 'artist': 'A', 'album': 'Junk'})
    with_picture(junk, b'\xff\xd8\xff' + b'corrupt', 'image/jpeg')
    return {'with_art': with_art, 'no_art': no_art, 'gif': gif, 'junk': junk}


# ── detection ────────────────────────────────────────────────────────────────────

def test_a_file_with_a_picture_frame_reports_true(library):
    assert has_picture_frame(library['with_art']) is True


def test_a_file_without_a_picture_reports_false(library):
    assert has_picture_frame(library['no_art']) is False


def test_a_damaged_or_missing_file_reports_false_and_does_not_raise(tmp_path):
    bad = tmp_path / 'bad.mp3'
    bad.write_bytes(b'not audio at all')
    assert has_picture_frame(bad) is False
    assert has_picture_frame(tmp_path / 'missing.mp3') is False


def test_detection_never_changes_the_file(library):
    before = (library['with_art'].read_bytes(), library['with_art'].stat().st_mtime_ns)
    has_picture_frame(library['with_art'])
    assert (library['with_art'].read_bytes(), library['with_art'].stat().st_mtime_ns) == before


def test_the_scanner_records_availability_for_each_real_file(library):
    scanner = LibraryScanner()
    flags = {name: scanner.read_track(path, path.stat()).has_picture for name, path in library.items()}
    assert flags == {'with_art': True, 'no_art': False, 'gif': True, 'junk': True}


# ── storage ──────────────────────────────────────────────────────────────────────

def test_the_flag_is_stored_as_unknown_yes_or_no(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    repo = LibraryRepository(db)
    base = dict(size=1, extension='.mp3', mtime_ns=1, title='T', artist='A', album='B', year='', status='tagged')
    repo.save_many([Track(path='/u.mp3', **base), Track(path='/n.mp3', has_picture=False, **base),
                    Track(path='/y.mp3', has_picture=True, **base)])
    rows = repo.get_many(['/u.mp3', '/n.mp3', '/y.mp3'])
    assert rows['/u.mp3']['embedded_art'] is None
    assert rows['/n.mp3']['embedded_art'] == 0
    assert rows['/y.mp3']['embedded_art'] == 1


def test_a_rescan_that_changes_the_file_updates_the_flag(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    repo = LibraryRepository(db)
    base = dict(size=1, extension='.mp3', mtime_ns=1, title='T', artist='A', album='B', year='', status='tagged')
    repo.save_many([Track(path='/a.mp3', has_picture=False, **base)])
    repo.save_many([Track(path='/a.mp3', has_picture=True, **base)])
    assert repo.get_many(['/a.mp3'])['/a.mp3']['embedded_art'] == 1


def test_the_migration_adds_the_column_to_a_schema_21_library(tmp_path):
    import sqlite3
    from meloshelf.infrastructure.database import SCHEMA_VERSION
    db = Database(tmp_path / 'old.sqlite3')
    db.initialize()
    with sqlite3.connect(db.path) as c:
        c.execute('ALTER TABLE tracks DROP COLUMN embedded_art')
        c.execute('PRAGMA user_version=21')
    db.initialize()
    with sqlite3.connect(db.path) as c:
        cols = [r[1] for r in c.execute('PRAGMA table_info(tracks)')]
        assert 'embedded_art' in cols
        assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


# ── listing: uses the record, never the file ─────────────────────────────────────

@pytest.fixture
def served(tmp_path, library):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    repo = LibraryRepository(running.database)
    scanner = LibraryScanner()
    repo.save_many([scanner.read_track(p, p.stat()) for p in library.values()])
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def listed(base):
    return {t['title']: t['art'] for t in json.loads(request(base, 'GET', '/api/tracks?limit=50')[2])['tracks']}


def test_a_file_without_a_picture_is_listed_with_no_artwork(served):
    _, base = served
    assert listed(base)['No Art'] is None


def test_a_file_with_a_picture_is_listed_as_embedded_and_unverified(served):
    _, base = served
    art = listed(base)['With Art']
    assert art['source'] == 'embedded' and art['verified'] is False


def test_the_listing_never_opens_an_audio_file(served, monkeypatch):
    import meloshelf.infrastructure.embedded_art as embedded
    import meloshelf.infrastructure.tags as tags

    def boom(*args, **kwargs):
        raise AssertionError('the listing must not read audio files')

    monkeypatch.setattr(embedded, 'read_embedded_art', boom)
    monkeypatch.setattr(tags, 'has_picture_frame', boom)
    _, base = served
    status, _, _ = request(base, 'GET', '/api/tracks?limit=50')
    assert status == 200


def test_a_row_whose_flag_is_unknown_is_not_requested_by_the_listing(tmp_path):
    """Unknown (legacy) rows get no thumbnail request: each page would otherwise send one request per row. The scan's
    bounded backfill is what learns the answer; until then the page shows the plain placeholder."""
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    plain = write_mp3(tmp_path / 'music' / 'plain.mp3', tags={'title': 'Plain', 'artist': 'A', 'album': 'Q'})
    LibraryRepository(running.database).save_many([Track(str(plain), 1, '.mp3', 1, 'Plain', 'A', 'Q', '', 'tagged')])
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{running.server_address[1]}'
    try:
        assert LibraryRepository(running.database).get_many([str(plain)])[str(plain)]['embedded_art'] is None
        assert json.loads(request(base, 'GET', '/api/tracks?limit=50')[2])['tracks'][0]['art'] is None
    finally:
        running.shutdown()
        running.server_close()


def test_a_scan_learns_the_flag_for_legacy_rows_within_the_backfill_budget(tmp_path, monkeypatch):
    import meloshelf.application.jobs as jobs
    from test_jobs import make_service, run_scan
    folder = tmp_path / 'music'
    for name in ('a', 'b', 'c'):
        write_mp3(folder / f'{name}.mp3', tags={'title': name, 'artist': 'A', 'album': 'Q'})
    with_picture(folder / 'b.mp3', PNG, 'image/png')
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    service = make_service(db)
    run_scan(service, folder)
    with db.write() as c:      # simulate rows indexed before the flag existed
        c.execute('UPDATE tracks SET embedded_art=NULL, numbers_indexed=0')
    monkeypatch.setattr(jobs, 'NUMBERS_PER_SCAN', 2)
    run_scan(service, folder)
    rows = LibraryRepository(db).get_many([str(folder / f'{n}.mp3') for n in 'abc'])
    learned = [rows[str(folder / f'{n}.mp3')]['embedded_art'] for n in 'abc']
    assert learned.count(None) == 1, 'the budget bounds one scan: exactly one file is left unknown'
    run_scan(service, folder)
    rows = LibraryRepository(db).get_many([str(folder / f'{n}.mp3') for n in 'abc'])
    assert rows[str(folder / 'b.mp3')]['embedded_art'] == 1
    assert all(rows[str(folder / f'{n}.mp3')]['embedded_art'] in (0, 1) for n in 'abc')


# ── corrupted and unsupported pictures ───────────────────────────────────────────

def test_an_unsupported_or_corrupt_picture_is_not_served_and_is_recorded_as_none(served, library):
    running, base = served
    from urllib.parse import quote
    for name in ('gif', 'junk'):
        status, _, _ = request(base, 'GET', '/api/track-artwork?path=' + quote(str(library[name]), safe=''))
        assert status == 404
    rows = LibraryRepository(running.database).get_many([str(library['gif']), str(library['junk'])])
    assert all(row['embedded_art'] == 0 for row in rows.values())
    assert listed(base)['Gif'] is None and listed(base)['Junk'] is None


def test_a_served_picture_is_recorded_as_present(served, library):
    running, base = served
    from urllib.parse import quote
    assert request(base, 'GET', '/api/track-artwork?path=' + quote(str(library['with_art']), safe=''))[0] == 200
    assert LibraryRepository(running.database).get_many([str(library['with_art'])])[str(library['with_art'])]['embedded_art'] == 1
