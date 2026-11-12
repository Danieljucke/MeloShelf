"""Version-aware recognition: structured characteristics, graded verdicts, and the real qualification cases.

A missing word in a catalogue title is an ambiguity, not proof of a different recording. Only explicit,
independent contradictions are confirmed conflicts.
"""
import pytest

from meloshelf.domain.versions import (CONFIRMED_CONFLICT, INSUFFICIENT, POSSIBLE_CONFLICT, PROBABLE_AGREEMENT,
                                       VERDICTS, VERIFIED_AGREEMENT, assess_version, characteristics)


# ── structured characteristics ─────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('Reckless Love (Spontaneous) [Live]', {'live', 'spontaneous'}),
    ('Pieces (Spontaneous) [Live] (320)', {'live', 'spontaneous'}),
    ('Our God (acoustic version)', {'acoustic'}),
    ('Song (Radio Edit)', {'radio_edit'}),
    ('Song - Extended Mix', {'extended'}),
    ('Song (Remastered 2011)', {'remaster'}),
    ('Song (Instrumental)', {'instrumental'}),
    ('Song (Alternate Take)', {'alternate_take'}),
    ('Song (Demo)', {'demo'}),
    ('Song (Explicit)', {'explicit'}),
    ('Song (Clean Version)', {'clean'}),
    ('Song (Studio Version)', {'studio'}),
    ('Song (Deluxe Edition)', set()),          # an edition, not a recording version
    ('Song (Official Audio)', set()),
    ('Song (Lyric Video)', set()),
])
def test_version_characteristics_are_structured_not_free_text(text, expected):
    assert characteristics(text) == frozenset(expected)


def test_words_inside_other_words_are_not_version_markers():
    assert characteristics('Liveline Mixtape Cleaner Demolition') == frozenset()


def test_unicode_and_diacritics_do_not_hide_version_markers():
    assert characteristics('Café Live') == frozenset({'live'})
    assert characteristics('CAFÉ LIVE') == frozenset({'live'})


# ── the qualification cases ────────────────────────────────────────────────────

def test_reckless_love_spontaneous_live_versus_bare_catalogue_title_is_only_a_possible_conflict():
    result = assess_version('Reckless Love (Spontaneous) [Live]', 'Reckless Love',
                            duration_state='match', release_titles=('Moments: Mighty Sound',))
    assert result.verdict == POSSIBLE_CONFLICT
    assert result.verdict != CONFIRMED_CONFLICT


def test_a_catalogue_release_that_says_live_turns_the_omitted_suffix_into_probable_agreement():
    result = assess_version('Victory Is Yours (Live)', 'Victory Is Yours',
                            duration_state='match', release_titles=('Victory (Live)',))
    assert result.verdict == PROBABLE_AGREEMENT


def test_pieces_spontaneous_without_release_support_stays_a_possible_conflict():
    result = assess_version('Pieces (Spontaneous) [Live] (320)', 'Pieces (spontaneous)',
                            duration_state='match', release_titles=('Moments: Mighty Sound',))
    assert result.verdict == POSSIBLE_CONFLICT


def test_our_god_acoustic_agrees_with_an_acoustic_candidate_and_is_not_the_standard_version():
    result = assess_version('Our God (Acoustic)', 'Our God (acoustic version)', duration_state='match')
    assert result.verdict == VERIFIED_AGREEMENT
    assert result.local == frozenset({'acoustic'}) and result.candidate == frozenset({'acoustic'})


def test_our_god_acoustic_against_a_standard_candidate_is_not_treated_as_agreement():
    result = assess_version('Our God (Acoustic)', 'Our God', duration_state='match')
    assert result.verdict == POSSIBLE_CONFLICT


def test_all_that_matters_standard_versions_agree_without_any_edition_being_chosen():
    result = assess_version('All That Matters', 'All That Matters', duration_state='match',
                            release_titles=('Identity', 'Identity (Deluxe Edition)'))
    assert result.verdict == PROBABLE_AGREEMENT
    assert 'edition' not in result.as_dict()


def test_an_unusual_filename_like_title_is_not_rejected_or_flagged():
    result = assess_version('So gone Easy DaveJ V1.4.mp3 (Official Audio)', 'So gone Easy DaveJ V1.4.mp3',
                            duration_state='match')
    assert result.verdict == PROBABLE_AGREEMENT
    assert result.local == frozenset() and result.candidate == frozenset()


# ── explicit contradictions are confirmed only when independent evidence agrees ─

def test_two_explicit_different_versions_with_a_duration_mismatch_are_a_confirmed_conflict():
    result = assess_version('Song (Live)', 'Song (Acoustic)', duration_state='mismatch')
    assert result.verdict == CONFIRMED_CONFLICT


def test_different_explicit_versions_without_a_duration_mismatch_are_only_possible():
    result = assess_version('Song (Live)', 'Song (Acoustic)', duration_state='match')
    assert result.verdict == POSSIBLE_CONFLICT


def test_a_catalogue_descriptor_that_contradicts_the_tag_is_a_confirmed_conflict_when_duration_also_differs():
    result = assess_version('Song (Live)', 'Song', duration_state='mismatch', catalog_descriptors=('studio',))
    assert result.verdict == CONFIRMED_CONFLICT


# ── insufficient evidence ──────────────────────────────────────────────────────

def test_missing_titles_or_no_duration_evidence_is_insufficient_not_a_conflict():
    assert assess_version('', 'Song', duration_state='match').verdict == INSUFFICIENT
    assert assess_version('Song (Live)', 'Song', duration_state='unknown').verdict == INSUFFICIENT


def test_absent_catalogue_version_labels_are_not_read_as_a_standard_recording():
    result = assess_version('Song (Live)', 'Song', duration_state='unknown', catalog_descriptors=())
    assert result.verdict != CONFIRMED_CONFLICT


def test_every_verdict_is_one_of_the_five_documented_levels():
    assert set(VERDICTS) == {VERIFIED_AGREEMENT, PROBABLE_AGREEMENT, POSSIBLE_CONFLICT, CONFIRMED_CONFLICT, INSUFFICIENT}


def test_the_assessment_exposes_its_reasons_as_structured_data():
    data = assess_version('Reckless Love (Spontaneous) [Live]', 'Reckless Love', duration_state='match').as_dict()
    assert data['verdict'] == POSSIBLE_CONFLICT
    assert data['local'] == ['live', 'spontaneous'] and data['candidate'] == []
    assert data['reasons'] and isinstance(data['reasons'][0], str)
