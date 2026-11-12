"""Albums get covers automatically by canonical artist and album title, with honest states, through the real HTTP layer."""
import json
import threading
import time
from pathlib import Path

import pytest

from audio_fixtures import write_mp3
from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

JPEG = bytes.fromhex('ffd8ffe000104a46494600') + bytes([1]) * 64 + bytes.fromhex('ffd9')
GROUP_A, GROUP_B = '11111111-2222-3333-4444-555555555555', '99999999-2222-3333-4444-555555555555'
SEARCHES = []


def search(album, artist):
    SEARCHES.append((album, artist))
    if (album, artist) == ('Anomaly', 'Lecrae'):
        return [{'group_id': GROUP_A, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 100, 'primary_type': 'Album', 'secondary_types': [], 'first_release_date': '2014-09-09'}]
    if (album, artist) == ('Cloud Nine', 'Kygo'):
        return [{'group_id': GROUP_B, 'title': 'Cloud Nine', 'artist': 'Kygo', 'score': 100, 'primary_type': 'Album', 'secondary_types': [], 'first_release_date': '2016-05-13'},
                {'group_id': GROUP_B, 'title': 'Cloud Nine (Live)', 'artist': 'Kygo', 'score': 100, 'primary_type': 'Album', 'secondary_types': ['Live']}]
    return []


def start(tmp_path, db='lib.sqlite3'):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    server = create_server(db_path=tmp_path / db, html_path=html, secret_store=MemorySecretStore(), environ={},
                           artwork_fetch=lambda url: ('image', 'image/jpeg', JPEG), release_group_search=search)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


def stop(server):
    server.shutdown()
    server.server_close()


@pytest.fixture
def app(tmp_path):
    SEARCHES.clear()
    music = tmp_path / 'Music'
    rows = [('Lecrae', 'Anomaly', 'Fear'), ('Lecrae & Andy Mineo', 'Anomaly', 'Say I Won’t'), ('Lecrae', 'Anomaly', 'Broken'),
            ('Kygo', 'Cloud Nine', 'Firestone'), ('Kygo, Selena Gomez', 'Cloud Nine', 'It Ain’t Me'), ('Kygo, John Legend', 'Cloud Nine', 'Hymn'),
            ('Nobody', 'Unreleased Demo Tape', 'Track')]
    for i, (artist, album, title) in enumerate(rows):
        write_mp3(music / f'{i}.mp3', seed=i, tags={'title': title, 'artist': artist, 'album': album, 'tracknumber': str(i + 1)})
    server, base = start(tmp_path)
    server.jobs.wait(server.jobs.start_scan(str(music))['job_id'], 60)
    yield {'server': server, 'base': base, 'tmp': tmp_path}
    stop(server)


def get(app, path):
    status, _, raw = request(app['base'], 'GET', path)
    return status, json.loads(raw.decode('utf-8')) if raw[:1] in (b'{', b'[') else raw


def post(app, path, body=None, token=True):
    status, _, raw = request(app['base'], 'POST', path, body or {}, token=app['server'].token if token else None)
    return status, json.loads(raw.decode('utf-8'))


def finish(app, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, status = get(app, '/api/albums/enrich/status')
        if status['state'] in ('done', 'cancelled', 'failed', 'interrupted'):
            return status
        time.sleep(0.1)
    raise AssertionError('enrichment did not finish')


def albums(app):
    _, data = get(app, '/api/albums?limit=100&sort=title')
    return {a['title']: a for a in data['albums']}


def test_albums_are_listed_once_by_the_canonical_primary_artist(app):
    found = albums(app)
    assert found['Anomaly']['artist'] == 'Lecrae' and found['Anomaly']['tracks'] == 3
    assert found['Cloud Nine']['artist'] == 'Kygo' and found['Cloud Nine']['tracks'] == 3


def test_bulk_enrichment_finds_covers_by_canonical_artist_and_album_and_labels_each_honestly(app):
    assert albums(app)['Anomaly']['cover'].get('url') is None, 'nothing yet'
    status, body = post(app, '/api/albums/enrich')
    assert status == 202 and body['started'] is True and body['total'] == 3
    done = finish(app)
    assert done['state'] == 'done' and done['done'] == 3 and done['counts']['confirmed'] == 2 and done['counts']['missing'] == 1
    assert ('Cloud Nine', 'Kygo') in SEARCHES and not any('Selena' in a or 'Andy' in a for _, a in SEARCHES), 'the collaboration string is never searched'
    found = albums(app)
    assert found['Anomaly']['cover']['art']['state'] == 'confirmed' and found['Anomaly']['cover']['url'].startswith('/api/album-art/image?')
    assert found['Cloud Nine']['cover']['art']['state'] == 'confirmed', 'a live release with the same title is ignored, the studio group is used'
    assert found['Unreleased Demo Tape']['cover']['art']['state'] == 'missing' and not found['Unreleased Demo Tape']['cover'].get('url')
    status, image = get(app, found['Anomaly']['cover']['url'])
    assert status == 200 and image == JPEG


def test_attribution_and_source_are_reported_for_a_found_cover(app):
    post(app, '/api/albums/enrich')
    finish(app)
    art = albums(app)['Anomaly']['cover']['art']
    assert art['source_url'] == 'https://musicbrainz.org/release-group/' + GROUP_A and art['provenance'] == 'coverartarchive'


def test_enrichment_needs_the_token_and_only_one_runs_at_a_time(app):
    assert post(app, '/api/albums/enrich', token=False)[0] == 403
    app['server'].album_enrichment.art.search = lambda a, b: (time.sleep(0.4), [])[1]
    assert post(app, '/api/albums/enrich')[0] == 202
    status, body = post(app, '/api/albums/enrich')
    assert status == 409 and body['code'] == 'album_enrichment_running'
    finish(app)


def test_enrichment_can_be_cancelled_and_resumed_without_asking_again(app):
    calls = []

    def slow(album, artist):
        calls.append(album)
        time.sleep(0.3)
        return []
    app['server'].album_enrichment.art.search = slow
    post(app, '/api/albums/enrich')
    time.sleep(0.15)
    assert post(app, '/api/albums/enrich/control', {'action': 'cancel'})[0] == 200
    cancelled = finish(app)
    assert cancelled['state'] == 'cancelled' and cancelled['done'] < 3
    asked = len(calls)
    post(app, '/api/albums/enrich')
    done = finish(app)
    assert done['state'] == 'done' and done['done'] == 3 and len(calls) <= asked + 3 - cancelled['done'] + 1


def test_covers_and_progress_survive_a_restart_and_offline_use_stays_quiet(app, tmp_path):
    post(app, '/api/albums/enrich')
    finish(app)
    stop(app['server'])
    SEARCHES.clear()
    server, base = start(tmp_path)
    app.update(server=server, base=base)
    try:
        assert albums(app)['Anomaly']['cover']['art']['state'] == 'confirmed', 'the cached decision persists'
        _, status = get(app, '/api/albums/enrich/status')
        assert status['state'] == 'done'
        server.album_art.search = lambda a, b: (_ for _ in ()).throw(OSError('offline'))
        post(app, '/api/albums/enrich')
        assert finish(app)['state'] == 'done' and SEARCHES == [], 'offline or cached: nothing is lost and nothing breaks'
    finally:
        stop(server)
        app['server'] = type('S', (), {'shutdown': lambda s: None, 'server_close': lambda s: None, 'token': ''})()


def test_a_status_left_running_by_a_crash_is_reported_as_interrupted(app, tmp_path):
    app['server'].store.set_setting('album_enrichment', {'state': 'running', 'done': 1, 'total': 3, 'counts': {}})
    stop(app['server'])
    server, base = start(tmp_path)
    try:
        status, _, raw = request(base, 'GET', '/api/albums/enrich/status')
        assert json.loads(raw)['state'] == 'interrupted'
    finally:
        stop(server)
        app['server'] = type('S', (), {'shutdown': lambda s: None, 'server_close': lambda s: None, 'token': ''})()


def test_the_artist_profile_endpoint_shows_albums_with_their_cover_state_and_original_credits(app):
    post(app, '/api/albums/enrich')
    finish(app)
    status, profile = get(app, '/api/artist-profile?name=Kygo')
    assert status == 200 and profile['name'] == 'Kygo'
    assert profile['counts'] == {'tracks': 3, 'albums': 1, 'singles': 0, 'collaborations': 0}
    album = profile['albums'][0]
    assert album['name'] == 'Cloud Nine' and album['art']['state'] == 'confirmed' and album['cover_url'].startswith('/api/album-art/image?')
    assert {c['credit'] for c in profile['credits']} == {'Kygo', 'Kygo, Selena Gomez', 'Kygo, John Legend'}
    assert get(app, '/api/artist-profile?name=Nobody+Known')[0] == 404
    status, lecrae = get(app, '/api/artist-profile?name=Lecrae')
    assert lecrae['counts']['tracks'] == 3 and {c['credit'] for c in lecrae['credits']} == {'Lecrae', 'Lecrae & Andy Mineo'}
    status, mineo = get(app, '/api/artist-profile?name=Andy+Mineo')
    assert mineo['counts'] == {'tracks': 0, 'albums': 0, 'singles': 0, 'collaborations': 1}, 'a collaborator is not a primary artist'
