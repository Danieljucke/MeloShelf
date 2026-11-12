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


def _create_job_tables(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT '',
            root TEXT NOT NULL DEFAULT '',
            params TEXT NOT NULL DEFAULT '{}',
            message TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            current_path TEXT NOT NULL DEFAULT '',
            discovery_done INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            active_seconds REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )''')
    connection.execute('''
        CREATE TABLE job_items (
            job_id TEXT NOT NULL,
            path TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            code TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (job_id, path)
        ) WITHOUT ROWID''')
    connection.execute('CREATE INDEX ix_job_items_state ON job_items(job_id, state)')
    connection.execute('CREATE INDEX ix_job_items_path ON job_items(path, state)')


# Ordered migrations: index N upgrades user_version N to N+1.
def _add_decision_state(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE match_results ADD COLUMN recording_status TEXT NOT NULL DEFAULT ''")
    connection.execute('''
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )''')


def _create_artwork(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE artwork (
            key TEXT PRIMARY KEY,
            release_id TEXT NOT NULL,
            status TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            bytes INTEGER NOT NULL DEFAULT 0,
            fetched_at REAL NOT NULL
        )''')
    connection.execute('CREATE INDEX ix_artwork_fetched ON artwork(status, fetched_at)')


def _create_human_labels(connection: sqlite3.Connection) -> None:
    # Human reference judgements. Kept apart from automatic decisions and never written by a scan.
    connection.execute('''
        CREATE TABLE human_labels (
            path TEXT PRIMARY KEY,
            label TEXT NOT NULL CHECK (label IN ('correct','incorrect','unsure')),
            proposal_recording_id TEXT NOT NULL DEFAULT '',
            proposal_status TEXT NOT NULL DEFAULT '',
            corrected_recording_id TEXT NOT NULL DEFAULT '',
            corrected_title TEXT NOT NULL DEFAULT '',
            corrected_artist TEXT NOT NULL DEFAULT '',
            labeled_at REAL NOT NULL
        )''')


def _create_metadata_decisions(connection: sqlite3.Connection) -> None:
    # Human decisions about metadata proposals. Scoped to one folder; never written by an analysis run.
    connection.execute('''
        CREATE TABLE metadata_decisions (
            key TEXT PRIMARY KEY,
            folder TEXT NOT NULL,
            field TEXT NOT NULL,
            original TEXT NOT NULL,
            proposed TEXT NOT NULL DEFAULT '',
            current_proposed TEXT NOT NULL DEFAULT '',
            classification TEXT NOT NULL,
            paths TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
            history TEXT NOT NULL DEFAULT '[]',
            decided_at REAL NOT NULL DEFAULT 0
        )''')
    connection.execute('CREATE INDEX ix_metadata_decisions_folder ON metadata_decisions(folder)')


def _create_correction_events(connection: sqlite3.Connection) -> None:
    # Append-only log of corrections: human confirmations and reversals, and analysis-raised conflicts.
    connection.execute("""
        CREATE TABLE correction_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL,
            folder TEXT NOT NULL,
            field TEXT NOT NULL,
            original TEXT NOT NULL,
            corrected TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL CHECK (event IN ('confirm','revert','conflict')),
            source TEXT NOT NULL CHECK (source IN ('human','analysis')),
            reason TEXT NOT NULL DEFAULT '',
            at REAL NOT NULL
        )""")
    connection.execute('CREATE INDEX ix_correction_events_group ON correction_events(group_key, id)')
    connection.execute('CREATE INDEX ix_correction_events_scope ON correction_events(folder, field, original)')


def _create_fallback_results(connection: sqlite3.Connection) -> None:
    # Metadata-only fallback answers. Never acoustic matches; kept apart from match_results.
    connection.execute("""
        CREATE TABLE fallback_results (
            path TEXT PRIMARY KEY,
            level TEXT NOT NULL CHECK (level IN ('METADATA_SUGGESTED','UNIDENTIFIED')),
            data TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")


def _create_artist_corrections(connection: sqlite3.Connection) -> None:
    # Append-only artist merge / keep-separate / undo log. Raw tags in tracks are never changed.
    connection.execute("""
        CREATE TABLE artist_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL CHECK (action IN ('merge','split','undo')),
            source_key TEXT NOT NULL DEFAULT '',
            target_key TEXT NOT NULL DEFAULT '',
            target_name TEXT NOT NULL DEFAULT '',
            ref INTEGER,
            at REAL NOT NULL
        )""")


def _create_artist_links_and_names(connection: sqlite3.Connection) -> None:
    # Provider links to MusicBrainz artist IDs, kept apart from user decisions. Reversible: drop the table and
    # the column (SQLite 3.35+) returns the library to schema 9 with no tag or decision lost.
    connection.execute("""
        CREATE TABLE artist_links (
            credit_key TEXT PRIMARY KEY,
            credit TEXT NOT NULL,
            mb_artist_id TEXT,
            mb_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('linked','ambiguous','no_match')),
            provenance TEXT NOT NULL DEFAULT 'musicbrainz',
            reason TEXT NOT NULL DEFAULT '',
            looked_up_at REAL NOT NULL
        )""")
    # The display name of each side of a decision, so saved decisions keep the original spelling.
    connection.execute("ALTER TABLE artist_corrections ADD COLUMN source_name TEXT NOT NULL DEFAULT ''")


def _widen_artist_link_statuses(connection: sqlite3.Connection) -> None:
    # Adds 'suggested' and 'conflict' to the status rule. SQLite cannot alter a CHECK, so the table is rebuilt;
    # every row is copied unchanged. Reversible by copying back into the narrower table while no new status exists.
    connection.execute('ALTER TABLE artist_links RENAME TO artist_links_before_11')
    connection.execute("""
        CREATE TABLE artist_links (
            credit_key TEXT PRIMARY KEY,
            credit TEXT NOT NULL,
            mb_artist_id TEXT,
            mb_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('linked','suggested','ambiguous','no_match','conflict')),
            provenance TEXT NOT NULL DEFAULT 'musicbrainz',
            reason TEXT NOT NULL DEFAULT '',
            looked_up_at REAL NOT NULL
        )""")
    connection.execute('INSERT INTO artist_links SELECT * FROM artist_links_before_11')
    connection.execute('DROP TABLE artist_links_before_11')


def _create_artist_link_decisions(connection: sqlite3.Connection) -> None:
    # Candidates the provider returned, kept with the provider row as evidence. Human decisions are a separate,
    # append-only log: a person's accept, reject, review or undo never edits provider evidence.
    connection.execute("ALTER TABLE artist_links ADD COLUMN candidates TEXT NOT NULL DEFAULT '[]'")
    connection.execute("""
        CREATE TABLE artist_link_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_key TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('accept','reject','review','undo')),
            mb_artist_id TEXT,
            ref INTEGER,
            fingerprint TEXT NOT NULL DEFAULT '',
            at REAL NOT NULL
        )""")


def _create_artwork_tables(connection: sqlite3.Connection) -> None:
    # Artist photographs fetched from an openly licensed source, with their attribution, keyed by MusicBrainz artist ID.
    # artwork_decisions is a person's append-only set / clear / undo log for artists and albums. Image bytes live on disk;
    # nothing is ever written into a music file.
    connection.execute("""
        CREATE TABLE artist_images (
            mb_artist_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('available','none','rejected_license','rejected_image')),
            provenance TEXT NOT NULL DEFAULT 'wikimedia',
            file_key TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            license TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            fetched_at REAL NOT NULL
        )""")
    connection.execute("""
        CREATE TABLE artwork_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('artist','album')),
            key TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('set','clear','undo')),
            source TEXT NOT NULL DEFAULT 'user',
            file_key TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            ref INTEGER,
            at REAL NOT NULL
        )""")
    connection.execute("CREATE INDEX artwork_decisions_key ON artwork_decisions(kind, key, id)")


def _create_file_instances(connection: sqlite3.Connection) -> None:
    # A file instance is one physical file with a local id that is neither its path nor a catalogue ID. The table
    # holds evidence only (size, mtime, sampled fingerprint); no music file is ever written. Decisions gain an
    # `instances` column; rows written before it keep their paths in `tracks` and are backfilled conservatively.
    connection.execute("""
        CREATE TABLE file_instances (
            instance_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            fingerprint TEXT,
            state TEXT NOT NULL CHECK (state IN ('present','missing','superseded')),
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        )""")
    connection.execute("CREATE UNIQUE INDEX file_instances_present_path ON file_instances(path) WHERE state='present'")
    connection.execute("CREATE INDEX file_instances_state ON file_instances(state, size)")
    connection.execute("""
        CREATE TABLE file_instance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            path TEXT NOT NULL,
            at REAL NOT NULL,
            reason TEXT NOT NULL
        )""")
    connection.execute("CREATE INDEX file_instance_history_instance ON file_instance_history(instance_id, id)")
    connection.execute("ALTER TABLE artist_link_decisions ADD COLUMN instances TEXT NOT NULL DEFAULT '[]'")


def _scope_artist_link_decisions_to_recordings(connection: sqlite3.Connection) -> None:
    # A decision may also name exact recordings (JSON list of file paths, the stable identifier of a recording).
    # Existing rows get an empty list and keep their meaning.
    connection.execute("ALTER TABLE artist_link_decisions ADD COLUMN tracks TEXT NOT NULL DEFAULT '[]'")


def _scope_artist_link_decisions(connection: sqlite3.Connection) -> None:
    # A decision now names the albums it applies to (JSON list of normalised album keys). Rows written before this
    # migration have no scope and keep their old meaning: they apply to every recording with the credit.
    connection.execute("ALTER TABLE artist_link_decisions ADD COLUMN scope TEXT NOT NULL DEFAULT '[]'")


def _create_artist_enrichment(connection: sqlite3.Connection) -> None:
    # Durable enrichment jobs. Each planned name is an item, so progress and resumption are exact.
    connection.execute("""
        CREATE TABLE artist_enrichment_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL CHECK (status IN ('queued','running','paused','interrupted','cancelled','done')),
            max_names INTEGER NOT NULL,
            planned INTEGER NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
    connection.execute("""
        CREATE TABLE artist_enrichment_items (
            job_id INTEGER NOT NULL REFERENCES artist_enrichment_jobs(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','done','failed')),
            result TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (job_id, position)
        )""")


def _create_human_label_history(connection: sqlite3.Connection) -> None:
    # A human label that is replaced (by the person, or by a CSV import they approved) is kept here, never lost.
    connection.execute("""
        CREATE TABLE human_label_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            label TEXT NOT NULL,
            proposal_recording_id TEXT NOT NULL DEFAULT '',
            proposal_status TEXT NOT NULL DEFAULT '',
            corrected_recording_id TEXT NOT NULL DEFAULT '',
            corrected_title TEXT NOT NULL DEFAULT '',
            corrected_artist TEXT NOT NULL DEFAULT '',
            labeled_at REAL NOT NULL,
            replaced_at REAL NOT NULL,
            source TEXT NOT NULL
        )""")
    connection.execute('CREATE INDEX human_label_history_path ON human_label_history(path, id)')


def _create_human_label_batches(connection: sqlite3.Connection) -> None:
    # A bulk judgement is a batch: which labels it added, so the whole batch can be undone without touching
    # any label the person changed afterwards.
    connection.execute("""
        CREATE TABLE human_label_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL CHECK (label IN ('correct','incorrect','unsure')),
            created_at REAL NOT NULL,
            undone_at REAL
        )""")
    connection.execute("""
        CREATE TABLE human_label_batch_items (
            batch_id INTEGER NOT NULL REFERENCES human_label_batches(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            labeled_at REAL NOT NULL,
            PRIMARY KEY (batch_id, path)
        )""")


def _add_track_numbers(connection: sqlite3.Connection) -> None:
    # Track and disc positions read from the tags. numbers_indexed says the file's tags were read for them, so a file
    # with no numbers is not read again and files indexed before this migration are backfilled a few at a time.
    # Idempotent: a column that is already there is left alone.
    have = {row[1] for row in connection.execute('PRAGMA table_info(tracks)')}
    for name in ('track_no', 'track_total', 'disc_no', 'disc_total'):
        if name not in have:
            connection.execute(f'ALTER TABLE tracks ADD COLUMN {name} INTEGER')
    if 'numbers_indexed' not in have:
        connection.execute('ALTER TABLE tracks ADD COLUMN numbers_indexed INTEGER NOT NULL DEFAULT 0')


def _bind_decisions_to_instances(connection: sqlite3.Connection) -> None:
    # A recognition decision records the file instance it was made for, so it can follow the physical file when the
    # file is renamed or moved. Decisions that are replaced or whose file changed are archived, never silently lost.
    # Idempotent: a column or table that is already there is left alone.
    have = {row[1] for row in connection.execute('PRAGMA table_info(match_results)')}
    if 'instance_id' not in have:
        connection.execute("ALTER TABLE match_results ADD COLUMN instance_id TEXT NOT NULL DEFAULT ''")
    connection.execute('''
        CREATE TABLE IF NOT EXISTS match_decision_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            instance_id TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL,
            recording_id TEXT NOT NULL DEFAULT '',
            recording_status TEXT NOT NULL DEFAULT '',
            file_size INTEGER,
            file_mtime_ns INTEGER,
            reason TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )''')
    # Existing path-keyed decisions are bound only to the file version they were made for.
    connection.execute("""UPDATE match_results SET instance_id=COALESCE((
        SELECT f.instance_id FROM file_instances f
        WHERE f.path=match_results.path AND f.state='present'
          AND f.size=match_results.file_size AND f.mtime_ns=match_results.file_mtime_ns LIMIT 1), '')""")


def _add_embedded_art_flag(connection: sqlite3.Connection) -> None:
    # Whether the file's own tags hold a picture entry: NULL = not read yet, 1 = present, 0 = absent or unusable.
    # Written by the scan and by the thumbnail endpoint, so the tracks listing never has to open an audio file.
    have = {row[1] for row in connection.execute('PRAGMA table_info(tracks)')}
    if 'embedded_art' not in have:
        connection.execute('ALTER TABLE tracks ADD COLUMN embedded_art INTEGER')
        # Existing rows are unknown. Clearing numbers_indexed makes the scan's bounded backfill read each file once
        # (it reads the tags for the flag as well), so no page has to ask the server about every row.
        connection.execute('UPDATE tracks SET numbers_indexed=0')


def _create_human_version_labels(connection: sqlite3.Connection) -> None:
    # A person's judgement of whether the proposed recording is the same VERSION as the file (live, remix,
    # remaster...). Separate from the recording label and from every automatic decision; used only to measure
    # MeloShelf's version verdicts. Replaced judgements go to the history table.
    connection.execute("""
        CREATE TABLE human_version_labels (
            path TEXT PRIMARY KEY,
            verdict TEXT NOT NULL CHECK (verdict IN ('same','different','unsure')),
            assessed_verdict TEXT NOT NULL,
            proposal_recording_id TEXT NOT NULL DEFAULT '',
            labeled_at REAL NOT NULL
        )""")
    connection.execute("""
        CREATE TABLE human_version_label_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            verdict TEXT NOT NULL,
            assessed_verdict TEXT NOT NULL,
            proposal_recording_id TEXT NOT NULL DEFAULT '',
            labeled_at REAL NOT NULL,
            replaced_at REAL NOT NULL
        )""")


def _create_organizer_journal(connection: sqlite3.Connection) -> None:
    # The durable journal of the copy-based organizer. A run is one source folder organized into one destination;
    # every file is an item with its own state, so a crash or a pause resumes exactly where it stopped.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS organize_runs (
            id TEXT PRIMARY KEY,
            source_root TEXT NOT NULL,
            dest_root TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'copy',
            state TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS organize_items (
            run_id TEXT NOT NULL REFERENCES organize_runs(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            relative TEXT NOT NULL,
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0,
            final_sha TEXT NOT NULL DEFAULT '',
            final_size INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '{}',
            cover TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            warning TEXT NOT NULL DEFAULT '',
            supersedes TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (run_id, seq)
        )""")
    connection.execute('CREATE INDEX IF NOT EXISTS organize_items_source ON organize_items(source_path)')
    connection.execute('CREATE INDEX IF NOT EXISTS organize_items_state ON organize_items(run_id, state)')


def _add_move_journal(connection: sqlite3.Connection) -> None:
    # A move records the hash of the source it came from, and whether the original location has been emptied, so a crash
    # at any step can be judged from the journal and the disk together.
    have = {row['name'] for row in connection.execute('PRAGMA table_info(organize_items)')}
    if 'source_sha' not in have:
        connection.execute("ALTER TABLE organize_items ADD COLUMN source_sha TEXT NOT NULL DEFAULT ''")
    if 'source_removed' not in have:
        connection.execute('ALTER TABLE organize_items ADD COLUMN source_removed INTEGER NOT NULL DEFAULT 0')


def _create_manual_identities(connection: sqlite3.Connection) -> None:
    # What a person typed in for a file the recognition could not identify. It belongs to the file version it was
    # entered for (size and modification time), so it never attaches to different content at the same path.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS manual_identities (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )""")


MIGRATIONS = (_create_tables, _create_job_tables, _add_decision_state, _create_artwork, _create_human_labels,
              _create_metadata_decisions, _create_correction_events, _create_fallback_results,
              _create_artist_corrections, _create_artist_links_and_names, _widen_artist_link_statuses,
              _create_artist_enrichment, _create_artist_link_decisions, _scope_artist_link_decisions,
              _scope_artist_link_decisions_to_recordings, _create_file_instances, _create_artwork_tables,
              _create_human_label_history, _create_human_label_batches,
              _create_human_version_labels, _add_track_numbers, _add_embedded_art_flag,
              _bind_decisions_to_instances, _create_organizer_journal, _create_manual_identities, _add_move_journal)
SCHEMA_VERSION = len(MIGRATIONS)


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
