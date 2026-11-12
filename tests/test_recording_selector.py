"""Recording selector: a complete, paginated, searchable list of every recording a name appears on.

* A page never exceeds the requested size; pages do not overlap; the total is always reported.
* Search narrows the list and reports the filtered total, so a person always knows how many records are hidden.
* 'All matching' is an explicit server call that returns every matching path (up to a cap). The page never
  selects hidden records by itself.
* Homonymous artists on one compilation appear as separate recordings: the list shows what the tags say and
  never decides which artist a recording belongs to.
"""
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.domain.models import Track
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request


def big_library(db):
    rows = []
    for n in range(300):
        rows.append(Track(f'D:/Library/Compilation/{n:03d}.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Compilation', '', 'tagged'))
    for n in range(100):
        rows.append(Track(f'D:/Library/Solo/{n:03d}.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged'))
    rows.append(Track('D:/Library/Feat/001.mp3', 1, '.mp3', 1, 'T', 'Tauren Wells Feat. Prince', 'Duet', '', 'tagged'))
    LibraryRepository(db).save_many(rows)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    big_library(database)
    return database


def test_a_page_has_the_requested_size_and_the_full_total(db):
    repo = LibraryRepository(db)
    rows, total = repo.credit_recordings('Prince', offset=0, limit=50)
    assert len(rows) == 50 and total == 401


def test_pages_do_not_overlap_and_cover_every_recording(db):
    repo = LibraryRepository(db)
    seen = []
    for offset in range(0, 401, 50):
        rows, _ = repo.credit_recordings('Prince', offset=offset, limit=50)
        seen.extend(r['path'] for r in rows)
    assert len(seen) == 401 and len(set(seen)) == 401


def test_the_last_page_is_partial_and_nothing_beyond_the_total_is_returned(db):
    rows, total = LibraryRepository(db).credit_recordings('Prince', offset=400, limit=50)
    assert total == 401 and len(rows) == 1
    rows, _ = LibraryRepository(db).credit_recordings('Prince', offset=500, limit=50)
    assert rows == []


def test_search_narrows_the_list_and_reports_the_filtered_total(db):
    rows, total = LibraryRepository(db).credit_recordings('Prince', query='Purple', offset=0, limit=500)
    assert total == 100 and all(r['album'] == 'Purple Rain' for r in rows)


def test_search_is_case_insensitive_and_matches_the_file_path(db):
    # Files 000 to 099 sit under 'compilation/0': a path search matches exactly those one hundred.
    _, total = LibraryRepository(db).credit_recordings('Prince', query='compilation/0', offset=0, limit=50)
    assert total == 100


def test_featured_appearances_are_included_and_keep_their_credit_text(db):
    rows, total = LibraryRepository(db).credit_recordings('Prince', query='Duet', offset=0, limit=50)
    assert total == 1 and rows[0]['artist'] == 'Tauren Wells Feat. Prince'


def test_an_unknown_name_has_no_recordings(db):
    assert LibraryRepository(db).credit_recordings('Nobody Here', offset=0, limit=50) == ([], 0)


def test_every_matching_path_is_available_for_an_explicit_select_all(db):
    paths, total = LibraryRepository(db).credit_recording_paths('Prince', query='Compilation', cap=5000)
    assert total == 300 and len(paths) == 300


def test_the_select_all_list_is_capped_and_says_so(db):
    paths, total = LibraryRepository(db).credit_recording_paths('Prince', query='', cap=50)
    assert total == 401 and len(paths) == 50


def test_a_decision_can_cover_three_hundred_recordings_explicitly(db):
    from meloshelf.domain.artist_identity import ArtistCandidate, decide_link
    from meloshelf.infrastructure.artist_identity_store import ArtistLinks
    from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions
    ArtistLinks(db).record('Prince', decide_link('Prince', [ArtistCandidate('mb-a', 'Prince', 100),
                                                           ArtistCandidate('mb-b', 'Prince', 100)]), looked_up_at=1.0)
    paths, _ = LibraryRepository(db).credit_recording_paths('Prince', query='Compilation', cap=5000)
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-b', tracks=paths)
    with db.read() as connection:
        mine = connection.execute("SELECT COUNT(*) FROM tracks WHERE artist='Prince' AND album='Compilation'").fetchone()[0]
    artists, _ = LibraryRepository(db).artist_identities(limit=300)
    assert sum(a['tracks'] for a in artists if a['mb_artist_id'] == 'mb-b') == mine == 300


def test_the_http_endpoints_page_search_and_cap(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'http.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={})
    big_library(running.database)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{running.server_address[1]}'
    try:
        status, _, body = request(base, 'GET', '/api/artist-recordings?credit=Prince&offset=0&limit=50')
        payload = json.loads(body)
        assert status == 200 and payload['total'] == 401 and len(payload['recordings']) == 50
        status, _, body = request(base, 'GET', '/api/artist-recordings?credit=Prince&q=Purple&limit=500')
        assert status == 400, 'a page larger than 200 is refused'
        status, _, body = request(base, 'GET', '/api/artist-recordings?credit=Prince&q=Purple&limit=200')
        assert json.loads(body)['total'] == 100
        status, _, body = request(base, 'GET', '/api/artist-recordings/paths?credit=Prince&q=Compilation')
        assert status == 200 and json.loads(body)['total'] == 300 and len(json.loads(body)['paths']) == 300
    finally:
        running.shutdown()
        running.server_close()
