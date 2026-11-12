"""Configuration API: local-only, token-protected, cross-origin safe, and never echoing secrets."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore

KEY = 'AbCdEf1234_xyz-SECRETVALUE'


@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html,
                            secret_store=MemorySecretStore(), environ={},
                            connection_tester=lambda target, config: {'ok': True, 'message': 'stub'})
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def call(base, method, path, body=None, *, token=None, origin=None, content_type='application/json', server=None):
    headers = {'Content-Type': content_type}
    if token is not None:
        headers['X-MeloShelf-Token'] = token
    if origin is not None:
        headers['Origin'] = origin
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_configuration_summary_is_readable_without_secrets(server):
    running, base = server
    status, text = call(base, 'GET', '/api/config')
    assert status == 200 and KEY not in text
    body = json.loads(text)
    assert body['configuration']['acoustid']['configured'] is False
    assert body['configuration']['secure_store']['available'] is True


def test_saving_the_key_requires_the_session_token(server):
    running, base = server
    assert call(base, 'POST', '/api/config', {'acoustid_key': KEY})[0] == 403
    assert call(base, 'POST', '/api/config', {'acoustid_key': KEY}, token='wrong')[0] == 403


def test_cross_origin_browser_requests_are_refused_even_with_a_token(server):
    running, base = server
    status, _ = call(base, 'POST', '/api/config', {'acoustid_key': KEY}, token=running.token,
                     origin='https://evil.example')
    assert status == 403


def test_cross_site_form_posts_without_the_token_are_refused(server):
    running, base = server
    status, _ = call(base, 'POST', '/api/config', b'acoustid_key=' + KEY.encode(),
                     content_type='application/x-www-form-urlencoded', origin='http://127.0.0.1:1')
    assert status == 403


def test_a_valid_request_saves_the_key_and_the_response_never_contains_it(server):
    running, base = server
    status, text = call(base, 'POST', '/api/config', {'acoustid_key': KEY}, token=running.token,
                        origin=base)
    assert status == 200 and KEY not in text
    assert json.loads(text)['configuration']['acoustid'] == {'configured': True, 'source': 'secure store'}
    assert KEY not in call(base, 'GET', '/api/config')[1]


def test_invalid_values_are_reported_with_a_reason(server):
    running, base = server
    status, text = call(base, 'POST', '/api/config', {'musicbrainz_contact': 'not-an-address'}, token=running.token)
    assert status == 400 and 'contact' in json.loads(text)['error'].lower()


def test_connection_test_is_limited_to_known_targets(server):
    running, base = server
    assert call(base, 'POST', '/api/config/test', {'target': 'everything'}, token=running.token)[0] == 400
    status, text = call(base, 'POST', '/api/config/test', {'target': 'acoustid'}, token=running.token)
    assert status == 200 and json.loads(text)['ok'] is True and KEY not in text


def test_the_configuration_endpoints_never_accept_a_missing_origin_with_a_bad_host(server):
    running, base = server
    request = urllib.request.Request(base + '/api/config', headers={'Host': 'evil.example'})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 403


def test_configuration_names_the_process_that_answered_so_stale_pages_are_visible(server):
    running, base = server
    body = json.loads(call(base, 'GET', '/api/config')[1])
    assert body['instance']['port'] == running.server_address[1]
    assert isinstance(body['instance']['process'], int) and body['instance']['process'] > 0
