"""Persisted jobs and their per-file items. All progress shown to the user is read from here."""
import json
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

from meloshelf.infrastructure.database import Database, as_database

UNFINISHED = ('running', 'paused', 'interrupted', 'failed')
ITEM_STATES = ('pending', 'done', 'unmatched', 'error', 'skipped', 'cancelled')
KEEP_ITEMS_FOR_RECENT_JOBS = 3


class JobStore:
    def __init__(self, database: Database | Path | str, *, clock=time.time):
        self.database = as_database(database)
        self._clock = clock

    # ── jobs ─────────────────────────────────────────────────────────────────

    def create(self, kind: str, *, root: str = '', params: dict | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = self._clock()
        with self.database.write() as db:
            self._prune(db, kind)
            db.execute('''INSERT INTO jobs(id,kind,state,root,params,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?)''',
                       (job_id, kind, 'paused', root, json.dumps(params or {}), now, now))
        return job_id

    @staticmethod
    def _prune(db, kind: str) -> None:
        """Drop the per-file rows of old finished jobs; their summary keeps the totals."""
        db.execute('''DELETE FROM job_items WHERE job_id IN (
                        SELECT id FROM jobs WHERE kind=? AND state NOT IN ('running','paused','interrupted','failed')
                        ORDER BY created_at DESC LIMIT -1 OFFSET ?)''', (kind, KEEP_ITEMS_FOR_RECENT_JOBS))

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        job = dict(row)
        job['params'] = json.loads(job['params'] or '{}')
        job['summary'] = json.loads(job['summary']) if job['summary'] else None
        return job

    def get(self, job_id: str) -> dict | None:
        with self.database.read() as db:
            return self._row(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def find_unfinished(self, kind: str, root: str | None = None) -> dict | None:
        marks = ','.join('?' * len(UNFINISHED))
        query = f'SELECT * FROM jobs WHERE kind=? AND state IN ({marks})'
        values = [kind, *UNFINISHED]
        if root is not None:
            query += ' AND root=?'
            values.append(root)
        with self.database.read() as db:
            return self._row(db.execute(query + ' ORDER BY created_at DESC LIMIT 1', values).fetchone())

    def mark_interrupted(self) -> int:
        """Called at startup: a job still marked running belonged to a process that is gone."""
        with self.database.write() as db:
            return db.execute('''UPDATE jobs SET state='interrupted', stage='', current_path='',
                                 message='Interrupted when MeloShelf closed. Resume to continue.', updated_at=?
                                 WHERE state='running' ''', (self._clock(),)).rowcount

    def update(self, job_id: str, *, connection=None, **fields) -> None:
        """Set job columns, e.g. ``update(id, state='running', stage='INDEXING', message='…')``."""
        fields['updated_at'] = self._clock()
        for name in ('summary', 'params'):
            if name in fields:
                fields[name] = json.dumps(fields[name])
        assignments = ','.join(f'{name}=?' for name in fields)
        statement = (f'UPDATE jobs SET {assignments} WHERE id=?', (*fields.values(), job_id))
        if connection is not None:
            connection.execute(*statement)
            return
        with self.database.write() as db:
            db.execute(*statement)

    def cancel(self, job_id: str, *, active_seconds: float | None = None) -> None:
        """Terminal state and item states change together, so nothing stays pending."""
        with self.database.write() as db:
            db.execute("UPDATE job_items SET state='cancelled' WHERE job_id=? AND state='pending'", (job_id,))
            extra = {} if active_seconds is None else {'active_seconds': active_seconds}
            self.update(job_id, connection=db, state='cancelled', stage='', current_path='', error_code='',
                        message='Cancelled. Files already processed were kept.', **extra)

    # ── items ────────────────────────────────────────────────────────────────

    def add_items(self, job_id: str, paths: Iterable[str]) -> int:
        """Queue files once: a path already queued for this job is ignored."""
        with self.database.write() as db:
            before = db.total_changes
            db.executemany('INSERT OR IGNORE INTO job_items(job_id,path) VALUES(?,?)',
                           ((job_id, path) for path in paths))
            return db.total_changes - before

    def pending_items(self, job_id: str, limit: int) -> list[str]:
        with self.database.read() as db:
            rows = db.execute("SELECT path FROM job_items WHERE job_id=? AND state='pending' ORDER BY path LIMIT ?",
                              (job_id, limit))
            return [row['path'] for row in rows]

    def update_items(self, job_id: str, updates: Iterable[tuple[str, str, str, str]], *, connection=None) -> None:
        """``updates`` holds (path, state, code, detail)."""
        rows = [(state, code, detail[:500], job_id, path) for path, state, code, detail in updates]
        statement = 'UPDATE job_items SET state=?, code=?, detail=? WHERE job_id=? AND path=?'
        if connection is not None:
            connection.executemany(statement, rows)
            return
        with self.database.write() as db:
            db.executemany(statement, rows)

    def requeue_errors(self, job_id: str, codes: Iterable[str]) -> int:
        codes = list(codes)
        marks = ','.join('?' * len(codes))
        with self.database.write() as db:
            return db.execute(f'''UPDATE job_items SET state='pending', code='', detail=''
                                  WHERE job_id=? AND state='error' AND code IN ({marks})''',
                              (job_id, *codes)).rowcount

    def counters(self, job: dict) -> dict[str, int]:
        with self.database.read() as db:
            rows = db.execute('SELECT state, COUNT(*) AS n FROM job_items WHERE job_id=? GROUP BY state',
                              (job['id'],)).fetchall()
        if not rows and job.get('summary') and 'counters' in job['summary']:
            return dict(job['summary']['counters'])  # items pruned after completion: keep the stored totals
        counters = {state: 0 for state in ITEM_STATES}
        counters.update({row['state']: row['n'] for row in rows})
        return {'total': sum(counters.values()), **counters}

    def error_breakdown(self, job: dict) -> list[tuple[str, int]]:
        with self.database.read() as db:
            rows = db.execute('''SELECT code, COUNT(*) AS n FROM job_items WHERE job_id=? AND state='error'
                                 GROUP BY code ORDER BY n DESC, code''', (job['id'],)).fetchall()
            # The stored summary stands in only when the job's items were pruned. When items exist and none is in
            # error (for example after a successful retry), there is nothing to report.
            has_items = bool(rows) or db.execute('SELECT 1 FROM job_items WHERE job_id=? LIMIT 1',
                                                 (job['id'],)).fetchone() is not None
        if not has_items and job.get('summary'):
            return [tuple(pair) for pair in job['summary'].get('errors', [])]
        return [(row['code'], row['n']) for row in rows]

    def error_items(self, job_id: str, code: str | None = None, *, limit: int = 100, offset: int = 0) -> list[dict]:
        query = "SELECT path, code, detail FROM job_items WHERE job_id=? AND state='error'"
        values: list = [job_id]
        if code:
            query += ' AND code=?'
            values.append(code)
        with self.database.read() as db:
            rows = db.execute(query + ' ORDER BY path LIMIT ? OFFSET ?', (*values, limit, offset))
            return [dict(row) for row in rows]
