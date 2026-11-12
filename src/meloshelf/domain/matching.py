"""Evidence scoring for recording candidates.

The application score is a transparent heuristic on a 0–100 scale used only to
rank candidates and to decide how a proposal is presented. It is not a
probability, and it is always reported next to — never instead of — the
provider's own match score.

    base      provider score × 60          (a fingerprint alone tops out at 60)
    duration  ±3 s: +15   ±10 s: +5   beyond: −20
    title     agrees: +15   contradicts a reliable tag: −15
    artist    agrees: +10   contradicts a reliable tag: −10
    version   same marker (live, remix…): +5   different: −10
"""
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from meloshelf.domain.models import is_generic_title, is_unknown_name
from meloshelf.domain.versions import assess_version

# Points per version verdict: agreement helps, a possible difference costs a little, a confirmed one costs more.
VERSION_POINTS = {'verified_agreement': 5, 'probable_agreement': 3, 'possible_conflict': -5,
                  'confirmed_conflict': -10, 'insufficient': 0}

STRONG_SCORE = 80
MINIMUM_SCORE = 35
CLEAR_MARGIN = 15
_VERSION = re.compile(r'\b(live|remix|remaster(?:ed)?|acoustic|instrumental|demo|karaoke|extended|radio edit|cover)\b')
_FEATURING = re.compile(r'\b(?:feat|ft|featuring)\b\.?.*$')
_BRACKETS = re.compile(r'[(\[{][^)\]}]*[)\]}]')


@dataclass(frozen=True)
class Evidence:
    provider_score: float
    score: int
    title: str      # match | partial | conflict | unknown
    artist: str     # match | partial | conflict | unknown
    duration: str   # match | close | mismatch | unknown
    duration_delta: float | None
    notes: tuple[str, ...] = ()
    # Version relation between the tag and the candidate (domain/versions.py). Only a confirmed conflict blocks
    # the recording; a possible conflict leaves the recording decision intact and keeps the version pending.
    version_verdict: str = 'insufficient'
    version_conflict: bool = False

    @property
    def version_pending(self) -> bool:
        return self.version_verdict not in ('verified_agreement', 'probable_agreement')

    @classmethod
    def from_stored(cls, data: dict) -> 'Evidence':
        """Rebuild evidence saved by an earlier version. Results saved before the flag existed still
        carry the 'Version differs' note, which is enough to recognise the conflict."""
        notes = tuple(data.get('notes') or ())
        verdict = data.get('version_verdict')
        if not verdict:  # results stored before verdicts existed: a recorded difference is at least a possible conflict
            legacy = bool(data.get('version_conflict')) or any(str(n).startswith('Version differs') for n in notes)
            verdict = 'possible_conflict' if legacy else 'insufficient'
        return cls(provider_score=data['provider_score'], score=data['score'], title=data['title'],
                   artist=data['artist'], duration=data['duration'], duration_delta=data.get('duration_delta'),
                   notes=notes, version_verdict=verdict,
                   version_conflict=verdict in ('possible_conflict', 'confirmed_conflict'))


@dataclass(frozen=True)
class Assessment:
    verdict: str    # strong | plausible | ambiguous | conflict | insufficient | none
    reason: str


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize('NFKD', text or '')
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def normalize(text: str) -> str:
    """Comparable form: no accents, case, bracketed additions, featured artists or punctuation."""
    folded = _FEATURING.sub('', _BRACKETS.sub(' ', _fold(text)))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', folded).split())


def similarity(left: str, right: str) -> float:
    a, b = normalize(left), normalize(right)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _versions(text: str) -> frozenset[str]:
    return frozenset(m.replace('remastered', 'remaster') for m in _VERSION.findall(_fold(text)))


def _compare_text(candidate: str, local: str, *, allow_containment: bool = False) -> str:
    ratio = similarity(candidate, local)
    if ratio >= 0.85:
        return 'match'
    if allow_containment:
        a, b = f' {normalize(candidate)} ', f' {normalize(local)} '
        if a.strip() and b.strip() and (a in b or b in a):
            return 'match'
    return 'conflict' if ratio < 0.5 else 'partial'


def _compare_duration(candidate: float | None, local: float | None) -> tuple[str, float | None]:
    if not candidate or not local:
        return 'unknown', None
    delta = float(candidate) - float(local)
    if abs(delta) <= 3:
        return 'match', delta
    return ('close' if abs(delta) <= 10 else 'mismatch'), delta


def assess_candidate(*, provider_score: float, title: str, artist: str, duration: float | None,
                     local_title: str = '', local_artist: str = '',
                     local_duration: float | None = None, releases=()) -> Evidence:
    points = provider_score * 60
    notes = []

    duration_state, delta = _compare_duration(duration, local_duration)
    points += {'match': 15, 'close': 5, 'mismatch': -20, 'unknown': 0}[duration_state]
    if duration_state == 'mismatch':
        notes.append(f'Duration differs by {abs(delta):.0f} s')

    title_state = 'unknown'
    version_verdict, version_conflict = 'insufficient', False
    if title and not is_generic_title(local_title):
        title_state = _compare_text(title, local_title)
        points += {'match': 15, 'partial': 0, 'conflict': -15}[title_state]
        if title_state == 'conflict':
            notes.append(f'Title tag "{local_title}" disagrees with "{title}"')
        version = assess_version(local_title, title, duration_state=duration_state, release_titles=tuple(releases))
        version_verdict = version.verdict
        points += VERSION_POINTS[version_verdict]
        version_conflict = version_verdict in ('possible_conflict', 'confirmed_conflict')
        if version_conflict or version_verdict == 'insufficient':
            notes.append('Version: ' + '; '.join(version.reasons))

    artist_state = 'unknown'
    if artist and not is_unknown_name(local_artist):
        artist_state = _compare_text(artist, local_artist, allow_containment=True)
        points += {'match': 10, 'partial': 0, 'conflict': -10}[artist_state]
        if artist_state == 'conflict':
            notes.append(f'Artist tag "{local_artist}" disagrees with "{artist}"')

    return Evidence(provider_score=provider_score, score=max(0, min(100, round(points))),
                    title=title_state, artist=artist_state, duration=duration_state,
                    duration_delta=delta, notes=tuple(notes), version_verdict=version_verdict,
                    version_conflict=version_conflict)


def assess_match(ranked: list[Evidence]) -> Assessment:
    """Judge a list of candidate evidence already sorted best first."""
    if not ranked:
        return Assessment('none', 'No reference recording matched this fingerprint')
    top = ranked[0]
    if top.provider_score >= 0.5 and (top.title == 'conflict' or
                                      (top.artist == 'conflict' and top.title != 'match')):
        return Assessment('conflict', 'The acoustic match contradicts the existing tags; one of them is wrong')
    if top.score < MINIMUM_SCORE:
        return Assessment('insufficient', 'Evidence is too weak to propose an identity')
    if len(ranked) > 1 and ranked[1].score >= MINIMUM_SCORE and top.score - ranked[1].score < CLEAR_MARGIN:
        return Assessment('ambiguous', 'Several candidates are similarly supported; choose the right recording')
    if top.score >= STRONG_SCORE:
        return Assessment('strong', 'Fingerprint, duration and tags agree.')
    return Assessment('plausible', 'Acoustic match without enough supporting evidence; review before accepting')
