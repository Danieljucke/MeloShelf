"""Domain models for files that have been inspected, not identified."""
from dataclasses import dataclass
import re

_GENERIC_TITLE = re.compile(r"^(?:\d{1,12}|(?:track|audio|song|file|unknown|untitled|recording|download)[\s._-]*\d*|unknown\s+artist[\s_-]*track[\s._-]*\d+)$", re.IGNORECASE)
_UNKNOWN = {'', 'unknown', 'unknown artist', 'various', 'n/a', 'none', 'untitled', 'null'}


def is_generic_title(title: str) -> bool:
    """True when a title carries no identity (empty, numeric, 'Track 01', …)."""
    title = (title or '').strip().replace('_', ' ')
    return not title or bool(_GENERIC_TITLE.fullmatch(title))


def is_unknown_name(name: str) -> bool:
    return (name or '').strip().lower() in _UNKNOWN


def classify_track(*, title: str, artist: str, album: str) -> str:
    """Classification of tag completeness, NOT an identity confidence score."""
    title = title.strip()
    artist = artist.strip()
    album = album.strip()
    if not title or _GENERIC_TITLE.fullmatch(title):
        return 'unidentified'
    if artist.lower() in _UNKNOWN or album.lower() in _UNKNOWN:
        return 'review'
    return 'tagged'


@dataclass(frozen=True, slots=True)
class Track:
    path: str
    size: int
    extension: str
    mtime_ns: int
    title: str
    artist: str
    album: str
    year: str
    status: str
    track_no: int | None = None
    track_total: int | None = None
    disc_no: int | None = None
    disc_total: int | None = None
    numbers_indexed: bool = False     # True once this file's tags have been read for track and disc numbers
    has_picture: bool | None = None   # the tags hold a picture entry; None until the file has been read (see migration 22)
