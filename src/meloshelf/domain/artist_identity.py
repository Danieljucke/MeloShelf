"""Linking a credit to a MusicBrainz artist ID. Pure rules.

Candidates are considered only when their normalised primary name or one of their aliases equals the credit's
normalised name and the score is at least SUGGEST_SCORE. Then:

* two or more distinct artist IDs among those candidates -> ambiguous; no link, a person must choose;
* a primary-name match scoring at least AUTO_SCORE, with no competing ID -> linked automatically;
* any other single-ID match (an alias, or a lower score) -> suggested; stored for review, never grouped on.

Similar names never link, and distinct IDs are never merged because their names are alike.
"""
from dataclasses import dataclass, field, replace

from meloshelf.domain.artists import canonical_key

SUGGEST_SCORE = 80
AUTO_SCORE = 95
LINKED, SUGGESTED, AMBIGUOUS, NO_MATCH = 'linked', 'suggested', 'ambiguous', 'no_match'
PROVENANCE = 'musicbrainz'


@dataclass(frozen=True)
class ArtistCandidate:
    mb_id: str
    name: str
    score: int
    aliases: tuple = ()
    disambiguation: str = ''


@dataclass(frozen=True)
class LinkDecision:
    status: str
    mb_id: str | None = None
    name: str = ''
    provenance: str = PROVENANCE
    reason: str = ''
    matched: tuple = field(default=())
    candidates: tuple = field(default=())   # every candidate the provider returned, as plain dicts


def describe(candidates) -> tuple:
    return tuple({'mb_artist_id': c.mb_id, 'name': c.name, 'score': c.score, 'aliases': list(c.aliases),
                  'disambiguation': c.disambiguation} for c in candidates)


def decide_link(credit: str, candidates) -> LinkDecision:
    candidates = list(candidates)
    return replace(_decide(credit, candidates), candidates=describe(candidates))


def _decide(credit: str, candidates) -> LinkDecision:
    key = canonical_key(credit)
    if not key:
        return LinkDecision(NO_MATCH, reason='the credit is empty')
    primary, alias = [], []
    for candidate in candidates:
        if candidate.score < SUGGEST_SCORE:
            continue
        if canonical_key(candidate.name) == key:
            primary.append(candidate)
        elif any(canonical_key(a) == key for a in candidate.aliases):
            alias.append(candidate)
    matches = primary + alias
    if not matches:
        return LinkDecision(NO_MATCH, reason='no candidate has this name or alias with a usable score')
    ids = {c.mb_id for c in matches}
    if len(ids) > 1:
        return LinkDecision(AMBIGUOUS, reason=f'{len(ids)} different artists share this name; a person must choose',
                            matched=tuple(sorted(ids)))
    best = max(primary, key=lambda c: c.score) if primary else None
    if best is not None and best.score >= AUTO_SCORE:
        return LinkDecision(LINKED, mb_id=best.mb_id, name=best.name, reason='name matches')
    chosen = best or max(alias, key=lambda c: c.score)
    how = 'name matches at a score below the automatic threshold' if best is not None else 'alias matches'
    return LinkDecision(SUGGESTED, mb_id=chosen.mb_id, name=chosen.name, reason=how)


ACCEPTED, REJECTED = 'accepted', 'rejected'
HUMAN_ACTIONS = ('accept', 'reject', 'review')


def active_decisions(decisions) -> list[dict]:
    """Decisions that still stand: undo removes the decision it names, and an undo can never be undone."""
    undone = {d['ref'] for d in decisions if d['action'] == 'undo'}
    return [d for d in sorted(decisions, key=lambda d: d['id'])
            if d['action'] in HUMAN_ACTIONS and d['id'] not in undone]


def is_historical(decision: dict) -> bool:
    """An accept or reject written before scopes existed: no album and no recording named."""
    return (not (decision.get('scope') or []) and not (decision.get('tracks') or [])
            and not (decision.get('instances') or []))


def covers_recording(decision: dict, track_path, instance_id) -> bool:
    """Does a recording-level decision name this recording?
    With the identity layer in use (instance_id is a string, possibly empty) only the physical file counts: a path
    alone never matches, so a new file at an old path does not inherit a decision. Without it (instance_id None, a pure
    domain caller) the recorded paths are used."""
    if instance_id is not None:
        return bool(instance_id) and instance_id in (decision.get('instances') or [])
    return track_path is not None and track_path in (decision.get('tracks') or [])


def effective_link(row, decisions, album_key=None, track_path=None, instance_id=None) -> tuple:
    """The link that applies to one recording, by the hierarchy:
      1. the latest standing decision naming this exact recording (track scope);
      2. else the latest standing decision naming this album (album scope);
      3. else a historical reject with no scope, which blocks a link and never creates one;
      4. else a strong, unambiguous provider link;
      5. else nothing.
    A historical ACCEPT with no scope is never applied: it no longer makes every recording with the name a match.
    Returns (state, mb_artist_id); state is accepted, rejected, linked or None."""
    standing = [d for d in active_decisions(decisions) if d['action'] in ('accept', 'reject')]
    by_track = [d for d in standing if covers_recording(d, track_path, instance_id)]
    if by_track:
        return _state(by_track[-1])
    by_album = [d for d in standing if (d.get('scope') or []) and album_key is not None and album_key in d['scope']]
    if by_album:
        return _state(by_album[-1])
    historical_rejects = [d for d in standing if is_historical(d) and d['action'] == 'reject']
    if historical_rejects:
        return REJECTED, historical_rejects[-1]['mb_artist_id']
    if row and row.get('status') == 'linked' and row.get('mb_artist_id'):
        return 'linked', row['mb_artist_id']
    return None, None


def _state(decision: dict) -> tuple:
    return (ACCEPTED if decision['action'] == 'accept' else REJECTED), decision['mb_artist_id']
