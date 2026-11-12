"""Automatic recording approval policy.

MeloShelf's score is a ranking heuristic, not a probability. Auto-approval only
confirms the recording identity; it never picks an album edition and never
changes a file. A user decision always wins over the automatic one.
"""
from unittest.mock import Mock

from meloshelf.application.recognition import RecognitionService, RecognitionStatus
from meloshelf.domain.decisions import (AUTO_APPROVED, DEFAULT_THRESHOLD, NEEDS_REVIEW, REJECTED, UNIDENTIFIED,
                                        USER_APPROVED, decide, edition_state, status_for)
from meloshelf.domain.matching import Evidence, assess_match
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.fingerprint import Fingerprint
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import RecordingCandidate


def ev(score, *, title='match', artist='match', duration='match', provider=0.97):
    return Evidence(provider_score=provider, score=score, title=title, artist=artist, duration=duration,
                    duration_delta=0.0 if duration == 'match' else None)


def auto(evidence, **options):
    verdict = assess_match(evidence).verdict
    return decide(evidence[0] if evidence else None, verdict, **options)


# ── the ten acceptance cases ─────────────────────────────────────────────────

def test_score_84_with_consistent_evidence_is_auto_approved():
    assert auto([ev(84)]) == AUTO_APPROVED


def test_score_exactly_80_with_consistent_evidence_is_auto_approved():
    assert auto([ev(80)]) == AUTO_APPROVED


def test_score_79_needs_review():
    assert auto([ev(79)]) == NEEDS_REVIEW


def test_score_95_with_strong_conflicting_title_needs_review():
    assert auto([ev(95, title='conflict')]) == NEEDS_REVIEW


def test_score_95_with_conflicting_duration_needs_review():
    assert auto([ev(95, duration='mismatch')]) == NEEDS_REVIEW


def test_two_similarly_supported_candidates_need_review():
    assert auto([ev(90), ev(86, title='partial')]) == NEEDS_REVIEW


def test_high_acoustid_confidence_with_low_meloshelf_score_is_not_auto_approved():
    weak = ev(64, provider=0.98, title='unknown', artist='unknown', duration='unknown')
    assert auto([weak]) == NEEDS_REVIEW


def test_a_user_rejection_is_never_overridden_by_automatic_approval():
    assert auto([ev(95)], user_decision='rejected') == REJECTED


def test_automatic_approval_survives_a_restart(tmp_path):
    path = tmp_path / 'song.mp3'
    path.write_bytes(b'audio')
    acoustid = Mock(lookup=Mock(return_value=[RecordingCandidate(
        '11111111-1111-4111-8111-111111111111', 'Good Song', 'Artist A', 0.97, duration=182)]))
    service = RecognitionService(Mock(compute=Mock(return_value=Fingerprint('fp', 182))), acoustid, None,
                                 MatchStore(tmp_path / 'lib.sqlite3'))
    report = service.identify(path, tags={'title': 'Good Song', 'artist': 'Artist A'})
    assert report.recording_status == AUTO_APPROVED and report.status == RecognitionStatus.REVIEW

    reopened = MatchStore(Database(tmp_path / 'lib.sqlite3'))
    assert reopened.recording_status(path) == AUTO_APPROVED


def test_an_identified_file_is_not_looked_up_again(tmp_path):
    path = tmp_path / 'song.mp3'
    path.write_bytes(b'audio')
    fingerprinter = Mock(compute=Mock(return_value=Fingerprint('fp', 182)))
    acoustid = Mock(lookup=Mock(return_value=[RecordingCandidate(
        '11111111-1111-4111-8111-111111111111', 'Good Song', 'Artist A', 0.97, duration=182)]))
    service = RecognitionService(fingerprinter, acoustid, None, MatchStore(tmp_path / 'lib.sqlite3'))
    service.identify(path, tags={'title': 'Good Song', 'artist': 'Artist A'})
    service.identify(path, tags={'title': 'Good Song', 'artist': 'Artist A'})
    assert acoustid.lookup.call_count == 1 and fingerprinter.compute.call_count == 1


def test_changing_the_threshold_reevaluates_without_destroying_user_decisions(tmp_path):
    store = MatchStore(tmp_path / 'lib.sqlite3')
    candidate = {'recording_id': '1', 'title': 'Good Song', 'artist': 'Artist A', 'acoustid_score': .97,
                 'releases': [], 'duration': None, 'release_details': [],
                 'evidence': {'provider_score': .97, 'score': 84, 'title': 'match', 'artist': 'match',
                              'duration': 'match', 'duration_delta': 0.0, 'notes': []}}
    store.put_result('kept.mp3', 1, 1, 'review', {'candidates': [candidate], 'reason': 'r', 'assessment': 'strong'},
                     recording_status=AUTO_APPROVED)
    store.put_result('mine.mp3', 1, 1, 'review', {'candidates': [candidate], 'reason': 'r', 'assessment': 'strong'},
                     recording_status=AUTO_APPROVED)
    store.set_decision('mine.mp3', 'accepted', recording_id='1')
    store.put_result('refused.mp3', 1, 1, 'review', {'candidates': [candidate], 'reason': 'r', 'assessment': 'strong'},
                     recording_status=AUTO_APPROVED)
    store.set_decision('refused.mp3', 'rejected')

    assert store.get_setting('auto_approve_threshold', DEFAULT_THRESHOLD) == 80
    store.set_setting('auto_approve_threshold', 90)
    store.reevaluate()

    assert store.recording_status('kept.mp3') == NEEDS_REVIEW, 'score 84 is below the new threshold of 90'
    assert store.recording_status('mine.mp3') == USER_APPROVED
    assert store.get_result('mine.mp3')['decision'] == 'accepted', 'the user decision is not touched'
    assert store.recording_status('refused.mp3') == REJECTED


def test_threshold_and_decisions_persist_across_restarts(tmp_path):
    first = MatchStore(tmp_path / 'lib.sqlite3')
    first.set_setting('auto_approve_threshold', 85)
    second = MatchStore(Database(tmp_path / 'lib.sqlite3'))
    assert second.get_setting('auto_approve_threshold', DEFAULT_THRESHOLD) == 85


def test_threshold_must_be_a_whole_number_between_50_and_100(tmp_path):
    store = MatchStore(tmp_path / 'lib.sqlite3')
    for invalid in (49, 101, 80.5, 'high'):
        try:
            store.set_setting('auto_approve_threshold', invalid)
        except ValueError:
            continue
        raise AssertionError(f'{invalid!r} must be rejected')
    store.set_setting('auto_approve_threshold', 100)
    assert store.get_setting('auto_approve_threshold', DEFAULT_THRESHOLD) == 100


# ── helpers ──────────────────────────────────────────────────────────────────

def test_multiple_album_editions_do_not_block_recording_approval():
    assert auto([ev(90)]) == AUTO_APPROVED
    assert edition_state(3) == 'EDITION_PENDING'
    assert edition_state(1) == ''


def test_no_candidates_are_unidentified_not_review():
    assert auto([]) == UNIDENTIFIED


# ── false-positive audit: each case must stay out of automatic approval ──

def ev_provider(score, provider, **kw):
    base = dict(title='match', artist='match', duration='match')
    base.update(kw)
    return Evidence(provider_score=provider, score=score, title=base['title'], artist=base['artist'],
                    duration=base['duration'], duration_delta=0.0 if base['duration'] == 'match' else None)


def test_weak_fingerprint_match_is_not_approved_even_when_tags_and_length_agree():
    """A low provider score with agreeing tags is not a strong fingerprint match."""
    assert status_for([ev_provider(85, 0.70)]) == NEEDS_REVIEW


def test_a_strong_fingerprint_with_agreeing_tags_is_still_approved():
    assert status_for([ev_provider(85, 0.95)]) == AUTO_APPROVED


def test_a_plausible_competitor_within_twenty_points_blocks_approval():
    """Two recordings both plausible and close in score: the lead is not meaningful separation."""
    top = ev_provider(88, 0.97)
    rival = ev_provider(70, 0.90, title='partial', artist='unknown', duration='unknown')
    assert status_for([top, rival]) == NEEDS_REVIEW


def test_a_clear_lead_over_a_weak_competitor_is_approved():
    top = ev_provider(88, 0.97)
    weak = ev_provider(40, 0.60, title='unknown', artist='unknown', duration='unknown')
    assert status_for([top, weak]) == AUTO_APPROVED


def test_a_length_match_with_a_different_title_is_a_contradiction_not_a_confirmation():
    """Same length, different title: the tags disagree with the recording."""
    assert status_for([ev_provider(92, 0.97, title='conflict', artist='unknown')]) == NEEDS_REVIEW


def test_an_artist_and_length_match_with_an_unknown_title_still_needs_a_second_recording_check():
    """Artist and length can describe a different track by the same artist; the title is missing,
    so the identity is not confirmed by a title and the decision stays with the user."""
    assert status_for([ev_provider(86, 0.85, title='unknown')]) == AUTO_APPROVED
    assert status_for([ev_provider(86, 0.84, title='unknown')]) == NEEDS_REVIEW


# ── found in the first real recognition run (qualification sample) ──────────────

def test_a_partial_title_against_the_file_tag_is_conflicting_evidence_not_approval():
    # Real case: tag "Better Because Of It", candidate "Because of You". Artist and length agree, title does not.
    evidence = [Evidence(provider_score=0.956, score=82, title='partial', artist='match', duration='match',
                         duration_delta=-1.0)]
    assert auto(evidence) == NEEDS_REVIEW


def test_a_different_version_of_the_same_song_is_not_approved_automatically():
    # Real case: tag "Reckless Love (Spontaneous) [Live]", candidate "Reckless Love" (standard version).
    evidence = [Evidence(provider_score=0.998, score=90, title='match', artist='match', duration='match',
                         duration_delta=-1.0, notes=('Version differs: live in the tag, standard in the candidate',),
                         version_verdict='confirmed_conflict', version_conflict=True)]
    assert auto(evidence) == NEEDS_REVIEW


def test_a_title_that_agrees_with_no_version_difference_is_still_approved():
    evidence = [ev(90)]
    assert auto(evidence) == AUTO_APPROVED


def test_evidence_stored_before_the_version_flag_still_blocks_a_version_conflict():
    # Results recorded before the flag existed carry only the note; reevaluation must still see the conflict.
    from meloshelf.domain.matching import Evidence as Stored
    stored = {'provider_score': 0.998, 'score': 90, 'title': 'match', 'artist': 'match', 'duration': 'match',
              'duration_delta': -1.0, 'notes': ['Version differs: live in the tag, standard in the candidate']}
    assert Stored.from_stored(stored).version_conflict is True
    # Round 2 rule: a possible difference keeps the recording approval and leaves the version pending.
    assert auto([Stored.from_stored(stored)]) == AUTO_APPROVED
    assert Stored.from_stored(stored).version_pending is True


# ── Round 2: version verdicts gate the version, not the recording identity ───────

def ev_version(verdict, score=90, provider=0.998):
    from meloshelf.domain.matching import Evidence as E
    return E(provider_score=provider, score=score, title='match', artist='match', duration='match',
             duration_delta=0.0, version_verdict=verdict)


def test_a_possible_version_conflict_keeps_a_confident_recording_approval_with_the_version_pending():
    from meloshelf.domain.versions import POSSIBLE_CONFLICT
    from meloshelf.domain.decisions import status_for
    evidence = [ev_version(POSSIBLE_CONFLICT)]
    assert status_for(evidence) == AUTO_APPROVED
    assert evidence[0].version_pending is True


def test_a_confirmed_version_conflict_blocks_the_recording_approval():
    from meloshelf.domain.versions import CONFIRMED_CONFLICT
    from meloshelf.domain.decisions import status_for
    assert status_for([ev_version(CONFIRMED_CONFLICT)]) == NEEDS_REVIEW


def test_verified_or_probable_agreement_leaves_nothing_pending():
    from meloshelf.domain.versions import PROBABLE_AGREEMENT, VERIFIED_AGREEMENT
    assert ev_version(VERIFIED_AGREEMENT).version_pending is False
    assert ev_version(PROBABLE_AGREEMENT).version_pending is False


def test_assessing_a_real_case_populates_the_version_verdict_from_titles_and_releases():
    from meloshelf.domain.matching import assess_candidate
    from meloshelf.domain.versions import POSSIBLE_CONFLICT
    evidence = assess_candidate(provider_score=0.998, title='Reckless Love', artist='Bethel Music', duration=599,
                                local_title='Reckless Love (Spontaneous) [Live]', local_artist='Bethel Music & Steffany Gretzinger',
                                local_duration=599, releases=('Moments: Mighty Sound',))
    assert evidence.version_verdict == POSSIBLE_CONFLICT
    assert evidence.version_pending is True
