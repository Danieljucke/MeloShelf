"""Fallback recognition levels and candidate comparison. A metadata suggestion is never an acoustic match."""
import pytest

from meloshelf.domain.fallback import (ACOUSTICALLY_MATCHED, CATALOG_MATCHED, LEVELS, METADATA_SUGGESTED,
                                       UNIDENTIFIED, auto_approvable, evaluate_fallback, parse_filename)


def cand(rec_id, title, artist, duration, score=100):
    return {'recording_id': rec_id, 'title': title, 'artist': artist, 'duration': duration, 'search_score': score,
            'releases': []}


def test_the_four_levels_are_distinct_and_named_as_specified():
    assert LEVELS == (ACOUSTICALLY_MATCHED, CATALOG_MATCHED, METADATA_SUGGESTED, UNIDENTIFIED)
    assert len(set(LEVELS)) == 4


def test_a_title_artist_and_length_agreement_is_a_metadata_suggestion_not_an_acoustic_match():
    parsed = parse_filename('Aaron_Cole_-_Making_History_(Official_Audio).mp3')
    result = evaluate_fallback(parsed, 188.0, [cand('rec-1', 'Making History', 'Aaron Cole', 188.0)])
    assert result.level == METADATA_SUGGESTED
    assert result.chosen['recording_id'] == 'rec-1'
    assert result.level != ACOUSTICALLY_MATCHED
    assert not hasattr(result, 'provider_score')


def test_a_suggestion_is_never_eligible_for_automatic_approval():
    parsed = parse_filename('Aaron_Cole_-_Making_History.mp3')
    result = evaluate_fallback(parsed, 188.0, [cand('rec-1', 'Making History', 'Aaron Cole', 188.0)])
    assert auto_approvable(result.level) is False
    assert auto_approvable(ACOUSTICALLY_MATCHED) is True


def test_a_matching_title_with_a_different_artist_is_not_suggested():
    parsed = parse_filename('Aaron_Cole_-_Making_History.mp3')
    result = evaluate_fallback(parsed, 188.0, [cand('rec-x', 'Making History', 'Someone Else', 188.0)])
    assert result.level == UNIDENTIFIED and result.reason


def test_a_length_that_differs_by_forty_seconds_is_not_suggested():
    parsed = parse_filename('Aaron_Cole_-_Making_History.mp3')
    result = evaluate_fallback(parsed, 188.0, [cand('rec-1', 'Making History', 'Aaron Cole', 230.0)])
    assert result.level == UNIDENTIFIED and 'length' in result.reason


def test_a_version_descriptor_missing_from_the_catalogue_title_stays_pending_but_can_be_suggested():
    parsed = parse_filename('Band - Making History (Live).mp3')
    result = evaluate_fallback(parsed, 188.0, [cand('rec-2', 'Making History', 'Band', 188.0)])
    assert result.level == METADATA_SUGGESTED
    assert result.version_pending is True


def test_a_confirmed_version_contradiction_is_not_suggested():
    parsed = parse_filename('Band - Song (Acoustic).mp3')
    result = evaluate_fallback(parsed, 200.0, [cand('rec-3', 'Song (Live)', 'Band', 260.0)])
    assert result.level == UNIDENTIFIED


def test_two_equally_plausible_recordings_are_ambiguous_and_nothing_is_chosen():
    parsed = parse_filename('Band - Song.mp3')
    result = evaluate_fallback(parsed, 200.0, [cand('a', 'Song', 'Band', 200.0), cand('b', 'Song', 'Band', 201.0)])
    assert result.level == UNIDENTIFIED and result.chosen is None
    assert 'ambiguous' in result.reason


def test_without_an_artist_in_the_filename_a_title_and_length_match_is_not_enough():
    parsed = parse_filename('Bloc 5.mp3')
    result = evaluate_fallback(parsed, 200.0, [cand('a', 'Bloc 5', 'Anyone', 200.0)])
    assert result.level == UNIDENTIFIED


def test_unicode_artist_names_agree_across_forms():
    parsed = parse_filename('Beyoncé - Song.mp3')
    result = evaluate_fallback(parsed, 200.0, [cand('a', 'Song', 'Beyonce', 200.0)])
    assert result.level == METADATA_SUGGESTED


def test_the_result_explains_its_evidence_in_words():
    parsed = parse_filename('Aaron_Cole_-_Making_History.mp3')
    result = evaluate_fallback(parsed, 188.0, [cand('rec-1', 'Making History', 'Aaron Cole', 188.0)])
    assert result.evidence and all(isinstance(line, str) for line in result.evidence)


def test_a_suggestion_needs_a_length_that_agrees_because_text_alone_is_weak():
    # Real case: an unreadable file was suggested from text alone; its length was never checked.
    parsed = parse_filename('Chris_Tomlin_-_The_Name_of_Jesus.mp3')
    result = evaluate_fallback(parsed, None, [cand('a', 'The Name of Jesus', 'Chris Tomlin', 240.0)])
    assert result.level == UNIDENTIFIED and 'length could not be checked' in result.reason
