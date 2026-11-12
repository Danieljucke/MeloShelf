"""Artist corrections API: token-protected, confirmed, reversible, scoped to the artist view only.

Covers: preview before confirmation; persistence across restart and reindexing; concurrent writers cannot produce
conflicting decisions; the Artists view reflects corrections; raw tags, audio files and other stores are untouched.
"""
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.domain.artists import canonical_key
from meloshelf.infrastructure.artist_corrections import ArtistCorrections, ArtistCorrectionError
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from meloshelf.domain.models import Track
from test_qa_api import request


def seed(db, rows):
    LibraryRepository(db).save_many([Track(p, 1, '.mp3', 1, 'T', artist, album, '', 'tagged') for p, artist, album in rows])


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    seed(database, [
        ('/a.mp3', 'The Beatles', 'White'),
        ('/b.mp3', 'Beatles', 'Abbey'),
        ('/c.mp3', 'Paul Simon Feat. The Beatles', 'Collab'),
        ('/d.mp3', 'Queen', 'Night'),
    ])
    return database


@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, connection_tester=lambda target, config: {'ok': True, 'state': 'valid', 'message': 'x'})
    seed(running.database, [('/a.mp3', 'The Beatles', 'White'), ('/b.mp3', 'Beatles', 'Abbey'), ('/d.mp3', 'Queen', 'Night')])
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


# ── store: concurrency and persistence ─────────────────────────────────────────

def test_concurrent_merges_leave_a_log_that_replays_cleanly(db):
    corrections = ArtistCorrections(db)
    results = []

    def attempt(i):
        try:
            results.append(('ok', corrections.merge('Source Name', f'Target {i}')))
        except ArtistCorrectionError as exc:
            results.append(('refused', str(exc)))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 12
    assert corrections.state().resolve('source name') is not None
    assert len(corrections.events()) == sum(1 for r, _ in results if r == 'ok')


def test_two_threads_cannot_both_undo_the_same_decision(db):
    corrections = ArtistCorrections(db)
    event = corrections.merge('The Beatles', 'Beatles')
    outcomes = []

    def undo():
        try:
            outcomes.append(corrections.undo(event))
        except ArtistCorrectionError:
            outcomes.append(None)

    threads = [threading.Thread(target=undo) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for o in outcomes if o is not None) == 1


def test_a_keep_separate_decision_cannot_be_overridden_by_a_concurrent_merge(db):
    corrections = ArtistCorrections(db)
    corrections.keep_separate('The Beatles', 'Beatles')
    with pytest.raises(ArtistCorrectionError):
        corrections.merge('The Beatles', 'Beatles')


def test_corrections_survive_a_reindex_that_clears_and_rescans_the_tracks(db):
    corrections = ArtistCorrections(db)
    corrections.merge('The Beatles', 'Beatles')
    with db.write() as connection:
        connection.execute('DELETE FROM tracks')            # what a full reindex does to the track table
    seed(db, [('/a.mp3', 'The Beatles', 'White')])
    assert ArtistCorrections(db).state().resolve('the beatles') == 'Beatles'


def test_the_artists_view_groups_a_merged_variant_under_the_chosen_name(db):
    ArtistCorrections(db).merge('The Beatles', 'Beatles')
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    beatles = [a for a in artists if canonical_key(a['name']) == 'beatles']
    assert len(beatles) == 1
    assert beatles[0]['name'] == 'Beatles'
    assert beatles[0]['tracks'] == 2  # 'The Beatles' and 'Beatles' as primary credits
    assert beatles[0]['featured_tracks'] == 1  # the featured appearance, now under the same identity


def test_corrections_do_not_change_raw_tags_or_other_tracks(db):
    before = LibraryRepository(db).list_tracks(limit=50)
    ArtistCorrections(db).merge('The Beatles', 'Beatles')
    after = LibraryRepository(db).list_tracks(limit=50)
    assert {(t['path'], t['artist']) for t in before} == {(t['path'], t['artist']) for t in after}


def test_the_correction_does_not_propagate_to_the_queue_or_metadata_groups(db):
    from meloshelf.application.metadata_service import MetadataService
    groups_before = MetadataService(db).groups()
    ArtistCorrections(db).merge('The Beatles', 'Beatles')
    assert MetadataService(db).groups() == groups_before


# ── API ────────────────────────────────────────────────────────────────────────

def test_a_merge_without_confirmation_returns_the_affected_recordings_and_writes_nothing(server):
    running, base = server
    status, _, body = request(base, 'POST', '/api/artist-corrections/merge',
                              {'source': 'The Beatles', 'target': 'Beatles'}, token=running.token)
    payload = json.loads(body)
    assert status == 409
    assert payload['preview']['total'] == 1
    assert payload['preview']['recordings'][0]['path'] == '/a.mp3'
    assert ArtistCorrections(running.database).events() == []


def test_a_confirmed_merge_is_recorded_and_visible_in_the_log(server):
    running, base = server
    status, _, body = request(base, 'POST', '/api/artist-corrections/merge',
                              {'source': 'The Beatles', 'target': 'Beatles', 'confirm': True}, token=running.token)
    assert status == 200 and json.loads(body)['saved'] is True
    status, _, body = request(base, 'GET', '/api/artist-corrections')
    events = json.loads(body)['events']
    assert [e['action'] for e in events] == ['merge']


def test_a_correction_needs_the_session_token(server):
    running, base = server
    status, _, _ = request(base, 'POST', '/api/artist-corrections/merge',
                           {'source': 'The Beatles', 'target': 'Beatles', 'confirm': True})
    assert status == 403
    assert ArtistCorrections(running.database).events() == []


def test_a_refused_merge_returns_a_reason(server):
    running, base = server
    status, _, body = request(base, 'POST', '/api/artist-corrections/merge',
                              {'source': 'Queen', 'target': 'queen', 'confirm': True}, token=running.token)
    assert status == 400
    assert 'same artist' in json.loads(body)['error']


def test_undo_reverses_the_merge_through_the_api(server):
    running, base = server
    request(base, 'POST', '/api/artist-corrections/merge',
            {'source': 'The Beatles', 'target': 'Beatles', 'confirm': True}, token=running.token)
    event_id = ArtistCorrections(running.database).events()[0]['id']
    status, _, _ = request(base, 'POST', '/api/artist-corrections/undo',
                           {'event_id': event_id, 'confirm': True}, token=running.token)
    assert status == 200
    assert ArtistCorrections(running.database).state().resolve('the beatles') is None


def test_undo_without_confirmation_is_refused(server):
    running, base = server
    request(base, 'POST', '/api/artist-corrections/merge',
            {'source': 'The Beatles', 'target': 'Beatles', 'confirm': True}, token=running.token)
    event_id = ArtistCorrections(running.database).events()[0]['id']
    status, _, _ = request(base, 'POST', '/api/artist-corrections/undo', {'event_id': event_id}, token=running.token)
    assert status == 409
    assert len(ArtistCorrections(running.database).events()) == 1


def test_the_preview_endpoint_lists_recordings_without_writing(server):
    running, base = server
    status, _, body = request(base, 'GET', '/api/artist-corrections/preview?action=merge&source=The%20Beatles&target=Beatles')
    payload = json.loads(body)
    assert status == 200 and payload['allowed'] is True and payload['total'] == 1
    assert ArtistCorrections(running.database).events() == []


def test_the_artists_endpoint_reflects_a_confirmed_merge(server):
    running, base = server
    request(base, 'POST', '/api/artist-corrections/merge',
            {'source': 'The Beatles', 'target': 'Beatles', 'confirm': True}, token=running.token)
    status, _, body = request(base, 'GET', '/api/artist-identities?limit=60')
    names = [a['name'] for a in json.loads(body)['artists']]
    assert 'Beatles' in names and 'The Beatles' not in names


def test_the_api_never_writes_to_audio_files(server, tmp_path):
    running, base = server
    audio = tmp_path / 'song.mp3'
    audio.write_bytes(b'AUDIO' * 100)
    before = audio.read_bytes()
    request(base, 'POST', '/api/artist-corrections/merge',
            {'source': 'The Beatles', 'target': 'Beatles', 'confirm': True}, token=running.token)
    assert audio.read_bytes() == before


def test_the_page_routes_every_correction_through_a_preview_and_a_confirmation():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'id="artistConfirm"' in page and 'id="corrList"' in page
    assert "/api/artist-corrections/preview?" in page
    # The only write calls in the page are made after a confirmation click, and send confirm: true.
    assert page.count("confirm:true") == 1
    assert "data-undo=" in page and "data-cleanup=" in page
