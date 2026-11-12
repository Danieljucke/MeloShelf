"""Recognition reports: evidence, ranking, release ambiguity and explained failures."""
import subprocess
from unittest.mock import Mock

import pytest

from meloshelf.application.recognition import RecognitionService, RecognitionStatus
from meloshelf.domain.outcomes import OUTCOMES, describe
from meloshelf.infrastructure.fingerprint import Fingerprint, Fingerprinter, FingerprintError, FingerprintUnavailable
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import APIError, OperationCancelled, RecordingCandidate

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
MB_A = {'id': A, 'title': 'Good Song', 'length': 182000, 'artist-credit': [{'name': 'Artist A'}], 'releases': [
    {'id': 'rel-1', 'title': 'First Album', 'date': '2020-03-01', 'country': 'GB',
     'release-group': {'primary-type': 'Album', 'secondary-types': []}},
    {'id': 'rel-2', 'title': 'First Album (Deluxe)', 'date': '2021-05-01',
     'release-group': {'primary-type': 'Album', 'secondary-types': []}},
    {'id': 'rel-3', 'title': 'Hits of 2020', 'date': '2020-12-01',
     'release-group': {'primary-type': 'Album', 'secondary-types': ['Compilation']}},
]}


def make_service(tmp_path, *, candidates=(), musicbrainz=None, fingerprint=None, acoustid='default', **options):
    fingerprinter = Mock(compute=Mock(return_value=fingerprint or Fingerprint('fp', 182)))
    if acoustid == 'default':
        acoustid = Mock(lookup=Mock(return_value=list(candidates)))
    store = MatchStore(tmp_path / 'm.sqlite3')
    return RecognitionService(fingerprinter, acoustid, musicbrainz, store, **options), store


def song(tmp_path, name='000147.mp3'):
    path = tmp_path / name
    path.write_bytes(b'audio bytes')
    return path


def cand(recording_id=A, title='Good Song', artist='Artist A', score=0.97, duration=None):
    return RecordingCandidate(recording_id, title, artist, score, duration=duration)


# ── evidence and ranking ─────────────────────────────────────────────────────

def test_report_carries_evidence_with_separate_scores(tmp_path):
    service, store = make_service(tmp_path, candidates=[cand(duration=182)])
    path = song(tmp_path)
    report = service.identify(path, tags={'title': 'Good Song', 'artist': 'Artist A'})
    assert report.status == RecognitionStatus.REVIEW
    assert report.assessment == 'strong'
    saved = store.get_result(path)['data']
    first = saved['candidates'][0]
    assert first['acoustid_score'] == 0.97
    assert first['evidence']['provider_score'] == 0.97
    assert first['evidence']['score'] >= 90
    assert first['evidence']['title'] == 'match'
    assert saved['assessment'] == 'strong'
    assert saved['fingerprint_duration'] == 182
    assert saved['local_tags'] == {'title': 'Good Song', 'artist': 'Artist A', 'album': ''}


def test_strong_evidence_is_still_only_a_pending_proposal(tmp_path):
    service, store = make_service(tmp_path, candidates=[cand(duration=182)])
    path = song(tmp_path)
    service.identify(path, tags={'title': 'Good Song', 'artist': 'Artist A'})
    saved = store.get_result(path)
    assert saved['decision'] == 'pending'
    assert saved['selected_recording_id'] == '' and saved['selected_release_id'] == ''


def test_candidates_are_ranked_by_application_score_not_provider_score(tmp_path):
    wrong = cand(B, 'Different Song', 'Artist B', 0.98, duration=300)
    right = cand(A, 'Good Song', 'Artist A', 0.93, duration=182)
    service, _ = make_service(tmp_path, candidates=[wrong, right])
    report = service.identify(song(tmp_path), tags={'title': 'Good Song', 'artist': 'Artist A'})
    assert [c.recording_id for c in report.candidates] == [A, B]
    assert report.assessment == 'strong'


def test_incorrect_tags_are_reported_as_a_conflict_not_overwritten(tmp_path):
    service, store = make_service(tmp_path, candidates=[cand(duration=182)])
    path = song(tmp_path, 'Adele - Hello.mp3')
    report = service.identify(path, tags={'title': 'Hello', 'artist': 'Adele', 'album': '25'})
    assert report.status == RecognitionStatus.REVIEW
    assert report.assessment == 'conflict'
    assert store.get_result(path)['data']['local_tags']['title'] == 'Hello'


def test_multiple_plausible_recordings_are_ambiguous(tmp_path):
    service, _ = make_service(tmp_path, candidates=[cand(A, score=0.97), cand(B, 'Different Song', 'Artist B', 0.95)])
    report = service.identify(song(tmp_path))
    assert report.status == RecognitionStatus.REVIEW
    assert report.assessment == 'ambiguous'


def test_insufficient_evidence_stays_unknown_but_keeps_the_lead(tmp_path):
    service, store = make_service(tmp_path, candidates=[cand(score=0.31, duration=420)])
    path = song(tmp_path)
    report = service.identify(path)
    assert report.status == RecognitionStatus.UNMATCHED
    assert report.code == 'insufficient_evidence'
    assert len(store.get_result(path)['data']['candidates']) == 1


def test_no_reference_match_is_a_valid_outcome_not_an_error(tmp_path):
    service, _ = make_service(tmp_path, candidates=[])
    report = service.identify(song(tmp_path))
    assert report.status == RecognitionStatus.UNMATCHED
    assert report.code == 'no_match'
    assert OUTCOMES['no_match'].is_error is False


# ── recordings versus releases ───────────────────────────────────────────────

def test_several_releases_are_listed_and_none_is_selected(tmp_path):
    musicbrainz = Mock(lookup_recording=Mock(return_value=MB_A))
    service, store = make_service(tmp_path, candidates=[cand()], musicbrainz=musicbrainz)
    path = song(tmp_path)
    report = service.identify(path)
    saved = store.get_result(path)
    first = saved['data']['candidates'][0]
    assert report.candidates[0].releases == ('First Album', 'First Album (Deluxe)', 'Hits of 2020')
    assert [r['id'] for r in first['release_details']] == ['rel-1', 'rel-2', 'rel-3']
    assert first['release_details'][2]['secondary_types'] == ['Compilation']
    assert saved['data']['release_ambiguous'] is True
    assert saved['selected_release_id'] == ''


def test_musicbrainz_length_supplies_the_candidate_duration(tmp_path):
    musicbrainz = Mock(lookup_recording=Mock(return_value=MB_A))
    service, store = make_service(tmp_path, candidates=[cand()], musicbrainz=musicbrainz)
    path = song(tmp_path)
    service.identify(path)
    assert store.get_result(path)['data']['candidates'][0]['evidence']['duration'] == 'match'


def test_metadata_lookup_failure_is_recorded_as_a_warning(tmp_path):
    musicbrainz = Mock(lookup_recording=Mock(side_effect=APIError('HTTP 503', retryable=True, status=503)))
    service, store = make_service(tmp_path, candidates=[cand()], musicbrainz=musicbrainz)
    path = song(tmp_path)
    report = service.identify(path)
    assert report.status == RecognitionStatus.REVIEW, 'the acoustic match is still usable'
    assert 'metadata_lookup_failed' in report.warnings
    assert 'metadata_lookup_failed' in store.get_result(path)['data']['warnings']


# ── explained failures ───────────────────────────────────────────────────────

def failing_service(tmp_path, error, **options):
    service, store = make_service(tmp_path, **options)
    service.fingerprinter.compute.side_effect = error
    return service, store


def test_missing_fpcalc_is_reported_as_unavailable(tmp_path):
    service, store = failing_service(tmp_path, FingerprintUnavailable('fpcalc was not found'))
    path = song(tmp_path)
    report = service.identify(path)
    assert report.status == RecognitionStatus.ERROR
    assert report.code == 'fpcalc_unavailable'
    assert store.get_result(path) is None, 'a technical failure is not an identification result'


def test_undecodable_audio_is_reported_as_invalid_audio(tmp_path):
    service, _ = failing_service(tmp_path, FingerprintError('fpcalc returned 2: could not decode'))
    assert service.identify(song(tmp_path)).code == 'invalid_audio'


@pytest.mark.parametrize('error, code', [
    (APIError('HTTP 401', status=401), 'provider_auth'),
    (APIError('HTTP 403', status=403), 'provider_auth'),
    (APIError('HTTP 429', retryable=True, status=429), 'rate_limited'),
    (APIError('Network error: TimeoutError', retryable=True), 'network_error'),
    (APIError('HTTP 500', retryable=True, status=500), 'provider_unavailable'),
    (APIError('Invalid AcoustID response'), 'provider_error'),
])
def test_provider_failures_are_classified(tmp_path, error, code):
    service, _ = make_service(tmp_path, acoustid=Mock(lookup=Mock(side_effect=error)))
    report = service.identify(song(tmp_path))
    assert report.status == RecognitionStatus.ERROR
    assert report.code == code


def test_missing_acoustid_key_has_its_own_code(tmp_path):
    service, _ = make_service(tmp_path, acoustid=None)
    assert service.identify(song(tmp_path)).code == 'acoustid_key_missing'


def test_missing_file_is_reported(tmp_path):
    service, _ = make_service(tmp_path)
    assert service.identify(tmp_path / 'gone.mp3').code == 'file_missing'


def test_unexpected_exception_becomes_internal_error_with_details(tmp_path):
    service, _ = failing_service(tmp_path, RuntimeError('boom'))
    report = service.identify(song(tmp_path))
    assert report.code == 'internal_error'
    assert 'boom' in report.reason


def test_cancellation_propagates_and_persists_nothing(tmp_path):
    service, store = make_service(tmp_path, acoustid=Mock(lookup=Mock(side_effect=OperationCancelled('stop'))))
    path = song(tmp_path)
    with pytest.raises(OperationCancelled):
        service.identify(path)
    assert store.get_result(path) is None


def test_stages_are_announced_in_order(tmp_path):
    stages = []
    musicbrainz = Mock(lookup_recording=Mock(return_value=MB_A))
    service, _ = make_service(tmp_path, candidates=[cand()], musicbrainz=musicbrainz)
    service.identify(song(tmp_path), on_stage=stages.append)
    assert stages == ['FINGERPRINTING', 'IDENTIFYING', 'ENRICHING']


# ── outcome catalogue ────────────────────────────────────────────────────────

def test_every_outcome_has_a_label_and_a_hint():
    for code, outcome in OUTCOMES.items():
        assert outcome.label and outcome.hint, code


def test_configuration_errors_block_the_job_and_are_not_auto_retried():
    for code in ('fpcalc_unavailable', 'acoustid_key_missing', 'provider_auth'):
        assert OUTCOMES[code].blocks_job is True, code
    for code in ('network_error', 'rate_limited', 'provider_unavailable'):
        assert OUTCOMES[code].retryable is True and OUTCOMES[code].blocks_job is False, code
    assert OUTCOMES['invalid_audio'].retryable is False


def test_describe_falls_back_to_internal_error_for_unknown_codes():
    assert describe('not-a-code').label == OUTCOMES['internal_error'].label


# ── fingerprinter ────────────────────────────────────────────────────────────

def test_fingerprinter_reports_missing_executable_distinctly(tmp_path):
    path = song(tmp_path)
    runner = Mock(side_effect=FileNotFoundError(2, 'not found'))
    with pytest.raises(FingerprintUnavailable):
        Fingerprinter(runner=runner).compute(path)


def test_fingerprinter_availability_check_uses_the_search_path():
    assert Fingerprinter(which=lambda name: None).available() is False
    assert Fingerprinter(which=lambda name: f'/usr/bin/{name}').available() is True


def test_fingerprinter_timeout_is_a_fingerprint_error_not_unavailable(tmp_path):
    path = song(tmp_path)
    runner = Mock(side_effect=subprocess.TimeoutExpired('fpcalc', 1))
    with pytest.raises(FingerprintError) as caught:
        Fingerprinter(runner=runner).compute(path)
    assert not isinstance(caught.value, FingerprintUnavailable)
