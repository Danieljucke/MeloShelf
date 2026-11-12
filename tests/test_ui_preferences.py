"""Language and theme are stored per installation, validated against fixed lists, and written only with the token."""
import json
import threading

import pytest

from meloshelf.application import ui_preferences
from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request


def make_server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    return create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})


@pytest.fixture
def served(tmp_path):
    running = make_server(tmp_path)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def test_the_eight_required_languages_are_the_only_ones_accepted():
    assert ui_preferences.LANGUAGES == ('en', 'fr', 'es', 'pt', 'ja', 'zh-Hans', 'ko', 'hi')
    assert ui_preferences.THEMES == ('light', 'dark', 'system')


def test_the_defaults_are_empty_so_the_browser_decides(served):
    _, base = served
    status, _, body = request(base, 'GET', '/api/ui-preferences')
    assert status == 200 and json.loads(body) == {'language': None, 'theme': None}


def test_a_write_needs_the_token(served):
    _, base = served
    assert request(base, 'POST', '/api/ui-preferences', {'language': 'fr'})[0] == 403


def test_language_and_theme_are_stored_and_read_back(served):
    running, base = served
    status, _, body = request(base, 'POST', '/api/ui-preferences', {'language': 'ja', 'theme': 'dark'},
                              token=running.token)
    assert status == 200 and json.loads(body) == {'language': 'ja', 'theme': 'dark'}
    assert json.loads(request(base, 'GET', '/api/ui-preferences')[2]) == {'language': 'ja', 'theme': 'dark'}


def test_one_value_can_change_without_touching_the_other(served):
    running, base = served
    request(base, 'POST', '/api/ui-preferences', {'language': 'ko', 'theme': 'light'}, token=running.token)
    request(base, 'POST', '/api/ui-preferences', {'theme': 'system'}, token=running.token)
    assert json.loads(request(base, 'GET', '/api/ui-preferences')[2]) == {'language': 'ko', 'theme': 'system'}


@pytest.mark.parametrize('payload', [
    {'language': 'xx'}, {'language': 'EN'}, {'language': ''}, {'language': 5}, {'language': ['en']},
    {'theme': 'purple'}, {'theme': None}, {'theme': 1}, {},
    {'language': '../../etc/passwd'}, {'language': '<script>'}, {'path': '/x'},
])
def test_unknown_or_malformed_values_are_refused_and_change_nothing(served, payload):
    running, base = served
    request(base, 'POST', '/api/ui-preferences', {'language': 'es', 'theme': 'dark'}, token=running.token)
    status, _, _ = request(base, 'POST', '/api/ui-preferences', payload, token=running.token)
    assert status == 400
    assert json.loads(request(base, 'GET', '/api/ui-preferences')[2]) == {'language': 'es', 'theme': 'dark'}


def test_a_stored_value_that_is_no_longer_valid_reads_back_as_unset(served):
    running, base = served
    running.store.set_setting(ui_preferences.LANGUAGE_KEY, 'tlh')
    assert json.loads(request(base, 'GET', '/api/ui-preferences')[2])['language'] is None
