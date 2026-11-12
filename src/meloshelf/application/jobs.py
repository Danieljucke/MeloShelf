"""Durable background work: scanning and identification.

Every job and every file it covers is recorded in SQLite, so progress shown to
the user comes from committed state and survives a restart. Slow work (reading
tags, fingerprinting, network calls) always happens outside write transactions.
"""
import logging
import os
import threading
import time
from pathlib import Path

from meloshelf.application.recognition import RecognitionStatus
from meloshelf.application.scanner import LibraryScanner
from meloshelf.domain.outcomes import OUTCOMES, describe
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.job_store import JobStore
from meloshelf.infrastructure.music_api import OperationCancelled
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.tags import read_tags

log = logging.getLogger(__name__)

RESUMABLE = ('paused', 'interrupted', 'failed')
NETWORK_CODES = frozenset({'network_error', 'rate_limited', 'provider_unavailable'})
RETRYABLE_CODES = tuple(code for code, outcome in OUTCOMES.items() if outcome.is_error and outcome.retryable)
PERMANENT_CODES = tuple(code for code, outcome in OUTCOMES.items()
                        if outcome.is_error and not outcome.retryable and not outcome.blocks_job)
MAX_CONSECUTIVE_NETWORK_FAILURES = 5
DISCOVERY_BATCH = 1000
IDENTIFY_BATCH = 20
COMMIT_INTERVAL = 1.0  # seconds
NUMBERS_PER_SCAN = 2000  # unchanged files indexed before track numbers existed, read per scan run (bounded backfill)


class _Control:
    """In-memory handle on a running worker."""
    def __init__(self, kind: str, active_seconds: float, clock):
        self.kind = kind
        self.pause = threading.Event()
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self._base = active_seconds
        self._clock = clock
        self._started = clock()

    def stop_requested(self) -> bool:
        return self.pause.is_set() or self.cancel.is_set()

    def active_seconds(self) -> float:
        return self._base + max(0.0, self._clock() - self._started)


class JobService:
    def __init__(self, database: Database, *, tag_reader=read_tags, recognition_factory=None,
                 preflight=None, batch_size: int = 200, clock=time.time, on_scan_completed=None):
        self.database = database
        self.on_scan_completed = on_scan_completed  # called with the job id after a scan completes; errors are logged
        self.store = JobStore(database, clock=clock)
        self.repo = LibraryRepository(database)
        self.tag_reader = tag_reader
        self.recognition_factory = recognition_factory
        self.preflight = preflight or (lambda offline: '')
        self.batch_size = batch_size
        self._clock = clock
        self._lock = threading.RLock()
        self._controls: dict[str, _Control] = {}
        self._threads: dict[str, threading.Thread] = {}
        interrupted = self.store.mark_interrupted()
        if interrupted:
            log.warning('jobs interrupted by previous shutdown count=%d', interrupted)

    # ── commands ─────────────────────────────────────────────────────────────

    def start_scan(self, folder: str) -> dict:
        root = str(Path(folder).expanduser().resolve())
        with self._lock:
            running = self._running('scan')
            if running:
                same = self.store.get(running)['root'] == root
                return {'started': False, 'job_id': running, 'resumed': False,
                        'reason': 'This folder is already being scanned' if same
                        else 'A scan is already running. Pause or cancel it first.'}
            unfinished = self.store.find_unfinished('scan', root)
            if unfinished:
                self._launch(unfinished)
                return {'started': True, 'job_id': unfinished['id'], 'resumed': True}
            job_id = self.store.create('scan', root=root)
            self._launch(self.store.get(job_id))
            return {'started': True, 'job_id': job_id, 'resumed': False}

    def start_identify(self, limit: int, offline: bool, paths: list[str] | None = None) -> dict:
        explicit = paths is not None
        with self._lock:
            running = self._running('identify')
            if running:
                return {'started': False, 'job_id': running, 'resumed': False,
                        'reason': 'Identification is already running.'}
            unfinished = self.store.find_unfinished('identify')
            if unfinished and paths is None:
                self.store.update(unfinished['id'], params={**unfinished['params'], 'offline': offline})
                self._launch(self.store.get(unfinished['id']))
                return {'started': True, 'job_id': unfinished['id'], 'resumed': True}
            if paths is None:
                paths = self.repo.awaiting_identification(limit, skip_codes=PERMANENT_CODES)
            else:
                # An explicit list is a scoped request (the organizer's incomplete files). It never resumes a library-wide
                # identification someone paused or cancelled: that job keeps its own queue and counts.
                paths = self.repo.without_current_result(paths)
            if not paths:
                return {'started': False, 'job_id': '', 'resumed': False, 'nothing_to_do': True,
                        'reason': 'Nothing left to identify: every indexed file already has a result '
                                  '(or the library is empty).'}
            job_id = self.store.create('identify', params={'offline': offline, 'limit': limit, 'scoped': True} if explicit else {'offline': offline, 'limit': limit})
            self.store.add_items(job_id, paths)
            self._launch(self.store.get(job_id))
            return {'started': True, 'job_id': job_id, 'resumed': False}

    def pause(self, job_id: str) -> dict:
        with self._lock:
            control = self._controls.get(job_id)
            if not control:
                return {'ok': False, 'reason': 'This job is not running.'}
            control.pause.set()
        self.store.update(job_id, message='Pausing after the current file…')
        return {'ok': True}

    def pause_all(self) -> None:
        with self._lock:
            running = list(self._controls)
        for job_id in running:
            self.pause(job_id)

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            control = self._controls.get(job_id)
            if control:
                control.cancel.set()
                return {'ok': True}
            job = self.store.get(job_id)
            if not job or job['state'] not in RESUMABLE:
                return {'ok': False, 'reason': 'This job is already finished.'}
            self.store.cancel(job_id)
            return {'ok': True}

    def resume(self, job_id: str) -> dict:
        with self._lock:
            job = self.store.get(job_id)
            if not job or job['state'] not in RESUMABLE:
                return {'started': False, 'job_id': job_id, 'reason': 'This job cannot be resumed.'}
            if self._running(job['kind']):
                return {'started': False, 'job_id': job_id, 'reason': f"Another {job['kind']} job is running."}
            self._launch(job)
            return {'started': True, 'job_id': job_id}

    def retry_failed(self, job_id: str) -> dict:
        with self._lock:
            job = self.store.get(job_id)
            if not job or job_id in self._controls or job['state'] == 'cancelled':
                return {'started': False, 'job_id': job_id, 'requeued': 0,
                        'reason': 'Failed files can be retried once the job is not running.'}
            if self._running(job['kind']):
                return {'started': False, 'job_id': job_id, 'requeued': 0,
                        'reason': f"Another {job['kind']} job is running."}
            requeued = self.store.requeue_errors(job_id, RETRYABLE_CODES)
            if not requeued:
                return {'started': False, 'job_id': job_id, 'requeued': 0, 'reason': 'No failed file can be retried.'}
            self._launch(self.store.get(job_id))
            return {'started': True, 'job_id': job_id, 'requeued': requeued}

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        """Block until the job's worker has stopped. True when it did within the timeout."""
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def error_items(self, job_id: str, code: str | None = None, *, limit: int = 100, offset: int = 0) -> list[dict]:
        return self.store.error_items(job_id, code, limit=limit, offset=offset)

    # ── worker lifecycle ─────────────────────────────────────────────────────

    def _running(self, kind: str) -> str | None:
        return next((job_id for job_id, control in self._controls.items() if control.kind == kind), None)

    def _launch(self, job: dict) -> None:
        control = _Control(job['kind'], job['active_seconds'], self._clock)
        self.store.update(job['id'], state='running', error_code='', message='Starting…')
        thread = threading.Thread(target=self._work, args=(job['id'], control),
                                  name=f"meloshelf-{job['kind']}-{job['id']}", daemon=True)
        control.thread = thread
        self._controls[job['id']] = control
        self._threads[job['id']] = thread
        thread.start()

    def _work(self, job_id: str, control: _Control) -> None:
        try:
            job = self.store.get(job_id)
            runner = self._run_scan if job['kind'] == 'scan' else self._run_identify
            try:
                stopped = runner(job, control)  # None, or (state, error_code, message) to stop early
            except Exception as exc:  # recorded so the user sees why; the job stays resumable
                log.exception('job failed id=%s kind=%s', job_id, job['kind'])
                stopped = ('failed', 'internal_error', type(exc).__name__)  # the detail is in the log
            self._finish(job_id, control, stopped)
        finally:
            with self._lock:
                self._controls.pop(job_id, None)

    def _finish(self, job_id: str, control: _Control, stopped) -> None:
        seconds = control.active_seconds()
        if control.cancel.is_set():
            self.store.cancel(job_id, active_seconds=seconds)
            return
        if stopped:
            state, code, message = stopped
            self.store.update(job_id, state=state, error_code=code, stage='', current_path='',
                              message=message, active_seconds=seconds)
            return
        if control.pause.is_set():
            self.store.update(job_id, state='paused', stage='', current_path='', active_seconds=seconds,
                              message='Paused. Resume to continue where it stopped.')
            return
        job = self.store.get(job_id)
        counters = self.store.counters(job)
        errors = self.store.error_breakdown(job)
        summary = {**(job['summary'] or {}), 'counters': counters, 'errors': errors}
        processed = counters['total'] - counters['pending']
        noun = 'files indexed' if job['kind'] == 'scan' else 'files analysed'
        message = f"{processed:,} {noun}" + (f", {counters['error']:,} with problems" if counters['error'] else '')
        if job['kind'] == 'scan':
            try:  # keep each file's stable identity current (a bounded sample of new or changed files), then bind decisions
                from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions
                from meloshelf.infrastructure.decision_binding import bind_after_scan
                from meloshelf.infrastructure.file_identity import FileRegistry
                summary['file_identity'] = FileRegistry(self.database).reconcile()
                SuggestionDecisions(self.database).backfill_instances()
                bound = bind_after_scan(self.database)
                summary['decisions_transferred'] = bound['transferred']
                summary['decisions_archived'] = bound['archived']
                summary['decisions_awaiting_review'] = bound['awaiting_review']
            except Exception:
                log.exception('file identity reconciliation failed job=%s', job_id)
        self.store.update(job_id, state='completed_with_errors' if counters['error'] else 'completed',
                          stage='', current_path='', message=message, active_seconds=seconds, summary=summary)
        if job['kind'] == 'scan' and self.on_scan_completed is not None:
            try:
                self.on_scan_completed(job_id)
            except Exception:  # a follow-on step must never turn a finished scan into a failed one
                log.exception('after-scan step failed job=%s', job_id)

    # ── scanning ─────────────────────────────────────────────────────────────

    def _run_scan(self, job: dict, control: _Control):
        job_id, stop = job['id'], control.stop_requested
        scanner = LibraryScanner(self.tag_reader)
        if not job['discovery_done']:
            self._discover(job, scanner, control)
            if stop():
                return None
            self.store.update(job_id, discovery_done=1)
        self.store.update(job_id, stage='INDEXING', message='Reading tags…')
        backfill = [NUMBERS_PER_SCAN]
        while not stop():
            paths = self.store.pending_items(job_id, self.batch_size)
            if not paths:
                break
            self._index_batch(job_id, paths, scanner, control, backfill)
        if stop():
            return None
        removed = [p for p in self.repo.paths_not_in_job(job['root'], job_id) if not os.path.exists(p)]
        if removed:
            self.repo.remove(removed)
        self.store.update(job_id, summary={'removed_from_index': len(removed),
                                           'folder_errors': scanner.errors[:50]})
        return None

    def _discover(self, job: dict, scanner: LibraryScanner, control: _Control) -> None:
        job_id = job['id']
        self.store.update(job_id, stage='DISCOVERING', message='Looking for audio files…')
        found = self.store.counters(job)['total']
        batch = []
        for path, _info in scanner.iter_files(Path(job['root']), should_stop=control.stop_requested):
            batch.append(str(path))
            if len(batch) >= DISCOVERY_BATCH:
                found += self.store.add_items(job_id, batch)
                batch.clear()
                self.store.update(job_id, message=f'Discovered {found:,} audio files…',
                                  active_seconds=control.active_seconds())
        if batch:
            self.store.add_items(job_id, batch)

    def _index_batch(self, job_id: str, paths: list[str], scanner: LibraryScanner, control: _Control,
                     backfill: list[int] | None = None) -> None:
        """``backfill`` is the number of unchanged, not-yet-number-indexed files this run may still read (one shared
        counter for the whole run); without it unchanged files are never read again."""
        known = {path: (row['size'], row['mtime_ns'], bool(row['numbers_indexed']))
                 for path, row in self.repo.get_many(paths).items()}
        tracks, updates, last = [], [], ''
        started = self._clock()
        for text in paths:
            # Commit at least once per interval so slow drives still show steady progress.
            if control.stop_requested() or (updates and self._clock() - started >= COMMIT_INTERVAL):
                break
            last = text
            path = Path(text)
            try:
                info = path.stat()
                before = known.get(text)
                if before is not None and before[:2] == (info.st_size, info.st_mtime_ns):
                    if before[2] or not backfill or backfill[0] <= 0:
                        updates.append((text, 'skipped', '', ''))
                        continue
                    backfill[0] -= 1        # unchanged, but its track and disc numbers were never read
                track = scanner.read_track(path, info)
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    updates.append((text, 'error', 'file_changed', 'Modified while its tags were being read'))
                    continue
                tracks.append(track)
                updates.append((text, 'done', '', ''))
            except FileNotFoundError as exc:
                log.info('file missing while indexing (%s, errno=%s)', type(exc).__name__, getattr(exc, 'errno', None))
                updates.append((text, 'error', 'file_missing', ''))
            except OSError as exc:
                log.info('file unreadable while indexing (%s, errno=%s)', type(exc).__name__, getattr(exc, 'errno', None))
                updates.append((text, 'error', 'file_unreadable', type(exc).__name__))
        if not updates:
            return
        # One transaction for the index rows and their job items: a crash leaves both or neither.
        with self.database.write() as connection:
            self.repo.save_many(tracks, connection=connection)
            self.store.update_items(job_id, updates, connection=connection)
            self.store.update(job_id, connection=connection, current_path=last, message='Reading tags…',
                              active_seconds=control.active_seconds())

    # ── identification ───────────────────────────────────────────────────────

    def _run_identify(self, job: dict, control: _Control):
        job_id, stop = job['id'], control.stop_requested
        offline = bool(job['params'].get('offline'))
        blocker = self.preflight(offline)
        if blocker:
            return ('failed', blocker, describe(blocker).label)
        service = self.recognition_factory(offline, stop)
        network_failures = 0
        stage = ['']

        def on_stage(name: str) -> None:
            if name != stage[0]:
                stage[0] = name
                self.store.update(job_id, stage=name)

        while not stop():
            paths = self.store.pending_items(job_id, IDENTIFY_BATCH)
            if not paths:
                break
            tracks = self.repo.get_many(paths)
            for text in paths:
                if stop():
                    break
                self.store.update(job_id, current_path=text, message='Analysing…',
                                  active_seconds=control.active_seconds())
                state, code, detail = self._identify_one(service, text, tracks.get(text), on_stage)
                if state is None:  # cancelled mid-file: it stays pending
                    return None
                if describe(code).blocks_job and state == 'error':
                    return ('failed', code, describe(code).label)
                self.store.update_items(job_id, [(text, state, code, detail)])
                network_failures = network_failures + 1 if code in NETWORK_CODES else 0
                if network_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                    return ('paused', code, f'Paused after {network_failures} consecutive network failures. '
                                            'Check the connection, then use Retry failed.')
        return None

    @staticmethod
    def _identify_one(service, text: str, track: dict | None, on_stage):
        path = Path(text)
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            log.info('file missing while indexing (%s, errno=%s)', type(exc).__name__, getattr(exc, 'errno', None))
            return 'error', 'file_missing', ''
        except OSError as exc:
            log.info('file unreadable while indexing (%s, errno=%s)', type(exc).__name__, getattr(exc, 'errno', None))
            return 'error', 'file_unreadable', type(exc).__name__
        if track is None or (info.st_size, info.st_mtime_ns) != (track['size'], track['mtime_ns']):
            return 'error', 'file_changed', 'Changed since it was scanned. Scan the folder again.'
        try:
            report = service.identify(path, tags=track, on_stage=on_stage)
        except OperationCancelled:
            return None, '', ''
        if report.status == RecognitionStatus.REVIEW:
            return 'done', '', ''
        if report.status in (RecognitionStatus.UNMATCHED, RecognitionStatus.OFFLINE):
            return 'unmatched', report.code, report.reason
        return 'error', report.code or 'internal_error', report.reason

    # ── read models ──────────────────────────────────────────────────────────

    def _describe_job(self, job: dict) -> dict:
        counters = self.store.counters(job)
        with self._lock:
            control = self._controls.get(job['id'])
        running = control is not None
        processed = counters['total'] - counters['pending']
        elapsed = control.active_seconds() if control else job['active_seconds']
        discovering = running and job['stage'] == 'DISCOVERING'
        errors = [{'code': code, 'count': count, 'label': describe(code).label, 'hint': describe(code).hint,
                   'retryable': describe(code).retryable} for code, count in self.store.error_breakdown(job)]
        finished = job['state'] in ('completed', 'completed_with_errors', 'cancelled')
        state = (job['stage'] or 'STARTING') if job['state'] == 'running' else job['state'].upper()
        return {
            'id': job['id'], 'kind': job['kind'], 'state': state, 'root': job['root'], 'message': job['message'],
            'error': ({'code': job['error_code'], 'label': describe(job['error_code']).label,
                       'hint': describe(job['error_code']).hint} if job['error_code'] else None),
            'current_file': job['current_path'] if running else '',
            'counters': counters, 'processed': processed,
            'percent': None if discovering else (round(processed * 100 / counters['total']) if counters['total']
                                                 else (100 if finished else 0)),
            'elapsed_seconds': round(elapsed, 1),
            'rate_per_second': round(processed / elapsed, 1) if elapsed > 0.5 and not discovering else None,
            'errors': errors,
            'removed_from_index': (job['summary'] or {}).get('removed_from_index', 0),
            'folder_errors': (job['summary'] or {}).get('folder_errors', []),
            'offline': bool(job['params'].get('offline')),
            'created_at': job['created_at'], 'updated_at': job['updated_at'],
            'can': {'pause': running and not control.stop_requested(),
                    'resume': not running and job['state'] in RESUMABLE,
                    'cancel': (running and not control.cancel.is_set()) or (not running and job['state'] in RESUMABLE),
                    'retry': not running and job['state'] != 'cancelled'
                             and any(e['retryable'] for e in errors)},
        }

    def _visible_jobs(self) -> list[dict]:
        """The latest job of each kind, plus any older one that still needs attention."""
        seen, visible = set(), []
        for job in self.store.list_recent(30):
            if job['kind'] not in seen or job['state'] in ('running', 'paused', 'interrupted'):
                visible.append(job)
            seen.add(job['kind'])
        return visible

    def activity(self) -> dict:
        jobs = [self._describe_job(job) for job in self._visible_jobs()]
        attention = {'INTERRUPTED': 1, 'PAUSED': 2, 'FAILED': 3}
        terminal = ('COMPLETED', 'COMPLETED_WITH_ERRORS', 'CANCELLED')
        jobs.sort(key=lambda j: (attention.get(j['state'], 4 if j['state'] in terminal else 0), -j['updated_at']))
        counts = self.repo.counts()
        summary = self.repo.identification_summary()
        scans = [job for job in self.store.list_recent(30) if job['kind'] == 'scan']
        identify = next((j for j in jobs if j['kind'] == 'identify'), None)
        return {
            'state': jobs[0]['state'] if jobs else 'IDLE',
            'library': {
                'root': scans[0]['root'] if scans else '',
                'indexed': counts['total'], 'tagged': counts['tagged'], 'incomplete': counts['review'],
                'unidentified_tags': counts['unidentified'],
                'awaiting_identification': summary['awaiting'], 'identified': summary['identified'],
                'unknown': summary['unknown'],
                'failed': identify['counters']['error'] if identify else 0,
            },
            'jobs': jobs,
        }

    def status(self) -> dict:
        """Compact summary kept for existing API clients."""
        with self._lock:
            running = bool(self._controls)
        recent = self.store.list_recent(1)
        if not recent:
            return {'running': False, 'kind': '', 'completed': 0, 'errors': 0,
                    'message': 'Ready. Your music has not been modified.'}
        counters = self.store.counters(recent[0])
        return {'running': running, 'kind': recent[0]['kind'], 'completed': counters['total'] - counters['pending'],
                'errors': counters['error'], 'message': recent[0]['message']}

