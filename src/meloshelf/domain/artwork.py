"""Artwork rules. Pure.

Licences: only openly reusable licences are accepted for downloaded photographs: CC0, public domain, CC BY and CC BY-SA.
Anything with a non-commercial or no-derivatives term, fair-use claims, 'all rights reserved' and unknown licences are
refused. Images are accepted only when the bytes really are the declared type (JPEG or PNG).
"""
import re

IMAGE_TYPES = {'image/jpeg': (b'\xff\xd8\xff',), 'image/png': (b'\x89PNG\r\n\x1a\n',)}
EXTENSION = {'image/jpeg': 'jpg', 'image/png': 'png'}
UUID = re.compile(r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$', re.IGNORECASE)


def license_acceptable(name: str) -> bool:
    text = ' '.join((name or '').lower().replace('_', ' ').split())
    if not text:
        return False
    if text.startswith('cc0') or text.startswith('public domain') or text == 'pd' or text.startswith('pd-') \
            or text.startswith('pd '):
        return True
    if text.startswith('cc by'):
        parts = re.split(r'[\s\-]+', text)
        return 'nc' not in parts and 'nd' not in parts
    return False


def sniff_image(data: bytes) -> str | None:
    """The content type the bytes really are, or None."""
    for content_type, signatures in IMAGE_TYPES.items():
        if any(data.startswith(sig) for sig in signatures):
            return content_type
    return None


def verified_release(album_title: str, release_lists) -> dict:
    """Which release, if any, do the approved recordings of an album agree on?

    `release_lists` has one list of release details ({'id', 'title', ...}) per approved recording, taken from the
    candidate that recording was approved as. A release counts for a recording when its title matches the album title
    (ignoring case, accents and punctuation) and its id is a real UUID. The releases common to EVERY approved recording
    are the candidates: exactly one is 'verified'; several equally good editions are 'ambiguous'; none is 'unverified'.
    Editions with their own titles (a deluxe edition) are told apart by title and id.
    """
    from meloshelf.domain.artists import canonical_key
    wanted = canonical_key(album_title)
    if not wanted or not release_lists:
        return {'state': 'unverified', 'release_id': None, 'release_ids': [],
                'reason': 'no approved recording of this album carries release information'}
    common = None
    group_of = {}
    for details in release_lists:
        ids = set()
        for d in details:
            if canonical_key(d.get('title', '')) == wanted and UUID.match(d.get('id', '')):
                ids.add(d['id'].lower())
                if UUID.match(d.get('group_id', '') or ''):
                    group_of.setdefault(d['id'].lower(), d['group_id'].lower())
        common = ids if common is None else common & ids
    common = sorted(common or [])
    if len(common) == 1:
        return {'state': 'verified', 'release_id': common[0], 'release_ids': common,
                'reason': f'{len(release_lists)} approved recording(s) agree on one release with this title'}
    if len(common) > 1:
        groups = {group_of.get(r) for r in common}
        if len(groups) == 1 and None not in groups:
            # Several editions, all in one release group: the album is verified, the edition is not.
            return {'state': 'verified_group', 'release_id': None, 'release_ids': common, 'group_id': groups.pop(),
                    'reason': f'{len(common)} editions share one release group; the album is verified, the edition is not'}
        return {'state': 'ambiguous', 'release_id': None, 'release_ids': common,
                'reason': f'{len(common)} editions share this title; the recordings cannot tell them apart'}
    return {'state': 'unverified', 'release_id': None, 'release_ids': [],
            'reason': 'the approved recordings do not agree on a release with this title'}
