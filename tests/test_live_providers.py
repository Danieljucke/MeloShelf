"""LIVE integration tests against the real fpcalc binary, AcoustID and MusicBrainz.

They run only when every prerequisite is present and are skipped otherwise:

    fpcalc on PATH                       https://acoustid.org/chromaprint
    MELOSHELF_ACOUSTID_KEY               your own AcoustID application key
    MELOSHELF_MUSICBRAINZ_USER_AGENT     e.g. "MeloShelf/0.2.0 (you@example.com)"
    MELOSHELF_LIVE_AUDIO                 path of one commercially released song you own

Nothing here writes to the audio file. Run with:  pytest tests/test_live_providers.py -v
"""
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from meloshelf.application.recognition import RecognitionService, RecognitionStatus
from meloshelf.infrastructure.fingerprint import Fingerprinter
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import AcoustIDClient, MusicBrainzClient

KEY = os.environ.get('MELOSHELF_ACOUSTID_KEY', '').strip()
AGENT = os.environ.get('MELOSHELF_MUSICBRAINZ_USER_AGENT', '').strip()
AUDIO = os.environ.get('MELOSHELF_LIVE_AUDIO', '').strip()

needs_fpcalc = pytest.mark.skipif(shutil.which('fpcalc') is None, reason='BLOCKED: fpcalc is not installed')
needs_audio = pytest.mark.skipif(not AUDIO or not Path(AUDIO).is_file(),
                                 reason='BLOCKED: MELOSHELF_LIVE_AUDIO does not point to a file')
needs_acoustid = pytest.mark.skipif(not KEY, reason='BLOCKED: MELOSHELF_ACOUSTID_KEY is not set')
needs_musicbrainz = pytest.mark.skipif(not AGENT, reason='BLOCKED: MELOSHELF_MUSICBRAINZ_USER_AGENT is not set')


@needs_fpcalc
@needs_audio
def test_real_fpcalc_fingerprints_a_file_without_modifying_it():
    path = Path(AUDIO)
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    fingerprint = Fingerprinter().compute(path)
    assert fingerprint.duration > 0 and len(fingerprint.value) > 50
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before


@needs_fpcalc
@needs_audio
@needs_acoustid
def test_real_acoustid_lookup_returns_candidates_for_a_released_song(tmp_path):
    service = RecognitionService(Fingerprinter(), AcoustIDClient(KEY), None, MatchStore(tmp_path / 'live.sqlite3'))
    report = service.identify(Path(AUDIO))
    assert report.status == RecognitionStatus.REVIEW, f'{report.code}: {report.reason}'
    assert report.candidates and report.candidates[0].recording_id


@needs_fpcalc
@needs_audio
@needs_acoustid
@needs_musicbrainz
def test_real_musicbrainz_enrichment_adds_release_information(tmp_path):
    service = RecognitionService(Fingerprinter(), AcoustIDClient(KEY), MusicBrainzClient(AGENT),
                                 MatchStore(tmp_path / 'live.sqlite3'))
    report = service.identify(Path(AUDIO))
    assert report.status == RecognitionStatus.REVIEW, f'{report.code}: {report.reason}'
    assert 'metadata_lookup_failed' not in report.warnings
    assert any(candidate.release_details for candidate in report.candidates)


# ── optional artwork providers: opt-in, a handful of requests each ───────────────────────────────────────────────
FANART_KEY = os.environ.get('MELOSHELF_FANART_PROJECT_KEY', '').strip()
FANART_CLIENT = os.environ.get('MELOSHELF_FANART_CLIENT_KEY', '').strip()
AUDIODB_KEY = os.environ.get('MELOSHELF_THEAUDIODB_KEY', '').strip()
RADIOHEAD = 'a74b1b7f-71a5-4011-9441-d0b5e4122711'


@pytest.mark.skipif(not FANART_KEY, reason='BLOCKED: MELOSHELF_FANART_PROJECT_KEY is not set')
def test_real_fanart_returns_an_artist_photo_that_downloads_as_a_real_image():
    from meloshelf.domain.artwork import sniff_image
    from meloshelf.infrastructure.art_sources import FanartClient
    client = FanartClient(FANART_KEY, FANART_CLIENT)
    found = client.artist_thumb(RADIOHEAD)
    assert found and found['provider'] == 'fanart.tv' and found['embeddable'] is False
    declared, data = client.download(found['url'])
    assert sniff_image(data) and len(data) < 2_000_000


@pytest.mark.skipif(not AUDIODB_KEY or AUDIODB_KEY in {'1', '2', '3', '123'},
                    reason='BLOCKED: MELOSHELF_THEAUDIODB_KEY (a premium key; the shared test key is refused) is not set')
def test_real_audiodb_returns_an_artist_photo_by_musicbrainz_id():
    from meloshelf.domain.artwork import sniff_image
    from meloshelf.infrastructure.art_sources import AudioDbClient
    client = AudioDbClient(AUDIODB_KEY)
    found = client.artist_thumb(RADIOHEAD)
    assert found and found['provider'] == 'theaudiodb' and found['embeddable'] is False
    assert sniff_image(client.download(found['url'])[1])
