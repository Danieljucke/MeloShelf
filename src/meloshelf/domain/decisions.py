"""Automatic recording decisions.

The MeloShelf score is a ranking heuristic, not a probability of being correct.
Automatic approval confirms only the recording identity. It never chooses an
album edition, never changes a file, and never overrides a user decision.

Options:
    auto_enabled      turn automatic approval off entirely (everything goes to review)
    strict            require at least one agreeing tag or duration before approving;
                      contradictions always block approval, in both modes
    review_ambiguous  keep ambiguous results in the review queue; when off, they are
                      left unidentified instead of being offered for review
"""
from meloshelf.domain.matching import Evidence, assess_match

DEFAULT_THRESHOLD = 80
# Automatic approval also needs a strong fingerprint match from the provider, whatever the heuristic score.
STRONG_FINGERPRINT = 0.85
# A competitor this plausible, this close in score, makes the lead insufficient for approval.
COMPETITOR_FLOOR = 60
COMPETITOR_MARGIN = 20
THRESHOLD_RANGE = (50, 100)

AUTO_APPROVED = 'AUTO_APPROVED'
USER_APPROVED = 'USER_APPROVED'
NEEDS_REVIEW = 'NEEDS_REVIEW'
REJECTED = 'REJECTED'
UNIDENTIFIED = 'UNIDENTIFIED'
EDITION_PENDING = 'EDITION_PENDING'

DEFAULT_POLICY = {'threshold': DEFAULT_THRESHOLD, 'auto_enabled': True, 'strict': True, 'review_ambiguous': True}


def _corroborated(top: Evidence) -> bool:
    """Strict approval needs a title that agrees, or an artist that agrees together with a matching length.
    A length alone, or an artist name alone, does not single out one recording."""
    return top.title == 'match' or (top.artist == 'match' and top.duration == 'match')


def decide(top: Evidence | None, verdict: str, *, threshold: int = DEFAULT_THRESHOLD,
           user_decision: str = '', auto_enabled: bool = True, strict: bool = True,
           review_ambiguous: bool = True) -> str:
    """Recording status for the best candidate, given the overall verdict."""
    if user_decision == 'rejected':
        return REJECTED
    if user_decision == 'accepted':
        return USER_APPROVED
    if top is None or verdict in ('none', 'insufficient'):
        return UNIDENTIFIED
    if verdict == 'ambiguous' and not review_ambiguous:
        return UNIDENTIFIED
    if verdict in ('ambiguous', 'conflict'):
        return NEEDS_REVIEW
    # A partial title disagreement and a confirmed version contradiction are conflicting evidence: they stay in review.
    # A possible version difference does not contradict the recording; it keeps the version pending (Evidence.version_pending).
    contradicted = (top.title in ('conflict', 'partial') or top.artist == 'conflict' or top.duration == 'mismatch'
                    or top.version_verdict == 'confirmed_conflict')
    if contradicted or not auto_enabled or top.provider_score < STRONG_FINGERPRINT:
        return NEEDS_REVIEW
    if top.score >= threshold and (not strict or _corroborated(top)):
        return AUTO_APPROVED
    return NEEDS_REVIEW


def status_for(evidence: list[Evidence], *, user_decision: str = '', **policy) -> str:
    """Recording status for a ranked list of candidate evidence (best first)."""
    options = {**DEFAULT_POLICY, **policy}
    verdict = assess_match(evidence).verdict
    if verdict == 'strong' and len(evidence) > 1:
        rival = evidence[1]
        if rival.score >= COMPETITOR_FLOOR and evidence[0].score - rival.score < COMPETITOR_MARGIN:
            verdict = 'ambiguous'  # a close, plausible rival: the lead is not meaningful separation
    return decide(evidence[0] if evidence else None, verdict, user_decision=user_decision, **options)


def edition_state(release_count: int) -> str:
    """Several albums can contain the recording; choosing one stays with the user or later evidence."""
    return EDITION_PENDING if release_count > 1 else ''
