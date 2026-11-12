"""Fallback recognition levels and candidate comparison. Pure rules.

Levels:
  ACOUSTICALLY_MATCHED  an acoustic match from the fingerprint provider (never produced here)
  CATALOG_MATCHED       reserved: a catalogue recording confirmed by an independent identifier (not produced yet)
  METADATA_SUGGESTED    text evidence agrees with a catalogue recording; for review only
  UNIDENTIFIED          no reliable suggestion

A suggestion requires the title to agree (after removing version descriptors, which are compared separately),
the artist to agree, the length not to contradict, no confirmed version conflict, and a clear lead over other
plausible recordings. A suggestion is never eligible for automatic approval.
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from meloshelf.domain.metadata import canonical_text, credits_agree
from meloshelf.domain.versions import assess_version

ACOUSTICALLY_MATCHED = 'ACOUSTICALLY_MATCHED'
CATALOG_MATCHED = 'CATALOG_MATCHED'
METADATA_SUGGESTED = 'METADATA_SUGGESTED'
UNIDENTIFIED = 'UNIDENTIFIED'
LEVELS = (ACOUSTICALLY_MATCHED, CATALOG_MATCHED, METADATA_SUGGESTED, UNIDENTIFIED)

SUGGESTION_FLOOR = 70
AMBIGUITY_MARGIN = 10
_DESCRIPTOR = re.compile(r'\s*[\(\[][^\)\]]*[\)\]]')


@dataclass(frozen=True)
class FallbackResult:
    level: str
    chosen: dict | None = None
    reason: str = ''
    evidence: tuple = ()
    version_pending: bool = False
    alternatives: tuple = field(default=())


def auto_approvable(level: str) -> bool:
    """Only an acoustic match may ever be approved automatically."""
    return level == ACOUSTICALLY_MATCHED


def _base_title(title: str) -> str:
    return canonical_text(_DESCRIPTOR.sub('', title or ''))


def _title_state(parsed_title: str, candidate_title: str) -> str:
    a, b = _base_title(parsed_title), _base_title(candidate_title)
    if not a or not b:
        return 'unknown'
    if a == b or SequenceMatcher(None, a, b).ratio() >= 0.92:
        return 'match'
    return 'conflict'


def _duration_state(local: float | None, remote: float | None) -> tuple[str, float | None]:
    if not local or not remote:
        return 'unknown', None
    delta = abs(local - remote)
    if delta <= 3:
        return 'match', delta
    if delta <= 10:
        return 'close', delta
    return 'mismatch', delta


def _compare(parsed, local_duration, candidate) -> dict:
    title = _title_state(parsed.title, candidate.get('title', ''))
    if not parsed.artist:
        artist = 'unknown'
    else:
        artist = 'match' if credits_agree(parsed.artist, candidate.get('artist', '')) else 'conflict'
    duration, delta = _duration_state(local_duration, candidate.get('duration'))
    version = assess_version(parsed.title, candidate.get('title', ''), duration_state=duration,
                             release_titles=tuple(candidate.get('releases') or ()))
    failures = []
    if duration == 'unknown':
        failures.append('the length could not be checked, so text alone is not enough to suggest a recording')
    if title != 'match':
        failures.append('the title does not agree')
    if artist == 'unknown':
        failures.append('the filename gives no artist to compare')
    elif artist == 'conflict':
        failures.append('the artist does not agree')
    if duration == 'mismatch':
        failures.append(f'the length differs by {delta:.0f} s')
    if version.verdict == 'confirmed_conflict':
        failures.append('the version is in confirmed conflict')
    score = 0
    if title == 'match':
        score += 50
    if artist == 'match':
        score += 30
    if duration == 'match':
        score += 20
    elif duration == 'close':
        score += 10
    if version.verdict == 'possible_conflict':
        score -= 10
    return {'candidate': candidate, 'score': score, 'failures': failures, 'version': version,
            'duration': duration, 'delta': delta, 'artist': artist, 'title': title}


def evaluate_fallback(parsed, local_duration, candidates) -> FallbackResult:
    if not parsed.title:
        return FallbackResult(UNIDENTIFIED, reason='the filename gives no title to search or compare')
    if not candidates:
        return FallbackResult(UNIDENTIFIED, reason='the catalogue returned no recordings for this text')
    compared = [_compare(parsed, local_duration, c) for c in candidates]
    qualifying = sorted((c for c in compared if not c['failures'] and c['score'] >= SUGGESTION_FLOOR),
                        key=lambda c: c['score'], reverse=True)
    if not qualifying:
        best = max(compared, key=lambda c: c['score'])
        reason = '; '.join(best['failures']) or 'no candidate is consistent enough to suggest'
        return FallbackResult(UNIDENTIFIED, reason=reason)
    top = qualifying[0]
    if len(qualifying) > 1 and top['score'] - qualifying[1]['score'] < AMBIGUITY_MARGIN:
        return FallbackResult(UNIDENTIFIED,
                              reason=f'ambiguous: {len(qualifying)} recordings fit this text equally well',
                              alternatives=tuple(c['candidate']['recording_id'] for c in qualifying))
    evidence = [f'title agrees: “{top["candidate"].get("title", "")}”',
                f'artist agrees: {top["candidate"].get("artist", "")}']
    if top['duration'] == 'match':
        evidence.append(f'length agrees within {top["delta"]:.0f} s')
    elif top['duration'] == 'close':
        evidence.append(f'length is close ({top["delta"]:.0f} s difference)')
    evidence.append('text evidence only: not acoustically verified')
    if top['version'].verdict != 'verified_agreement':
        evidence.append(f'version: {top["version"].verdict.replace("_", " ")}')
    return FallbackResult(METADATA_SUGGESTED, chosen=top['candidate'], evidence=tuple(evidence),
                          version_pending=top['version'].verdict not in ('verified_agreement', 'probable_agreement'),
                          alternatives=tuple(c['candidate']['recording_id'] for c in qualifying[1:]))
