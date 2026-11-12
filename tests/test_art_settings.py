"""Keys and the source selector: stored securely, never shown, off by default, and wired to the pipelines."""
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request


def start(tmp_path, secrets=None, environ=None, **options):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    secrets = secrets or MemorySecretStore()
    server = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=secrets, environ=environ or {}, **options)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_address[1]}', secrets


@pytest.fixture
def app(tmp_path):
    server, base, secrets = start(tmp_path)
    yield {'server': server, 'base': base, 'secrets': secrets, 'tmp': tmp_path}
    server.shutdown()
    server.server_close()


def post(app, body, token=True):
    status, _, raw = request(app['base'], 'POST', '/api/config', body, token=app['server'].token if token else None)
    return status, json.loads(raw.decode('utf-8'))


def config(app):
    return json.loads(request(app['base'], 'GET', '/api/config')[2].decode('utf-8'))


def test_the_optional_sources_are_off_and_unkeyed_by_default(app):
    art = config(app)['configuration']['art']
    assert art == {'fanart': {'enabled': False, 'configured': False, 'personal_key': False},
                   'theaudiodb': {'enabled': False, 'configured': False, 'test_key_only': False}}
    assert app['server'].config.art_clients() == []


def test_keys_go_to_the_secure_store_and_are_never_returned_or_written_to_the_database(app):
    status, body = post(app, {'fanart_project_key': 'PROJECT-KEY-1234', 'fanart_client_key': 'PERSONAL-KEY-5678', 'theaudiodb_key': 'PREMIUM-KEY-9012'})
    assert status == 200
    text = json.dumps(config(app)) + json.dumps(body)
    for secret in ('PROJECT-KEY-1234', 'PERSONAL-KEY-5678', 'PREMIUM-KEY-9012'):
        assert secret not in text, 'a key is never shown again'
    assert app['server'].config.art_key('fanart_project_key') == 'PROJECT-KEY-1234'
    assert config(app)['configuration']['art']['fanart'] == {'enabled': False, 'configured': True, 'personal_key': True}
    import sqlite3
    raw = sqlite3.connect(app['tmp'] / 'lib.sqlite3')
    dump = ' '.join(str(row) for table in ('app_settings',) for row in raw.execute(f'SELECT * FROM {table}'))
    assert 'PROJECT-KEY-1234' not in dump and 'PREMIUM-KEY-9012' not in dump


def test_a_key_that_is_too_short_or_has_odd_characters_is_refused_with_a_code(app):
    status, body = post(app, {'fanart_project_key': 'short'})
    assert status == 400 and body['code'] == 'art_key_invalid'
    assert post(app, {'theaudiodb_key': 'has spaces in it!'})[0] == 400


def test_keys_can_be_cleared_and_the_environment_is_a_fallback(tmp_path):
    server, base, secrets = start(tmp_path, environ={'MELOSHELF_FANART_PROJECT_KEY': 'ENV-PROJECT-KEY-1'})
    try:
        app = {'server': server, 'base': base}
        assert config(app)['configuration']['art']['fanart']['configured'] is True
        assert post(app, {'fanart_project_key': 'STORED-KEY-1234'})[0] == 200
        assert server.config.art_key('fanart_project_key') == 'STORED-KEY-1234', 'the secure store wins'
        assert post(app, {'fanart_project_key': ''})[0] == 200
        assert server.config.art_key('fanart_project_key') == 'ENV-PROJECT-KEY-1'
    finally:
        server.shutdown()
        server.server_close()


def test_a_provider_is_used_only_when_switched_on_and_keyed(app):
    post(app, {'fanart_project_key': 'PROJECT-KEY-1234'})
    assert app['server'].config.art_clients() == [], 'keyed but off'
    assert post(app, {'art_sources': {'fanart': True}})[0] == 200
    assert [c.name for c in app['server'].config.art_clients()] == ['fanart.tv']
    post(app, {'art_sources': {'theaudiodb': True}})
    assert [c.name for c in app['server'].config.art_clients()] == ['fanart.tv'], 'switched on but unkeyed'
    post(app, {'theaudiodb_key': 'PREMIUM-KEY-9012'})
    assert [c.name for c in app['server'].config.art_clients()] == ['fanart.tv', 'theaudiodb']
    assert app['server']._art_extras() and config(app)['configuration']['art']['theaudiodb']['enabled'] is True


def test_theaudiodbs_shared_test_key_is_reported_and_never_used_for_lookups(app):
    post(app, {'theaudiodb_key': '12345678', 'art_sources': {'theaudiodb': True}})
    assert [c.name for c in app['server'].config.art_clients()] == ['theaudiodb']
    app['server'].config.secrets.set('theaudiodb-key', '123')
    assert app['server'].config.art_clients() == []
    assert config(app)['configuration']['art']['theaudiodb']['test_key_only'] is True


def test_the_selector_rejects_unknown_sources_and_non_booleans(app):
    for bad in ({'art_sources': {'discogs': True}}, {'art_sources': {'fanart': 'yes'}}, {'art_sources': ['fanart']}):
        status, body = post(app, bad)
        assert status == 400 and body['code'] == 'art_sources_invalid', bad


def test_changing_settings_needs_the_session_token(app):
    assert post(app, {'art_sources': {'fanart': True}}, token=False)[0] == 403


def test_the_pipelines_receive_the_configured_providers(app):
    post(app, {'fanart_project_key': 'PROJECT-KEY-1234', 'art_sources': {'fanart': True}})
    assert [c.name for c in app['server'].photos.extras()] == ['fanart.tv']
    assert [c.name for c in app['server'].album_art.fallbacks()] == ['fanart.tv']


def test_injected_providers_override_the_settings_for_tests_and_validation(tmp_path):
    marker = object()
    server, base, _ = start(tmp_path, art_clients=[marker])
    try:
        assert server.photos.extras() == [marker] and server.album_art.fallbacks() == [marker]
    finally:
        server.shutdown()
        server.server_close()
