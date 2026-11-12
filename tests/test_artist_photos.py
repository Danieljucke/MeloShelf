"""Artist photographs: licensed, attributed, verified-identity only, cached within a bound, correctable by a person.

* A photograph is fetched only for an artist ID that is verified (a strong provider link or a person's accept).
  Suggested, ambiguous or unlinked identities get no photograph; initials are shown instead.
* Only openly licensed images are stored (CC0, public domain, CC BY, CC BY-SA). Anything else is refused and the
  refusal is remembered. Author, licence and source page are stored with every image.
* A missing photograph is remembered and not requested again; a provider failure is not remembered.
* The cache is bounded; the oldest image is evicted first.
* A person can set, clear or restore an image for an identity; the choice is an append-only log with provenance
  'user', undo restores the previous state, and nothing is ever written into a music file.
Deterministic: providers and bytes are fakes. Nothing here contacts a live service.
"""
import json
import threading

import pytest

from meloshelf.application.artist_photos import ArtistPhotoService, PhotoError
from meloshelf.application.webapp import create_server
from meloshelf.domain.artist_identity import ArtistCandidate, LinkDecision, decide_link
from meloshelf.domain.artwork import license_acceptable
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.music_api import APIError
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

JPEG = b'\xff\xd8\xff\xe0' + b'J' * 600
PNG = b'\x89PNG\r\n\x1a\n' + b'P' * 600
MB = 'cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493'
OTHER = '0383dadf-2a4e-4d10-a46a-e9e041da8eb3'


class FakeProvider:
    def __init__(self, found=None, bytes_=JPEG, content_type='image/jpeg', fail=False):
        self.found = found if found is not None else {
            'status': 'found', 'file': 'Adele_2016.jpg', 'license': 'CC BY-SA 4.0', 'author': 'A. Photographer',
            'source_url': 'https://commons.wikimedia.org/wiki/File:Adele_2016.jpg',
            'thumb_url': 'https://upload.example/thumb.jpg'}
        self.bytes, self.content_type, self.fail = bytes_, content_type, fail
        self.find_calls, self.download_calls = [], []

    def find(self, mb_id):
        self.find_calls.append(mb_id)
        if self.fail:
            raise APIError('the service is unreachable', retryable=True)
        return self.found

    def download(self, url):
        self.download_calls.append(url)
        return self.content_type, self.bytes


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    LibraryRepository(database).save_many([Track('/a.mp3', 1, '.mp3', 1, 'T', 'Adele', 'A', '', 'tagged')])
    ArtistLinks(database).record('Adele', decide_link('Adele', [ArtistCandidate(MB, 'Adele', 100)]), looked_up_at=1.0)
    return database


def service(db, tmp_path, provider, **kw):
    return ArtistPhotoService(db, tmp_path / 'photos', provider, **kw)


# ── the licence rule ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name,ok', [
    ('CC0 1.0', True), ('Public domain', True), ('PD-self', True), ('CC BY 4.0', True), ('CC BY-SA 3.0', True),
    ('CC BY-NC 2.0', False), ('CC BY-ND 4.0', False), ('CC BY-NC-SA 4.0', False), ('Fair use', False),
    ('Copyrighted free use', False), ('', False), ('GFDL', False), ('All rights reserved', False)])
def test_only_openly_reusable_licences_are_accepted(name, ok):
    assert license_acceptable(name) is ok


# ── the verified-identity gate ──────────────────────────────────────────────────

def test_a_verified_artist_gets_a_cached_photo_with_attribution(db, tmp_path):
    provider = FakeProvider()
    svc = service(db, tmp_path, provider)
    result = svc.ensure(MB)
    assert result['status'] == 'available'
    info = svc.info(MB)
    assert info['license'] == 'CC BY-SA 4.0' and info['author'] == 'A. Photographer'
    assert info['source_url'].startswith('https://commons.wikimedia.org/')
    assert info['provenance'] == 'wikimedia'
    content_type, data = svc.image(MB)
    assert content_type == 'image/jpeg' and data == JPEG


def test_a_photo_is_fetched_once_and_then_served_from_the_cache(db, tmp_path):
    provider = FakeProvider()
    svc = service(db, tmp_path, provider)
    svc.ensure(MB)
    svc.ensure(MB)
    assert provider.find_calls == [MB] and len(provider.download_calls) == 1


def test_an_unverified_artist_gets_no_photo_and_no_request_is_made(db, tmp_path):
    provider = FakeProvider()
    svc = service(db, tmp_path, provider)
    assert svc.ensure(OTHER)['status'] == 'unverified'
    assert provider.find_calls == []


def test_a_suggestion_is_not_verified_so_it_gets_no_photo(db, tmp_path):
    ArtistLinks(db).record('Tauren W.', decide_link('Tauren W.', [
        ArtistCandidate(OTHER, 'Tauren Wells', 100, aliases=('Tauren W.',))]), looked_up_at=1.0)
    provider = FakeProvider()
    assert service(db, tmp_path, provider).ensure(OTHER)['status'] == 'unverified'
    assert provider.find_calls == []


def test_a_persons_accept_verifies_the_artist_for_a_photo(db, tmp_path):
    LibraryRepository(db).save_many([Track('/p1.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged')])
    ArtistLinks(db).record('Prince', decide_link('Prince', [ArtistCandidate(OTHER, 'Prince', 100),
                                                           ArtistCandidate('mb-b', 'Prince', 100)]), looked_up_at=1.0)
    provider = FakeProvider()
    svc = service(db, tmp_path, provider)
    assert svc.ensure(OTHER)['status'] == 'unverified'
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id=OTHER, albums=['Purple Rain'])
    assert svc.ensure(OTHER)['status'] == 'available'


def test_an_invalid_artist_id_is_refused(db, tmp_path):
    with pytest.raises(PhotoError):
        service(db, tmp_path, FakeProvider()).ensure('not-a-uuid')


# ── licence, missing images, failures, bad bytes ────────────────────────────────

def test_a_restrictive_licence_is_refused_stored_nothing_and_remembered(db, tmp_path):
    found = {'status': 'found', 'file': 'x.jpg', 'license': 'CC BY-NC 2.0', 'author': 'Z', 'source_url': 'https://c/x',
             'thumb_url': 'https://u/x'}
    provider = FakeProvider(found=found)
    svc = service(db, tmp_path, provider)
    assert svc.ensure(MB)['status'] == 'rejected_license'
    assert provider.download_calls == [] and svc.image(MB) is None
    svc.ensure(MB)
    assert provider.find_calls == [MB], 'the refusal is remembered'


def test_no_photograph_is_remembered_and_not_requested_again(db, tmp_path):
    provider = FakeProvider(found={'status': 'none', 'reason': 'no Wikidata image'})
    svc = service(db, tmp_path, provider)
    assert svc.ensure(MB)['status'] == 'none'
    svc.ensure(MB)
    assert provider.find_calls == [MB]


def test_a_provider_failure_is_not_remembered_so_a_later_try_can_succeed(db, tmp_path):
    provider = FakeProvider(fail=True)
    svc = service(db, tmp_path, provider)
    assert svc.ensure(MB)['status'] == 'failed'
    provider.fail = False
    assert svc.ensure(MB)['status'] == 'available'


def test_bytes_that_are_not_an_image_are_refused(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider(bytes_=b'<html>not an image</html>', content_type='image/jpeg'))
    assert svc.ensure(MB)['status'] == 'rejected_image'
    assert svc.image(MB) is None


def test_an_oversized_image_is_refused(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider(bytes_=JPEG + b'x' * 5000), max_image_bytes=1000)
    assert svc.ensure(MB)['status'] == 'rejected_image'


def test_the_cache_stays_within_its_bound_by_evicting_the_oldest(db, tmp_path):
    ids = [f'00000000-0000-4000-8000-00000000000{n}' for n in range(4)]
    for n, mb in enumerate(ids):
        ArtistLinks(db).record(f'Artist {n}', LinkDecision('linked', mb_id=mb, name=f'Artist {n}', reason='fixture'),
                               looked_up_at=1.0)
    clock = iter(range(1, 100))
    svc = service(db, tmp_path, FakeProvider(), max_total_bytes=2 * len(JPEG) + 10, clock=lambda: next(clock))
    for mb in ids:
        svc.ensure(mb)
    assert svc.total_bytes() <= 2 * len(JPEG) + 10
    assert svc.image(ids[0]) is None and svc.image(ids[-1]) is not None


def test_a_cached_photo_survives_a_restart(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider())
    svc.ensure(MB)
    reopened = ArtistPhotoService(db, tmp_path / 'photos', FakeProvider())
    assert reopened.image(MB)[1] == JPEG


# ── manual correction ───────────────────────────────────────────────────────────

def test_a_person_can_set_an_image_and_it_wins_over_the_provider_photo(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider())
    svc.ensure(MB)
    svc.set_manual(MB, 'image/png', PNG, note='my own photo')
    assert svc.image(MB) == ('image/png', PNG)
    assert svc.info(MB)['provenance'] == 'user'


def test_undo_restores_the_previous_photo(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider())
    svc.ensure(MB)
    event = svc.set_manual(MB, 'image/png', PNG)
    svc.undo(event)
    assert svc.image(MB) == ('image/jpeg', JPEG) and svc.info(MB)['provenance'] == 'wikimedia'


def test_a_person_can_clear_the_photo_so_initials_are_shown(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider())
    svc.ensure(MB)
    svc.clear_manual(MB)
    assert svc.image(MB) is None


def test_the_correction_log_is_append_only_and_records_provenance(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider())
    event = svc.set_manual(MB, 'image/png', PNG, note='n')
    svc.undo(event)
    log = svc.decisions()
    assert [d['action'] for d in log] == ['set', 'undo'] and log[0]['source'] == 'user'


def test_a_manual_image_must_be_a_real_small_image(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider(), max_image_bytes=1000)
    with pytest.raises(PhotoError):
        svc.set_manual(MB, 'image/png', b'not an image')
    with pytest.raises(PhotoError):
        svc.set_manual(MB, 'image/png', PNG + b'x' * 2000)
    with pytest.raises(PhotoError):
        svc.set_manual(MB, 'image/gif', PNG)


def test_an_undo_cannot_be_applied_twice_or_to_an_unknown_event(db, tmp_path):
    svc = service(db, tmp_path, FakeProvider())
    event = svc.set_manual(MB, 'image/png', PNG)
    svc.undo(event)
    with pytest.raises(PhotoError):
        svc.undo(event)
    with pytest.raises(PhotoError):
        svc.undo(999)


# ── HTTP ────────────────────────────────────────────────────────────────────────

def _server(tmp_path, provider):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'http.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, photo_provider=provider)
    LibraryRepository(running.database).save_many([Track('/a.mp3', 1, '.mp3', 1, 'T', 'Adele', 'A', '', 'tagged')])
    ArtistLinks(running.database).record('Adele', decide_link('Adele', [ArtistCandidate(MB, 'Adele', 100)]),
                                         looked_up_at=1.0)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return running, f'http://127.0.0.1:{running.server_address[1]}'


def test_the_photo_endpoint_serves_cached_bytes_and_refuses_what_is_not_there(tmp_path):
    running, base = _server(tmp_path, FakeProvider())
    try:
        assert request(base, 'GET', f'/api/artist-image?mb={MB}')[0] == 404
        assert request(base, 'GET', '/api/artist-image?mb=nope')[0] == 400
        assert request(base, 'POST', '/api/artist-images/fetch', {'mb_artist_id': MB})[0] == 403
        status, _, body = request(base, 'POST', '/api/artist-images/fetch', {'mb_artist_id': MB}, token=running.token)
        assert status == 200 and json.loads(body)['status'] == 'available'
        status, headers, data = request(base, 'GET', f'/api/artist-image?mb={MB}')
        assert status == 200 and data == JPEG and headers['Content-Type'] == 'image/jpeg'
        info = json.loads(request(base, 'GET', f'/api/artist-image/info?mb={MB}')[2])
        assert info['license'] == 'CC BY-SA 4.0' and info['author'] == 'A. Photographer'
    finally:
        running.shutdown()
        running.server_close()


def test_fetching_for_an_unverified_artist_is_refused(tmp_path):
    running, base = _server(tmp_path, FakeProvider())
    try:
        status, _, body = request(base, 'POST', '/api/artist-images/fetch', {'mb_artist_id': OTHER}, token=running.token)
        assert status == 409 and json.loads(body)['status'] == 'unverified'
    finally:
        running.shutdown()
        running.server_close()


def test_manual_correction_and_undo_over_http(tmp_path):
    import base64
    running, base = _server(tmp_path, FakeProvider())
    try:
        body = {'mb_artist_id': MB, 'content_type': 'image/png', 'data': base64.b64encode(PNG).decode()}
        assert request(base, 'POST', '/api/artist-images/manual', body)[0] == 403
        status, _, raw = request(base, 'POST', '/api/artist-images/manual', body, token=running.token)
        event = json.loads(raw)['event_id']
        assert status == 200 and request(base, 'GET', f'/api/artist-image?mb={MB}')[2] == PNG
        assert request(base, 'POST', '/api/artist-images/undo', {'event_id': event}, token=running.token)[0] == 200
        assert request(base, 'GET', f'/api/artist-image?mb={MB}')[0] == 404
    finally:
        running.shutdown()
        running.server_close()


def test_the_identities_endpoint_tells_the_page_which_artists_have_a_photo(tmp_path):
    running, base = _server(tmp_path, FakeProvider())
    try:
        request(base, 'POST', '/api/artist-images/fetch', {'mb_artist_id': MB}, token=running.token)
        artists = json.loads(request(base, 'GET', '/api/artist-identities?limit=10')[2])['artists']
        adele = next(a for a in artists if a['name'] == 'Adele')
        assert adele['image']['url'] == f'/api/artist-image?mb={MB}' and adele['image']['license'] == 'CC BY-SA 4.0'
    finally:
        running.shutdown()
        running.server_close()


def test_the_page_shows_circular_lazy_photos_with_attribution_and_controls():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'loading="lazy"' in page and 'object-fit:cover' in page and 'border-radius:50%' in page
    for hook in ('id="photoList"', 'id="photoConfirm"', 'data-photo="fetch"', 'data-photo="clear"', 'data-photo="undo"',
                 'data-photo-file', "'/api/artist-images/manual'", "'/api/artist-images/undo'", 'photoCreditText'):
        assert hook in page, hook
    assert "addEventListener('error'" in page, 'a broken image falls back to the initials'
    assert 'rel="noopener noreferrer"' in page and "safeLink" in page


def test_summaries_for_a_whole_page_use_a_fixed_number_of_queries_and_read_no_image_bytes(db, tmp_path):
    ids = [f'00000000-0000-4000-8000-{n:012d}' for n in range(300)]
    for n, mb in enumerate(ids):
        ArtistLinks(db).record(f'Artist {n}', LinkDecision('linked', mb_id=mb, name=f'Artist {n}', reason='fixture'),
                               looked_up_at=1.0)
    svc = service(db, tmp_path, FakeProvider(), max_total_bytes=10_000_000)
    for mb in ids[:50]:
        svc.ensure(mb)
    svc.clear_manual(ids[0])
    reads = []
    original = svc._read
    svc._read = lambda *a, **k: reads.append(a) or original(*a, **k)
    statements = []
    real_read = db.read

    from contextlib import contextmanager

    @contextmanager
    def counting_read():
        with real_read() as connection:
            connection.set_trace_callback(statements.append)
            yield connection
    db.read = counting_read
    try:
        summaries = svc.summaries(ids)
    finally:
        db.read = real_read
    assert len(summaries) == 49, 'fifty fetched, one cleared by the person'
    assert reads == [], 'no image file is read just to list artists'
    assert len(statements) <= 6, f'{len(statements)} statements for 300 artists'


def test_migration_seventeen_adds_the_artwork_tables_and_keeps_existing_data(tmp_path):
    path = tmp_path / 'schema16.sqlite3'
    database = Database(path)
    database.initialize()
    LibraryRepository(database).save_many([Track('/a.mp3', 1, '.mp3', 1, 'T', 'Adele', 'A', '', 'tagged')])
    with database.write() as c:
        c.execute('DROP TABLE artwork_decisions')
        c.execute('DROP TABLE artist_images')
        c.execute('DROP TABLE human_label_history')  # created by the later migration 18
        c.execute('DROP TABLE human_label_batch_items')  # created by the later migration 19
        c.execute('DROP TABLE human_label_batches')
        c.execute('DROP TABLE human_version_label_history')
        c.execute('DROP TABLE human_version_labels')  # created by the later migration 20
        c.execute('PRAGMA user_version=16')
    reopened = Database(path)
    reopened.initialize()
    with reopened.read() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'artist_images', 'artwork_decisions'} <= tables
        assert c.execute('SELECT COUNT(*) FROM tracks').fetchone()[0] == 1
