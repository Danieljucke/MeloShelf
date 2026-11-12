"""Fallback search as a background job with visible progress: started and cancelled with the token, one at a time,
bounded, resumable (answered files are never asked again), restart-safe, and still text-only and read-only."""
import json
import threading
import time

import pytest

from meloshelf.application.fallback_job import FallbackJobs
from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.fallback_store import FallbackStore
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_metadata_store import seed_track
from test_qa_api import request


@pytest.fixture(autouse=True)
def fixed_length(monkeypatch):
    import meloshelf.application.fallback_runner as runner_module
    monkeypatch.setattr(runner_module, 'read_duration', lambda path: 188.0)


class GatedClient:
    """Answers every search, but can hold the worker between files so a test can look at the running state."""

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Semaphore(0)

    def search_recordings(self, title, artist='', *, limit=5):
        self.calls.append(title)
        self.entered.release()
        assert self.gate.wait(10), 'test left the gate closed'
        return [{'recording_id': f'rec-{title}', 'title': title, 'artist': 'Band', 'duration': 188.0,
                 'releases': [], 'search_score': 100}]


NAMES = ['Alpha', 'Bravo', 'Charlie', 'Delta']


def seed(db, names=NAMES):
    for name in names:
        seed_track(db, f'D:/Library/Band/Band_-_{name}.mp3', title=name, artist='', album='', cand_artist=None)


@pytest.fixture
def served(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    client = GatedClient()
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={'MELOSHELF_MUSICBRAINZ_USER_AGENT': 'MeloShelf/0.2 (test@example.org)'},
                            fallback_client=client)
    seed(running.database)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}', client
    client.gate.set()
    running.shutdown()
    running.server_close()


def status_of(base):
    return json.loads(request(base, 'GET', '/api/fallback/status')[2])


def wait_for(base, predicate, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = status_of(base)
        if predicate(current):
            return current
        time.sleep(0.02)
    raise AssertionError(f'timed out; last status {status_of(base)}')


def start(running, base, **payload):
    return request(base, 'POST', '/api/fallback/start', payload, token=running.token)


# ── start, progress, finish ──────────────────────────────────────────────────────

def test_the_status_before_any_run_is_idle_and_counts_what_is_waiting(served):
    _, base, _ = served
    current = status_of(base)
    assert current['state'] == 'idle' and current['remaining'] == 4 and current['last_run'] is None


def test_starting_needs_the_token(served):
    _, base, _ = served
    assert request(base, 'POST', '/api/fallback/start', {'limit': 5})[0] == 403
    assert request(base, 'POST', '/api/fallback/cancel', {})[0] == 403


def test_a_run_reports_progress_while_it_works_and_a_summary_when_done(served):
    running, base, client = served
    client.gate.clear()
    status, _, body = start(running, base, limit=4)
    assert status == 202 and json.loads(body)['planned'] == 4
    assert client.entered.acquire(timeout=5)
    mid = wait_for(base, lambda s: s['state'] == 'running')
    assert mid['planned'] == 4 and mid['examined'] == 0 and mid['remaining'] == 4
    client.gate.set()
    done = wait_for(base, lambda s: s['state'] == 'done')
    assert (done['examined'], done['suggested'], done['planned']) == (4, 4, 4) and done['remaining'] == 0
    assert done['finished_at'] >= done['started_at']
    assert FallbackStore(running.database).get('D:/Library/Band/Band_-_Alpha.mp3')['level'] == 'METADATA_SUGGESTED'


def test_progress_counts_each_answered_file_as_it_happens(served):
    running, base, client = served
    client.gate.clear()
    start(running, base, limit=4)
    assert client.entered.acquire(timeout=5)
    client.gate.set()
    seen = set()
    deadline = time.time() + 10
    while time.time() < deadline and status_of(base)['state'] != 'done':
        seen.add(status_of(base)['examined'])
    assert max(seen | {4}) == 4 and status_of(base)['examined'] == 4


def test_a_second_start_while_running_is_refused(served):
    running, base, client = served
    client.gate.clear()
    start(running, base, limit=4)
    assert client.entered.acquire(timeout=5)
    status, _, body = start(running, base, limit=4)
    assert status == 409 and 'already running' in json.loads(body)['error']
    client.gate.set()
    wait_for(base, lambda s: s['state'] == 'done')


# ── limits and validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize('payload', [{'limit': 0}, {'limit': 201}, {'limit': '5'}, {'limit': 2.5}, {'limit': True},
                                     {'limit': 5, 'offline': 'yes'}])
def test_bad_start_requests_are_refused(served, payload):
    running, base, client = served
    assert start(running, base, **payload)[0] == 400
    assert client.calls == [] and status_of(base)['state'] == 'idle'


def test_the_batch_is_bounded_by_the_limit_and_the_rest_waits_for_a_later_run(served):
    running, base, client = served
    start(running, base, limit=2)
    done = wait_for(base, lambda s: s['state'] == 'done')
    assert done['examined'] == 2 and done['remaining'] == 2
    start(running, base, limit=10)
    again = wait_for(base, lambda s: s['state'] == 'done' and s['remaining'] == 0)
    assert again['examined'] == 2 and len(client.calls) == 4, 'answered files are never asked again'


def test_a_run_with_nothing_left_says_so_instead_of_starting(served):
    running, base, client = served
    start(running, base, limit=10)
    wait_for(base, lambda s: s['state'] == 'done')
    status, _, body = start(running, base, limit=10)
    assert status == 409 and 'nothing' in json.loads(body)['error'].lower()


# ── offline, contact, failures ───────────────────────────────────────────────────

def test_an_offline_run_sends_nothing_and_says_how_many_it_skipped(served):
    running, base, client = served
    start(running, base, limit=4, offline=True)
    done = wait_for(base, lambda s: s['state'] == 'done')
    assert done['offline_skipped'] == 4 and done['suggested'] == 0 and client.calls == [] and done['remaining'] == 4


def test_without_a_contact_a_live_run_is_refused_with_the_reason(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    seed(running.database)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{running.server_address[1]}'
        status, _, body = start(running, base, limit=5)
        assert status == 409 and 'MusicBrainz contact' in json.loads(body)['error']
    finally:
        running.shutdown()
        running.server_close()


def test_a_worker_crash_is_reported_as_failed_and_the_next_run_is_possible(served, monkeypatch):
    running, base, client = served
    import meloshelf.application.fallback_job as module

    class Boom(module.FallbackRunner):
        def run(self, **kwargs):
            raise RuntimeError('provider blew up')

    monkeypatch.setattr(module, 'FallbackRunner', Boom)
    start(running, base, limit=4)
    failed = wait_for(base, lambda s: s['state'] == 'failed')
    assert failed['error'] == 'RuntimeError', 'the run reports the exception class; its text stays in the server log'
    assert 'provider blew up' not in json.dumps(failed)
    monkeypatch.undo()
    assert start(running, base, limit=4)[0] == 202


# ── cancel ───────────────────────────────────────────────────────────────────────

def test_cancel_stops_between_files_and_keeps_what_was_answered(served):
    running, base, client = served
    client.gate.clear()
    start(running, base, limit=4)
    assert client.entered.acquire(timeout=5)
    status, _, body = request(base, 'POST', '/api/fallback/cancel', {}, token=running.token)
    assert status == 200 and json.loads(body)['cancelling'] is True
    client.gate.set()
    stopped = wait_for(base, lambda s: s['state'] == 'cancelled')
    assert stopped['examined'] == 1 and stopped['remaining'] == 3
    assert start(running, base, limit=4)[0] == 202
    wait_for(base, lambda s: s['state'] == 'done')
    assert len(client.calls) == 4, 'the file answered before the cancel was not asked again'


def test_cancel_with_nothing_running_is_harmless(served):
    running, base, _ = served
    status, _, body = request(base, 'POST', '/api/fallback/cancel', {}, token=running.token)
    assert status == 200 and json.loads(body)['cancelling'] is False


# ── durability ───────────────────────────────────────────────────────────────────

def test_the_last_run_survives_a_restart(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed(db)
    client = GatedClient()
    jobs = FallbackJobs(db, client_factory=lambda offline: client)
    jobs.start(limit=4, offline=False)
    jobs.wait(10)
    again = FallbackJobs(Database(tmp_path / 'lib.sqlite3'), client_factory=lambda offline: client)
    status = again.status()
    assert status['state'] == 'idle' and status['last_run']['state'] == 'done' and status['last_run']['examined'] == 4


def test_a_run_left_running_by_a_crash_is_shown_as_interrupted_and_can_be_continued(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed(db)
    MatchStore(db).set_setting('fallback_run', {'state': 'running', 'planned': 4, 'examined': 2, 'suggested': 2,
                                                  'unidentified': 0, 'provider_failed': 0, 'offline_skipped': 0,
                                                  'started_at': 1.0, 'finished_at': None, 'error': ''})
    jobs = FallbackJobs(db, client_factory=lambda offline: GatedClient())
    status = jobs.status()
    assert status['state'] == 'idle' and status['last_run']['state'] == 'interrupted' and status['last_run']['examined'] == 2
    assert jobs.start(limit=4, offline=False)[0] == 202
    jobs.wait(10)


# ── what the run may not do ──────────────────────────────────────────────────────

def test_a_run_changes_no_acoustic_result_and_reads_no_audio(served):
    running, base, client = served
    path = 'D:/Library/Band/Band_-_Alpha.mp3'
    before = MatchStore(running.database).get_result(path)
    start(running, base, limit=4)
    wait_for(base, lambda s: s['state'] == 'done')
    assert MatchStore(running.database).get_result(path) == before


def test_the_page_can_start_watch_and_cancel_the_search():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert '/api/fallback/start' in page and '/api/fallback/status' in page and '/api/fallback/cancel' in page
    assert 'role="progressbar"' in page and 'Search by text' in page
