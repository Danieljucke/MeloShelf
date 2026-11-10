"""Single entry point for SQLite access.

Concurrency model
-----------------
* ``initialize()`` runs once at startup: it switches the file to WAL and applies
  schema migrations. Nothing else ever changes the journal mode or the schema.
* Every operation uses its own short-lived connection, so no connection is
  shared between threads.
* Readers run concurrently (WAL snapshot isolation).
* Writers are serialized inside the process by a per-file lock and start with
  ``BEGIN IMMEDIATE``; other processes are handled by SQLite's busy timeout.
"""
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_JOURNAL_SWITCH_ATTEMPTS = 6

_registry_guard = threading.Lock()
_write_locks: dict[str, threading.RLock] = {}


class DatabaseBusyError(sqlite3.OperationalError):
    """The database stayed locked longer than the configured timeout."""


def _write_lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _registry_guard:
        return _write_locks.setdefault(key, threading.RLock())


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return 'locked' in message or 'busy' in message


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS tracks (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            extension TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            artist TEXT NOT NULL DEFAULT '',
            album TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('tagged','review','unidentified'))
        )''')
    connection.execute('CREATE INDEX IF NOT EXISTS ix_tracks_status ON tracks(status)')
    connection.execute('CREATE INDEX IF NOT EXISTS ix_tracks_artist ON tracks(artist)')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS remote_cache (
            cache_key TEXT PRIMARY KEY, data TEXT NOT NULL, created_at INTEGER NOT NULL
        )''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS match_results (
            path TEXT PRIMARY KEY, file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
            status TEXT NOT NULL, data TEXT NOT NULL, created_at INTEGER NOT NULL,
            decision TEXT NOT NULL DEFAULT 'pending',
            selected_recording_id TEXT NOT NULL DEFAULT '',
            selected_release_id TEXT NOT NULL DEFAULT ''
        )''')
    # Databases created before the selection columns existed.
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(match_results)')}
    for column in ('selected_recording_id', 'selected_release_id'):
        if column not in columns:
            connection.execute(f"ALTER TABLE match_results ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")


# Ordered migrations: index N upgrades user_version N to N+1.
MIGRATIONS = (_create_tables,)


class Database:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000):
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._write_lock = _write_lock_for(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        # Per-connection setting, takes no lock. With WAL, NORMAL keeps the file
        # consistent after a crash and avoids one fsync per commit.
        connection.execute('PRAGMA synchronous=NORMAL')
        return connection

    def _busy_error(self, action: str) -> DatabaseBusyError:
        return DatabaseBusyError(
            f'Database busy: could not {action} within {self.busy_timeout_ms} ms ({self.path}). '
            'Another MeloShelf window or program is holding the library; close it and retry.')

    # ── startup ──────────────────────────────────────────────────────────────

    def initialize(self) -> None:
        """Idempotent. Issues no write when the file is already current."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            connection = self._connect()
            try:
                if connection.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                    self._switch_to_wal(connection)
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise sqlite3.DatabaseError(
                        f'{self.path} uses schema {version}, newer than this MeloShelf supports '
                        f'({SCHEMA_VERSION}). Upgrade MeloShelf or choose another --db file.')
                if version < SCHEMA_VERSION:
                    self._migrate(connection)
            finally:
                connection.close()

    def _switch_to_wal(self, connection: sqlite3.Connection) -> None:
        """The switch needs an exclusive lock and SQLite ignores the busy timeout
        for it, so contention with another process is retried here, bounded."""
        for attempt in range(1, _JOURNAL_SWITCH_ATTEMPTS + 1):
            try:
                mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower()
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                if attempt == _JOURNAL_SWITCH_ATTEMPTS:
                    raise self._busy_error('enable write-ahead logging') from exc
                log.warning('journal switch contended path=%s attempt=%d', self.path, attempt)
                time.sleep(0.05 * 2 ** attempt)
                continue
            if mode != 'wal':
                log.warning('write-ahead logging unavailable path=%s mode=%s; '
                            'reads will wait for writes', self.path, mode)
            return

    def _migrate(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            raise (self._busy_error('upgrade the schema') if _is_busy(exc) else exc) from exc
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]  # re-read under lock
            for step in MIGRATIONS[version:]:
                step(connection)
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        log.info('schema migrated path=%s from=%d to=%d', self.path, version, SCHEMA_VERSION)

    # ── access ───────────────────────────────────────────────────────────────

    @contextmanager
    def read(self):
        """Read-only connection; never blocks on, or is blocked by, a writer."""
        connection = self._connect()
        try:
            connection.execute('PRAGMA query_only=ON')
            yield connection
        except sqlite3.OperationalError as exc:
            if _is_busy(exc) and not isinstance(exc, DatabaseBusyError):
                raise self._busy_error('read') from exc
            raise
        finally:
            connection.close()

    @contextmanager
    def write(self):
        """One atomic transaction: committed on success, rolled back on any error."""
        with self._write_lock:
            connection = self._connect()
            try:
                try:
                    connection.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError as exc:
                    raise (self._busy_error('start a write') if _is_busy(exc) else exc) from exc
                try:
                    yield connection
                    connection.execute('COMMIT')
                except BaseException:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                    raise
            finally:
                connection.close()

    def integrity_check(self) -> list[str]:
        """Problems reported by SQLite; an empty list means the file is healthy."""
        with self.read() as connection:
            rows = [row[0] for row in connection.execute('PRAGMA integrity_check')]
        return [] if rows == ['ok'] else rows


def as_database(source) -> Database:
    """Accept a ready Database, or a path (initialized on the spot for scripts and tests)."""
    if isinstance(source, Database):
        return source
    database = Database(source)
    database.initialize()
    return database
