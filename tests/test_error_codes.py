"""Server errors carry a stable, machine-readable code that the page translates in all eight languages.

The English 'error' sentence stays in every response for logs and older callers. A code is what the page keys on.
Parameters are values the server chose; exception text, paths outside the request, and credentials never reach a body.
"""
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

from meloshelf.application.error_codes import MESSAGES, PARAMETERS

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'src' / 'meloshelf' / 'presentation' / 'web'
LANGUAGES = ('en', 'es', 'fr', 'hi', 'ja', 'ko', 'pt', 'zh-Hans')
PLACEHOLDER = re.compile(r'\{[a-zA-Z0-9_]+\}')


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


def catalog(lang):
    return json.loads((WEB / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))


def test_refusals_from_the_web_layer_carry_a_registered_code(server):
    running, base = server
    token = running.token
    cases = [
        ('POST', '/api/scan', {'folder': str(running.server_address)}, token, 400, 'folder_not_found'),
        ('POST', '/api/decision', {}, token, 400, 'invalid_decision'),
        ('POST', '/api/identify', {'limit': 0}, token, 400, 'identify_limit'),
        ('POST', '/api/scan', {'folder': 'x'}, None, 403, 'token_invalid'),
        ('GET', '/api/no-such-route', None, None, 404, 'not_found'),
    ]
    for method, path, body, tok, status_code, code in cases:
        status, data = failure(base, method, path, body, token=tok)
        assert status == status_code, (path, status)
        assert data.get('code') == code, (path, data)
        assert code in MESSAGES and data['error'], (path, data)


def test_a_setting_out_of_range_names_its_code_and_limits(server):
    running, base = server
    status, data = failure(base, 'POST', '/api/settings', {'threshold': 200}, token=running.token)
    assert status == 400
    assert data['code'] == 'threshold_out_of_range'
    assert data['params'] == {'low': 50, 'high': 100}
    assert 'from 50 to 100' in data['error']


def test_a_database_error_never_exposes_the_exception_text(server, monkeypatch):
    running, base = server

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError('database is locked at C:\\Users\\private\\secret-library.sqlite3')

    monkeypatch.setattr(running.repo, 'counts', locked)
    status, data = failure(base, 'GET', '/api/stats')
    assert status == 500
    assert data['code'] == 'library_database_error'
    text = json.dumps(data)
    assert 'secret' not in text and 'private' not in text and 'locked' not in text


def test_every_code_has_an_english_message_and_eight_translations_with_the_same_placeholders():
    assert set(MESSAGES) == set(PARAMETERS), 'every code declares its parameters'
    for code, english in MESSAGES.items():
        assert english.strip() and '<' not in english
        expected = sorted(set(PLACEHOLDER.findall(english)))
        assert expected == sorted('{' + name + '}' for name in PARAMETERS[code]), code
        for lang in LANGUAGES:
            value = catalog(lang).get('err.' + code)
            assert value is not None, f'{lang} has no err.{code}'
            assert value.strip(), f'{lang} err.{code} is blank'
            assert sorted(set(PLACEHOLDER.findall(value))) == expected, f'{lang} err.{code} placeholders differ'


def test_the_page_maps_every_code_to_its_catalogue_key_and_defines_its_helper():
    page = (WEB / 'index.html').read_text(encoding='utf-8')
    script = page.split('<script id="meloshelf-app">', 1)[1]
    assert 'function serverText(' in script, 'serverText is called by api() but was never defined'
    for code in MESSAGES:
        assert f"err.{code}" in script, f'the page has no key for code {code}'
