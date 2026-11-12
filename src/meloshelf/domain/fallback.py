"""Filename parsing for metadata-only fallback recognition.

Pure rules, no I/O. A parse proposes text that may be searched; it never proves a recording's identity, and
its output is never treated as an acoustic match. Presentation noise (official audio, lyric videos, bitrate
tags, download identifiers) is removed and recorded, so the removal can be inspected. Version descriptors
(live, acoustic, remix, ...) are kept and also reported as structured terms.
"""
import re
import unicodedata
from dataclasses import dataclass, field

from meloshelf.domain.versions import characteristics

AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.aiff', '.aif')
_LEADING_TRACK = re.compile(r'^\s*(\d{1,3})(?:\s+|\.\s+)(?=[^\W\d_])')  # a track number precedes a word, not a dash
_FEATURE = re.compile(r'[\(\[]?\s*\b(?:feat|ft)\.?\s+([^()\[\]]+?)\s*[\)\]]?(?=\s*(?:-\s|$|\())', re.I)
_NOISE_PATTERNS = [
    r'[\(\[]\s*(?:official\s+(?:music\s+)?(?:audio|video)|lyric\s+video|lyrics?(?:\s+video)?|ep\s+visual|viral\s+video)\s*[\)\]]',
    r'\(\s*\d{2,4}\s*k?\s*\)',                 # (128k), (320)
    r'\blyrics?(?:\s+medium)?\b',              # "Lyrics Medium" left in the name
    r'\bofficial\s+(?:music\s+)?(?:audio|video)\b',
    r'\s-mc$',                                 # download-site suffix
    r'[\(\[]\s*audio\s*[\)\]]',           # a bare (audio) marker
]
_NOISE = [re.compile(p, re.I) for p in _NOISE_PATTERNS]
_DOWNLOAD_ID = re.compile(r'(?<![\w-])[A-Za-z0-9_-]{11}(?![\w-])')


@dataclass(frozen=True)
class ParsedName:
    original: str
    artist: str
    title: str
    featured: tuple = ()
    track_number: int | None = None
    version_terms: tuple = ()
    extension: str = ''
    removed: tuple = field(default=())


def _strip_extensions(name: str) -> tuple[str, str]:
    stem, extension = name, ''
    while True:
        lower = stem.lower()
        match = next((ext for ext in AUDIO_EXTENSIONS if lower.endswith(ext)), None)
        if not match:
            break
        extension = extension or match
        stem = stem[:-len(match)]
    return stem, extension


def _is_download_id(token: str) -> bool:
    return bool(re.search(r'\d', token) and re.search(r'[A-Za-z]', token) and len(token) == 11)


def parse_filename(name: str) -> ParsedName:
    removed: list[str] = []
    stem, extension = _strip_extensions(unicodedata.normalize('NFC', name))
    text = re.sub(r'_+', ' ', stem)
    text = re.sub(r'\s+', ' ', text).strip()

    track = None
    match = _LEADING_TRACK.match(text)
    if match:
        track = int(match.group(1))
        text = text[match.end():]

    for pattern in _NOISE:
        for hit in pattern.findall(text):
            removed.append(hit.strip())
        text = pattern.sub(' ', text)

    tokens = []
    for token in text.split(' '):
        if _is_download_id(token):
            removed.append(token)
            continue
        tokens.append(token)
    text = ' '.join(tokens)

    featured: list[str] = []
    feature = _FEATURE.search(text)
    if feature:
        removed.append(feature.group(0).strip())
        featured.extend(_split_credits(feature.group(1)))
        text = text[:feature.start()] + ' ' + text[feature.end():]

    text = re.sub(r'\(\s*\)|\[\s*\]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip(' -,')
    text = re.sub(r'\(\s+', '(', text)
    text = re.sub(r'\s+\)', ')', text)
    text = re.sub(r'\s*\(\s*$', '', text).strip()

    artist, title = '', text
    if ' - ' in text:
        left, right = text.split(' - ', 1)
        artist, title = left.strip(), right.strip()
        credits = _split_credits(artist)
        if len(credits) > 1:
            artist = credits[0]
            featured.extend(credits[1:])
        if artist.isupper() and any(c.isalpha() for c in artist):
            artist = ' '.join(w.capitalize() for w in artist.split(' '))

    return ParsedName(
        original=name, artist=artist, title=title, featured=tuple(dict.fromkeys(featured)),
        track_number=track, version_terms=tuple(sorted(characteristics(title))), extension=extension,
        removed=tuple(dict.fromkeys(r for r in removed if r)))


def _split_credits(text: str) -> list[str]:
    parts = re.split(r',|&|\band\b', text, flags=re.I)
    return [p.strip(' ,') for p in parts if p.strip(' ,')]


# Comparison and levels live in fallback_scoring; re-exported here for callers.
from meloshelf.domain.fallback_scoring import (ACOUSTICALLY_MATCHED, CATALOG_MATCHED, LEVELS, METADATA_SUGGESTED,  # noqa: E402
                                               UNIDENTIFIED, FallbackResult, auto_approvable, evaluate_fallback)
