"""Domain models for files that have been inspected, not identified."""
from dataclasses import dataclass
import re

_GENERIC_TITLE = re.compile(r"^(?:\d{1,12}|(?:track|audio|song|file|unknown|untitled|recording|download)[\s._-]*\d*|unknown\s+artist[\s_-]*track[\s._-]*\d+)$", re.IGNORECASE)
_UNKNOWN = {'', 'unknown', 'unknown artist', 'various', 'n/a', 'none', 'untitled', 'null'}


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
