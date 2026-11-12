"""Sample categories for qualification: which kind of file a proposal is about.

The category is derived from the file name, the tags and the current proposal every time it is asked for, so it
cannot go stale when a file is rescanned, retagged or re-analysed. The order below decides ties: a file that is
both numbered and live is "numeric/generic", because that is the harder problem for recognition."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str


CATEGORIES = (
    Category('tagged_and_named', 'Tagged and named'),
    Category('named_no_metadata', 'Named, tags missing or incomplete'),
    Category('numeric_generic_names', 'Numeric or generic file name'),
    Category('conflicting_tags', 'Tags disagree with the proposal'),
    Category('singles_collabs_rare_live', 'Singles, collaborations, rare, live or remix'),
    Category('other', 'Other'),
)
KEYS = tuple(c.key for c in CATEGORIES)

_GENERIC = re.compile(r'(?:aud|img|vid|pxl|dsc|rec|wa|audio|track|recording|voice|new recording|untitled)'
                      r'[\s._-]*[\d\s._-]*(?:wa\d+)?')
_NUMBERED = re.compile(r'\d{1,3}[\s._-]+[^\W\d_]')
_MARKERS = re.compile(r'\b(?:feat|ft|featuring|live|remix|remixed|remaster|remastered|acoustic|official|lyric|lyrics|'
                      r'video|instrumental|demo|cover|version)\b|&|,|\w\s+x\s+\w')


def _stem(path: str) -> str:
    name = path.replace('\\', '/').rsplit('/', 1)[-1]
    return name.rsplit('.', 1)[0] if '.' in name else name


def _norm(text: str) -> str:
    return re.sub(r'[\W_]+', '', (text or '').casefold())


def _disagree(tag: str, proposed: str) -> bool:
    a, b = _norm(tag), _norm(proposed)
    return bool(a and b and a not in b and b not in a)


def categorize(*, path: str, title: str, artist: str, album: str, proposed_title: str, proposed_artist: str,
               version_conflict: bool) -> str:
    stem = _stem(path).casefold().replace('_', ' ').strip()
    if (stem.isdigit() or _GENERIC.fullmatch(stem)
            or (_NUMBERED.match(stem) and ' - ' not in stem and ' -- ' not in stem)):
        return 'numeric_generic_names'
    has_proposal = bool(proposed_title or proposed_artist)
    if has_proposal and ((artist and _disagree(artist, proposed_artist)) or (title and _disagree(title, proposed_title))):
        return 'conflicting_tags'
    if version_conflict or _MARKERS.search(stem):
        return 'singles_collabs_rare_live'
    if title and artist:
        return 'tagged_and_named'
    if re.search(r'[^\W\d_]{2,}', stem):        # a readable name but title and/or artist missing
        return 'named_no_metadata'
    return 'other'
