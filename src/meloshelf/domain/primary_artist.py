"""Primary artist versus collaborators in a credit string.

Two facts are kept apart:
- the credit as it was tagged ("Kygo, Selena Gomez"), which is never rewritten, and
- the primary artist (Kygo), which names the artist's profile and the destination folder.

The first credited artist is the primary artist of an ordinary multi-artist credit. Genuine group names are protected
(a built-in list of well-known ones, names the library or a person has declared whole, and names MusicBrainz reports as
a single artist). A credit that could be one duo or two artists ("A & B" where neither name is known anywhere else) is
not guessed: it stays whole and is flagged for review.
"""
import re
from dataclasses import dataclass

from meloshelf.domain.artists import canonical_key

_FEATURING = re.compile(r'\s+(?:feat\.?|ft\.?|featuring)\s+', re.IGNORECASE)
_MARKER_SYMMETRIC = re.compile(r'\s*(?:&|\band\b|\+)\s*', re.IGNORECASE)
_MARKER_LISTING = re.compile(r'\s*(?:,|;|\bvs\.?(?=\s)|\bversus\b|\s[x×]\s|\bwith\b)\s*', re.IGNORECASE)
_ANY_JOINER = re.compile(r'\s*(?:,|;|&|\band\b|\+|\bvs\.?(?=\s)|\bversus\b|\s[x×]\s|\bwith\b)\s*', re.IGNORECASE)

# Genuine group names that contain a joiner. Compared by canonical key.
BUILTIN_GROUPS = frozenset(canonical_key(n) for n in (
    'Simon & Garfunkel', 'Earth, Wind & Fire', 'Crosby, Stills & Nash', 'Crosby, Stills, Nash & Young', 'Hall & Oates',
    'Florence + the Machine', 'Florence and the Machine', 'Mumford & Sons', 'Brooks & Dunn', 'Years & Years',
    'Of Monsters and Men', 'Belle and Sebastian', 'Peter, Paul and Mary', 'Tom Petty and the Heartbreakers',
    'Echo & the Bunnymen', 'Katrina and the Waves', 'Ike & Tina Turner', 'Bob Marley & The Wailers', 'Sly & the Family Stone',
    'Kool & the Gang', 'Gladys Knight & the Pips', 'Martha and the Vandellas', 'Marina and the Diamonds', 'Andrew Bird & the Mysterious Production of Eggs',
    'Huey Lewis and the News', 'Tyler, The Creator', 'Prince and the Revolution', 'Emerson, Lake & Palmer', 'Hootie & the Blowfish',
    'Matt and Kim', 'Sam & Dave', 'Big Brother and the Holding Company', 'Nick Cave and the Bad Seeds', 'Iron & Wine',
    'Angus & Julia Stone', 'Ashford & Simpson', 'Chas & Dave', 'Right Said Fred', 'Lonestar', 'Black Eyed Peas', 'Dire Straits',
))


@dataclass(frozen=True)
class CreditAnalysis:
    display: str                      # the credit exactly as tagged (whitespace normalised); never rewritten
    primary: str                      # names the profile and the destination folder
    collaborators: tuple              # in credit order, without the primary
    confidence: str                   # single | explicit | known | rule | protected | ambiguous
    review: bool = False              # True when a person should decide (genuinely ambiguous)
    reason: str = ''


def _clean(text: str) -> str:
    return ' '.join((text or '').split())


def _slash_list(text: str) -> str:
    """ID3 tags separate several artists with "/". It is a list separator only when a name on either side has a space
    ("DJ Standout/Kennis Clark"); compact names such as AC/DC stay whole, and declared groups are protected by the caller."""
    if '/' not in text or not any(' ' in side.strip() for side in text.split('/')):
        return text
    return ', '.join(side.strip() for side in text.split('/') if side.strip())


def _names(text: str, pattern=_ANY_JOINER) -> list[str]:
    return [p.strip(' ,;') for p in pattern.split(_slash_list(text)) if p and p.strip(' ,;')]


def known_artists(credits) -> frozenset:
    """Names the library already shows as artists on their own: standalone credits, and names credited first in
    several different credits (an artist is what keeps appearing first)."""
    firsts: dict = {}
    known = set()
    for credit in credits:
        text = _clean(credit)
        main = _FEATURING.split(text, maxsplit=1)[0]
        parts = _names(main)
        if not parts:
            continue
        if len(parts) == 1:
            known.add(canonical_key(parts[0]))
        else:
            firsts.setdefault(canonical_key(parts[0]), set()).add(canonical_key(text))
        if _FEATURING.search(text):
            known.add(canonical_key(parts[0]))
    known.update(k for k, seen in firsts.items() if len(seen) >= 2)
    return frozenset(known)


def analyse_credit(text: str, *, known=frozenset(), groups=frozenset(), together=frozenset()) -> CreditAnalysis:
    """``known``: canonical keys of artists seen on their own; ``groups``: names that are one artist (e.g. reported as
    one artist by MusicBrainz); ``together``: names a person declared whole. All are sets of canonical keys."""
    display = _clean(text)
    if not display:
        return CreditAnalysis('', '', (), 'single')
    protected = BUILTIN_GROUPS | set(groups) | set(together)
    main, *rest = _FEATURING.split(display, maxsplit=1)
    featured = _names(rest[0]) if rest else []
    if canonical_key(main) in protected or canonical_key(display) in protected:
        whole = main if canonical_key(main) in protected else display
        return CreditAnalysis(display, whole.strip(), tuple(dict.fromkeys(featured)), 'protected', reason='a group name')
    parts = _names(main)
    if len(parts) <= 1:
        return CreditAnalysis(display, (parts[0] if parts else main).strip(), tuple(dict.fromkeys(featured)),
                              'explicit' if featured else 'single')
    first, others = parts[0], parts[1:]
    # "A & B": a duo or two artists? Only the library can tell; with no evidence it is not guessed.
    symmetric_only = _MARKER_SYMMETRIC.search(main) and not _MARKER_LISTING.search(main)
    if symmetric_only and len(parts) == 2 and canonical_key(first) not in known and canonical_key(others[0]) not in known:
        return CreditAnalysis(display, main.strip(), tuple(dict.fromkeys(featured)), 'ambiguous', True,
                              'could be one duo or two artists; no other credit in the library settles it')
    confidence = 'known' if canonical_key(first) in known else 'rule'
    return CreditAnalysis(display, first, tuple(dict.fromkeys(others + featured)), confidence)
