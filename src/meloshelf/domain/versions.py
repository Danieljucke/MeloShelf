"""Structured recording versions and graded version verdicts.

Pure rules, no I/O. Version characteristics are parsed from titles with word-boundary matching, so
'Liveline' is not 'live'. Verdicts are graded: a catalogue title that omits a descriptor the local tag
carries is an ambiguity (possible conflict), not evidence of a different recording. Only two explicit,
different versions plus an independent duration mismatch, or an explicit catalogue descriptor that
contradicts the tag with a duration mismatch, is a confirmed conflict.

Recording version is a separate level from the musical work, the recording, the release and the exact
release edition. Nothing here selects an album edition.
"""
import re
import unicodedata
from dataclasses import dataclass, field

VERIFIED_AGREEMENT = 'verified_agreement'
PROBABLE_AGREEMENT = 'probable_agreement'
POSSIBLE_CONFLICT = 'possible_conflict'
CONFIRMED_CONFLICT = 'confirmed_conflict'
INSUFFICIENT = 'insufficient'
VERDICTS = (VERIFIED_AGREEMENT, PROBABLE_AGREEMENT, POSSIBLE_CONFLICT, CONFIRMED_CONFLICT, INSUFFICIENT)

# Characteristic -> word patterns. Deliberately excludes editions (deluxe) and presentation (official audio).
_PATTERNS = {
    'live': (r'live', r'in concert'),
    'spontaneous': (r'spontaneous',),
    'acoustic': (r'acoustic',),
    'remix': (r'remix(?:ed)?', r'rmx'),
    'remaster': (r'remaster(?:ed)?',),
    'radio_edit': (r'radio (?:edit|version)',),
    'extended': (r'extended(?: (?:mix|version|edit))?',),
    'instrumental': (r'instrumental',),
    'alternate_take': (r'(?:alternate|alt\.?) (?:take|version)', r'take \d+'),
    'demo': (r'demo',),
    'explicit': (r'explicit',),
    'clean': (r'clean(?: version)?',),
    'studio': (r'studio(?: version| recording)?',),
}
_COMPILED = {name: re.compile(r'\b(?:' + '|'.join(patterns) + r')\b') for name, patterns in _PATTERNS.items()}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize('NFKD', text or '')
    return ''.join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def characteristics(text: str) -> frozenset[str]:
    folded = _fold(text)
    return frozenset(name for name, pattern in _COMPILED.items() if pattern.search(folded))


@dataclass(frozen=True)
class VersionAssessment:
    verdict: str
    local: frozenset = frozenset()
    candidate: frozenset = frozenset()
    reasons: tuple = field(default=())

    def as_dict(self) -> dict:
        return {'verdict': self.verdict, 'local': sorted(self.local), 'candidate': sorted(self.candidate),
                'reasons': list(self.reasons)}


def assess_version(local_title: str, candidate_title: str, *, duration_state: str = 'unknown',
                   release_titles=(), catalog_descriptors=()) -> VersionAssessment:
    if not (local_title or '').strip() or not (candidate_title or '').strip():
        return VersionAssessment(INSUFFICIENT, reasons=('a title is missing',))
    local = characteristics(local_title)
    candidate = characteristics(candidate_title) | frozenset(_fold(d) for d in catalog_descriptors)
    release_support = frozenset().union(*(characteristics(t) for t in release_titles)) if release_titles else frozenset()
    duration_match = duration_state == 'match'

    if local == candidate:
        if local:
            verdict = VERIFIED_AGREEMENT if duration_match else PROBABLE_AGREEMENT
            return VersionAssessment(verdict, local, candidate, (f'both describe the same version: {", ".join(sorted(local))}',))
        if duration_match:
            return VersionAssessment(PROBABLE_AGREEMENT, local, candidate, ('both are standard versions and the length agrees',))
        return VersionAssessment(INSUFFICIENT, local, candidate, ('no version marker and no length evidence',))

    extra_local, extra_candidate = local - candidate, candidate - local
    if not extra_candidate and extra_local or not extra_local and extra_candidate:
        extra = extra_local or extra_candidate
        if extra <= release_support:
            return VersionAssessment(PROBABLE_AGREEMENT, local, candidate,
                                     (f'the catalogue release also says {", ".join(sorted(extra))}',))
        if duration_state == 'unknown':
            return VersionAssessment(INSUFFICIENT, local, candidate,
                                     (f'{", ".join(sorted(extra))} is on one side only and the length is unknown',))
        return VersionAssessment(POSSIBLE_CONFLICT, local, candidate,
                                 (f'{", ".join(sorted(extra))} is on one side only; the catalogue title may simply omit it',))

    # Both sides carry different explicit descriptors.
    if duration_state == 'mismatch':
        return VersionAssessment(CONFIRMED_CONFLICT, local, candidate,
                                 ('different explicit versions and the lengths differ',))
    return VersionAssessment(POSSIBLE_CONFLICT, local, candidate,
                             ('different explicit versions; the length does not settle it',))
