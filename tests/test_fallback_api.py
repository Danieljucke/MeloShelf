"""Fallback API: token-protected runs, bounded batches, results listed for QA, and no acoustic writes."""
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_fallback_run import CATALOG, FakeClient
from test_metadata_store import seed_track
from test_qa_api import request


@pytest.fixture(autouse=True)
def fixed_length(monkeypatch):
    import meloshelf.application.fallback_runner as runner_module
    monkeypatch.setattr(runner_module, 'read_duration', lambda path: 188.0)


@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    client = FakeClient(CATALOG)
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={'MELOSHELF_MUSICBRAINZ_USER_AGENT': 'MeloShelf/0.2 (test@example.org)'},
                            connection_tester=lambda target, config: {'ok': True, 'state': 'valid', 'message': 'x'},
                            fallback_client=client)
    seed_track(running.database, 'D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3', title='Making History', artist='',
               album='', cand_artist=None)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}', client
    running.shutdown()
    running.server_close()


def test_a_run_needs_the_session_token(server):
    running, base, client = server
    assert request(base, 'POST', '/api/fallback/run', {'limit': 5})[0] == 403


def test_a_run_stores_suggestions_and_reports_its_totals(server):
    running, base, client = server
    status, _, body = request(base, 'POST', '/api/fallback/run', {'limit': 5}, token=running.token)
    summary = json.loads(body)
    assert status == 202 and summary['suggested'] == 1
    results = json.loads(request(base, 'GET', '/api/fallback/results')[2])['results']
    assert results[0]['level'] == 'METADATA_SUGGESTED' and results[0]['acoustically_verified'] is False


def test_the_batch_size_is_bounded(server):
    running, base, client = server
    status, _, body = request(base, 'POST', '/api/fallback/run', {'limit': 100000}, token=running.token)
    assert status == 400


def test_an_offline_run_sends_no_search(server):
    running, base, client = server
    status, _, body = request(base, 'POST', '/api/fallback/run', {'limit': 5, 'offline': True}, token=running.token)
    assert status == 202 and json.loads(body)['offline_skipped'] == 1 and client.calls == []


def test_a_run_never_changes_the_acoustic_results(server):
    running, base, client = server
    from meloshelf.infrastructure.match_store import MatchStore
    path = 'D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3'
    before = MatchStore(running.database).get_result(path)
    request(base, 'POST', '/api/fallback/run', {'limit': 5}, token=running.token)
    assert MatchStore(running.database).get_result(path) == before
