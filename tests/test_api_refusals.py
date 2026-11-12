"""A refused POST must reach the client as a clean 403, even when the client sent a body the server did not need."""
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request


@pytest.fixture
def served(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def test_a_refused_post_with_a_body_is_always_a_clean_403(served):
    _, base = served
    body = {'note': 'x' * 30000}
    for _ in range(40):
        status, _, answer = request(base, 'POST', '/api/qa/import', body)
        assert status == 403 and json.loads(answer)['error'] == 'Invalid session token'


def test_a_refused_post_with_an_oversized_body_does_not_hang_the_server(served):
    _, base = served
    try:                    # for a body this large the server closes the connection; the client may see 403 or a reset
        status, _, _ = request(base, 'POST', '/api/qa/import', {'csv': 'x' * 200_000})
        assert status == 403
    except OSError:
        pass
    status, _, answer = request(base, 'GET', '/api/qa/progress')
    assert status == 200 and 'proposals' in json.loads(answer)
