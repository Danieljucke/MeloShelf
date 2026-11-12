"""Where each music file belongs: a pure plan, decided without touching the filesystem.

Layout:  Destination/Artist/Album/NN - Title.ext
         Destination/Artist/Unknown Album/Title.ext   (recording known, album not: never an invented release)
         Destination/À vérifier/<original file name>  (identity not reliable: original name, no speculative tags)

Names are made safe for Windows (reserved device names, illegal characters, trailing dots and spaces, path length)
and two different files never share a destination (comparison ignores letter case, as Windows does).
"""
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from meloshelf.domain.artists import canonical_key, parse_credit

REVIEW_FOLDER = 'À vérifier'
UNKNOWN_ALBUM = 'Unknown Album'
MAX_PATH_LENGTH = 240          # below Windows' 260 so a ".meloshelf-tmp" sibling and a collision suffix still fit
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_ILLEGAL = re.compile(r'[<>:"|?*]')
_SEPARATORS = re.compile(r'[\\/]')


@dataclass(frozen=True)
class Decision:
    """What to do with one file. ``organize`` files have artist and title; ``review`` files keep their name."""
    status: str                      # 'organize' | 'review'
    artist: str = ''
    album: str = ''
    title: str = ''
    track_no: int | None = None
    reason: str = ''
    reason_code: str = ''
    origin: str = 'tags'             # where the identity came from: 'tags' | 'recognition'
    folder_artist: str = ''          # the canonical PRIMARY artist that names the folder; the credit in ``artist`` is kept whole
    candidates: tuple = ()           # for review: what the evidence suggests, shown to the person, never applied


@dataclass(frozen=True)
class PlanEntry:
    source: str
    decision: Decision
    extension: str
    original_name: str = ''


@dataclass(frozen=True)
class PlannedFile:
    source: str
    relative: str                    # always '/'-separated; the engine converts to the platform separator
    decision: Decision


def safe_component(text: str, *, limit: int = 100) -> str:
    """One folder or file name that Windows accepts and a person can read."""
    value = unicodedata.normalize('NFC', str(text or ''))
    value = _CONTROL.sub(' ', value)
    value = _SEPARATORS.sub('-', value)
    value = _ILLEGAL.sub('_', value)
    value = ' '.join(value.split()).strip(' ')
    value = value.rstrip(' .')
    if not value:
        return 'Unknown'
    if value.split('.', 1)[0].upper() in _RESERVED:
        value += '_'
    if len(value) > limit:
        value = value[:limit].rstrip(' .') or 'Unknown'
    return value


def canonical_spellings(names) -> dict[str, str]:
    """{canonical key: the spelling to use}: the most common one, preferring mixed case over ALL or all lower."""
    counts: dict[str, Counter] = {}
    for name in names:
        key = canonical_key(name)
        if key:
            counts.setdefault(key, Counter())[' '.join(name.split())] += 1

    def rank(item):
        spelling, count = item
        plain = spelling.islower() or spelling.isupper()
        return (-count, plain, spelling)

    return {key: sorted(counter.items(), key=rank)[0][0] for key, counter in counts.items()}


def _artist_folder(decision: Decision, limit: int) -> str:
    if decision.folder_artist.strip():
        return safe_component(decision.folder_artist, limit=limit)
    from meloshelf.domain.primary_artist import analyse_credit
    return safe_component(analyse_credit(decision.artist).primary or decision.artist, limit=limit)


def relative_destination(decision: Decision, extension: str, original_name: str, *, limit: int = 100) -> str:
    """The '/'-separated path of a file under the destination."""
    extension = extension or ''
    if decision.status != 'organize':
        stem = original_name
        if extension and stem.lower().endswith(extension.lower()):
            stem = stem[:-len(extension)]
        return f'{REVIEW_FOLDER}/{safe_component(stem, limit=limit)}{extension}'
    if not decision.artist.strip() or not decision.title.strip():
        raise ValueError('an organized file needs an artist and a title')
    album = safe_component(decision.album, limit=limit) if decision.album.strip() else UNKNOWN_ALBUM
    title = safe_component(decision.title, limit=limit)
    number = f'{decision.track_no:02d} - ' if decision.track_no and decision.track_no > 0 else ''
    return f'{_artist_folder(decision, limit)}/{album}/{number}{title}{extension}'


def _with_suffix(relative: str, number: int) -> str:
    stem, extension = os.path.splitext(relative)
    return f'{stem} ({number}){extension}'


def plan_destinations(entries, destination_root: str = '', occupied=frozenset()) -> list[PlannedFile]:
    """A destination for every entry, in a stable order, with no two files sharing one. ``occupied`` holds the
    lower-case relative paths already present in the destination."""
    taken: set[str] = {name.lower() for name in occupied}
    planned: list[PlannedFile] = []
    for entry in sorted(entries, key=lambda e: e.source):
        name = entry.original_name or os.path.basename(entry.source)
        relative = ''
        for limit in (100, 80, 60, 40, 25, 15):
            relative = relative_destination(entry.decision, entry.extension, name, limit=limit)
            if len(destination_root) + 1 + len(relative) <= MAX_PATH_LENGTH:
                break
        candidate, number = relative, 1
        while candidate.lower() in taken:
            number += 1
            candidate = _with_suffix(relative, number)
        taken.add(candidate.lower())
        planned.append(PlannedFile(entry.source, candidate, entry.decision))
    return planned
