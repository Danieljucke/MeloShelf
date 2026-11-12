"""Canonical artist identity derived from raw credit strings.

Conservative by design: without catalogue IDs, two credits are the same artist
only when they differ in case, accents, spacing or punctuation. A credit is split
into a primary artist and featured artists only when the separator is explicit
("feat.", "ft.", "featuring") or when the leading name is an artist already
established in the library. Names such as "Simon & Garfunkel" are never broken apart
unless "Simon" is itself a known artist.
"""
import re
import unicodedata
from dataclasses import dataclass

_FEATURING = re.compile(r'\s+(?:feat\.?|ft\.?|featuring)\s+', re.IGNORECASE)
_JOINER = re.compile(r'\s*(?:,|&|\band\b|\bx\b)\s*', re.IGNORECASE)
_PUNCTUATION = re.compile(r'[^\w\s]')


def canonical_key(name: str) -> str:
    """Comparison key: no accents, no case, no punctuation, single spaces."""
    decomposed = unicodedata.normalize('NFKD', name or '')
    folded = ''.join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return ' '.join(_PUNCTUATION.sub(' ', folded).split())


@dataclass(frozen=True)
class ArtistCredit:
    display: str                 # the credit exactly as it was tagged (whitespace-normalized)
    primary: tuple[str, ...]
    featured: tuple[str, ...]


def _names(text: str) -> list[str]:
    return [part.strip() for part in _JOINER.split(text) if part and part.strip()]


def is_standalone(text: str) -> bool:
    """A credit with no featuring marker and no joiner names a single artist on its own."""
    return not _FEATURING.search(text) and not _JOINER.search(text)


def parse_credit(text: str, known: frozenset[str] | set[str] = frozenset()) -> ArtistCredit:
    display = ' '.join((text or '').split())
    main, *featuring = _FEATURING.split(display, maxsplit=1)
    featured = _names(featuring[0]) if featuring else []
    parts = _names(main)
    if len(parts) > 1 and canonical_key(parts[0]) in known:
        primary = (parts[0],)
        featured = parts[1:] + featured
    else:
        primary = (main.strip(),)
    return ArtistCredit(display=display, primary=tuple(p for p in primary if p),
                        featured=tuple(dict.fromkeys(f for f in featured if f)))
