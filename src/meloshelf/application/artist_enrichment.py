"""Automatic artist enrichment: a bounded, durable, resumable job over unresolved artist names.

Planning reads the library once and stores each unresolved name as an item. Running looks up one name at a time
(one provider request at a time, as the MusicBrainz policy asks), stores each answer in the same transaction as the
item's result, and re-reads the job's state between names so pause and cancel take effect promptly.

Retry: a transient error is retried up to MAX_ATTEMPTS with exponential backoff (2 s, 4 s, ...). A name that exhausts
its attempts is recorded as failed and the job continues. Two such names in a row mean the provider is probably
unavailable, so the job pauses instead of sending more requests.
"""
import time

from meloshelf.application.artist_lookup import distinct_names
from meloshelf.domain.artist_identity import ArtistCandidate, decide_link
from meloshelf.domain.artists import canonical_key
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.music_api import APIError

MAX_NAMES_LIMIT = 10_000
REFRESH_AFTER_DAYS = 90
MAX_ATTEMPTS = 3
CONSECUTIVE_FAILURES_TO_PAUSE = 2
ACTIVE, TERMINAL = ('queued', 'running'), ('done', 'cancelled')


class JobError(ValueError):
    """A job action was refused; the message is safe to show to the user."""


class EnrichmentService:
    def __init__(self, database, client, *, sleep=time.sleep, clock=time.time):
        self.database = database
        self.client = client
        self.sleep = sleep
        self.clock = clock
        self.links = ArtistLinks(database)

    # ── planning ──────────────────────────────────────────────────────────────────

    def create(self, *, max_names: int = 500, refresh_after_days: float | None = REFRESH_AFTER_DAYS) -> dict:
        """Plan a bounded batch. Names with no stored answer come first-come; a stored 'no match' older than
        `refresh_after_days` is asked again, because the catalogue grows. A verified link, a suggestion, an ambiguity
        or anything a person has decided is never refreshed automatically. None switches the refresh off."""
        if not type(max_names) is int or not 1 <= max_names <= MAX_NAMES_LIMIT:
            raise JobError(f'max_names must be between 1 and {MAX_NAMES_LIMIT:,}')
        stored = self.links.all()
        now = self.clock()
        stale_before = None if refresh_after_days is None else now - refresh_after_days * 86400
        names, refresh = [], set()
        for name in distinct_names(self.database):
            row = stored.get(canonical_key(name))
            if row is None:
                names.append(name)
            elif stale_before is not None and row['status'] == 'no_match' and row['looked_up_at'] < stale_before:
                names.append(name)
                refresh.add(name)
        names = names[:max_names]
        with self.database.write() as db:
            # An empty plan is finished at once: it must not sit in the queue looking unfinished.
            status = 'queued' if names else 'done'
            cursor = db.execute('''INSERT INTO artist_enrichment_jobs(status,max_names,planned,created_at,updated_at)
                                   VALUES(?,?,?,?,?)''', (status, max_names, len(names), now, now))
            job_id = cursor.lastrowid
            # While pending, 'refresh' in the result column marks an item whose old answer is being asked again.
            db.executemany('INSERT INTO artist_enrichment_items(job_id,position,name,state,result) VALUES(?,?,?,?,?)',
                           [(job_id, i, name, 'pending', 'refresh' if name in refresh else '')
                            for i, name in enumerate(names)])
        return {'id': job_id, 'planned': len(names)}

    # ── reading ───────────────────────────────────────────────────────────────────

    def status(self, job_id: int) -> dict:
        with self.database.read() as db:
            job = db.execute('SELECT * FROM artist_enrichment_jobs WHERE id=?', (job_id,)).fetchone()
            if job is None:
                raise JobError('No enrichment job has this number.')
            results = db.execute('''SELECT state, result, COUNT(*) AS n FROM artist_enrichment_items
                                    WHERE job_id=? GROUP BY state, result''', (job_id,)).fetchall()
        counts = {'linked': 0, 'suggested': 0, 'ambiguous': 0, 'no_match': 0}
        done = failed = 0
        for row in results:
            if row['state'] == 'done':
                done += row['n']
                if row['result'] in counts:
                    counts[row['result']] += row['n']
            elif row['state'] == 'failed':
                failed += row['n']
        return {'id': job['id'], 'status': job['status'], 'planned': job['planned'], 'done': done + failed,
                'failed': failed, 'last_error': job['last_error'], **counts}

    # ── control ───────────────────────────────────────────────────────────────────

    def pause(self, job_id: int) -> None:
        self._set_status(job_id, allowed_from=ACTIVE, to='paused', refusal='Only a queued or running job can be paused.')

    def resume(self, job_id: int) -> None:
        self._set_status(job_id, allowed_from=('paused', 'interrupted'), to='queued',
                         refusal='Only a paused or interrupted job can be resumed.')

    def cancel(self, job_id: int) -> None:
        self._set_status(job_id, allowed_from=('queued', 'running', 'paused', 'interrupted'), to='cancelled',
                         refusal='This job has already finished or been cancelled.')

    def recover(self) -> int:
        """After a restart nothing is really running: such jobs are marked interrupted, to be resumed on request."""
        with self.database.write() as db:
            cursor = db.execute("UPDATE artist_enrichment_jobs SET status='interrupted', updated_at=? WHERE status='running'",
                                (self.clock(),))
            return cursor.rowcount

    def _set_status(self, job_id, *, allowed_from, to, refusal):
        with self.database.write() as db:
            row = db.execute('SELECT status FROM artist_enrichment_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise JobError('No enrichment job has this number.')
            if row['status'] not in allowed_from:
                raise JobError(refusal)
            db.execute('UPDATE artist_enrichment_jobs SET status=?, updated_at=? WHERE id=?',
                       (to, self.clock(), job_id))

    # ── running ───────────────────────────────────────────────────────────────────

    def run_batch(self, job_id: int, *, batch: int = 25) -> dict:
        """Look up at most `batch` names, honouring pause and cancel between names."""
        if not type(batch) is int or not 1 <= batch <= 500:
            raise JobError('batch must be between 1 and 500')
        job = self.status(job_id)
        if job['status'] in ('paused', 'cancelled', 'done'):
            return job
        self._set_job(job_id, 'running')
        lookups = 0
        consecutive_failures = 0
        while lookups < batch:
            if self._job_status(job_id) in ('paused', 'cancelled'):
                break
            item = self._next_pending(job_id)
            if item is None:
                break
            stored = self.links.get(item['name'])
            # A stored answer is reused, not asked again, unless this item is a refresh of an old 'no match' that has
            # not been answered by anything better since the plan was made.
            if stored is not None and not (item['result'] == 'refresh' and stored['status'] == 'no_match'):
                self._finish(job_id, item['position'], 'done', stored['status'], attempts=0)
                continue
            outcome = self._look_up(job_id, item)
            lookups += 1
            if outcome == 'failed':
                consecutive_failures += 1
                if consecutive_failures >= CONSECUTIVE_FAILURES_TO_PAUSE:
                    self._set_job(job_id, 'paused', error='The provider did not answer for several names in a row.')
                    break
            else:
                consecutive_failures = 0
        if self._job_status(job_id) == 'running':
            remaining = self._next_pending(job_id)
            self._set_job(job_id, 'done' if remaining is None else 'queued')
        return self.status(job_id)

    def run_to_completion(self, job_id: int, *, batch: int = 50) -> dict:
        while True:
            before = self.status(job_id)
            after = self.run_batch(job_id, batch=batch)
            if after['status'] in ('done', 'cancelled', 'paused') or after['done'] == before['done']:
                return after

    def _look_up(self, job_id, item) -> str:
        attempts = item['attempts']
        while True:
            try:
                hits = self.client.search_artists(item['name'])
            except APIError as exc:
                attempts += 1
                self._record_attempt(job_id, item['position'], attempts)
                if exc.retryable and attempts < MAX_ATTEMPTS:
                    self.sleep(2 ** attempts)
                    continue
                self._finish(job_id, item['position'], 'failed', '', attempts=attempts, error=str(exc))
                return 'failed'
            candidates = [ArtistCandidate(mb_id=h['mb_artist_id'], name=h['name'], score=int(h.get('score') or 0),
                                          aliases=tuple(h.get('aliases') or ()),
                                          disambiguation=h.get('disambiguation') or '') for h in hits]
            decision = decide_link(item['name'], candidates)
            self.links.record(item['name'], decision, looked_up_at=self.clock())
            self._finish(job_id, item['position'], 'done', decision.status, attempts=attempts + 1)
            return 'done'

    # ── helpers ───────────────────────────────────────────────────────────────────

    def _next_pending(self, job_id):
        with self.database.read() as db:
            row = db.execute("""SELECT position, name, attempts, result FROM artist_enrichment_items
                                WHERE job_id=? AND state='pending' ORDER BY position LIMIT 1""", (job_id,)).fetchone()
        return dict(row) if row else None

    def _job_status(self, job_id):
        with self.database.read() as db:
            return db.execute('SELECT status FROM artist_enrichment_jobs WHERE id=?', (job_id,)).fetchone()['status']

    def _set_job(self, job_id, status, *, error=None):
        with self.database.write() as db:
            if error is None:
                db.execute('UPDATE artist_enrichment_jobs SET status=?, updated_at=? WHERE id=?',
                           (status, self.clock(), job_id))
            else:
                db.execute('UPDATE artist_enrichment_jobs SET status=?, last_error=?, updated_at=? WHERE id=?',
                           (status, error, self.clock(), job_id))

    def _record_attempt(self, job_id, position, attempts):
        with self.database.write() as db:
            db.execute('UPDATE artist_enrichment_items SET attempts=? WHERE job_id=? AND position=?',
                       (attempts, job_id, position))

    def _finish(self, job_id, position, state, result, *, attempts, error=None):
        with self.database.write() as db:
            db.execute('UPDATE artist_enrichment_items SET state=?, result=?, attempts=? WHERE job_id=? AND position=?',
                       (state, result, attempts, job_id, position))
            if error is not None:
                db.execute('UPDATE artist_enrichment_jobs SET last_error=?, updated_at=? WHERE id=?',
                           (error, self.clock(), job_id))
