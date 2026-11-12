"""Which side of "A - B" in a file name is the artist? Evidence decides; a guess is never silently filled in.

File names come in both orders: "Artist - Title" and "Title - Artist". The parser only splits the name; this module
weighs what is known about each side - the file's own artist tag, the folder name, a name that repeats on one side across
the files of a folder (an artist repeats, titles do not), and artists already known in the library. When one side is
supported and the other is not, that reading is offered first. When the evidence does not decide, BOTH readings are
offered for a person to choose. Either way the result is a suggestion from the file name: it is never acoustically
verified and is labelled so.
"""
from dataclasses import dataclass, field

from meloshelf.domain.artists import canonical_key, parse_credit
from meloshelf.domain.fallback import parse_filename

# strongest first
PRIORITY = ('tag', 'folder', 'recurring', 'known_artist')


@dataclass(frozen=True)
class OrderHints:
    """canonical artist key -> the kind of evidence that this name is an artist."""
    evidence: dict = field(default_factory=dict)

    def for_side(self, text: str) -> str:
        found = set()
        credit = parse_credit(text)
        for name in (text, *credit.primary):
            kind = self.evidence.get(canonical_key(name))
            if kind:
                found.add(kind)
        return next((kind for kind in PRIORITY if kind in found), '')


def sides(filename: str):
    """(left, right) of a "A - B" name after the usual clean-up, or None when there is no second part."""
    parsed = parse_filename(filename)
    if parsed.artist and parsed.title:
        return parsed.artist, parsed.title
    return None


def order_candidates(filename: str, hints: OrderHints | None = None) -> list[dict]:
    """Suggested readings, best first. Each: title, artist, source='filename', order, evidence, ambiguous."""
    split = sides(filename)
    if split is None:
        parsed = parse_filename(filename)
        return [{'title': parsed.title, 'artist': '', 'source': 'filename', 'order': '', 'evidence': '', 'ambiguous': False}] \
            if parsed.title else []
    left, right = split
    hints = hints or OrderHints()
    left_kind, right_kind = hints.for_side(left), hints.for_side(right)
    artist_title = {'title': right, 'artist': left, 'source': 'filename', 'order': 'artist_title'}
    title_artist = {'title': left, 'artist': right, 'source': 'filename', 'order': 'title_artist'}
    rank = {kind: i for i, kind in enumerate(PRIORITY)}
    if left_kind and not right_kind:
        return [{**artist_title, 'evidence': left_kind, 'ambiguous': False}]
    if right_kind and not left_kind:
        return [{**title_artist, 'evidence': right_kind, 'ambiguous': False}]
    if left_kind and right_kind and rank[left_kind] != rank[right_kind]:     # both known: the stronger evidence wins
        return [{**artist_title, 'evidence': left_kind, 'ambiguous': False}] if rank[left_kind] < rank[right_kind] \
            else [{**title_artist, 'evidence': right_kind, 'ambiguous': False}]
    # Nothing decides it: offer both, with the more usual "Artist - Title" first but NOT chosen for the person.
    return [{**artist_title, 'evidence': '', 'ambiguous': True}, {**title_artist, 'evidence': '', 'ambiguous': True}]


def recurring_sides(filenames: list[str]) -> dict:
    """For the files of one folder: canonical keys of names that repeat on the same side in two or more files.
    An artist repeats across a folder's files; a title almost never does."""
    left, right = {}, {}
    for name in filenames:
        split = sides(name)
        if split:
            left[canonical_key(split[0])] = left.get(canonical_key(split[0]), 0) + 1
            right[canonical_key(split[1])] = right.get(canonical_key(split[1]), 0) + 1
    found = {}
    for counts in (left, right):
        for key, n in counts.items():
            if key and n >= 2:
                found[key] = 'recurring'
    return found
