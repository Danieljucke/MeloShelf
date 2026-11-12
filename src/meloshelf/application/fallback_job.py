"""The text-only fallback search as a background job with visible progress.

One run at a time, bounded by a limit. Every answered file is stored by the runner at once, so a cancelled,
failed or interrupted run loses nothing and the next run only asks about files that have no answer yet. The last
run's totals are kept in the app settings; a run found still "running" after a restart is reported as interrupted.
Only text goes to the provider; no audio is read for the search and no acoustic result is touched."""
import threading
import time

from meloshelf.application.fallback import FallbackService
from meloshelf.application.fallback_runner import FallbackRunner
from meloshelf.infrastructure.match_store import MatchStore

SETTING = 'fallback_run'
MAX_LIMIT = 200
SAVE_EVERY = 5          # files between saves of the progress record
COUNTERS = ('examined', 'suggested', 'unidentified', 'provider_failed', 'offline_skipped')


from meloshelf.application import error_codes as ec
import logging

log = logging.getLogger(__name__)


class FallbackJobs:
    def __init__(self, database, client_factory):
        """``client_factory(offline)`` returns the text-search client, or None when none can be made."""
        self.database = database
        self.client_factory = client_factory
        self.settings = MatchStore(database)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._current: dict | None = None       # this process's run, kept until the next one starts
        self._recover()

    # ── persistence ────────────────────────────────────────────────────────────

    def _recover(self) -> None:
        record = self.settings.get_setting(SETTING)
        if isinstance(record, dict) and record.get('state') == 'running':
            self.settings.set_setting(SETTING, {**record, 'state': 'interrupted', 'finished_at': time.time()})

    def _last_run(self) -> dict | None:
        record = self.settings.get_setting(SETTING)
        return record if isinstance(record, dict) else None

    def _save(self, run: dict) -> None:
        self.settings.set_setting(SETTING, dict(run))

    # ── start / cancel / status ────────────────────────────────────────────────

    def start(self, *, limit, offline) -> tuple[int, dict]:
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            return 400, ec.body('limit_out_of_range', max=MAX_LIMIT)
        if not isinstance(offline, bool):
            return 400, ec.body('offline_not_boolean')
        with self._lock:
            if self._busy():
                return 409, ec.body('fallback_running')
            client = self.client_factory(offline)
            if client is None and not offline:
                return 409, ec.body('search_contact_required')
            service = FallbackService(self.database, client, offline=offline)
            runner = FallbackRunner(self.database, service, should_cancel=self._cancel.is_set)
            planned = len(runner._pending(limit))
            if planned == 0:
                return 409, ec.body('fallback_nothing_left')
            self._cancel.clear()
            run = {'state': 'running', 'planned': planned, **{k: 0 for k in COUNTERS}, 'started_at': time.time(),
                   'finished_at': None, 'error': ''}
            self._current = run
            self._save(run)
            self._thread = threading.Thread(target=self._work, args=(runner, limit, run), name='fallback-search', daemon=True)
            self._thread.start()
        return 202, {'started': True, 'planned': planned}

    def _busy(self) -> bool:
        """A run is busy until it records its end state, not merely until its thread exits (which follows a moment later)."""
        return self._current is not None and self._current.get('state') == 'running'

    def _work(self, runner: FallbackRunner, limit: int, run: dict) -> None:
        def progress(summary: dict) -> None:
            for key in COUNTERS:
                run[key] = summary[key]
            if summary['examined'] % SAVE_EVERY == 0:
                self._save(run)

        try:
            summary = runner.run(limit=limit, on_progress=progress)
            for key in COUNTERS:
                run[key] = summary[key]
            run['state'] = 'cancelled' if summary['cancelled'] else 'done'
        except Exception as exc:  # the worker must always end in a recorded state
            log.exception('text search stopped unexpectedly')
            run['state'], run['error'] = 'failed', type(exc).__name__
        run['finished_at'] = time.time()
        self._save(run)

    def cancel(self) -> dict:
        running = self._thread is not None and self._thread.is_alive()
        if running:
            self._cancel.set()
        return {'cancelling': running}

    def wait(self, timeout: float = 30) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def status(self) -> dict:
        current = dict(self._current) if self._current is not None else None
        runner = FallbackRunner(self.database, None)
        remaining = len(runner._pending(10 ** 9))
        base = {'state': 'idle', 'planned': 0, **{k: 0 for k in COUNTERS}, 'started_at': None, 'finished_at': None, 'error': ''}
        return {**base, **(current or {}), 'remaining': remaining, 'last_run': self._last_run()}
