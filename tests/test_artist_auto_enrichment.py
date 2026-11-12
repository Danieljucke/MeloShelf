"""Opt-in automatic artist enrichment after a completed scan.

* Off by default. Nothing is requested from MusicBrainz unless the person turns it on.
* When it runs it is bounded (AUTO_LIMIT names per scan), uses one job at a time, and never starts while another
  enrichment job is running.
* Without a configured contact or an injected client, it refuses and sends nothing.
* Ambiguous or uncertain results go to the suggestions workflow; no homonym is merged automatically.
* Human decisions are respected: a rejected credit is not re-linked by a later run.
Deterministic: the provider is a fake, and nothing here contacts the live service.
"""
import json
import threading
import time

import pytest

from meloshelf.application import enrichment_api
from meloshelf.application.webapp import create_server
from meloshelf.domain.artist_identity import ArtistCandidate, decide_link
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request


class FastClient:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def search_artists(self, name, *, limit=5):
        self.calls.append(name)
        return self.answers.get(name, [{'mb_artist_id': 'mb-' + name.lower().replace(' ', '-'), 'name': name,
                                        'score': 100, 'aliases': [], 'disambiguation': ''}])


class SlowClient(FastClient):
    def __init__(self, delay=0.3):
        super().__init__()
        self.delay = delay

    def search_artists(self, name, *, limit=5):
        time.sleep(self.delay)
        return super().search_artists(name, limit=limit)


def make_server(tmp_path, client=None, environ=None):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ=environ if environ is not None else {}, artist_client=client)
    names = ['Adele', 'Queen', 'Prince', 'Tauren Wells']
    LibraryRepository(running.database).save_many([Track(f'/{i}.mp3', 1, '.mp3', 1, 'T', n, 'A', '', 'tagged')
                                                   for i, n in enumerate(names)])
    return running


def wait_until(predicate, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_automatic_enrichment_is_off_by_default_and_a_scan_starts_nothing(tmp_path):
    client = FastClient()
    running = make_server(tmp_path, client)
    try:
        enrichment_api.after_scan(running)
        assert client.calls == []
        assert enrichment_api.status(running, None)[1]['status'] == 'none'
    finally:
        running.server_close()


def test_when_enabled_a_completed_scan_starts_one_bounded_job(tmp_path):
    client = FastClient()
    running = make_server(tmp_path, client)
    try:
        running.store.set_setting(enrichment_api.SETTING, True)
        enrichment_api.after_scan(running)
        assert wait_until(lambda: enrichment_api.status(running, None)[1].get('status') == 'done')
        assert sorted(client.calls) == ['Adele', 'Prince', 'Queen', 'Tauren Wells']
    finally:
        running.server_close()


def test_the_automatic_run_is_bounded_per_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(enrichment_api, 'AUTO_LIMIT', 2)
    client = FastClient()
    running = make_server(tmp_path, client)
    try:
        running.store.set_setting(enrichment_api.SETTING, True)
        enrichment_api.after_scan(running)
        assert wait_until(lambda: enrichment_api.status(running, None)[1].get('status') == 'done')
        assert len(client.calls) == 2
    finally:
        running.server_close()


def test_without_a_contact_or_client_an_enabled_scan_sends_nothing(tmp_path):
    running = make_server(tmp_path, client=None, environ={})
    try:
        running.store.set_setting(enrichment_api.SETTING, True)
        assert enrichment_api.after_scan(running)['started'] is False
        assert enrichment_api.status(running, None)[1]['status'] == 'none'
    finally:
        running.server_close()


def test_a_second_automatic_run_does_not_start_while_one_is_running(tmp_path):
    client = SlowClient(delay=0.5)
    running = make_server(tmp_path, client)
    try:
        running.store.set_setting(enrichment_api.SETTING, True)
        first = enrichment_api.after_scan(running)
        second = enrichment_api.after_scan(running)
        assert first['started'] is True and second['started'] is False
        assert second['reason'].startswith('An enrichment job is already running')
    finally:
        running.server_close()


def test_a_later_scan_does_not_repeat_identities_already_resolved(tmp_path):
    client = FastClient()
    running = make_server(tmp_path, client)
    try:
        running.store.set_setting(enrichment_api.SETTING, True)
        enrichment_api.after_scan(running)
        assert wait_until(lambda: enrichment_api.status(running, None)[1].get('status') == 'done')
        first_calls = len(client.calls)
        enrichment_api.after_scan(running)
        assert wait_until(lambda: enrichment_api.status(running, None)[1].get('status') == 'done')
        assert len(client.calls) == first_calls
    finally:
        running.server_close()


def test_an_ambiguous_result_goes_to_suggestions_and_is_not_merged(tmp_path):
    two = [{'mb_artist_id': 'mb-1', 'name': 'Prince', 'score': 100, 'aliases': [], 'disambiguation': ''},
           {'mb_artist_id': 'mb-2', 'name': 'Prince', 'score': 100, 'aliases': [], 'disambiguation': ''}]
    client = FastClient({'Prince': two})
    running = make_server(tmp_path, client)
    try:
        running.store.set_setting(enrichment_api.SETTING, True)
        enrichment_api.after_scan(running)
        assert wait_until(lambda: enrichment_api.status(running, None)[1].get('status') == 'done')
        assert ArtistLinks(running.database).get('Prince')['status'] == 'ambiguous'
        assert 'Prince' in [s['credit'] for s in SuggestionDecisions(running.database).open_suggestions()]
        artists, _ = LibraryRepository(running.database).artist_identities(limit=300)
        assert all(a['mb_artist_id'] is None for a in artists if a['name'] == 'Prince')
    finally:
        running.server_close()


def test_a_human_rejection_is_not_undone_by_a_later_automatic_run(tmp_path):
    client = FastClient()
    running = make_server(tmp_path, client)
    try:
        ArtistLinks(running.database).record('Queen', decide_link('Queen', [ArtistCandidate('mb-queen', 'Queen', 100)]),
                                             looked_up_at=1.0)
        SuggestionDecisions(running.database).decide('Queen', 'reject', mb_artist_id='mb-queen', albums=['A'])
        running.store.set_setting(enrichment_api.SETTING, True)
        enrichment_api.after_scan(running)
        assert wait_until(lambda: enrichment_api.status(running, None)[1].get('status') == 'done')
        assert 'Queen' not in client.calls, 'a stored answer is reused, not requested again'
        assert SuggestionDecisions(running.database).effective('Queen', 'A') == ('rejected', 'mb-queen')
    finally:
        running.server_close()


def test_the_setting_is_read_and_written_through_the_token_protected_api(tmp_path):
    running = make_server(tmp_path, FastClient())
    base = f'http://127.0.0.1:{running.server_address[1]}'
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        assert json.loads(request(base, 'GET', '/api/artist-enrichment/settings')[2])['auto_enrich'] is False
        assert request(base, 'POST', '/api/artist-enrichment/settings', {'auto_enrich': True})[0] == 403
        status, _, body = request(base, 'POST', '/api/artist-enrichment/settings', {'auto_enrich': True},
                                  token=running.token)
        assert status == 200 and json.loads(body)['auto_enrich'] is True
        assert request(base, 'POST', '/api/artist-enrichment/settings', {'auto_enrich': 'yes'},
                       token=running.token)[0] == 400
    finally:
        running.shutdown()
        running.server_close()


def test_a_completed_scan_calls_the_after_scan_hook_once_and_a_failing_hook_does_not_fail_the_scan(tmp_path):
    from meloshelf.application.jobs import JobService, _Control
    from meloshelf.infrastructure.database import Database
    database = Database(tmp_path / 'jobs.sqlite3')
    database.initialize()
    calls = []

    def hook(job_id):
        calls.append(job_id)
        raise RuntimeError('a follow-on step that breaks')

    service = JobService(database, on_scan_completed=hook)
    job_id = service.store.create('scan', root=str(tmp_path))
    service._finish(job_id, _Control('scan', 0.0, time.time), None)
    assert calls == [job_id]
    assert service.store.get(job_id)['state'] == 'completed', 'the scan stays completed although the hook failed'


# ── the batch limit is not a permanent cutoff ───────────────────────────────────

def _many_names(db, count):
    LibraryRepository(db).save_many([Track(f'/m{i}.mp3', 1, '.mp3', 1, 'T', f'Band {i:04d}', 'A', '', 'tagged')
                                     for i in range(count)])


def test_repeated_automatic_runs_work_through_a_library_larger_than_one_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(enrichment_api, 'AUTO_LIMIT', 40)
    client = FastClient()
    running = make_server(tmp_path, client)
    try:
        _many_names(running.database, 96)            # 100 names in all with the four from make_server
        running.store.set_setting(enrichment_api.SETTING, True)
        runs = 0
        while enrichment_api.remaining(running) > 0 and runs < 10:
            result = enrichment_api.after_scan(running)
            assert result['started'] is True and result['planned'] <= 40
            assert wait_until(lambda: not enrichment_api._worker_busy(running))
            runs += 1
        assert runs == 3, 'one hundred names in batches of forty'
        assert enrichment_api.remaining(running) == 0
        assert len(client.calls) == 100 and len(set(client.calls)) == 100, 'no name is looked up twice'
    finally:
        running.server_close()


def test_the_settings_endpoint_reports_how_many_names_still_have_no_stored_answer(tmp_path):
    running = make_server(tmp_path, FastClient())
    base = f'http://127.0.0.1:{running.server_address[1]}'
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        assert json.loads(request(base, 'GET', '/api/artist-enrichment/settings')[2])['remaining'] == 4
        ArtistLinks(running.database).record('Adele', decide_link('Adele', [ArtistCandidate('mb-a', 'Adele', 100)]),
                                             looked_up_at=1.0)
        assert json.loads(request(base, 'GET', '/api/artist-enrichment/settings')[2])['remaining'] == 3
    finally:
        running.shutdown()
        running.server_close()
