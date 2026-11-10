# MeloShelf — intelligent music library studio

**v0.2.0 · Phase 2 (read-only identification) · Windows, macOS and Linux**

MeloShelf is a local-first graphical application for auditing a music collection, reading its existing tags, and identifying unknown recordings from acoustic fingerprints. Its visual direction is based on the user's `Meloshelf.html`: warm rose theme, circular artists, vinyl-inspired albums, dark/light themes, and an explicit review workflow.

**Important:** This version DOES NOT rename, retag, move, overwrite or delete audio. The proposed file move transaction interface is *not implemented*. A saved review decision only records a recording ID in SQLite, not an album edition and not a file change. The UI ships with clearly identified demo examples until you scan a real folder.

## Start the GUI

Python **3.11+** is required. No build tooling is needed for the built-in local graphical UI (opens in your browser):

```bash
cd MeloShelf-v0.2.0
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m meloshelf
```

The application opens a loopback-only interface, normally at `http://127.0.0.1:<random-port>/`. It uses a one-time session token for any state-changing request and does not accept external connections. To launch without opening a tab automatically, use `python -m meloshelf --no-browser`.

**Optional native window:** `pip install -e '.[desktop]'` and then `python -m meloshelf --desktop`. This is an experimental Qt WebEngine wrapper, **not runtime-validated in the build environment**. The browser UI is the primary tested frontend.

The local SQLite index is saved at `~/.meloshelf/library.sqlite3`. Use `--db /path/to/library.sqlite3` to select another location. Scanning a folder requires its full local absolute path in the dialog, which the Python service reads from the same computer.

For a static visual demo with sample data, simply open `design-preview/index.html` without Python. It cannot read your audio or identify tracks.

## Enable acoustic identification

1. Install the Chromaprint command-line utility (`fpcalc`) and verify it can be launched from a terminal (`fpcalc -version`). Installing FFmpeg as well is recommended for broad audio-format support.
2. Obtain your **own** AcoustID application API key from https://acoustid.org/new-application . The free service has non-commercial use conditions, and limits requests to **three per second**.
3. Supply the key as an environment variable. Do not commit credentials to Git or source files.
4. Optionally configure MusicBrainz enrichment using a **meaningful User-Agent with your real project contact**. MusicBrainz limits requests to one per second and requires a properly identifying User-Agent.

**macOS / Linux:**

```bash
export MELOSHELF_ACOUSTID_KEY='YOUR_OWN_ACOUSTID_KEY'
export MELOSHELF_MUSICBRAINZ_USER_AGENT='MeloShelf/0.2.0 (you@example.com)'
python -m meloshelf
```

**Windows PowerShell:**

```powershell
$env:MELOSHELF_ACOUSTID_KEY = 'YOUR_OWN_ACOUSTID_KEY'
$env:MELOSHELF_MUSICBRAINZ_USER_AGENT = 'MeloShelf/0.2.0 (you@example.com)'
python -m meloshelf
```

The email shown is a **placeholder**, not a usable contact: replace it with your own before using MusicBrainz. If no User-Agent is provided, MeloShelf skips MusicBrainz enrichment instead of sending an anonymous request.

Click **Scan folder** first, then **Identify** → **Analyze fingerprints**. The recognition queue prioritizes files with missing/generic tags. The app sends **only fingerprint text and duration** to AcoustID, never raw audio. MusicBrainz receives recording identifiers, never raw audio. Scoring is a provider-provided match strength, **not a calibrated probability or verified album edition**. Multiple plausible editions remain unresolved.

Selecting a candidate and clicking **Accept recording** stores the chosen recording ID only. It does not authorize any modification of the file or album assignment. Recordings without matches remain unidentified. Offline mode can reuse cached recognition data but cannot identify never-seen recordings without an offline reference database.

## Tests

```bash
python -m pip install -e '.[dev]'
pytest -q
python -m compileall -q src tests
```

The suite currently contains **30 passing tests**: scanner and metadata index, AcoustID/MusicBrainz parsing, caching, offline paths, external API failures, file-change detection, rate limiting, review decisions and integration tests for the loopback API. Tests use deterministic mocked external responses; **they do not prove recognition quality with a real music collection**.

The HTML preview was separately rendered in Chromium at desktop and mobile viewport sizes. The real loopback UI could not be navigated in automated Chromium in this sandbox because browser URL navigation was blocked by environment policy; the backend HTTP routes were tested separately.

## Design and architecture

- `design-preview/index.html` — standalone visual demo, also the local UI shell.
- `src/meloshelf/presentation/web/index.html` — packaged UI copy for installed distributions.
- `src/meloshelf/application/scanner.py` — filesystem traversal, read-only tags, cancellation.
- `src/meloshelf/application/recognition.py` — fingerprint + online lookup + conservative proposals.
- `src/meloshelf/application/webapp.py` — loopback service, indexing jobs, review decisions.
- `src/meloshelf/infrastructure/fingerprint.py` — subprocess-based Chromaprint CLI adapter.
- `src/meloshelf/infrastructure/music_api.py` — AcoustID and MusicBrainz adapters.
- `src/meloshelf/infrastructure/match_store.py` — SQLite evidence and decision records.
- `tests/` — test-first implementation evidence.

The old Qt Quick interface is archived under `docs/legacy-qt/`. The HTML/Qt WebEngine direction supersedes that UI; no v0.2.0 tests exercise the archived QML code.

## Known limitations / next phase

- `fpcalc` is required for recognition, and most live matches require network connectivity and API keys.
- Review is per **recording**, not finalized album edition. Album-aware batch reconstruction comes next.
- The application does not yet write tags, fetch cover art, compare duplicates or move files.
- Live Artists/Albums/Genres screens currently summarize the **loaded page**, not the entire 50,000-song database; this will be replaced with aggregate paged API endpoints.
- Local scan jobs are cancellable but do not yet have durable transaction checkpoints; incremental indexing is persisted.
- Packaging installers, notarization, code signing and real Windows/macOS/Linux desktop validation remain outstanding.
- No public license has been selected yet. Review licenses for dependencies and API providers before distributing commercially.

## Security boundary

The server binds only to `127.0.0.1`, verifies the Host header, rejects foreign request Origins, requires an unpredictable token on POST, caps JSON request sizes, and never exposes a remote listener. Identified filenames and tags are escaped in the HTML UI. No user-controlled tag or filename is ever used as a shell command.

The source files are never changed. **Do not rely on this release to move music collections**; destructive actions will only be added after journal, backup, checksum, rollback and interrupted-operation TDD is complete.
