"""Artist photographs appear for the CANONICAL artist, with licence and attribution, and fail quietly."""
import json
import threading
import time

import pytest

from audio_fixtures import write_mp3
from meloshelf.application.webapp import create_server
from meloshelf.domain.artist_identity import ArtistCandidate, decide_link
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_artist_photos import FakeProvider, JPEG
from test_qa_api import request

KYGO = 'cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493'
LECRAE = '0383dadf-2a4e-4d10-a46a-e9e041da8eb3'


def build(tmp_path, provider, db='lib.sqlite3'):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    server = create_server(db_path=tmp_path / db, html_path=html, secret_store=MemorySecretStore(), environ={}, photo_provider=provider)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


@pytest.fixture
def app(tmp_path):
    music = tmp_path / 'Music'
    for i, credit in enumerate(['Kygo', 'Kygo, Selena Gomez', 'Kygo, John Legend', 'Lecrae & Andy Mineo', 'Lecrae']):
        write_mp3(music / f'{i}.mp3', seed=i, tags={'title': f'T{i}', 'artist': credit, 'album': 'A', 'tracknumber': str(i + 1)})
    provider = FakeProvider()
    server, base = build(tmp_path, provider)
    server.jobs.wait(server.jobs.start_scan(str(music))['job_id'], 60)
    for name, mb in (('Kygo', KYGO), ('Lecrae', LECRAE)):
        ArtistLinks(server.database).record(name, decide_link(name, [ArtistCandidate(mb, name, 100)]), looked_up_at=1.0)
    yield {'server': server, 'base': base, 'provider': provider, 'tmp': tmp_path}
    server.shutdown()
    server.server_close()


def call(app, method, path, body=None, token=True):
    status, _, raw = request(app['base'], method, path, body if body is not None or method == 'GET' else {}, token=app['server'].token if token else None)
    return status, (json.loads(raw.decode('utf-8')) if raw[:1] == b'{' else raw)


def wait(app):
    deadline = time.time() + 20
    while time.time() < deadline:
        _, st = call(app, 'GET', '/api/artist-images/batch/status')
        if st['state'] not in ('running', 'cancelling'):
            return st
        time.sleep(0.05)
    raise AssertionError('batch did not finish')


def identities(app):
    return {a['name']: a for a in call(app, 'GET', '/api/artist-identities?limit=100')[1]['artists']}


def test_one_pass_fetches_a_photo_for_each_verified_canonical_artist(app):
    assert 'image' not in identities(app)['Kygo'], 'nothing is fetched until the pass runs'
    status, body = call(app, 'POST', '/api/artist-images/batch')
    assert status == 202 and body['total'] == 2
    done = wait(app)
    assert done['state'] == 'done' and done['counts'] == {'available': 2}
    found = identities(app)
    assert found['Kygo']['image']['url'] and found['Lecrae']['image']['url'], 'the photo belongs to the canonical artist, not to a collaboration string'
    assert set(app['provider'].find_calls) == {KYGO, LECRAE}


def test_licence_author_and_source_are_reported_with_the_photo(app):
    call(app, 'POST', '/api/artist-images/batch')
    wait(app)
    image = identities(app)['Kygo']['image']
    assert image['license'] == 'CC BY-SA 4.0' and image['author'] == 'A. Photographer' and image['source_url'].startswith('https://commons.wikimedia.org/')
    status, _ = call(app, 'GET', image['url'])
    assert status == 200


def test_a_licence_that_forbids_reuse_leaves_initials_and_is_never_asked_again(tmp_path):
    music = tmp_path / 'Music'
    write_mp3(music / 'a.mp3', seed=1, tags={'title': 'T', 'artist': 'Kygo, X', 'album': 'A'})
    provider = FakeProvider(found={'status': 'found', 'file': 'x.jpg', 'license': 'All rights reserved', 'author': 'x', 'source_url': 'https://commons.wikimedia.org/wiki/File:x.jpg', 'thumb_url': 'u'})
    server, base = build(tmp_path, provider)
    app = {'server': server, 'base': base, 'tmp': tmp_path}
    try:
        server.jobs.wait(server.jobs.start_scan(str(music))['job_id'], 60)
        ArtistLinks(server.database).record('Kygo', decide_link('Kygo', [ArtistCandidate(KYGO, 'Kygo', 100)]), looked_up_at=1.0)
        call(app, 'POST', '/api/artist-images/batch')
        assert wait(app)['counts'] == {'rejected_license': 1}
        assert 'image' not in identities(app)['Kygo']
        call(app, 'POST', '/api/artist-images/batch')
        assert wait(app)['total'] == 0 and len(provider.find_calls) == 1, 'a permanent answer is not retried'
    finally:
        server.shutdown()
        server.server_close()


def test_an_unreachable_provider_stops_the_pass_without_storing_a_failure(tmp_path):
    music = tmp_path / 'Music'
    for i, name in enumerate(('Kygo', 'Lecrae', 'Adele')):
        write_mp3(music / f'{i}.mp3', seed=i, tags={'title': 'T', 'artist': name, 'album': 'A'})
    provider = FakeProvider(fail=True)
    server, base = build(tmp_path, provider)
    app = {'server': server, 'base': base, 'tmp': tmp_path}
    try:
        server.jobs.wait(server.jobs.start_scan(str(music))['job_id'], 60)
        for name, mb in (('Kygo', KYGO), ('Lecrae', LECRAE), ('Adele', 'aaaaaaaa-b7bc-4b8b-84d8-4fbd8779e493')):
            ArtistLinks(server.database).record(name, decide_link(name, [ArtistCandidate(mb, name, 100)]), looked_up_at=1.0)
        call(app, 'POST', '/api/artist-images/batch')
        done = wait(app)
        assert done['state'] == 'provider_unavailable' and len(provider.find_calls) == 2, 'it stops instead of hammering the provider'
        assert not any('image' in a for a in identities(app).values())
        provider.fail = False
        call(app, 'POST', '/api/artist-images/batch')
        assert wait(app)['state'] == 'done', 'transient failures were not stored, so the next pass succeeds'
    finally:
        server.shutdown()
        server.server_close()


def test_the_photos_and_the_pass_status_survive_a_restart(app):
    call(app, 'POST', '/api/artist-images/batch')
    wait(app)
    app['server'].shutdown()
    app['server'].server_close()
    server, base = build(app['tmp'], FakeProvider(fail=True))
    app.update(server=server, base=base)
    assert identities(app)['Kygo']['image']['url'], 'cached photos need no provider after a restart'
    assert call(app, 'GET', '/api/artist-images/batch/status')[1]['state'] == 'done'


def test_a_pass_left_running_by_a_crash_is_reported_as_interrupted(app):
    app['server'].store.set_setting('photo_batch', {'state': 'running', 'done': 1, 'total': 2, 'counts': {}})
    app['server'].shutdown()
    app['server'].server_close()
    server, base = build(app['tmp'], FakeProvider())
    app.update(server=server, base=base)
    assert call(app, 'GET', '/api/artist-images/batch/status')[1]['state'] == 'interrupted'


def test_without_a_musicbrainz_contact_the_pass_asks_for_one_instead_of_failing(tmp_path):
    music = tmp_path / 'Music'
    write_mp3(music / 'a.mp3', seed=1, tags={'title': 'T', 'artist': 'Kygo', 'album': 'A'})
    server, base = build(tmp_path, None)
    app = {'server': server, 'base': base, 'tmp': tmp_path}
    try:
        server.jobs.wait(server.jobs.start_scan(str(music))['job_id'], 60)
        ArtistLinks(server.database).record('Kygo', decide_link('Kygo', [ArtistCandidate(KYGO, 'Kygo', 100)]), looked_up_at=1.0)
        call(app, 'POST', '/api/artist-images/batch')
        assert wait(app)['state'] == 'needs_contact'
    finally:
        server.shutdown()
        server.server_close()


def test_a_manual_photo_correction_still_wins_over_the_fetched_one(app):
    import base64
    call(app, 'POST', '/api/artist-images/batch')
    wait(app)
    manual = b'\xff\xd8\xff\xe0' + b'M' * 700
    status, _ = call(app, 'POST', '/api/artist-images/manual', {'mb_artist_id': KYGO, 'content_type': 'image/jpeg', 'data': base64.b64encode(manual).decode(), 'note': 'mine'})
    assert status == 200
    url = identities(app)['Kygo']['image']['url']
    assert call(app, 'GET', url)[1] == manual


def test_enabling_a_new_source_makes_previously_missing_photos_eligible_again_in_the_next_pass(tmp_path):
    from test_art_providers_integration import Extra
    music = tmp_path / 'Music'
    write_mp3(music / 'a.mp3', seed=1, tags={'title': 'T', 'artist': 'Kygo', 'album': 'A'})
    provider = FakeProvider(found={'status': 'none', 'reason': 'nothing on Commons'})
    server, base = build(tmp_path, provider)
    app = {'server': server, 'base': base, 'tmp': tmp_path}
    try:
        server.jobs.wait(server.jobs.start_scan(str(music))['job_id'], 60)
        ArtistLinks(server.database).record('Kygo', decide_link('Kygo', [ArtistCandidate(KYGO, 'Kygo', 100)]), looked_up_at=1.0)
        server.art_clients = []
        call(app, 'POST', '/api/artist-images/batch')
        assert wait(app)['counts'] == {'none': 1}
        call(app, 'POST', '/api/artist-images/batch')
        assert wait(app)['total'] == 0, 'same sources: the missing photograph is not asked about again'
        server.art_clients = [Extra('fanart.tv')]
        call(app, 'POST', '/api/artist-images/batch')
        done = wait(app)
        assert done['total'] == 1 and done['counts'] == {'available': 1}
        assert identities(app)['Kygo']['image']['provenance'] == 'fanart.tv'
    finally:
        server.shutdown()
        server.server_close()
