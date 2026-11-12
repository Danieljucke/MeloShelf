"""End-to-end pipeline with MOCKED providers:

    file → tags → fingerprint → AcoustID → MusicBrainz → evidence → SQLite → review API

Real code runs everywhere except at the three external edges: the fpcalc process
and the two HTTP transports are replaced by deterministic stand-ins. These tests
say nothing about real-world recognition quality; see tests/test_live_providers.py.
"""
import hashlib
import json
import shutil
import threading
import time
from urllib.request import Request, urlopen

import pytest

from audio_fixtures import (FakeFpcalc, FakeProviders, acoustid_result, mbid, musicbrainz_recording, write_mp3)
from meloshelf.application.jobs import JobService
from meloshelf.application.recognition import RecognitionService
from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.fingerprint import Fingerprinter
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import AcoustIDClient, APIError, MusicBrainzClient, RateLimiter, RetryPolicy
from meloshelf.infrastructure.tags import read_tags

LENGTH = 4.0


class CountingLimiter(RateLimiter):
    def __init__(self):
        super().__init__(0)
        self.waits = 0

    def wait(self, should_cancel=None):
        self.waits += 1
        super().wait(should_cancel)


def build_library(tmp_path):
    """Ten representative files and the providers' knowledge about them."""
    root = tmp_path / 'Music'
    providers = FakeProviders()
    files = {}

    files['tagged'] = write_mp3(root / 'Adele' / '25' / '01 - Hello.mp3', seed=1,
                                tags={'title': 'Hello', 'artist': 'Adele', 'album': '25', 'date': '2015'})
    providers.add(files['tagged'], [acoustid_result(1, 'Hello', 'Adele', score=0.97, duration=LENGTH)])

    files['numeric'] = write_mp3(root / 'Unsorted' / '000147.mp3', seed=2)
    providers.add(files['numeric'], [acoustid_result(2, 'Blinding Lights', 'The Weeknd', score=0.95, duration=LENGTH)])

    files['untagged'] = write_mp3(root / 'Unsorted' / 'recording.mp3', seed=3)
    providers.add(files['untagged'], [acoustid_result(3, 'Levitating', 'Dua Lipa', score=0.9, duration=LENGTH)])

    files['wrong_tags'] = write_mp3(root / 'Downloads' / 'Get Lucky.mp3', seed=4,
                                    tags={'title': 'Get Lucky', 'artist': 'Daft Punk', 'album': 'Random Access Memories'})
    providers.add(files['wrong_tags'], [acoustid_result(4, 'Sinnerman', 'Nina Simone', score=0.96, duration=LENGTH)])

    files['two_recordings'] = write_mp3(root / 'Unsorted' / '000200.mp3', seed=5)
    providers.add(files['two_recordings'], [acoustid_result(5, 'So What', 'Miles Davis', score=0.94, duration=LENGTH),
                                            acoustid_result(6, 'So What (Live)', 'Miles Davis', score=0.92, duration=LENGTH)])

    files['many_releases'] = write_mp3(root / 'The Weeknd' / 'Save Your Tears.mp3', seed=7,
                                       tags={'title': 'Save Your Tears', 'artist': 'The Weeknd'})
    providers.add(files['many_releases'], [acoustid_result(7, 'Save Your Tears', 'The Weeknd', score=0.98, duration=LENGTH)])
    providers.recordings[mbid(7)] = musicbrainz_recording(7, 'Save Your Tears', 'The Weeknd', seconds=LENGTH, releases=[
        ('After Hours', '2020-03-20', 'Album', []), ('After Hours (Deluxe)', '2020-03-23', 'Album', []),
        ('Save Your Tears', '2020-08-09', 'Single', []), ('The Highlights', '2021-02-05', 'Album', ['Compilation'])])

    files['unknown'] = write_mp3(root / 'Unsorted' / 'field recording.mp3', seed=8)   # providers know nothing

    files['corrupted'] = root / 'Unsorted' / 'broken.mp3'
    files['corrupted'].write_bytes(b'this is not audio at all' * 40)

    files['duplicate'] = root / 'Backup' / 'copy of hello.mp3'
    files['duplicate'].parent.mkdir()
    shutil.copyfile(files['tagged'], files['duplicate'])

    files['plain'] = write_mp3(root / 'Dua Lipa' / 'Physical.mp3', seed=9,
                               tags={'title': 'Physical', 'artist': 'Dua Lipa', 'album': 'Future Nostalgia'})
    providers.add(files['plain'], [acoustid_result(9, 'Physical', 'Dua Lipa', score=0.99, duration=LENGTH)])

    for n in (1, 2, 3, 4, 5, 6, 9):
        providers.recordings.setdefault(mbid(n), musicbrainz_recording(
            n, {1: 'Hello', 2: 'Blinding Lights', 3: 'Levitating', 4: 'Sinnerman', 5: 'So What',
                6: 'So What (Live)', 9: 'Physical'}[n],
            {1: 'Adele', 2: 'The Weeknd', 3: 'Dua Lipa', 4: 'Nina Simone', 5: 'Miles Davis', 6: 'Miles Davis',
             9: 'Dua Lipa'}[n], seconds=LENGTH, releases=[('Only Album', '2020', 'Album', [])]))
    return root, files, providers


def digest_tree(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob('*')) if p.is_file()}


class Pipeline:
    """A running server wired to the stand-ins, driven through its HTTP API."""
    def __init__(self, tmp_path, providers, monkeypatch):
        monkeypatch.setenv('MELOSHELF_ACOUSTID_KEY', 'test-key-not-real')
        self.providers = providers
        self.fpcalc = FakeFpcalc()
        self.limiters = {'acoustid': CountingLimiter(), 'musicbrainz': CountingLimiter()}
        fingerprinter = Fingerprinter(runner=self.fpcalc, which=lambda name: name)
        retry = RetryPolicy(sleeper=lambda seconds: None)

        def factory(offline, should_cancel):
            acoustid = None if offline else AcoustIDClient('test-key-not-real', transport=providers, retry=retry,
                                                           limiter=self.limiters['acoustid'], should_cancel=should_cancel)
            musicbrainz = None if offline else MusicBrainzClient('MeloShelf-tests/1 (dev@example.org)',
                                                                 transport=providers, retry=retry,
                                                                 limiter=self.limiters['musicbrainz'],
                                                                 should_cancel=should_cancel)
            return RecognitionService(fingerprinter, acoustid, musicbrainz, MatchStore(self.server.database),
                                      offline=offline)

        self.server = create_server(db_path=tmp_path / 'lib.sqlite3', fingerprinter=fingerprinter,
                                    recognition_factory=factory)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path):
        with urlopen(self.base + path, timeout=15) as response:
            return json.load(response)

    def post(self, path, data):
        request = Request(self.base + path, data=json.dumps(data).encode(), method='POST',
                          headers={'Content-Type': 'application/json', 'X-MeloShelf-Token': self.server.token})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def run(self, path, data):
        result = self.post(path, data)
        deadline = time.monotonic() + 30
        while self.get('/api/status')['running']:
            assert time.monotonic() < deadline, 'job did not finish'
            time.sleep(.02)
        return result

    def job(self, kind):
        return next(j for j in self.get('/api/activity')['jobs'] if j['kind'] == kind)

    def matches(self):
        return {m['path']: m for m in self.get('/api/matches?limit=200')['matches']}


@pytest.fixture
def library(tmp_path):
    return build_library(tmp_path)


@pytest.fixture(scope='module')
def pipeline(tmp_path_factory):
    """Scanned and identified once; the tests below inspect the outcome."""
    tmp_path = tmp_path_factory.mktemp('pipeline')
    root, files, providers = build_library(tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    running = Pipeline(tmp_path, providers, monkeypatch)
    running.root, running.files = root, files
    running.before = digest_tree(root)
    running.run('/api/scan', {'folder': str(root)})
    running.run('/api/identify', {'limit': 100, 'offline': False})
    yield running
    running.close()
    monkeypatch.undo()


def match_for(pipeline, name):
    return pipeline.matches()[str(pipeline.files[name].resolve())]


def test_scan_indexes_every_fixture_with_its_tags(pipeline):
    stats = pipeline.get('/api/stats')
    assert stats['total'] == 10
    assert stats['tagged'] == 4            # tagged, its duplicate, wrong_tags, plain
    tracks = {t['path']: t for t in pipeline.get('/api/tracks?limit=50')['tracks']}
    hello = tracks[str(pipeline.files['tagged'].resolve())]
    assert (hello['title'], hello['artist'], hello['album'], hello['year']) == ('Hello', 'Adele', '25', '2015')
    assert tracks[str(pipeline.files['numeric'].resolve())]['status'] == 'unidentified'
    assert tracks[str(pipeline.files['many_releases'].resolve())]['status'] == 'review'


def test_job_summary_separates_proposals_unknown_and_failures(pipeline):
    job = pipeline.job('identify')
    assert job['state'] == 'COMPLETED_WITH_ERRORS'
    assert job['counters']['total'] == 10
    assert job['counters']['done'] == 8 and job['counters']['unmatched'] == 1 and job['counters']['error'] == 1
    assert [(e['code'], e['count']) for e in job['errors']] == [('invalid_audio', 1)]
    library = pipeline.get('/api/activity')['library']
    assert (library['identified'], library['unknown'], library['awaiting_identification']) == (8, 1, 1)


def test_correctly_tagged_song_is_strongly_supported(pipeline):
    match = match_for(pipeline, 'tagged')
    top = match['candidates'][0]
    assert match['assessment'] == 'strong' and match['decision'] == 'pending'
    assert (top['title'], top['artist']) == ('Hello', 'Adele')
    assert top['acoustid_score'] == 0.97 and top['evidence']['score'] >= 90
    assert (top['evidence']['title'], top['evidence']['artist'], top['evidence']['duration']) == ('match', 'match', 'match')


def test_numeric_filename_gets_a_plausible_proposal_from_audio_alone(pipeline):
    match = match_for(pipeline, 'numeric')
    assert match['assessment'] == 'plausible'
    assert match['candidates'][0]['title'] == 'Blinding Lights'
    assert match['candidates'][0]['evidence']['title'] == 'unknown'
    assert match['local_tags'] == {'title': '', 'artist': '', 'album': ''}


def test_untagged_song_gets_a_proposal(pipeline):
    assert match_for(pipeline, 'untagged')['candidates'][0]['artist'] == 'Dua Lipa'


def test_incorrect_tags_are_flagged_as_conflict(pipeline):
    match = match_for(pipeline, 'wrong_tags')
    assert match['assessment'] == 'conflict'
    assert match['local_tags']['title'] == 'Get Lucky' and match['candidates'][0]['title'] == 'Sinnerman'
    assert match['candidates'][0]['evidence']['title'] == 'conflict'


def test_multiple_recordings_stay_ambiguous(pipeline):
    match = match_for(pipeline, 'two_recordings')
    assert match['assessment'] == 'ambiguous'
    assert {c['title'] for c in match['candidates']} == {'So What', 'So What (Live)'}


def test_several_album_releases_are_offered_and_none_is_chosen(pipeline):
    match = match_for(pipeline, 'many_releases')
    top = match['candidates'][0]
    assert top['releases'] == ['After Hours', 'After Hours (Deluxe)', 'Save Your Tears', 'The Highlights']
    assert [r['primary_type'] for r in top['release_details']] == ['Album', 'Album', 'Single', 'Album']
    assert top['release_details'][3]['secondary_types'] == ['Compilation']
    assert match['release_ambiguous'] is True
    stored = MatchStore(pipeline.server.database).get_result(pipeline.files['many_releases'].resolve())
    assert stored['selected_release_id'] == '' and stored['selected_recording_id'] == ''


def test_unknown_recording_stays_unknown(pipeline):
    match = match_for(pipeline, 'unknown')
    assert match['status'] == 'unmatched' and match['code'] == 'no_match' and match['candidates'] == []


def test_corrupted_audio_is_an_explained_error_and_not_a_result(pipeline):
    assert str(pipeline.files['corrupted'].resolve()) not in pipeline.matches()
    job = pipeline.job('identify')
    items = pipeline.get(f"/api/jobs/errors?job_id={job['id']}&code=invalid_audio")['items']
    assert [i['path'] for i in items] == [str(pipeline.files['corrupted'].resolve())]
    assert 'decoding' in items[0]['detail']


def test_duplicate_file_reuses_the_cached_lookup(pipeline):
    original, duplicate = match_for(pipeline, 'tagged'), match_for(pipeline, 'duplicate')
    assert duplicate['candidates'][0]['recording_id'] == original['candidates'][0]['recording_id']
    assert len(pipeline.providers.acoustid_calls) == len(set(pipeline.providers.acoustid_calls)) == 8, \
        'nine decodable files, eight distinct fingerprints: the duplicate cost no request'
    assert len(pipeline.providers.musicbrainz_calls) == len(set(pipeline.providers.musicbrainz_calls))


def test_every_provider_request_went_through_its_rate_limiter(pipeline):
    assert pipeline.limiters['acoustid'].waits == len(pipeline.providers.acoustid_calls)
    assert pipeline.limiters['musicbrainz'].waits == len(pipeline.providers.musicbrainz_calls)


def test_second_run_makes_no_provider_request(pipeline):
    calls = (len(pipeline.providers.acoustid_calls), len(pipeline.providers.musicbrainz_calls), len(pipeline.fpcalc.calls))
    result = pipeline.run('/api/identify', {'limit': 100, 'offline': False})
    assert result['started'] is False and 'Nothing' in result['reason']
    pipeline.run('/api/scan', {'folder': str(pipeline.root)})
    assert pipeline.job('scan')['counters']['skipped'] == 10
    assert (len(pipeline.providers.acoustid_calls), len(pipeline.providers.musicbrainz_calls),
            len(pipeline.fpcalc.calls)) == calls


def test_decision_is_recorded_without_touching_the_file(pipeline):
    path = str(pipeline.files['numeric'].resolve())
    recording = match_for(pipeline, 'numeric')['candidates'][0]['recording_id']
    assert pipeline.post('/api/decision', {'path': path, 'decision': 'accepted', 'recording_id': recording})['saved']
    track = next(t for t in pipeline.get('/api/tracks?limit=50')['tracks'] if t['path'] == path)
    assert track['identification'] == 'proposal' and track['decision'] == 'accepted'
    assert read_tags(pipeline.files['numeric']) == {}, 'accepting a proposal writes no tag'


def test_audio_files_are_byte_for_byte_unchanged(pipeline):
    assert digest_tree(pipeline.root) == pipeline.before


def test_transient_provider_failure_is_retried_and_the_track_still_identified(tmp_path, library, monkeypatch):
    root, files, providers = library
    providers.fail_next = [APIError('HTTP 503', retryable=True, status=503),
                           APIError('Network error: TimeoutError', retryable=True)]
    running = Pipeline(tmp_path, providers, monkeypatch)
    try:
        running.run('/api/scan', {'folder': str(root)})
        running.run('/api/identify', {'limit': 100, 'offline': False})
        job = running.job('identify')
        assert job['counters']['done'] == 8 and [e['code'] for e in job['errors']] == ['invalid_audio']
    finally:
        running.close()


def test_file_modified_during_the_scan_is_reported_and_picked_up_by_the_next_scan(tmp_path, library):
    root, files, providers = library
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    target = files['numeric']

    def reader(path):
        tags = read_tags(path)
        if path == target and not reader.done:
            reader.done = True
            with open(path, 'ab') as handle:   # another program appends to the file mid-scan
                handle.write(b'\x00' * 417)
        return tags
    reader.done = False

    service = JobService(database, tag_reader=reader)
    assert service.wait(service.start_scan(str(root))['job_id'], 30)
    job = service.activity()['jobs'][0]
    assert job['state'] == 'COMPLETED_WITH_ERRORS'
    assert [(e['code'], e['count']) for e in job['errors']] == [('file_changed', 1)]
    assert service.activity()['library']['indexed'] == 9
    assert service.wait(service.start_scan(str(root))['job_id'], 30)
    assert service.activity()['library']['indexed'] == 10
    assert service.activity()['jobs'][0]['state'] == 'COMPLETED'
