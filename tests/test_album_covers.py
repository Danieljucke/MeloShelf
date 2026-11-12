"""Album covers from verified releases.

A cover is shown as verified only when the approved recordings of an album agree on exactly ONE release whose title
matches the album title. Several equally good editions are 'ambiguous' and get no cover; no approved recording gives
'unverified'. Editions are kept apart by release id (a deluxe edition has its own title and id). A person can choose
an image, clear it, or undo; that log is append-only with provenance. Fixtures are fakes: no service is contacted.
"""
import base64
import json
import threading

import pytest

from meloshelf.application.album_covers import AlbumCoverService
from meloshelf.application.webapp import create_server
from meloshelf.domain.artwork import verified_release
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artwork import ArtworkCache
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

R1 = '11111111-1111-4111-8111-111111111111'
R2 = '22222222-2222-4222-8222-222222222222'
R3 = '33333333-3333-4333-8333-333333333333'
JPEG = b'\xff\xd8\xff\xe0' + b'C' * 500
PNG = b'\x89PNG\r\n\x1a\n' + b'P' * 500


def rel(release_id, title):
    return {'id': release_id, 'title': title, 'date': '', 'country': '', 'primary_type': 'Album', 'secondary_types': []}


# ── pure rule ──────────────────────────────────────────────────────────────────

def test_one_shared_matching_release_is_verified():
    result = verified_release('Purple Rain', [[rel(R1, 'Purple Rain')], [rel(R1, 'Purple Rain'), rel(R2, 'Other')]])
    assert result['state'] == 'verified' and result['release_id'] == R1


def test_two_editions_with_the_same_title_in_every_recording_are_ambiguous():
    both = [rel(R1, 'Purple Rain'), rel(R2, 'Purple Rain')]
    result = verified_release('Purple Rain', [both, both])
    assert result['state'] == 'ambiguous' and set(result['release_ids']) == {R1, R2} and result['release_id'] is None


def test_an_edition_with_its_own_title_is_told_apart_from_the_standard_one():
    both = [rel(R1, 'Purple Rain'), rel(R2, 'Purple Rain (Deluxe Edition)')]
    result = verified_release('Purple Rain (Deluxe Edition)', [both, both])
    assert result['state'] == 'verified' and result['release_id'] == R2


def test_a_recording_that_does_not_list_the_album_removes_the_agreement():
    result = verified_release('Purple Rain', [[rel(R1, 'Purple Rain')], [rel(R2, 'Something Else')]])
    assert result['state'] == 'unverified'


def test_no_approved_recording_means_unverified():
    assert verified_release('Purple Rain', [])['state'] == 'unverified'


def test_titles_compare_without_case_or_punctuation():
    assert verified_release('purple rain!', [[rel(R1, 'Purple Rain')]])['state'] == 'verified'


def test_a_release_id_that_is_not_a_uuid_is_ignored():
    assert verified_release('Purple Rain', [[rel('not-an-id', 'Purple Rain')]])['state'] == 'unverified'


# ── service ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    return database, tmp_path


def add(db, path, artist, album, releases, status='AUTO_APPROVED', recording='rec-1', selected=''):
    LibraryRepository(db).save_many([Track(path, 1, '.mp3', 1, 'T', artist, album, '', 'tagged')])
    store = MatchStore(db)
    store.put_result(path, 1, 1, 'matched', {'candidates': [
        {'recording_id': recording, 'title': 'T', 'release_details': releases},
        {'recording_id': 'rec-other', 'title': 'T', 'release_details': [rel(R3, 'Unrelated')]}]},
        recording_status=status)
    if selected:
        with db.write() as connection:
            connection.execute("UPDATE match_results SET decision='accepted', selected_recording_id=? WHERE path=?",
                               (selected, path))


def service(env, fetch=None, **kw):
    db, tmp = env
    calls = []

    def fake(url):
        calls.append(url)
        return ('image', 'image/jpeg', JPEG)
    cache = ArtworkCache(db, tmp / 'art', fetch=fetch or fake)
    svc = AlbumCoverService(db, cache, tmp / 'covers', **kw)
    svc.calls = calls
    return svc


def test_an_album_of_approved_recordings_sharing_one_release_is_verified(env):
    db, _ = env
    add(db, '/1.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain')])
    add(db, '/2.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain'), rel(R2, 'Other')])
    state = service(env).resolve('Purple Rain', 'Prince')
    assert state['state'] == 'verified' and state['release_id'] == R1


def test_recordings_that_are_not_approved_do_not_count(env):
    db, _ = env
    add(db, '/1.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain')], status='NEEDS_REVIEW')
    assert service(env).resolve('Purple Rain', 'Prince')['state'] == 'unverified'


def test_a_person_approved_recording_uses_the_candidate_the_person_selected(env):
    db, _ = env
    add(db, '/1.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain')], status='USER_APPROVED', selected='rec-1')
    assert service(env).resolve('Purple Rain', 'Prince')['release_id'] == R1


def test_the_same_album_title_by_two_artists_is_kept_apart(env):
    db, _ = env
    add(db, '/1.mp3', 'Artist A', 'Greatest Hits', [rel(R1, 'Greatest Hits')])
    add(db, '/2.mp3', 'Artist B', 'Greatest Hits', [rel(R2, 'Greatest Hits')])
    svc = service(env)
    assert svc.resolve('Greatest Hits', 'Artist A')['release_id'] == R1
    assert svc.resolve('Greatest Hits', 'Artist B')['release_id'] == R2


def test_a_verified_cover_is_fetched_once_and_served_from_the_cache(env):
    db, _ = env
    add(db, '/1.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain')])
    svc = service(env)
    assert svc.cover('Purple Rain', 'Prince') == ('image/jpeg', JPEG)
    svc.cover('Purple Rain', 'Prince')
    assert len(svc.calls) == 1
    info = svc.info('Purple Rain', 'Prince')
    assert info['provenance'] == 'coverartarchive' and R1 in info['source_url']


def test_an_ambiguous_album_gets_no_cover_and_no_request(env):
    db, _ = env
    both = [rel(R1, 'Purple Rain'), rel(R2, 'Purple Rain')]
    add(db, '/1.mp3', 'Prince', 'Purple Rain', both)
    svc = service(env)
    assert svc.cover('Purple Rain', 'Prince') is None and svc.calls == []
    assert svc.resolve('Purple Rain', 'Prince')['state'] == 'ambiguous'


def test_a_person_can_set_an_album_image_that_wins_and_undo_restores_the_verified_cover(env):
    db, _ = env
    add(db, '/1.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain')])
    svc = service(env)
    event = svc.set_manual('Purple Rain', 'Prince', 'image/png', PNG, note='scan of my copy')
    assert svc.cover('Purple Rain', 'Prince') == ('image/png', PNG)
    assert svc.info('Purple Rain', 'Prince')['provenance'] == 'user'
    svc.undo(event)
    assert svc.cover('Purple Rain', 'Prince') == ('image/jpeg', JPEG)


def test_a_manual_image_gives_an_ambiguous_album_a_cover_without_calling_it_verified(env):
    db, _ = env
    both = [rel(R1, 'Purple Rain'), rel(R2, 'Purple Rain')]
    add(db, '/1.mp3', 'Prince', 'Purple Rain', both)
    svc = service(env)
    svc.set_manual('Purple Rain', 'Prince', 'image/png', PNG)
    assert svc.cover('Purple Rain', 'Prince') == ('image/png', PNG)
    assert svc.resolve('Purple Rain', 'Prince')['state'] == 'ambiguous'


def test_a_person_can_clear_the_cover(env):
    db, _ = env
    add(db, '/1.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain')])
    svc = service(env)
    svc.clear_manual('Purple Rain', 'Prince')
    assert svc.cover('Purple Rain', 'Prince') is None


def test_a_manual_image_must_be_a_real_small_image(env):
    from meloshelf.application.artist_photos import PhotoError
    svc = service(env, max_image_bytes=1000)
    with pytest.raises(PhotoError):
        svc.set_manual('A', 'B', 'image/png', b'nope')
    with pytest.raises(PhotoError):
        svc.set_manual('A', 'B', 'image/png', PNG + b'x' * 2000)


def test_states_for_a_whole_page_use_a_fixed_number_of_queries(env):
    from contextlib import contextmanager
    db, _ = env
    for n in range(60):
        add(db, f'/{n}.mp3', 'Artist', f'Album {n}', [rel(R1, f'Album {n}')])
    svc = service(env)
    statements = []
    real_read = db.read

    @contextmanager
    def counting():
        with real_read() as connection:
            connection.set_trace_callback(statements.append)
            yield connection
    db.read = counting
    try:
        states = svc.states([(f'Album {n}', 'Artist') for n in range(60)])
    finally:
        db.read = real_read
    assert len(states) == 60 and all(s['state'] == 'verified' for s in states.values())
    assert len(statements) <= 5


def test_the_decision_log_is_append_only_with_provenance(env):
    svc = service(env)
    event = svc.set_manual('A', 'B', 'image/png', PNG)
    svc.undo(event)
    log = svc.decisions()
    assert [d['action'] for d in log] == ['set', 'undo'] and log[0]['source'] == 'user' and log[0]['kind'] == 'album'


# ── HTTP ────────────────────────────────────────────────────────────────────────

def _server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'http.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, artwork_fetch=lambda url: ('image', 'image/jpeg', JPEG))
    add(running.database, '/1.mp3', 'Prince', 'Purple Rain', [rel(R1, 'Purple Rain')])
    add(running.database, '/2.mp3', 'Prince', 'Lovesexy', [rel(R1, 'Lovesexy'), rel(R2, 'Lovesexy')])
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return running, f'http://127.0.0.1:{running.server_address[1]}'


def test_the_cover_endpoint_serves_verified_covers_and_refuses_the_rest(tmp_path):
    running, base = _server(tmp_path)
    try:
        status, headers, data = request(base, 'GET', '/api/album-cover?album=Purple%20Rain&artist=Prince')
        assert status == 200 and data == JPEG and headers['Content-Type'] == 'image/jpeg'
        assert request(base, 'GET', '/api/album-cover?album=Lovesexy&artist=Prince')[0] == 404
        info = json.loads(request(base, 'GET', '/api/album-cover/info?album=Lovesexy&artist=Prince')[2])
        assert info['state'] == 'ambiguous'
        assert request(base, 'GET', '/api/album-cover?artist=Prince')[0] == 400
    finally:
        running.shutdown()
        running.server_close()


def test_the_albums_endpoint_reports_each_albums_cover_state(tmp_path):
    running, base = _server(tmp_path)
    try:
        albums = json.loads(request(base, 'GET', '/api/albums?limit=10')[2])['albums']
        by_title = {a['title']: a for a in albums}
        assert by_title['Purple Rain']['cover']['state'] == 'verified' and by_title['Purple Rain']['cover']['url']
        assert by_title['Lovesexy']['cover']['state'] == 'ambiguous' and not by_title['Lovesexy']['cover'].get('url')
    finally:
        running.shutdown()
        running.server_close()


def test_manual_cover_and_undo_over_http_need_the_token(tmp_path):
    running, base = _server(tmp_path)
    try:
        body = {'album': 'Lovesexy', 'artist': 'Prince', 'content_type': 'image/png',
                'data': base64.b64encode(PNG).decode()}
        assert request(base, 'POST', '/api/album-covers/manual', body)[0] == 403
        status, _, raw = request(base, 'POST', '/api/album-covers/manual', body, token=running.token)
        event = json.loads(raw)['event_id']
        assert status == 200 and request(base, 'GET', '/api/album-cover?album=Lovesexy&artist=Prince')[2] == PNG
        assert request(base, 'POST', '/api/album-covers/undo', {'event_id': event}, token=running.token)[0] == 200
        assert request(base, 'GET', '/api/album-cover?album=Lovesexy&artist=Prince')[0] == 404
    finally:
        running.shutdown()
        running.server_close()


def test_the_page_shows_verified_covers_lazily_and_offers_a_previewed_manual_change():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    for hook in ('data-cover-file', 'data-cover="undo"', 'data-cover="clear"', 'id="coverConfirm"',
                 "'/api/album-covers/manual'", "'/api/album-covers/undo'", 'Several editions match: no cover is assumed',
                 'Cover from the verified release'):
        assert hook in page, hook
    assert 'class="cv cover"' in page and 'loading="lazy"' in page
    assert "tile.classList.remove('cover')" in page, 'a cover that fails to load falls back to the neutral tile'


# ── release groups: the album is verified even when its edition is not ──────────

G1 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
G2 = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


def grel(release_id, title, group):
    return {**rel(release_id, title), 'group_id': group}


def test_several_editions_of_one_release_group_verify_the_album_but_not_the_edition():
    both = [grel(R1, '21', G1), grel(R2, '21', G1)]
    result = verified_release('21', [both, both])
    assert result['state'] == 'verified_group' and result['group_id'] == G1
    assert result['release_id'] is None and set(result['release_ids']) == {R1, R2}


def test_editions_in_different_release_groups_stay_ambiguous():
    both = [grel(R1, '21', G1), grel(R2, '21', G2)]
    assert verified_release('21', [both, both])['state'] == 'ambiguous'


def test_editions_without_group_information_stay_ambiguous():
    both = [rel(R1, '21'), rel(R2, '21')]
    assert verified_release('21', [both, both])['state'] == 'ambiguous'


def test_a_single_release_is_still_edition_verified_even_when_it_has_a_group():
    result = verified_release('21', [[grel(R1, '21', G1)]])
    assert result['state'] == 'verified' and result['release_id'] == R1


def test_the_archive_cache_fetches_a_release_group_cover_from_its_own_url_and_key(env):
    db, tmp = env
    urls = []
    cache = ArtworkCache(db, tmp / 'art', fetch=lambda url: urls.append(url) or ('image', 'image/jpeg', JPEG))
    assert cache.cover(G1, group=True) == ('image/jpeg', JPEG)
    assert cache.cover(G1) == ('image/jpeg', JPEG)       # the same id as a release is a different cover
    assert len(urls) == 2 and 'release-group/' + G1 in urls[0] and 'release/' + G1 in urls[1]


def test_the_service_gives_a_group_verified_album_its_cover_and_says_the_edition_is_unknown(env):
    db, _ = env
    both = [grel(R1, '21', G1), grel(R2, '21', G1)]
    add(db, '/1.mp3', 'Adele', '21', both)
    add(db, '/2.mp3', 'Adele', '21', both)
    svc = service(env)
    assert svc.cover('21', 'Adele') == ('image/jpeg', JPEG)
    assert 'release-group/' + G1 in svc.calls[0]
    info = svc.info('21', 'Adele')
    assert info['state'] == 'verified_group' and info['edition'] == 'unknown'
    assert info['source_url'].endswith('/release-group/' + G1)
    summary = svc.summaries([('21', 'Adele')])[('21', 'Adele')]
    assert summary['has_cover'] is True and summary['edition'] == 'unknown'


def test_recognition_keeps_the_release_group_id_in_the_stored_release_details():
    from meloshelf.application.recognition import _release_details
    payload = {'releases': [{'id': R1, 'title': '21', 'release-group': {'id': G1, 'primary-type': 'Album'}}]}
    assert _release_details(payload)[0]['group_id'] == G1
