"""HTTP-facing logic for artist photographs. Every write is explicit; the server validates it."""
import base64
import binascii

from meloshelf.application.artist_photos import PhotoError

import logging

_REFUSED = {'unverified': 409, 'no_contact': 409, 'failed': 502}
_CODES = {'unverified': 'photo_unverified', 'no_contact': 'photo_contact_required', 'failed': 'photo_fetch_failed'}
log = logging.getLogger(__name__)


from meloshelf.application import error_codes as ec
def fetch(server, payload: dict) -> tuple[int, dict]:
    try:
        result = server.photos.ensure(payload.get('mb_artist_id'))
    except PhotoError as exc:
        return 400, ec.from_text(exc)
    status = result['status']
    if status in _REFUSED:
        # The provider's reason can carry its own text; it is logged here and the page receives the coded sentence.
        log.info('photograph not fetched status=%s reason=%s', status, result.get('reason', ''))
        coded = ec.body(_CODES[status])
        return _REFUSED[status], {'status': status, 'reason': coded['error'], **coded}
    return 200, result


def manual(server, payload: dict) -> tuple[int, dict]:
    try:
        data = base64.b64decode(payload.get('data') or '', validate=True)
    except (binascii.Error, ValueError):
        return 400, ec.body('image_base64_invalid')
    try:
        event = server.photos.set_manual(payload.get('mb_artist_id'), payload.get('content_type'), data,
                                         note=str(payload.get('note') or ''))
    except PhotoError as exc:
        return 400, ec.from_text(exc)
    return 200, {'saved': True, 'event_id': event}


def clear(server, payload: dict) -> tuple[int, dict]:
    try:
        return 200, {'saved': True, 'event_id': server.photos.clear_manual(payload.get('mb_artist_id'))}
    except PhotoError as exc:
        return 400, ec.from_text(exc)


def undo(server, payload: dict) -> tuple[int, dict]:
    event_id = payload.get('event_id')
    if type(event_id) is not int:
        return 400, ec.body('correction_event_id_invalid')
    try:
        return 200, {'saved': True, 'event_id': server.photos.undo(event_id)}
    except PhotoError as exc:
        return 400, ec.from_text(exc)


def annotate(server, artists: list[dict]) -> list[dict]:
    """Add the image summary (url, attribution, provenance) to every artist whose verified ID has an image."""
    summaries = server.photos.summaries(a.get('mb_artist_id') for a in artists)
    for artist in artists:
        mb = artist.get('mb_artist_id')
        if mb and mb in summaries:
            artist['image'] = {'url': f'/api/artist-image?mb={mb}', **summaries[mb]}
    return artists


def batch_status(server) -> tuple[int, dict]:
    return 200, server.photo_batch.status()


def batch_start(server) -> tuple[int, dict]:
    result = server.photo_batch.start()
    return (202, result) if result['started'] else (409, ec.body(result['code']))


def batch_control(server, payload: dict) -> tuple[int, dict]:
    if payload.get('action') != 'cancel':
        return 400, ec.body('organize_action_invalid')
    server.photo_batch.cancel()
    return 200, {'ok': True}


def profile(server, name: str) -> tuple[int, dict]:
    """One canonical artist: albums, singles, collaborations, original credits, photograph and cover state per album."""
    from urllib.parse import urlencode
    from meloshelf.domain.artists import canonical_key
    data = server.repo.artist_profile(name)
    if data is None:
        return 404, ec.body('artist_not_found')
    records, _ = server.repo.artist_identities(query=data['name'], limit=50)
    record = next((r for r in records if canonical_key(r['name']) == canonical_key(data['name'])), None)
    if record is not None:
        annotated = annotate(server, [dict(record)])[0]
        data.update(mb_artist_id=annotated.get('mb_artist_id'), link_status=annotated.get('link_status'), image=annotated.get('image'),
                    aliases=annotated.get('aliases', []), collaborators=annotated.get('collaborators', []))
    for album in data['albums']:
        art = server.album_art.describe(album['name'], data['name'])
        album['art'] = art
        if art['state'] in ('confirmed', 'plausible'):
            album['cover_url'] = '/api/album-art/image?' + urlencode({'album': album['name'], 'artist': data['name']})
    return 200, data
