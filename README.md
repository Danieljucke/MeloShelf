# MeloShelf

**A local-first music library organizer.** MeloShelf audits a music collection, reads its existing tags, and identifies unknown recordings from acoustic fingerprints — all from a small web interface served on your own machine.

> **Version 0.2.0 — read-only.** MeloShelf currently inspects and identifies music. It does **not** rename, retag, move, overwrite or delete any audio file. Those features are on the roadmap and stay disabled until their safety engine is complete.

## Contents

- [What it does today](#what-it-does-today)
- [Project status](#project-status)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running MeloShelf](#running-meloshelf)
- [Enabling acoustic identification](#enabling-acoustic-identification)
- [How it works](#how-it-works)
- [HTTP API](#http-api)
- [Project layout](#project-layout)
- [Development](#development)
- [Security and privacy](#security-and-privacy)
- [Roadmap](#roadmap)
- [License](#license)

## What it does today

- **Scans a folder recursively** for audio files (`.mp3 .flac .m4a .aac .ogg .opus .wav .aiff .aif`). Symbolic links are skipped; scans can be cancelled.
- **Reads existing tags** (title, artist, album, year) and classifies each file as *tagged*, *needs review* or *unidentified* (missing or generic titles such as `Track 01`).
- **Indexes the library in SQLite**, with paged and searchable listing.
- **Identifies recordings** by computing a Chromaprint fingerprint, looking it up on AcoustID and enriching candidates through MusicBrainz. Results are cached, so repeated runs and offline mode reuse earlier answers.
- **Keeps you in charge.** Candidates are proposals. Accepting one records your chosen recording ID in the database and nothing else. Unknown songs stay in the review queue; identities are never invented.

## Project status

Statuses describe evidence, not intent: `VALIDATED` means automated tests plus a real run on the named platform; `GREEN` means automated tests pass (external services mocked where noted).

| Area | Status | Notes |
| --- | --- | --- |
| Library scanning | GREEN | Unit and HTTP tests; cancellable; read-only |
| Tag reading | GREEN | Title, artist, album, year via Mutagen |
| SQLite indexing and concurrency | VALIDATED (Windows) | Concurrent reads/writes, restart, integrity check, live multi-request runs |
| Local HTTP server | VALIDATED (Windows) | Loopback only, token-protected writes |
| Fingerprinting (`fpcalc`) | GREEN (mocked) | Not yet verified against a real Chromaprint binary |
| AcoustID lookup | GREEN (mocked) | Real-service run requires your own API key |
| MusicBrainz enrichment | IN_PROGRESS | Works with mocked responses; retry/backoff not implemented yet |
| Response caching | GREEN | Fingerprints, AcoustID and MusicBrainz answers |
| Review workflow | GREEN | Per recording; album edition is left unresolved |
| Web interface | IN_PROGRESS | Shows labelled sample data until a folder is scanned; artist/album views summarize the loaded page only |
| Packaging | IN_PROGRESS | `pip install` works; no installers yet |
| Album-edition matching | NOT_STARTED | |
| Duplicate detection | NOT_STARTED | |
| Artwork retrieval | NOT_STARTED | |
| Tag writing | NOT_STARTED | |
| File organization (move) | NOT_STARTED | Deliberately disabled |
| Backup and recovery | NOT_STARTED | |

Platform support: developed and run on **Windows 11 / Python 3.13**. The code avoids platform-specific paths, but **macOS and Linux are untested** so far.

## Requirements

- Python **3.11 or newer**
- Optional, for identification: the Chromaprint command-line tool [`fpcalc`](https://acoustid.org/chromaprint) on your `PATH`, plus a free [AcoustID application key](https://acoustid.org/new-application)

The only runtime dependency is [Mutagen](https://mutagen.readthedocs.io/).

## Installation

Clone the repository and install it into a virtual environment. Calling the environment's interpreter by path guarantees the packages land in the right place.

**Windows (PowerShell)**

```powershell
git clone https://github.com/Danieljucke/MeloShelf.git
cd MeloShelf
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

**macOS / Linux**

```bash
git clone https://github.com/Danieljucke/MeloShelf.git
cd MeloShelf
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## Running MeloShelf

```powershell
.\.venv\Scripts\python.exe -m meloshelf      # Windows
```

```bash
.venv/bin/python -m meloshelf                # macOS / Linux
```

MeloShelf prints its address (`http://127.0.0.1:<port>/`) and opens it in your browser. Click **Scan folder**, enter the full path of your music folder, then explore the indexed library.

| Option | Effect |
| --- | --- |
| `--no-browser` | Start the service without opening a browser tab |
| `--port N` | Use a fixed loopback port instead of a random one |
| `--db PATH` | Use another library database (default: `~/.meloshelf/library.sqlite3`) |
| `--desktop` | Experimental native window; needs `pip install -e ".[desktop]"` (untested) |

Stop the application with `Ctrl+C`. Your library index is kept between runs.

## Enabling acoustic identification

1. Install `fpcalc` and check that `fpcalc -version` works in a terminal.
2. Create your own AcoustID application key.
3. Provide the settings as environment variables — never commit them.

```powershell
$env:MELOSHELF_ACOUSTID_KEY = 'your-acoustid-key'
$env:MELOSHELF_MUSICBRAINZ_USER_AGENT = 'MeloShelf/0.2.0 (you@example.com)'
```

```bash
export MELOSHELF_ACOUSTID_KEY='your-acoustid-key'
export MELOSHELF_MUSICBRAINZ_USER_AGENT='MeloShelf/0.2.0 (you@example.com)'
```

MusicBrainz requires a User-Agent with a real contact; replace the placeholder address. Without it, MeloShelf skips enrichment rather than sending anonymous requests. Without an AcoustID key, only **offline mode** (cached answers) is available.

Then open **Identify → Analyze fingerprints**. Files with missing or generic tags are processed first. Requests are rate-limited to respect each provider (AcoustID: 3 per second; MusicBrainz: 1 per second).

A match score is the provider's match strength, not a probability, and a recording match does not determine the album edition.

## How it works

```
Browser UI  ──HTTP (127.0.0.1)──►  Local service  ──►  Scanner ── reads ──► your audio files
                                        │              Recognition ──► fpcalc, AcoustID, MusicBrainz
                                        ▼
                                  SQLite library (tracks, cache, review decisions)
```

The code is layered: `domain` (models and tag classification), `application` (scanning, recognition, the HTTP service), `infrastructure` (SQLite, tag reader, fingerprint and API adapters) and `presentation` (the web page). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details, including the database concurrency model.

## HTTP API

All endpoints are served on the loopback interface only.

| Method and path | Purpose |
| --- | --- |
| `GET /api/status` | Current background job and progress |
| `GET /api/config` | Whether identification services are configured |
| `GET /api/stats` | Track counts by status |
| `GET /api/tracks?limit=&offset=&q=` | Paged, searchable track list |
| `GET /api/matches` | Identification proposals and decisions |
| `POST /api/scan` `{folder}` | Start a read-only scan |
| `POST /api/identify` `{limit, offline}` | Start an identification batch |
| `POST /api/stop` `{}` | Ask the running job to stop |
| `POST /api/decision` `{path, decision, recording_id}` | Save a review decision |

`POST` requests must carry the session token (`X-MeloShelf-Token`) that the service embeds in the page. Database contention is reported as `503` with an explanatory message.

## Project layout

```
src/meloshelf/
├── __main__.py              command-line entry point
├── domain/models.py         Track model, tag-completeness classification
├── application/
│   ├── scanner.py           recursive read-only folder scan
│   ├── recognition.py       fingerprint → lookup → proposals
│   └── webapp.py            loopback HTTP service and background jobs
├── infrastructure/
│   ├── database.py          SQLite setup, migrations, read/write coordination
│   ├── repository.py        track index
│   ├── match_store.py       lookup cache and review decisions
│   ├── tags.py              tag reader
│   ├── fingerprint.py       fpcalc adapter
│   └── music_api.py         AcoustID and MusicBrainz clients
└── presentation/web/index.html   user interface
tests/                       automated test suite
scripts/scan_cli.py          command-line scan without the web interface
docs/                        architecture and design notes
```

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

The suite has 44 tests covering scanning, the track index, recognition (with mocked providers), review decisions, the HTTP API and database concurrency. Mocked provider tests do not demonstrate real-world recognition quality.

**Branches**

| Branch | Purpose |
| --- | --- |
| `main` | Stable, released state |
| `dev` | Integration of ongoing feature work |
| `bug` | Bug fixes awaiting integration |
| `test` | Test-suite and tooling work |
| `qa` | Release candidates under verification |

Work is developed test-first, and commits follow [Conventional Commits](https://www.conventionalcommits.org/).

## Security and privacy

- The service binds to `127.0.0.1` only, validates the `Host` and `Origin` headers, requires an unpredictable per-session token for every state-changing request and caps request sizes.
- Audio never leaves your computer. AcoustID receives a fingerprint and a duration; MusicBrainz receives recording identifiers.
- Credentials are read from environment variables and are never stored in the repository or sent to the browser.
- File names and tags are escaped before display and are never passed to a shell.

## Roadmap

1. **Identification hardening** — retries with backoff, duration-aware candidate comparison, resumable scans, verification against real providers.
2. **Album-aware matching** — distinguish original, deluxe, remastered, live, compilation and multi-disc editions; ask when several remain plausible.
3. **Artwork and metadata** — cover retrieval, side-by-side comparison, editable proposals, undo.
4. **Duplicate detection** — exact copies, different encodings of one recording, related recordings. Never deletes automatically.
5. **Safe organization** — `Artist/Year - Album/NN - Title` structure with dry-run preview, explicit approval, operation journal, verification and recovery. File moves remain disabled until this is fully validated.
6. **Interface** — whole-library artist and album views, progress and evidence displays, settings, duplicate manager.
7. **Packaging** — installers and validation on macOS and Linux.

## License

No license has been chosen yet. Until one is added, the code is published for reference and all rights are reserved by the author.
