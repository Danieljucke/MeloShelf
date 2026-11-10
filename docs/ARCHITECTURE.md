# Architecture · MeloShelf v0.2.0

## Runtime topology

Desktop launcher (`python -m meloshelf`) → loopback-only HTTP server (`127.0.0.1`, unpredictable session token) → local HTML/JS UI, populated by paged SQLite queries.

Optional `--desktop` opens the same UI inside PySide6 Qt WebEngine (not runtime verified here). The default path uses any local browser and does not require Qt.

## Feature flow

1. `POST /api/scan` starts a read-only threaded scan of a chosen local folder.
2. `LibraryScanner` reads metadata (Mutagen); `LibraryRepository` indexes paths and tags in SQLite WAL mode. It does not verify musical identity.
3. `POST /api/identify` selects a limited, priority-ordered batch of unidentified/review files.
4. `Fingerprinter` calls `fpcalc` as a subprocess without a shell, reads JSON output and verifies that source size and mtime are unchanged.
5. `RecognitionService` caches fingerprints by source identity, looks up results from AcoustID using only fingerprint data and duration, and optionally enriches recording IDs using MusicBrainz.
6. `MatchStore` stores candidates and match evidence separately from original tags. Recordings with multiple album releases remain unresolved.
7. The UI displays candidates and requires an explicit recording choice to save a review decision. Selected album release is intentionally blank and no file modification follows.

## API contracts

- `GET /api/status`, `GET /api/config`, `GET /api/stats`, `GET /api/tracks?limit=&offset=&q=`, `GET /api/matches`.
- `POST /api/scan {folder}`, `POST /api/identify {limit,offline}`, `POST /api/stop {}`, `POST /api/decision {path,decision,recording_id}`.
- POST requests require `X-MeloShelf-Token`, injected in the HTML response by the local server, and check Host/Origin.

## Isolation / side effects

Only the application SQLite database is written. The source audio files are scanned and optionally decoded for a fingerprint; they are **never** retagged, renamed, moved or deleted. No web server binds to a non-loopback interface. Secrets are read from environment variables, never embedded in the front end.

## Remaining work

Album edition resolution, full-library aggregate queries for UI, persistent scan-job checkpoints, duplicate detection, safe tag writing, transactional moves and recovery, app packaging and real desktop OS qualification.
