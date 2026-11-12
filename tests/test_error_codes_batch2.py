"""P3b: the remaining server refusals carry registered codes, and no raw exception text or provider detail reaches a body.

Each module's refusal is checked through the real HTTP layer where the route exists, and the sanitising helpers are
checked directly. Translation completeness and placeholder agreement for every registered code are checked in
tests/test_error_codes.py; this file adds the module-level codes and the safety properties.
"""
import json
import threading

import pytest

from meloshelf.application import error_codes
from meloshelf.application.error_codes import MESSAGES
from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.music_api import APIError
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

SECRET = 'C:\\Users\\private\\secret-library.sqlite3'
VALID_MB = '11111111-2222-3333-4444-555555555555'


@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, connection_tester=lambda target, config: {'ok': True, 'state': 'valid', 'message': 'x'})
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def failure(base, method, path, body=None, *, token=None):
    status, _, raw = request(base, method, path, body, token=token)
    return status, json.loads(raw.decode('utf-8'))


@pytest.mark.parametrize('method,path,body,status,code', [
    ('POST', '/api/artist-images/manual', {'mb': VALID_MB, 'data': '!!not base64!!'}, 400, 'image_base64_invalid'),
    ('POST', '/api/artist-images/manual', {'mb': 'nope', 'data': ''}, 400, 'image_type_invalid'),
    ('POST', '/api/album-covers/manual', {'album': '', 'artist': ''}, 400, 'image_type_invalid'),
    ('POST', '/api/artist-enrichment/start', {'max_names': 0}, 409, 'enrichment_contact_required'),
    ('POST', '/api/artist-enrichment/control', {'id': 'x', 'action': 'pause'}, 400, 'job_id_not_integer'),
    ('POST', '/api/artist-enrichment/control', {'id': 1, 'action': 'explode'}, 400, 'enrichment_action_invalid'),
    ('POST', '/api/artist-corrections/merge', {}, 400, 'source_target_required'),
    ('POST', '/api/artist-corrections/keep-separate', {}, 400, 'a_b_required'),
    ('POST', '/api/ui-preferences', {'language': 'xx'}, 400, 'preference_invalid'),
    ('POST', '/api/ui-preferences', {}, 400, 'preferences_empty'),
    ('POST', '/api/fallback/start', {'limit': 0}, 400, 'limit_out_of_range'),
])
def test_module_refusals_carry_their_registered_code(server, method, path, body, status, code):
    running, base = server
    got_status, data = failure(base, method, path, body, token=running.token)
    assert got_status == status, (path, got_status, data)
    assert data.get('code') == code, (path, data)
    assert code in MESSAGES and data.get('error'), (path, data)


def test_an_exception_becomes_a_safe_code_and_keeps_no_text():
    body = error_codes.unexpected(OSError(SECRET + ' is locked'))
    assert body['code'] == 'unexpected_error'
    assert body['params'] == {'exception': 'OSError'}
    assert 'secret' not in json.dumps(body) and 'locked' not in json.dumps(body)


def test_a_provider_failure_is_coded_and_drops_the_provider_text():
    from meloshelf.application.webapp import _acoustid_failure
    result = _acoustid_failure(APIError('bad', kind='request_rejected', provider_code=4,
                                        provider_message='secret key abc123 was refused'))
    assert result['code'] == 'acoustid_request_rejected'
    assert result['params'] == {'code': '4'}
    text = json.dumps(result)
    assert 'abc123' not in text and 'secret' not in text


def test_a_provider_code_that_is_not_a_number_is_not_passed_through():
    from meloshelf.application.webapp import _acoustid_failure
    result = _acoustid_failure(APIError('bad', kind='invalid_key', provider_code='<script>x</script>'))
    assert result['code'] == 'acoustid_invalid_key'
    assert '<script>' not in json.dumps(result)


def test_the_connection_result_names_its_code_and_the_page_translates_it():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    script = page.split('<script id="meloshelf-app">', 1)[1]
    assert 'answer.message' in script, 'serverText must read the message of a connection result'
    assert 'serverText(r)' in script, 'the connection test must show the translated text'


def test_server_text_is_never_written_as_markup():
    """A parameter chosen by the server (a field name, a path) must not be able to inject HTML into the page."""
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(
        encoding='utf-8').split('<script id="meloshelf-app">', 1)[1]
    assert "el.textContent=message" in script, 'toasts are text'
    assert "$('testResult').textContent=" in script and "$('testResult').innerHTML" not in script
    helper = script.split('function serverText(answer){', 1)[1].split('\n  }\n', 1)[0]
    assert 'innerHTML' not in helper and 'esc(' not in helper, 'serverText returns text; the caller writes it as text'
