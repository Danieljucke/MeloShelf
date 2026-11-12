"""Round 4 performance and durability checks at 10,000 and 50,000 synthetic tracks.

Synthetic data only: no audio is read and no provider is contacted (the lookup uses a fake client).
Budgets are generous upper bounds that catch regressions to quadratic behaviour; the measured numbers are written
to MELOSHELF_PERF_OUT (if set) so the report can cite them. Failing a budget is a finding, not a reason to loosen it
silently: change the budget only with a measurement that justifies it, and record why.
"""
import json
import os
import threading
import time

import pytest

from meloshelf.application.artist_lookup import ArtistLookup
from meloshelf.application.webapp import create_server
from meloshelf.domain.artist_identity import ArtistCandidate, decide_link  # noqa: F401  (import check)
from meloshelf.domain.artists import canonical_key
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_corrections import ArtistCorrections
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

RESULTS = {}
BUDGET_SECONDS = {10_000: {'grouping': 8.0, 'lookup_store': 20.0, 'recordings': 3.0, 'http_identities': 8.0},
                  50_000: {'grouping': 40.0, 'lookup_store': 90.0, 'recordings': 10.0, 'http_identities': 40.0}}


def synthetic(total_tracks):
    artists = max(50, total_tracks // 7)
    rows = []
    for t in range(total_tracks):
        a = t % artists
        primary = f'Artist {a:05d}' if t % 5 else f'artist {a:05d}'           # capitalisation variants
        if t % 11 == 0:
            primary = f'Artist {a:05d} feat. Guest {(t * 7) % artists:05d}'   # featured credits
        rows.append(Track(f'D:/Library/{t:06d}.mp3', 1, '.mp3', 1, 'T', primary, f'Album {t // 12}', '', 'tagged'))
    return rows


def build(path, total):
    db = Database(path)
    db.initialize()
    started = time.perf_counter()
    LibraryRepository(db).save_many(synthetic(total))
    RESULTS.setdefault(total, {})['build_seconds'] = round(time.perf_counter() - started, 2)
    return db


class FastClient:
    """Deterministic fake: every name answers with one strong primary match of its own ID."""

    def search_artists(self, name, *, limit=5):
        return [{'mb_artist_id': 'mb-' + canonical_key(name).replace(' ', '-'), 'name': name, 'score': 100,
                 'aliases': [], 'disambiguation': ''}]


def record(total, key, seconds):
    RESULTS.setdefault(total, {})[key] = round(seconds, 3)


@pytest.fixture(scope='module', autouse=True)
def write_results():
    yield
    out = os.environ.get('MELOSHELF_PERF_OUT')
    if out and RESULTS:
        with open(out, 'w', encoding='utf-8') as handle:
            json.dump({str(k): v for k, v in RESULTS.items()}, handle, indent=2)


@pytest.mark.parametrize('total', [10_000, 50_000])
def test_grouping_stays_within_budget(tmp_path, total):
    db = build(tmp_path / f'{total}.sqlite3', total)
    started = time.perf_counter()
    artists, count = LibraryRepository(db).artist_identities(limit=60)
    elapsed = time.perf_counter() - started
    record(total, 'grouping_seconds', elapsed)
    assert count > 0 and len(artists) == 60
    assert elapsed < BUDGET_SECONDS[total]['grouping'], f'grouping took {elapsed:.2f}s at {total}'


@pytest.mark.parametrize('total', [10_000, 50_000])
def test_recording_lookup_for_one_artist_stays_bounded(tmp_path, total):
    db = build(tmp_path / f'rec{total}.sqlite3', total)
    started = time.perf_counter()
    rows, count = LibraryRepository(db).artist_recordings('Artist 00003', limit=200)
    elapsed = time.perf_counter() - started
    record(total, 'recordings_seconds', elapsed)
    assert count > 0 and len(rows) <= 200
    assert elapsed < BUDGET_SECONDS[total]['recordings'], f'recordings took {elapsed:.2f}s at {total}'


@pytest.mark.parametrize('total', [10_000, 50_000])
def test_linked_lookup_over_every_distinct_name_stays_bounded(tmp_path, total):
    db = build(tmp_path / f'lk{total}.sqlite3', total)
    started = time.perf_counter()
    summary = ArtistLookup(db, FastClient()).run()
    elapsed = time.perf_counter() - started
    record(total, 'lookup_store_seconds', elapsed)
    record(total, 'lookup_names', summary['looked_up'])
    assert summary['linked'] == summary['looked_up'] > 0 and summary['failed'] == 0
    assert elapsed < BUDGET_SECONDS[total]['lookup_store']
    again = ArtistLookup(db, FastClient()).run()
    assert again['looked_up'] == 0, 'stored answers are reused; no name is looked up twice'


@pytest.mark.parametrize('total', [10_000, 50_000])
def test_the_identities_endpoint_responds_within_budget_at_scale(tmp_path, total):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    db_path = tmp_path / f'http{total}.sqlite3'
    build(db_path, total)
    running = create_server(db_path=db_path, html_path=html, secret_store=MemorySecretStore(), environ={})
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{running.server_address[1]}'
    try:
        started = time.perf_counter()
        status, _, body = request(base, 'GET', '/api/artist-identities?limit=60')
        elapsed = time.perf_counter() - started
        record(total, 'http_identities_seconds', elapsed)
        assert status == 200 and len(json.loads(body)['artists']) == 60
        assert elapsed < BUDGET_SECONDS[total]['http_identities']
    finally:
        running.shutdown()
        running.server_close()


def test_corrections_and_links_survive_a_restart_and_a_reindex(tmp_path):
    db = build(tmp_path / 'durable.sqlite3', 10_000)
    ArtistCorrections(db).keep_separate('Artist 00001', 'Artist 00002')
    ArtistLinks(db).record('Artist 00001', decide_link('Artist 00001', [
        ArtistCandidate(mb_id='mb-1', name='Artist 00001', score=100)]), looked_up_at=1.0)
    with db.write() as connection:
        connection.execute('DELETE FROM tracks')            # a full reindex clears and rebuilds the tracks table
    LibraryRepository(db).save_many(synthetic(10_000))
    reopened = Database(tmp_path / 'durable.sqlite3')
    reopened.initialize()
    assert ArtistCorrections(reopened).state().is_separated('Artist 00001', 'Artist 00002')
    assert ArtistLinks(reopened).get('Artist 00001')['mb_artist_id'] == 'mb-1'


class _FakeFile:
    def __init__(self, size):
        self.size, self.pos = size, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        n = self.size - self.pos if n < 0 else min(n, self.size - self.pos)
        self.pos += n
        return b'x' * n

    def seek(self, position):
        self.pos = position


@pytest.mark.parametrize('total', [10_000, 50_000])
def test_file_instance_reconciliation_scales_and_a_repeat_scan_reads_nothing(tmp_path, total):
    from meloshelf.infrastructure.file_identity import FileRegistry
    db = build(tmp_path / f'fi{total}.sqlite3', total)
    opened = []

    def opener(path, mode='rb'):
        opened.append(path)
        return _FakeFile(1000)

    registry = FileRegistry(db, opener=opener, max_fingerprints_per_run=500)
    started = time.perf_counter()
    first = registry.reconcile()
    first_seconds = time.perf_counter() - started
    record(total, 'instances_first_reconcile_seconds', first_seconds)
    record(total, 'instances_first_reads', len(opened))
    assert first['new'] == total
    assert len(opened) <= 500, 'a first reconcile reads at most the per-run cap of files'
    opened.clear()
    started = time.perf_counter()
    again = registry.reconcile()
    record(total, 'instances_second_reconcile_seconds', time.perf_counter() - started)
    assert again['new'] == 0 and again['unchanged'] == total
    assert len(opened) <= 500, 'only the deferred fingerprints are read; unchanged files are not re-read'
    assert first_seconds < (60 if total == 50_000 else 15)


@pytest.mark.parametrize('total', [10_000, 50_000])
def test_artwork_lookups_for_a_page_stay_fast_at_scale(tmp_path, total):
    """Cover state for a page of albums, thumbnails for a page of songs, photo summaries for 300 artists, and the count of
    names still to look up. Synthetic data; nothing is fetched and no image is read."""
    import json as _json
    from types import SimpleNamespace
    from meloshelf.application import album_cover_api, enrichment_api
    from meloshelf.application.album_covers import AlbumCoverService
    from meloshelf.application.artist_photos import ArtistPhotoService
    from meloshelf.domain.artist_identity import LinkDecision
    from meloshelf.infrastructure.artwork import ArtworkCache
    from meloshelf.infrastructure.match_store import MatchStore

    db = build(tmp_path / f'art{total}.sqlite3', total)
    store = MatchStore(db)
    group = 'e4174758-d333-4a8e-a31f-dd0edd51518e'
    details = [{'id': f'00000000-0000-4000-8000-{n:012d}', 'title': 'x', 'group_id': group} for n in range(3)]
    with db.read() as connection:
        sample = connection.execute('SELECT path, album, artist FROM tracks LIMIT ?', (total // 4,)).fetchall()
    with db.write() as connection:        # approved recognition results for a quarter of the library, in one transaction
        for row in sample:
            connection.execute(
                """INSERT INTO match_results(path,file_size,file_mtime_ns,status,data,created_at,decision,recording_status,
                   selected_recording_id,selected_release_id) VALUES(?,?,?,?,?,?, 'pending', 'AUTO_APPROVED', '', '')""",
                (row['path'], 1, 1, 'matched', _json.dumps({'candidates': [{'recording_id': 'r', 'release_details': [
                    {**d, 'title': row['album']} for d in details]}]}), 1))
    covers = AlbumCoverService(db, ArtworkCache(db, tmp_path / 'art', fetch=lambda url: ('missing', '', b'')), tmp_path / 'c')
    photos = ArtistPhotoService(db, tmp_path / 'p', None)
    server = SimpleNamespace(covers=covers, photos=photos, database=db)

    with db.read() as connection:
        albums = [{'title': r['album'], 'artist': r['artist']} for r in connection.execute(
            'SELECT DISTINCT album, artist FROM tracks ORDER BY album LIMIT 60')]
        tracks = [dict(r) for r in connection.execute('SELECT path, album, artist FROM tracks ORDER BY path LIMIT 50')]
    started = time.perf_counter()
    album_cover_api.annotate(server, albums)
    record(total, 'cover_state_60_albums_seconds', time.perf_counter() - started)
    started = time.perf_counter()
    album_cover_api.annotate_tracks(server, tracks)
    record(total, 'thumbnails_50_songs_seconds', time.perf_counter() - started)

    ids = [f'00000000-0000-4000-9000-{n:012d}' for n in range(300)]
    for n, mb in enumerate(ids):
        ArtistLinks(db).record(f'Linked {n}', LinkDecision('linked', mb_id=mb, name=f'Linked {n}', reason='fixture'),
                               looked_up_at=1.0)
    started = time.perf_counter()
    photos.summaries(ids)
    record(total, 'photo_summaries_300_seconds', time.perf_counter() - started)

    started = time.perf_counter()
    left = enrichment_api.remaining(SimpleNamespace(database=db))
    record(total, 'remaining_names_seconds', time.perf_counter() - started)
    assert left > 0
    limits = {10_000: 2.0, 50_000: 6.0}[total]
    assert RESULTS[total]['cover_state_60_albums_seconds'] < limits
    assert RESULTS[total]['thumbnails_50_songs_seconds'] < limits
    assert RESULTS[total]['photo_summaries_300_seconds'] < 1.0
    assert RESULTS[total]['remaining_names_seconds'] < limits * 2
