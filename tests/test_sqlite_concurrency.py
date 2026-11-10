"""Regression suite for the Windows 'database is locked' startup failure.

Root cause under test: every request opened a new connection and ran
``PRAGMA journal_mode=WAL`` plus schema DDL. While the file is not yet in WAL
mode, concurrent connections race for the exclusive lock that the switch needs
and SQLite fails them immediately, bypassing the busy timeout.
"""
import gc
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.domain.models import Track
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.repository import LibraryRepository

DB_ENDPOINTS = ('/api/stats', '/api/tracks?limit=5', '/api/matches')
CANDIDATE = {'recording_id': '11111111-1111-4111-8111-111111111111', 'title': 'One',
             'artist': 'Band', 'acoustid_score': .98, 'releases': []}


def make_track(n, status='tagged'):
    return Track(f'/music/{n}.mp3', 10, '.mp3', 1, f'Title {n}', 'Artist', 'Album', '', status)


@contextmanager
def running_server(db_path, tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    server = create_server(db_path=db_path, html_path=html)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get_json(base, path):
    with urlopen(base + path, timeout=30) as response:
        return json.load(response)


def post_json(server, base, path, data):
    request = Request(base + path, data=json.dumps(data).encode(), method='POST',
                      headers={'Content-Type': 'application/json', 'X-MeloShelf-Token': server.token})
    return urlopen(request, timeout=30)


def fetch_all_at_once(base, paths):
    """Release every request at the same instant; return (path, outcome) pairs."""
    barrier = threading.Barrier(len(paths))

    def fetch(path):
        barrier.wait(timeout=10)
        try:
            with urlopen(base + path, timeout=30) as response:
                json.load(response)
                return path, response.status
        except Exception as exc:  # the outcome is asserted on by the caller
            return path, f'{type(exc).__name__}: {exc}'

    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        return list(pool.map(fetch, paths))


def run_together(workers):
    """Run callables simultaneously; return the exceptions they raised."""
    barrier = threading.Barrier(len(workers))
    errors = []

    def guarded(worker):
        barrier.wait(timeout=10)
        try:
            worker()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return errors


def open_sqlite_connections():
    gc.collect()
    live = []
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.total_changes
            except sqlite3.ProgrammingError:
                continue  # closed
            live.append(obj)
    return live


def journal_mode_on_disk(path):
    """Read the journal mode from the file header without opening a connection."""
    with open(path, 'rb') as handle:
        header = handle.read(20)
    return 'wal' if header[18:20] == b'\x02\x02' else 'rollback'


# 1 ─ simultaneous GETs -------------------------------------------------------

def test_simultaneous_gets_on_fresh_databases_all_succeed(tmp_path):
    failures = []
    for attempt in range(12):
        with running_server(tmp_path / f'fresh{attempt}.sqlite3', tmp_path) as (_, base):
            outcomes = fetch_all_at_once(base, DB_ENDPOINTS * 8)
            failures += [o for o in outcomes if o[1] != 200]
    assert not failures, f'{len(failures)} requests failed, e.g. {failures[:3]}'


def test_database_is_fully_initialized_before_requests_are_accepted(tmp_path):
    db_path = tmp_path / 'lib.sqlite3'
    with running_server(db_path, tmp_path):
        assert db_path.exists(), 'schema must be created at startup, not by the first GET'
        assert journal_mode_on_disk(db_path) == 'wal'


def test_get_requests_do_not_alter_schema_or_journal_mode(tmp_path):
    db_path = tmp_path / 'lib.sqlite3'
    with running_server(db_path, tmp_path) as (server, base):
        with server.database.read() as connection:
            before = connection.execute('PRAGMA schema_version').fetchone()[0]
        fetch_all_at_once(base, DB_ENDPOINTS * 4)
        with server.database.read() as connection:
            assert connection.execute('PRAGMA schema_version').fetchone()[0] == before


# 2 ─ both repository classes concurrently -----------------------------------

def test_both_repository_classes_open_fresh_database_concurrently(tmp_path):
    errors = []
    for attempt in range(20):
        db_path = tmp_path / f'both{attempt}.sqlite3'

        def use_repository():
            repo = LibraryRepository(db_path)
            repo.counts()
            repo.close()

        def use_store():
            with MatchStore(db_path) as store:
                store.list_results()

        errors += run_together([use_repository, use_store] * 6)
    assert not errors, f'{len(errors)} failures, e.g. {errors[0]!r}'


# 3 ─ read during an open write transaction ----------------------------------

def test_read_is_not_blocked_by_an_open_write_transaction(tmp_path):
    from meloshelf.infrastructure.database import Database
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    repo = LibraryRepository(database)
    repo.save_many([make_track(1)])
    with database.write() as connection:
        connection.execute("UPDATE tracks SET title='uncommitted'")
        started = time.perf_counter()
        rows = repo.list_tracks(limit=5)
        elapsed = time.perf_counter() - started
    assert rows[0]['title'] == 'Title 1', 'readers must see the last committed snapshot'
    assert elapsed < 1, f'read waited {elapsed:.2f}s on the writer'
    assert repo.list_tracks(limit=5)[0]['title'] == 'uncommitted'


# 4 ─ coordinated concurrent writes ------------------------------------------

def test_concurrent_writes_are_coordinated_without_loss(tmp_path):
    db_path = tmp_path / 'lib.sqlite3'
    repo, store = LibraryRepository(db_path), MatchStore(db_path)

    def track_writer(worker):
        return lambda: [repo.save_many([make_track(f'{worker}-{i}')]) for i in range(25)]

    def cache_writer(worker):
        return lambda: [store.put_cache(f'{worker}-{i}', {'i': i}) for i in range(25)]

    errors = run_together([track_writer(w) for w in range(6)] + [cache_writer(w) for w in range(6)])
    assert not errors, errors[:1]
    assert repo.counts()['total'] == 150
    assert all(store.get_cache(f'{w}-{i}') == {'i': i} for w in range(6) for i in range(25))


# 5 / 6 ─ idempotent initialization, no needless migration ---------------------

def test_initialization_is_idempotent(tmp_path):
    from meloshelf.infrastructure.database import Database
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    with database.read() as connection:
        before = connection.execute('PRAGMA schema_version').fetchone()[0]
    for _ in range(3):
        database.initialize()
    with database.read() as connection:
        assert connection.execute('PRAGMA schema_version').fetchone()[0] == before
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_opening_existing_database_issues_no_schema_or_journal_statements(tmp_path, monkeypatch):
    from meloshelf.infrastructure import database as database_module
    db_path = tmp_path / 'lib.sqlite3'
    database_module.Database(db_path).initialize()
    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database_module.sqlite3, 'connect', traced_connect)
    database_module.Database(db_path).initialize()
    repo = LibraryRepository(db_path)
    repo.counts()
    forbidden = [s for s in statements
                 if s.upper().startswith(('CREATE', 'ALTER', 'DROP', 'BEGIN'))
                 or 'JOURNAL_MODE=' in s.upper().replace(' ', '')]
    assert not forbidden, forbidden


def test_legacy_database_is_migrated_once_and_keeps_its_rows(tmp_path):
    from meloshelf.infrastructure.database import Database
    db_path = tmp_path / 'legacy.sqlite3'
    legacy = sqlite3.connect(db_path)  # 0.2.0 layout before selection columns, rollback journal
    legacy.executescript('''
        CREATE TABLE match_results (path TEXT PRIMARY KEY, file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
            created_at INTEGER NOT NULL, decision TEXT NOT NULL DEFAULT 'pending');
        INSERT INTO match_results VALUES('a.mp3',1,1,'review','{"candidates":[],"reason":"r"}',1,'pending');
    ''')
    legacy.close()
    Database(db_path).initialize()
    assert journal_mode_on_disk(db_path) == 'wal'
    with MatchStore(db_path) as store:
        assert store.get_result('a.mp3')['selected_recording_id'] == ''
    assert LibraryRepository(db_path).counts()['total'] == 0


# 7 ─ resource cleanup --------------------------------------------------------

def test_connections_are_closed_after_use_and_after_failures(tmp_path):
    baseline = len(open_sqlite_connections())
    db_path = tmp_path / 'lib.sqlite3'
    repo, store = LibraryRepository(db_path), MatchStore(db_path)
    repo.save_many([make_track(1)])
    repo.counts()
    repo.list_tracks(query='Title')
    store.put_result('a.mp3', 1, 1, 'review', {'candidates': [CANDIDATE], 'reason': 'r'})
    with pytest.raises(ValueError):
        store.set_decision('a.mp3', 'accepted', recording_id='missing')
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_many([make_track(2, status='not-a-status')])
    assert len(open_sqlite_connections()) == baseline
    db_path.rename(tmp_path / 'renamed.sqlite3')  # fails on Windows if a handle leaked


# 8 ─ recovery after a failed transaction ------------------------------------

def test_failed_transaction_rolls_back_and_releases_the_writer(tmp_path):
    from meloshelf.infrastructure.database import Database
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    repo = LibraryRepository(database)
    with pytest.raises(RuntimeError):
        with database.write() as connection:
            connection.execute("INSERT INTO tracks VALUES('/x.mp3',1,'.mp3',1,'t','a','b','','tagged')")
            raise RuntimeError('simulated crash mid-transaction')
    assert repo.counts()['total'] == 0, 'partial write must be rolled back'
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_many([make_track(1), make_track(2, status='not-a-status')])
    assert repo.counts()['total'] == 0, 'a failing batch must not be half-saved'
    repo.save_many([make_track(3)])
    assert repo.counts()['total'] == 1


def test_persistent_lock_is_reported_as_actionable_503_not_a_dropped_connection(tmp_path):
    db_path = tmp_path / 'lib.sqlite3'
    with running_server(db_path, tmp_path) as (server, base):
        MatchStore(server.database).put_result('a.mp3', 1, 1, 'review', {'candidates': [CANDIDATE], 'reason': 'r'})
        server.database.busy_timeout_ms = 150
        blocker = sqlite3.connect(db_path, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')  # another process holding the write lock
        try:
            with pytest.raises(HTTPError) as caught:
                post_json(server, base, '/api/decision', {'path': 'a.mp3', 'decision': 'rejected'})
            assert caught.value.code == 503
            assert 'busy' in json.load(caught.value)['error'].lower()
            assert get_json(base, '/api/stats')['total'] == 0, 'reads keep working meanwhile'
        finally:
            blocker.execute('ROLLBACK')
            blocker.close()
        with post_json(server, base, '/api/decision', {'path': 'a.mp3', 'decision': 'rejected'}) as response:
            assert response.status == 200, 'server must recover once the lock is released'


# 9 ─ rapid navigation --------------------------------------------------------

def test_rapid_page_navigation_does_not_crash_server(tmp_path):
    with running_server(tmp_path / 'lib.sqlite3', tmp_path) as (_, base):
        for _ in range(8):
            outcomes = fetch_all_at_once(base, ('/', '/api/status', '/api/config') + DB_ENDPOINTS * 3)
            assert all(status == 200 or path == '/' for path, status in outcomes), outcomes
        assert get_json(base, '/api/status')['running'] is False


# 10 / 11 / 12 ─ restart, integrity, audio untouched ---------------------------

def test_restart_preserves_data_integrity_holds_and_audio_is_untouched(tmp_path):
    from meloshelf.infrastructure.database import Database
    music = tmp_path / 'music'
    music.mkdir()
    for n in range(30):
        (music / f'{n:03}.mp3').write_bytes(f'fake audio payload {n}'.encode() * 50)

    def audio_state():
        return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in sorted(music.iterdir())}

    before = audio_state()
    db_path = tmp_path / 'lib.sqlite3'
    with running_server(db_path, tmp_path) as (server, base):
        with post_json(server, base, '/api/scan', {'folder': str(music)}) as response:
            assert response.status == 202
        while True:  # hammer reads while the scan job is writing
            outcomes = fetch_all_at_once(base, DB_ENDPOINTS * 3)
            assert all(status == 200 for _, status in outcomes), outcomes
            if not get_json(base, '/api/status')['running']:
                break
        assert get_json(base, '/api/stats')['total'] == 30

    for _ in range(3):  # repeated startup/shutdown cycles
        with running_server(db_path, tmp_path) as (_, base):
            assert get_json(base, '/api/stats')['total'] == 30
            assert len(get_json(base, '/api/tracks?limit=50')['tracks']) == 30

    assert Database(db_path).integrity_check() == []
    assert audio_state() == before
    assert sorted(p.name for p in music.iterdir()) == sorted(before)
