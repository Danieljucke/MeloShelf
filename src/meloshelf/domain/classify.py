"""Decides what to do with each file, from what its tags say and what recognition found. Pure: no I/O.

- Reliable tags are kept as the person wrote them. A correctly tagged song never needs recognition.
- Generic or contradictory tags are completed from a confident recognition only.
- Anything else is a review: the original name and audio stay, no metadata is guessed, and the person sees what the
  evidence suggests. An album is used only when the evidence names exactly one; an edition is never chosen.
"""
from dataclasses import dataclass

from meloshelf.domain.artists import canonical_key
from meloshelf.domain.fallback import _is_download_id, parse_filename
from meloshelf.domain.filename_order import OrderHints, order_candidates, sides
from meloshelf.domain.metadata import credits_agree
from meloshelf.domain.models import is_generic_title, is_unknown_name
from meloshelf.domain.organize import Decision
from meloshelf.domain.track_numbers import parse_position

_VERSION_CONFLICTS = ('possible_conflict', 'confirmed_conflict')


@dataclass(frozen=True)
class Recognized:
    """What the recognition pipeline concluded about a file (built from its stored result)."""
    approved: bool                       # the recording was approved, automatically or by a person
    attempted: bool = True               # a recognition result exists for the file's current content
    title: str = ''
    artist: str = ''
    releases: tuple = ()                 # release titles of the recording; editions are never told apart here
    version_verdict: str = 'probable_agreement'
    reason_code: str = ''                # why it was not approved: not_recognised | weak_evidence | ...


def _text(tags: dict, key: str) -> str:
    return str(tags.get(key) or '').strip()


def _title_unreliable(title: str) -> bool:
    return not title or is_generic_title(title) or (' ' not in title and _is_download_id(title))


def _contradicts_filename(tags: dict, filename: str) -> bool:
    """The tags disagree with the name only if they fit NEITHER reading: "Artist - Title" and "Title - Artist" both exist."""
    split = sides(filename)
    if split is None:
        return False
    left, right = split
    if _title_unreliable(left) and _title_unreliable(right):
        return False                      # "03 - Song.mp3" says nothing about the artist: not a contradiction
    title, artist = _text(tags, 'title'), _text(tags, 'artist')

    def fits(song: str, who: str) -> bool:
        return canonical_key(title) == canonical_key(song) or canonical_key(artist) == canonical_key(who)             or credits_agree(artist, who)
    if _title_unreliable(right):
        return not fits(right, left)
    return not (fits(right, left) or fits(left, right))


def needs_recognition(tags: dict, filename: str) -> tuple[bool, str]:
    """(True, reason code) when the tags cannot be trusted as they are or are incomplete."""
    title, artist, album = _text(tags, 'title'), _text(tags, 'artist'), _text(tags, 'album')
    if _title_unreliable(title) or is_unknown_name(artist):
        return True, 'generic_tags'
    if _contradicts_filename(tags, filename):
        return True, 'contradictory_tags'
    if is_unknown_name(album):
        return True, 'incomplete_album'
    return False, ''


def _album_from(recognized: Recognized, tag_album: str) -> str:
    """The album the evidence supports: the tagged one when it names a release, the only one when there is one,
    otherwise none. Never a choice between several."""
    titles: dict[str, str] = {}
    for release in recognized.releases:
        titles.setdefault(canonical_key(release), release)
    if tag_album and canonical_key(tag_album) in titles:
        return tag_album
    if len(titles) == 1:
        return next(iter(titles.values()))
    return '' if titles else (tag_album if not is_unknown_name(tag_album) else '')


def _candidates(recognized: Recognized | None, filename: str, hints: OrderHints | None = None) -> tuple:
    found = []
    if recognized and recognized.title and recognized.artist:
        found.append({'title': recognized.title, 'artist': recognized.artist, 'source': 'recognition'})
    for reading in order_candidates(filename, hints):
        if _title_unreliable(reading['title']):
            continue
        if any(canonical_key(c['title']) == canonical_key(reading['title']) and canonical_key(c['artist']) == canonical_key(reading['artist'])
               for c in found):
            continue
        found.append(reading)
    return tuple(found)


def _review(code: str, reason: str, recognized, filename, hints=None) -> Decision:
    return Decision('review', reason=reason, reason_code=code, candidates=_candidates(recognized, filename, hints))


def decide(tags: dict, filename: str, recognized: Recognized | None, hints: OrderHints | None = None) -> Decision:
    title, artist, album = _text(tags, 'title'), _text(tags, 'artist'), _text(tags, 'album')
    album = '' if is_unknown_name(album) else album
    wanted, reason = needs_recognition(tags, filename)
    reliable = not wanted or reason == 'incomplete_album'
    number = parse_position(_text(tags, 'tracknumber'))[0]

    if reliable:
        filled = album
        if not album and recognized and recognized.approved and recognized.releases \
                and canonical_key(recognized.title) == canonical_key(title) and credits_agree(artist, recognized.artist):
            filled = _album_from(recognized, '')
        return Decision('organize', artist=artist, album=filled, title=title, track_no=number if filled else None,
                        reason='the file’s own tags are reliable', origin='tags')

    if recognized and recognized.approved and recognized.title and recognized.artist:
        if recognized.version_verdict in _VERSION_CONFLICTS:
            return _review('version_unclear', 'It is not clear which version of the recording this is (live, studio, remix…)',
                           recognized, filename, hints)
        filled = _album_from(recognized, album)
        number = number or parse_filename(filename).track_number
        return Decision('organize', artist=recognized.artist, album=filled, title=recognized.title,
                        track_no=number if filled else None, reason='recognised from its sound', origin='recognition')

    if reason == 'contradictory_tags':
        return _review('contradictory_tags', 'The tags disagree with the file name and nothing confirms either',
                       recognized, filename, hints)
    if recognized is None or not recognized.attempted:
        return _review('not_checked', 'The file has no usable tags and was not recognised', recognized, filename, hints)
    code = recognized.reason_code or 'not_recognised'
    sentence = {'weak_evidence': 'A possible match exists but the evidence is too weak',
                'not_recognised': 'No recording matched this file'}.get(code, 'The file could not be identified reliably')
    return _review(code, sentence, recognized, filename, hints)


def fields_to_write(tags: dict, decision: Decision) -> dict:
    """Tag fields to set on the copy. A decision taken from the tags only fills empty fields; one taken from a
    recognition also corrects fields that differ. Nothing equal to what is already there is rewritten."""
    if decision.status != 'organize':
        return {}
    wanted = {'title': decision.title, 'artist': decision.artist, 'album': decision.album}
    changes = {}
    for key, value in wanted.items():
        if not value:
            continue
        current = _text(tags, key)
        if canonical_key(current) == canonical_key(value) and current:
            continue
        if decision.origin == 'tags' and current and not is_unknown_name(current):
            continue
        changes[key] = value
    return changes
