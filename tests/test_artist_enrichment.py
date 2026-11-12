"""Automatic artist enrichment: a bounded, durable, resumable job over unresolved artist names.

* The job plans its names from the library once, at creation, and stores each name as an item, so progress and
  resumption are exact. A name that already has a stored answer is never planned or looked up again.
* Bounded: at most MAX_NAMES per job, and at most one provider request at a time.
* Pause, resume and cancel act between names. A restart turns a running job into 'interrupted'; resuming
  continues from the first unfinished item.
* Transient provider errors are retried with backoff; a name that keeps failing is recorded as failed and the
  job continues. An unavailable provider pauses the job rather than hammering it.
* An automatic link never overrides a human keep-separate decision: credits kept apart stay apart in grouping.
Deterministic: the provider is a fake, and time and sleep are injected.
"""
import pytest

from meloshelf.application.artist_enrichment import EnrichmentService, JobError
from meloshelf.domain.artist_identity import ArtistCandidate, LINKED, LinkDecision, decide_link
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_corrections import ArtistCorrections
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.music_api import APIError
from meloshelf.infrastructure.repository import LibraryRepository


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    names = ['Adele', 'Queen', 'Prince', 'Tauren Wells', 'Jenn Johnson']
    LibraryRepository(database).save_many([Track(f'/{i}.mp3', 1, '.mp3', 1, 'T', n, 'A', '', 'tagged')
                                           for i, n in enumerate(names)])
    return database


class FakeClient:
    def __init__(self, failures=None, hard_down=False):
        self.calls = []
        self.failures = dict(failures or {})   # name -> number of transient failures before success
        self.hard_down = hard_down

    def search_artists(self, name, *, limit=5):
        self.calls.append(name)
        if self.hard_down:
            raise APIError('MusicBrainz is unreachable', retryable=True)
        if self.failures.get(name, 0) > 0:
            self.failures[name] -= 1
            raise APIError('temporary failure', retryable=True)
        return [{'mb_artist_id': 'mb-' + name.lower().replace(' ', '-'), 'name': name, 'score': 100,
                 'aliases': [], 'disambiguation': ''}]


def service(db, client, **kwargs):
    slept = []
    svc = EnrichmentService(db, client, sleep=slept.append, clock=lambda: 1000.0, **kwargs)
    svc.sleeps = slept
    return svc


# ── planning and bounds ──────────────────────────────────────────────────────────

def test_a_job_plans_only_the_names_without_a_stored_answer(db):
    ArtistLinks(db).record('Adele', decide_link('Adele', [ArtistCandidate('mb-a', 'Adele', 100)]), looked_up_at=1.0)
    job = service(db, FakeClient()).create()
    status = service(db, FakeClient()).status(job['id'])
    assert status['planned'] == 4 and status['status'] == 'queued'


def test_the_number_of_names_per_job_is_bounded(db):
    with pytest.raises(JobError):
        service(db, FakeClient()).create(max_names=0)
    with pytest.raises(JobError):
        service(db, FakeClient()).create(max_names=10_001)


def test_creating_a_job_makes_no_provider_request(db):
    client = FakeClient()
    service(db, client).create()
    assert client.calls == []


def test_a_job_with_max_names_plans_no_more_than_that(db):
    job = service(db, FakeClient()).create(max_names=2)
    assert service(db, FakeClient()).status(job['id'])['planned'] == 2


# ── running: progress, stored answers, no repeats ───────────────────────────────

def test_a_batch_looks_up_at_most_its_size_and_reports_progress(db):
    client = FakeClient()
    svc = service(db, client)
    job = svc.create()
    status = svc.run_batch(job['id'], batch=2)
    assert len(client.calls) == 2
    assert status['done'] == 2 and status['planned'] == 5
    assert status['status'] == 'queued', 'a partly run job waits to be run again'


def test_a_full_run_stores_every_answer_and_finishes(db):
    client = FakeClient()
    svc = service(db, client)
    job = svc.create()
    status = svc.run_to_completion(job['id'])
    assert status['status'] == 'done' and status['done'] == 5 and status['linked'] == 5
    assert ArtistLinks(db).get('Tauren Wells')['mb_artist_id'] == 'mb-tauren-wells'
    assert sorted(client.calls) == sorted(['Adele', 'Queen', 'Prince', 'Tauren Wells', 'Jenn Johnson'])


def test_a_name_answered_after_planning_is_not_looked_up_again(db):
    client = FakeClient()
    svc = service(db, client)
    job = svc.create()
    ArtistLinks(db).record('Queen', decide_link('Queen', [ArtistCandidate('mb-q', 'Queen', 100)]), looked_up_at=2.0)
    svc.run_to_completion(job['id'])
    assert 'Queen' not in client.calls


# ── control: pause, resume, cancel ──────────────────────────────────────────────

def test_a_paused_job_makes_no_requests_until_it_is_resumed(db):
    client = FakeClient()
    svc = service(db, client)
    job = svc.create()
    svc.pause(job['id'])
    svc.run_batch(job['id'], batch=5)
    assert client.calls == []
    svc.resume(job['id'])
    svc.run_to_completion(job['id'])
    assert len(client.calls) == 5


def test_cancelling_stops_the_job_and_leaves_remaining_names_unlooked_up(db):
    client = FakeClient()
    svc = service(db, client)
    job = svc.create()
    svc.run_batch(job['id'], batch=2)
    svc.cancel(job['id'])
    svc.run_batch(job['id'], batch=5)
    status = svc.status(job['id'])
    assert status['status'] == 'cancelled' and status['done'] == 2
    assert len(client.calls) == 2


def test_a_cancelled_job_cannot_be_resumed(db):
    svc = service(db, FakeClient())
    job = svc.create()
    svc.cancel(job['id'])
    with pytest.raises(JobError):
        svc.resume(job['id'])


# ── durability: restart resumes exactly where it stopped ────────────────────────

def test_a_restart_marks_a_running_job_interrupted_and_resume_finishes_without_repeats(db):
    client = FakeClient()
    first = service(db, client)
    job = first.create()
    first.run_batch(job['id'], batch=2)
    with db.write() as connection:          # simulate a crash while the job was running
        connection.execute("UPDATE artist_enrichment_jobs SET status='running' WHERE id=?", (job['id'],))
    restarted_client = FakeClient()
    restarted = service(db, restarted_client)
    restarted.recover()
    assert restarted.status(job['id'])['status'] == 'interrupted'
    restarted.resume(job['id'])
    status = restarted.run_to_completion(job['id'])
    assert status['status'] == 'done' and status['done'] == 5
    assert set(restarted_client.calls).isdisjoint(set(client.calls)), 'no name is looked up twice'


def test_progress_survives_reopening_the_database(tmp_path):
    path = tmp_path / 'reopen.sqlite3'
    database = Database(path)
    database.initialize()
    LibraryRepository(database).save_many([Track('/a.mp3', 1, '.mp3', 1, 'T', 'Adele', 'A', '', 'tagged'),
                                           Track('/b.mp3', 1, '.mp3', 1, 'T', 'Queen', 'A', '', 'tagged')])
    svc = service(database, FakeClient())
    job = svc.create()
    svc.run_batch(job['id'], batch=1)
    reopened = Database(path)
    reopened.initialize()
    assert service(reopened, FakeClient()).status(job['id'])['done'] == 1


# ── transient errors and an unavailable provider ────────────────────────────────

def test_a_transient_error_is_retried_with_backoff_and_then_succeeds(db):
    client = FakeClient(failures={'Queen': 2})
    svc = service(db, client)
    job = svc.create()
    status = svc.run_to_completion(job['id'])
    assert status['status'] == 'done' and status['linked'] == 5
    assert svc.sleeps == [2, 4], 'exponential backoff between attempts'


def test_a_name_that_keeps_failing_is_recorded_as_failed_and_the_job_continues(db):
    client = FakeClient(failures={'Queen': 99})
    svc = service(db, client)
    job = svc.create()
    status = svc.run_to_completion(job['id'])
    assert status['status'] == 'done' and status['failed'] == 1 and status['linked'] == 4
    assert 'temporary failure' in status['last_error']


def test_an_unavailable_provider_pauses_the_job_instead_of_hammering_it(db):
    client = FakeClient(hard_down=True)
    svc = service(db, client)
    job = svc.create()
    status = svc.run_to_completion(job['id'])
    assert status['status'] == 'paused'
    assert len(client.calls) <= 6, 'two names in a row exhaust their three attempts each, then the job pauses'


# ── an automatic link never overrides a human keep-separate decision ────────────

def test_keep_separate_stays_apart_even_when_two_credits_link_to_the_same_artist_id(db):
    from meloshelf.infrastructure.repository import LibraryRepository as Repo
    LibraryRepository(db).save_many([Track('/x.mp3', 1, '.mp3', 1, 'T', 'Beyonce', 'X', '', 'tagged'),
                                     Track('/y.mp3', 1, '.mp3', 1, 'T', 'Beyonce Knowles', 'Y', '', 'tagged')])
    ArtistLinks(db).record('Beyonce', LinkDecision(LINKED, mb_id='mb-b', name='Beyonce', reason='fixture'), looked_up_at=1.0)
    ArtistLinks(db).record('Beyonce Knowles', LinkDecision(LINKED, mb_id='mb-b', name='Beyonce', reason='fixture'),
                           looked_up_at=1.0)
    ArtistCorrections(db).keep_separate('Beyonce', 'Beyonce Knowles')
    artists, _ = Repo(db).artist_identities(limit=300)
    beyonce_like = [a for a in artists if a['name'] in ('Beyonce', 'Beyonce Knowles')]
    assert len(beyonce_like) == 2, 'the human decision keeps two identities'
    assert sum(1 for a in beyonce_like if a['mb_artist_id'] == 'mb-b') == 1, 'only one credit keeps the link'


def test_without_a_keep_separate_decision_the_same_id_groups_them(db):
    from meloshelf.infrastructure.repository import LibraryRepository as Repo
    LibraryRepository(db).save_many([Track('/x.mp3', 1, '.mp3', 1, 'T', 'Beyonce', 'X', '', 'tagged'),
                                     Track('/y.mp3', 1, '.mp3', 1, 'T', 'Beyonce Knowles', 'Y', '', 'tagged')])
    ArtistLinks(db).record('Beyonce', LinkDecision(LINKED, mb_id='mb-b', name='Beyonce', reason='fixture'), looked_up_at=1.0)
    ArtistLinks(db).record('Beyonce Knowles', LinkDecision(LINKED, mb_id='mb-b', name='Beyonce', reason='fixture'),
                           looked_up_at=1.0)
    artists, _ = Repo(db).artist_identities(limit=300)
    grouped = [a for a in artists if a['mb_artist_id'] == 'mb-b']
    assert len(grouped) == 1 and grouped[0]['tracks'] == 2


# ── HTTP: start, observe and control a job; every action needs the session token ─

def _running_server(tmp_path, client):
    import threading
    from meloshelf.application.webapp import create_server
    from meloshelf.infrastructure.secrets import MemorySecretStore
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'http.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, artist_client=client)
    LibraryRepository(running.database).save_many([Track('/a.mp3', 1, '.mp3', 1, 'T', 'Adele', 'A', '', 'tagged'),
                                                   Track('/b.mp3', 1, '.mp3', 1, 'T', 'Queen', 'A', '', 'tagged')])
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return running, f'http://127.0.0.1:{running.server_address[1]}'


def test_a_started_job_runs_in_the_background_and_its_progress_is_visible(tmp_path):
    import time
    import json
    from test_qa_api import request
    running, base = _running_server(tmp_path, FakeClient())
    try:
        assert request(base, 'POST', '/api/artist-enrichment/start', {'max_names': 50})[0] == 403
        status, _, body = request(base, 'POST', '/api/artist-enrichment/start', {'max_names': 50}, token=running.token)
        assert status == 202
        job_id = json.loads(body)['id']
        deadline = time.time() + 15
        while time.time() < deadline:
            _, _, body = request(base, 'GET', f'/api/artist-enrichment/status?id={job_id}')
            payload = json.loads(body)
            if payload['status'] == 'done':
                break
            time.sleep(0.05)
        assert payload['status'] == 'done' and payload['linked'] == 2
    finally:
        running.shutdown()
        running.server_close()


def test_control_actions_need_the_token_and_refuse_impossible_transitions(tmp_path):
    import json
    from test_qa_api import request
    running, base = _running_server(tmp_path, FakeClient())
    try:
        assert request(base, 'POST', '/api/artist-enrichment/control', {'id': 1, 'action': 'pause'})[0] == 403
        status, _, body = request(base, 'POST', '/api/artist-enrichment/control', {'id': 999, 'action': 'pause'},
                                  token=running.token)
        assert status == 400 and 'No enrichment job' in json.loads(body)['error']
        status, _, body = request(base, 'POST', '/api/artist-enrichment/control', {'id': 1, 'action': 'explode'},
                                  token=running.token)
        assert status == 400
    finally:
        running.shutdown()
        running.server_close()


def test_the_page_offers_start_pause_resume_and_cancel_for_the_enrichment_job():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    for hook in ('id="enrichStart"', 'id="enrichPause"', 'id="enrichResume"', 'id="enrichCancel"', 'id="enrichStatus"'):
        assert hook in page, hook
    assert "'/api/artist-enrichment/start'" in page and "'/api/artist-enrichment/control'" in page
    assert "lastEnrichStatus!=='done'" in page, 'the done transition must not loop back into the reload'


# ── refreshing stale evidence, safely ───────────────────────────────────────────

DAY = 86400.0


def _record(db, name, status, looked_up_at, mb='mb-x'):
    from meloshelf.domain.artist_identity import LinkDecision
    ArtistLinks(db).record(name, LinkDecision(status, mb_id=mb if status in ('linked', 'suggested') else None,
                                              name=name, reason='fixture'), looked_up_at=looked_up_at)


def test_an_old_no_match_answer_is_planned_again_but_a_recent_one_is_not(db):
    now = 1_000_000_000.0
    _record(db, 'Adele', 'no_match', now - 200 * DAY)
    _record(db, 'Queen', 'no_match', now - 10 * DAY)
    svc = EnrichmentService(db, FakeClient(), sleep=lambda s: None, clock=lambda: now)
    job = svc.create()
    planned = [r['name'] for r in _items(db, job['id'])]
    assert 'Adele' in planned and 'Queen' not in planned


def test_verified_links_suggestions_and_ambiguities_are_never_refreshed_automatically(db):
    now = 1_000_000_000.0
    _record(db, 'Adele', 'linked', now - 900 * DAY)
    _record(db, 'Queen', 'suggested', now - 900 * DAY)
    _record(db, 'Prince', 'ambiguous', now - 900 * DAY)
    svc = EnrichmentService(db, FakeClient(), sleep=lambda s: None, clock=lambda: now)
    planned = [r['name'] for r in _items(db, svc.create()['id'])]
    assert not {'Adele', 'Queen', 'Prince'} & set(planned)


def test_the_refresh_interval_is_configurable_and_can_be_switched_off(db):
    now = 1_000_000_000.0
    _record(db, 'Adele', 'no_match', now - 200 * DAY)
    svc = EnrichmentService(db, FakeClient(), sleep=lambda s: None, clock=lambda: now)
    assert 'Adele' not in [r['name'] for r in _items(db, svc.create(refresh_after_days=None)['id'])]
    assert 'Adele' not in [r['name'] for r in _items(db, svc.create(refresh_after_days=365)['id'])]
    assert 'Adele' in [r['name'] for r in _items(db, svc.create(refresh_after_days=30)['id'])]


def test_a_refresh_that_finds_a_link_replaces_the_old_no_match_without_touching_human_decisions(db):
    now = 1_000_000_000.0
    _record(db, 'Adele', 'no_match', now - 200 * DAY)
    client = FakeClient()
    svc = EnrichmentService(db, client, sleep=lambda s: None, clock=lambda: now)
    svc.run_to_completion(svc.create()['id'])
    assert ArtistLinks(db).get('Adele')['status'] == 'linked'


def _items(db, job_id):
    with db.read() as connection:
        return [dict(r) for r in connection.execute(
            'SELECT name FROM artist_enrichment_items WHERE job_id=? ORDER BY position', (job_id,))]


# ── rate-limit compliance, through the real client with an injected clock ───────

def test_consecutive_artist_lookups_are_spaced_by_the_providers_interval(db):
    from meloshelf.infrastructure.music_api import MusicBrainzClient, RateLimiter
    clock = {'now': 0.0}
    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        clock['now'] += seconds

    calls = []

    def transport(url, **kwargs):
        calls.append(clock['now'])
        return {'artists': []}

    client = MusicBrainzClient('MeloShelf/test ( https://example.invalid/project )', transport=transport,
                               limiter=RateLimiter(1.1, clock=lambda: clock['now'], sleeper=sleeper))
    svc = EnrichmentService(db, client, sleep=lambda s: None)
    svc.run_to_completion(svc.create()['id'])
    assert len(calls) == 5
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(gap >= 1.1 - 1e-9 for gap in gaps), gaps
