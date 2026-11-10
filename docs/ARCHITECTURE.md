# Architecture · MeloShelf v0.2.0

## Runtime topology

Launcher (`python -m meloshelf`) → loopback-only HTTP server (`127.0.0.1`, unpredictable session token) → local HTML/JS interface, populated by paged SQLite queries.

Optional `--desktop` opens the same interface inside PySide6 Qt WebEngine (untested). The default path uses any local browser and does not require Qt.

## Layers

| Layer | Modules | Responsibility |
| --- | --- | --- |
| Domain | `domain/models.py` | `Track`, tag-completeness classification |
| Application | `application/scanner.py`, `recognition.py`, `webapp.py` | Use cases, background jobs, HTTP routing |
| Infrastructure | `infrastructure/database.py`, `repository.py`, `match_store.py`, `tags.py`, `fingerprint.py`, `music_api.py` | SQLite, tag reading, external tools and services |
| Presentation | `presentation/web/index.html` | User interface; talks to the service only through the HTTP API |

## Feature flow

1. `POST /api/scan` starts a read-only threaded scan of a chosen local folder.
2. `LibraryScanner` reads metadata (Mutagen); `LibraryRepository` indexes paths and tags. It does not verify musical identity.
3. `POST /api/identify` selects a limited, priority-ordered batch of unidentified/review files.
4. `Fingerprinter` calls `fpcalc` as a subprocess without a shell, reads its JSON output and verifies that source size and mtime are unchanged.
5. `RecognitionService` caches fingerprints by source identity, looks up results from AcoustID using only fingerprint data and duration, and optionally enriches recording IDs using MusicBrainz.
6. `MatchStore` stores candidates and match evidence separately from original tags. Recordings with multiple album releases remain unresolved.
7. The interface displays candidates and requires an explicit recording choice to save a review decision. The selected album release is intentionally blank and no file modification follows.

## Database access

All SQLite access goes through `infrastructure/database.py`.

- **Startup.** `Database.initialize()` runs once, before the server accepts requests. It switches the file to write-ahead logging (WAL) and applies versioned schema migrations tracked in `PRAGMA user_version`. It is idempotent and issues no write when the file is already current.
- **Connections.** Each operation opens its own short-lived connection and closes it afterwards, so no connection is shared between threads. `LibraryRepository` and `MatchStore` hold no connection and can be shared freely.
- **Reads** use `Database.read()`: a query-only connection. Under WAL, readers see the last committed snapshot and neither block nor are blocked by a writer.
- **Writes** use `Database.write()`: one transaction started with `BEGIN IMMEDIATE`, committed on success and rolled back on any exception. Writers in the same process are serialized by a per-file lock; other processes are handled by SQLite's busy timeout.
- **Long work stays outside transactions.** Tag reading, fingerprinting and network calls happen between writes, never inside one.
- **Failures are visible.** A lock that outlasts the timeout raises `DatabaseBusyError`; the HTTP layer answers `503` with the explanation and logs it. Other database errors answer `500` and are logged with a traceback.

### Why initialization is centralized

Switching a database to WAL requires an exclusive lock, and SQLite does not apply the busy timeout to that switch: competing connections fail immediately with `database is locked`. Earlier versions performed the switch and schema creation on every new connection, so the first simultaneous requests against a new database could fail. Performing both exactly once at startup removes the race; a bounded retry covers two processes starting on the same new file.

## API contracts

- `GET /api/status`, `GET /api/config`, `GET /api/stats`, `GET /api/tracks?limit=&offset=&q=`, `GET /api/matches`.
- `POST /api/scan {folder}`, `POST /api/identify {limit,offline}`, `POST /api/stop {}`, `POST /api/decision {path,decision,recording_id}`.
- POST requests require `X-MeloShelf-Token`, injected in the HTML response by the local server, and check Host/Origin.

## Isolation / side effects

Only the application SQLite database is written. The source audio files are scanned and optionally decoded for a fingerprint; they are **never** retagged, renamed, moved or deleted. No web server binds to a non-loopback interface. Secrets are read from environment variables, never embedded in the front end.

## Remaining work

Album edition resolution, full-library aggregate queries for the interface, persistent scan-job checkpoints, duplicate detection, safe tag writing, transactional moves and recovery, packaging and validation on macOS and Linux.
