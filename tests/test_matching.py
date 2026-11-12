"""Candidate evidence scoring. The application score is a documented heuristic,
kept apart from the provider's raw score, and is never a probability."""
import pytest

from meloshelf.domain.matching import Evidence, assess_candidate, assess_match, normalize, similarity


def evidence(provider=0.97, title='Good Song', artist='Artist A', duration=None,
             local_title='', local_artist='', local_duration=None):
    return assess_candidate(provider_score=provider, title=title, artist=artist, duration=duration,
                            local_title=local_title, local_artist=local_artist, local_duration=local_duration)


# ── normalization ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('left, right', [
    ('Beyoncé', 'beyonce'),
    ('Blinding Lights (feat. Someone)', 'Blinding Lights'),
    ('The Weeknd feat. Daft Punk', 'The Weeknd'),
    ('The Weeknd ft Daft Punk', 'the weeknd'),
    ("Don't Start Now", 'Don’t Start Now'),
    ('  Hello  ', 'hello'),
    ('AC/DC', 'ac dc'),
])
def test_normalize_treats_cosmetic_variants_as_equal(left, right):
    assert normalize(left) == normalize(right)


def test_similarity_is_high_for_typos_and_low_for_different_titles():
    assert similarity('Blinding Lights', 'Blinding Lihgts') > 0.85
    assert similarity('Hello', 'Rolling in the Deep') < 0.5
    assert similarity('', 'anything') == 0.0


# ── single-candidate evidence ───────────────────────────────────────────────

def test_provider_score_is_reported_untouched_and_separately():
    result = evidence(provider=0.97)
    assert isinstance(result, Evidence)
    assert result.provider_score == 0.97
    assert isinstance(result.score, int) and 0 <= result.score <= 100
    assert result.score != round(0.97 * 100), 'the application score is not the provider score'


def test_fingerprint_alone_never_reaches_a_strong_score():
    result = evidence(provider=1.0)
    assert result.title == 'unknown' and result.artist == 'unknown' and result.duration == 'unknown'
    assert result.score < 80


def test_agreeing_tags_and_duration_raise_the_score():
    alone = evidence()
    supported = evidence(duration=182, local_duration=183, local_title='Good Song', local_artist='Artist A')
    assert (supported.title, supported.artist, supported.duration) == ('match', 'match', 'match')
    assert supported.duration_delta == pytest.approx(-1)
    assert supported.score > alone.score
    assert supported.score >= 90


def test_conflicting_reliable_tags_lower_the_score_and_are_flagged():
    alone = evidence()
    conflicting = evidence(local_title='Hello', local_artist='Adele')
    assert conflicting.title == 'conflict' and conflicting.artist == 'conflict'
    assert conflicting.score < alone.score
    assert any('title' in note.lower() for note in conflicting.notes)


@pytest.mark.parametrize('generic', ['000147', 'Track 01', 'track_0039', 'Unknown', 'audio 3', ''])
def test_generic_or_missing_local_titles_are_not_evidence(generic):
    result = evidence(local_title=generic, local_artist='Unknown Artist')
    assert result.title == 'unknown' and result.artist == 'unknown'
    assert result.score == evidence().score


def test_duration_difference_tiers():
    assert evidence(duration=180, local_duration=182).duration == 'match'
    assert evidence(duration=180, local_duration=186).duration == 'close'
    assert evidence(duration=180, local_duration=300).duration == 'mismatch'
    assert evidence(duration=180, local_duration=300).score < evidence().score
    assert evidence(duration=None, local_duration=300).duration == 'unknown'


def test_featured_artist_is_compatible_with_primary_artist():
    result = evidence(artist='The Weeknd', local_title='Good Song', local_artist='The Weeknd feat. Daft Punk')
    assert result.artist == 'match'


def test_live_and_studio_versions_are_distinguished():
    studio = evidence(title='Good Song', local_title='Good Song (Live)', local_artist='Artist A')
    live = evidence(title='Good Song (Live at Wembley)', local_title='Good Song (Live)', local_artist='Artist A')
    assert live.score > studio.score
    assert any('version' in note.lower() for note in studio.notes)


def test_score_is_clamped():
    assert evidence(provider=0.0, duration=10, local_duration=900, local_title='Zzz', local_artist='Qqq').score == 0
    assert evidence(provider=1.0, duration=182, local_duration=182, local_title='Good Song',
                    local_artist='Artist A').score <= 100


# ── overall assessment ───────────────────────────────────────────────────────

def test_no_candidates_means_no_match():
    assert assess_match([]).verdict == 'none'


def test_weak_evidence_stays_unknown():
    weak = evidence(provider=0.35, duration=120, local_duration=300)
    assert assess_match([weak]).verdict == 'insufficient'


def test_well_supported_single_candidate_is_strong():
    top = evidence(duration=182, local_duration=182, local_title='Good Song', local_artist='Artist A')
    assert assess_match([top]).verdict == 'strong'


def test_fingerprint_only_match_is_plausible_not_strong():
    assert assess_match([evidence(provider=0.97)]).verdict == 'plausible'


def test_two_close_candidates_are_ambiguous():
    first = evidence(provider=0.97, title='Good Song', artist='Artist A')
    second = evidence(provider=0.93, title='Different Song', artist='Artist B')
    verdict = assess_match([first, second])
    assert verdict.verdict == 'ambiguous'
    assert 'candidates' in verdict.reason.lower()


def test_tags_can_separate_otherwise_close_candidates():
    first = evidence(provider=0.95, title='Good Song', artist='Artist A', duration=182,
                     local_duration=182, local_title='Good Song', local_artist='Artist A')
    second = evidence(provider=0.94, title='Different Song', artist='Artist B', duration=240,
                      local_duration=182, local_title='Good Song', local_artist='Artist A')
    assert assess_match([first, second]).verdict == 'strong'


def test_best_candidate_contradicting_reliable_tags_is_a_conflict():
    top = evidence(provider=0.99, local_title='Hello', local_artist='Adele')
    verdict = assess_match([top])
    assert verdict.verdict == 'conflict'
    assert 'tag' in verdict.reason.lower()
