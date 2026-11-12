"""Durable scan and identification jobs: persisted state, control, recovery, diagnostics."""
import hashlib
import threading

import pytest

from meloshelf.application.jobs import JobService
from meloshelf.application.recognition import RecognitionReport, RecognitionStatus
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.job_store import JobStore
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import OperationCancelled
from meloshelf.infrastructure.repository import LibraryRepository

WAIT = 20


class SimulatedCrash(BaseException):
    """Kills a worker thread without running any of its error handling, like a process crash."""


@pytest.fixture(autouse=True)
def quiet_thread_crashes(monkeypatch):
    monkeypatch.setattr(threading, 'excepthook', lambda args: None)


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    return database


def make_library(tmp_path, count=12, name='music'):
    root = tmp_path / name
    (root / 'sub').mkdir(parents=True)
    for n in range(count):
        folder = root if n % 2 else root / 'sub'
        (folder / f'{n:04}.mp3').write_bytes(f'audio {n}'.encode())
    return root


def snapshot(root):
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob('*')) if p.is_file()}


class CountingReader:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook

    def __call__(self, path):
        self.calls.append(str(path))
        if self.hook:
            self.hook(path, len(self.calls))
        return {'title': 'T', 'artist': 'A', 'album': 'B'}


class FakeRecognition:
    """Stands in for RecognitionService; outcome chosen per file name."""
    def __init__(self, outcomes=None, default='review', hook=None):
        self.outcomes = outcomes or {}
        self.default = default
        self.calls = []
        self.hook = hook
        self.store = None

    def identify(self, path, *, tags=None, on_stage=None):
        self.calls.append(path.name)
        if on_stage:
            on_stage('FINGERPRINTING')
        if self.hook:
            self.hook(path, len(self.calls))
        outcome = self.outcomes.get(path.name, self.default)
        if outcome == 'cancel':
            raise OperationCancelled('stop')
        if outcome == 'review':
            stat = path.stat()
            self.store.put_result(path, stat.st_size, stat.st_mtime_ns, 'review', {'candidates': [], 'reason': 'ok'})
            return RecognitionReport(str(path), RecognitionStatus.REVIEW)
        if outcome == 'no_match':
            stat = path.stat()
            self.store.put_result(path, stat.st_size, stat.st_mtime_ns, 'unmatched', {'candidates': [], 'reason': 'none'})
            return RecognitionReport(str(path), RecognitionStatus.UNMATCHED, code='no_match')
        return RecognitionReport(str(path), RecognitionStatus.ERROR, code=outcome, reason=f'detail for {outcome}')


def make_service(database, *, reader=None, recognition=None, preflight=None, **options):
    recognition = recognition or FakeRecognition()
    recognition.store = MatchStore(database)
    return JobService(database, tag_reader=reader or CountingReader(),
                      recognition_factory=lambda offline, should_cancel: recognition,
                      preflight=preflight or (lambda offline: ''), **options)


def job_of(service, kind='scan'):
    return next(j for j in service.activity()['jobs'] if j['kind'] == kind)


def run_scan(service, root):
    result = service.start_scan(str(root))
    assert service.wait(result['job_id'], WAIT)
    return result['job_id']


def run_identify(service, limit=100, offline=False):
    result = service.start_identify(limit, offline)
    assert result['started'], result
    assert service.wait(result['job_id'], WAIT)
    return result['job_id']


# ── scanning ─────────────────────────────────────────────────────────────────

def test_scan_job_is_persisted_with_folder_counts_and_final_state(tmp_path, database):
    root = make_library(tmp_path, 12)
    service = make_service(database)
    job_id = run_scan(service, root)
    job = job_of(service)
    assert job['id'] == job_id and len(job_id) >= 8
    assert job['state'] == 'COMPLETED'
    assert job['root'] == str(root.resolve())
    assert job['counters']['total'] == 12 and job['counters']['done'] == 12 and job['counters']['pending'] == 0
    assert job['percent'] == 100
    assert job['elapsed_seconds'] >= 0
    assert LibraryRepository(database).counts()['total'] == 12
    assert JobStore(database).get(job_id)['state'] == 'completed', 'state must survive in the database'


def test_activity_reports_the_selected_folder_and_library_totals(tmp_path, database):
    service = make_service(database)
    assert service.activity()['state'] == 'IDLE'
    assert service.activity()['library']['root'] == ''
    root = make_library(tmp_path, 6)
    run_scan(service, root)
    activity = service.activity()
    assert activity['library']['root'] == str(root.resolve())
    assert activity['library']['indexed'] == 6
    assert activity['library']['awaiting_identification'] == 6
    assert activity['library']['identified'] == 0 and activity['library']['unknown'] == 0
    assert activity['state'] == 'COMPLETED'


def test_stages_are_visible_while_scanning(tmp_path, database):
    root = make_library(tmp_path, 6)
    seen = []
    release = threading.Event()

    def hook(path, n):
        if n == 3:
            seen.append(service.activity())
            release.set()

    service = make_service(database, reader=CountingReader(hook), batch_size=2)
    run_scan(service, root)
    assert release.is_set()
    running = seen[0]
    job = running['jobs'][0]
    assert running['state'] == 'INDEXING' and job['state'] == 'INDEXING'
    assert job['counters']['total'] == 6, 'discovery is complete before indexing starts'
    assert job['counters']['done'] == 2, 'progress comes from committed batches'
    assert job['current_file']
    assert job['can'] == {'pause': True, 'resume': False, 'cancel': True, 'retry': False}


def test_incremental_scan_skips_unchanged_files_and_rereads_changed_ones(tmp_path, database):
    root = make_library(tmp_path, 8)
    reader = CountingReader()
    service = make_service(database, reader=reader)
    run_scan(service, root)
    assert len(reader.calls) == 8
    changed = root / '0001.mp3'
    changed.write_bytes(b'different content, different size')
    (root / 'new.mp3').write_bytes(b'new')
    reader.calls.clear()
    run_scan(service, root)
    job = job_of(service)
    assert sorted(p.rsplit('\\', 1)[-1].rsplit('/', 1)[-1] for p in reader.calls) == ['0001.mp3', 'new.mp3']
    assert job['counters']['skipped'] == 7 and job['counters']['done'] == 2
    assert LibraryRepository(database).counts()['total'] == 9


def test_deleted_files_leave_the_index_on_the_next_scan(tmp_path, database):
    root = make_library(tmp_path, 5)
    service = make_service(database)
    run_scan(service, root)
    (root / '0001.mp3').unlink()  # the user deletes a file themselves, outside MeloShelf
    run_scan(service, root)
    assert LibraryRepository(database).counts()['total'] == 4
    assert job_of(service)['removed_from_index'] == 1


def test_file_changed_while_being_read_is_reported_not_indexed(tmp_path, database):
    root = make_library(tmp_path, 3)

    def hook(path, n):
        if path.name == '0001.mp3':
            path.write_bytes(b'rewritten by another program during the scan')

    service = make_service(database, reader=CountingReader(hook))
    run_scan(service, root)
    job = job_of(service)
    assert job['state'] == 'COMPLETED_WITH_ERRORS'
    assert job['counters']['error'] == 1 and job['counters']['done'] == 2
    assert [(e['code'], e['count']) for e in job['errors']] == [('file_changed', 1)]
    assert LibraryRepository(database).counts()['total'] == 2


def test_one_unreadable_file_does_not_stop_the_scan(tmp_path, database):
    root = make_library(tmp_path, 4)

    def hook(path, n):
        if path.name == '0002.mp3':
            raise PermissionError('access denied')

    service = make_service(database, reader=CountingReader(hook))
    run_scan(service, root)
    job = job_of(service)
    assert job['counters']['done'] == 3 and job['counters']['error'] == 1
    assert job['errors'][0]['code'] == 'file_unreadable'
    details = service.error_items(job['id'], 'file_unreadable')
    assert details[0]['path'].endswith('0002.mp3') and details[0]['detail'] == 'PermissionError'


def test_same_folder_is_not_queued_twice(tmp_path, database):
    root = make_library(tmp_path, 6)
    gate = threading.Event()
    service = make_service(database, reader=CountingReader(lambda p, n: gate.wait(WAIT)), batch_size=2)
    first = service.start_scan(str(root))
    second = service.start_scan(str(root))
    other = service.start_scan(str(make_library(tmp_path, 2, 'other')))
    gate.set()
    assert service.wait(first['job_id'], WAIT)
    assert second == {'started': False, 'job_id': first['job_id'], 'resumed': False,
                      'reason': 'This folder is already being scanned'}
    assert other['started'] is False and 'already running' in other['reason']
    assert len([j for j in JobStore(database).list_recent(10) if j['kind'] == 'scan']) == 1


# ── pause, resume, cancel ────────────────────────────────────────────────────

def test_pause_then_resume_processes_every_file_exactly_once(tmp_path, database):
    root = make_library(tmp_path, 10)
    reader = CountingReader()
    service = make_service(database, reader=reader, batch_size=2)
    reader.hook = lambda path, n: service.pause(job_id[0]) if n == 4 else None
    job_id = [None]
    started = service.start_scan(str(root))
    job_id[0] = started['job_id']
    assert service.wait(job_id[0], WAIT)
    paused = job_of(service)
    assert paused['state'] == 'PAUSED'
    assert 0 < paused['counters']['done'] < 10
    assert paused['can'] == {'pause': False, 'resume': True, 'cancel': True, 'retry': False}

    reader.hook = None
    assert service.resume(job_id[0])['started']
    assert service.wait(job_id[0], WAIT)
    done = job_of(service)
    assert done['state'] == 'COMPLETED' and done['counters']['done'] == 10
    assert sorted(reader.calls) == sorted(set(reader.calls)), 'no file was read twice'
    assert LibraryRepository(database).counts()['total'] == 10


def test_cancel_leaves_a_consistent_terminal_state(tmp_path, database):
    root = make_library(tmp_path, 10)
    reader = CountingReader()
    service = make_service(database, reader=reader, batch_size=2)
    holder = {}
    reader.hook = lambda path, n: service.cancel(holder['id']) if n == 4 else None
    holder['id'] = service.start_scan(str(root))['job_id']
    assert service.wait(holder['id'], WAIT)
    job = job_of(service)
    counters = job['counters']
    assert job['state'] == 'CANCELLED'
    assert counters['pending'] == 0, 'nothing may stay pending in a terminal job'
    assert counters['done'] + counters['cancelled'] == counters['total'] == 10
    assert LibraryRepository(database).counts()['total'] == counters['done']
    assert job['can'] == {'pause': False, 'resume': False, 'cancel': False, 'retry': False}
    assert service.resume(holder['id'])['started'] is False


def test_starting_the_same_folder_resumes_a_paused_scan(tmp_path, database):
    root = make_library(tmp_path, 8)
    reader = CountingReader()
    service = make_service(database, reader=reader, batch_size=2)
    holder = {}
    reader.hook = lambda path, n: service.pause(holder['id']) if n == 2 else None
    holder['id'] = service.start_scan(str(root))['job_id']
    assert service.wait(holder['id'], WAIT)
    reader.hook = None
    again = service.start_scan(str(root))
    assert again['job_id'] == holder['id'] and again['resumed'] is True and again['started'] is True
    assert service.wait(holder['id'], WAIT)
    assert job_of(service)['state'] == 'COMPLETED'


# ── crash recovery ───────────────────────────────────────────────────────────

def test_interrupted_scan_is_offered_after_restart_and_resumes_cleanly(tmp_path, database):
    root = make_library(tmp_path, 12)
    before = snapshot(root)

    def crash(path, n):
        if n == 7:
            raise SimulatedCrash()

    first_reader = CountingReader(crash)
    crashed = make_service(database, reader=first_reader, batch_size=3)
    job_id = crashed.start_scan(str(root))['job_id']
    assert crashed.wait(job_id, WAIT)
    assert JobStore(database).get(job_id)['state'] == 'running', 'a crash leaves the job marked running'

    second_reader = CountingReader()
    restarted = make_service(database, reader=second_reader, batch_size=3)  # application restart
    job = job_of(restarted)
    assert job['state'] == 'INTERRUPTED'
    assert job['counters']['done'] == 6 and job['counters']['pending'] == 6
    assert job['can']['resume'] is True
    assert restarted.activity()['state'] == 'INTERRUPTED'

    assert restarted.resume(job_id)['started']
    assert restarted.wait(job_id, WAIT)
    final = job_of(restarted)
    assert final['state'] == 'COMPLETED' and final['counters']['done'] == 12
    assert len(second_reader.calls) == 6, 'files committed before the crash are not processed again'
    assert LibraryRepository(database).counts()['total'] == 12
    assert snapshot(root) == before
    assert database.integrity_check() == []


# ── identification ───────────────────────────────────────────────────────────

def scanned(tmp_path, database, count=6, **options):
    root = make_library(tmp_path, count)
    service = make_service(database, **options)
    run_scan(service, root)
    return service, root


def test_identify_job_tracks_each_file_and_separates_no_match_from_errors(tmp_path, database):
    recognition = FakeRecognition({'0001.mp3': 'no_match', '0002.mp3': 'invalid_audio', '0003.mp3': 'network_error'})
    service, _ = scanned(tmp_path, database, recognition=recognition)
    run_identify(service)
    job = job_of(service, 'identify')
    assert job['state'] == 'COMPLETED_WITH_ERRORS'
    assert job['counters'] == {'total': 6, 'pending': 0, 'done': 3, 'unmatched': 1, 'error': 2,
                               'skipped': 0, 'cancelled': 0}
    errors = {e['code']: e for e in job['errors']}
    assert set(errors) == {'invalid_audio', 'network_error'}, 'a valid no-match is not an error'
    assert errors['network_error']['retryable'] is True and errors['invalid_audio']['retryable'] is False
    assert errors['invalid_audio']['label'] == 'Invalid or unsupported audio' and errors['invalid_audio']['hint']
    library = service.activity()['library']
    assert library['identified'] == 3 and library['unknown'] == 1 and library['awaiting_identification'] == 2


def test_missing_fpcalc_fails_the_job_once_with_an_explanation(tmp_path, database):
    recognition = FakeRecognition()
    service, _ = scanned(tmp_path, database, recognition=recognition,
                         preflight=lambda offline: 'fpcalc_unavailable')
    run_identify(service)
    job = job_of(service, 'identify')
    assert job['state'] == 'FAILED'
    assert job['error']['code'] == 'fpcalc_unavailable'
    assert 'fpcalc' in job['error']['label'] and 'Install' in job['error']['hint']
    assert recognition.calls == [], 'no file is attempted when the tool is missing'
    assert job['counters']['error'] == 0 and job['counters']['pending'] == 6
    assert job['can']['resume'] is True, 'resumable once the problem is fixed'


def test_configuration_error_discovered_mid_run_stops_instead_of_failing_every_file(tmp_path, database):
    recognition = FakeRecognition(default='provider_auth')
    service, _ = scanned(tmp_path, database, recognition=recognition)
    run_identify(service)
    job = job_of(service, 'identify')
    assert job['state'] == 'FAILED' and job['error']['code'] == 'provider_auth'
    assert len(recognition.calls) == 1
    assert job['counters']['pending'] == 6


def test_repeated_network_failures_pause_the_job(tmp_path, database):
    recognition = FakeRecognition(default='network_error')
    service, _ = scanned(tmp_path, database, count=12, recognition=recognition)
    run_identify(service)
    job = job_of(service, 'identify')
    assert job['state'] == 'PAUSED'
    assert len(recognition.calls) == 5
    assert 'network' in job['message'].lower()


def test_retry_failed_requeues_only_retryable_errors(tmp_path, database):
    recognition = FakeRecognition({'0002.mp3': 'invalid_audio', '0003.mp3': 'network_error'})
    service, _ = scanned(tmp_path, database, recognition=recognition)
    job_id = run_identify(service)
    assert job_of(service, 'identify')['can']['retry'] is True
    recognition.outcomes.clear()  # the network is back
    recognition.calls.clear()
    result = service.retry_failed(job_id)
    assert result == {'started': True, 'job_id': job_id, 'requeued': 1}
    assert service.wait(job_id, WAIT)
    job = job_of(service, 'identify')
    assert recognition.calls == ['0003.mp3']
    assert job['counters']['done'] == 5 and job['counters']['error'] == 1
    assert job['can']['retry'] is False


def test_identification_only_queues_files_without_a_current_result(tmp_path, database):
    recognition = FakeRecognition()
    service, root = scanned(tmp_path, database, recognition=recognition)
    run_identify(service, limit=4)
    assert len(recognition.calls) == 4
    recognition.calls.clear()
    run_identify(service, limit=100)
    assert len(recognition.calls) == 2, 'already identified files are not queued again'
    nothing_left = service.start_identify(100, False)
    assert nothing_left['started'] is False and 'Nothing' in nothing_left['reason']


def test_identify_pause_keeps_the_interrupted_file_pending(tmp_path, database):
    recognition = FakeRecognition({'0003.mp3': 'cancel'})
    service, _ = scanned(tmp_path, database, recognition=recognition)
    holder = {}
    recognition.hook = lambda path, n: service.pause(holder['id']) if path.name == '0003.mp3' else None
    holder['id'] = service.start_identify(100, False)['job_id']
    assert service.wait(holder['id'], WAIT)
    job = job_of(service, 'identify')
    assert job['state'] == 'PAUSED'
    assert job['counters']['error'] == 0 and job['counters']['pending'] >= 1


def test_scan_and_identify_can_run_at_the_same_time(tmp_path, database):
    gate = threading.Event()
    recognition = FakeRecognition(hook=lambda path, n: gate.wait(WAIT))
    service, _ = scanned(tmp_path, database, recognition=recognition)
    identify = service.start_identify(100, False)
    other = make_library(tmp_path, 20, 'second')
    scan = service.start_scan(str(other))
    assert scan['started'] and identify['started']
    assert service.wait(scan['job_id'], WAIT)
    gate.set()
    assert service.wait(identify['job_id'], WAIT)
    assert LibraryRepository(database).counts()['total'] == 26
    assert job_of(service, 'identify')['counters']['done'] == 6
    assert database.integrity_check() == []


def test_legacy_status_shape_is_preserved(tmp_path, database):
    service, _ = scanned(tmp_path, database)
    status = service.status()
    assert status['running'] is False and status['kind'] == 'scan'
    assert status['completed'] == 6 and status['errors'] == 0 and status['message']


def test_slow_files_do_not_delay_progress_until_a_full_batch(tmp_path, database):
    """Progress must be committed on a time budget, not only when a large batch fills up."""
    root = make_library(tmp_path, 6)
    now = [1000.0]
    seen = []

    def slow(path, n):
        now[0] += 2.0  # each file takes two seconds on the injected clock
        if n == 3:
            seen.append(job_of(service)['counters']['done'])

    service = make_service(database, reader=CountingReader(slow), batch_size=200, clock=lambda: now[0])
    run_scan(service, root)
    assert seen == [2], 'the first slow files were committed before the batch of 200 filled up'
    assert job_of(service)['counters']['done'] == 6


def test_cancelling_a_failed_job_clears_its_error(tmp_path, database):
    service, _ = scanned(tmp_path, database, preflight=lambda offline: 'fpcalc_unavailable')
    job_id = run_identify(service)
    assert job_of(service, 'identify')['error']['code'] == 'fpcalc_unavailable'
    assert service.cancel(job_id) == {'ok': True}
    job = job_of(service, 'identify')
    assert job['state'] == 'CANCELLED' and job['error'] is None
