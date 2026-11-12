"""HTTP-facing logic for album covers. Every write is explicit; the server validates it."""
import base64
import binascii
from urllib.parse import urlencode

from meloshelf.application.artist_photos import PhotoError


from meloshelf.application import error_codes as ec
def manual(server, payload: dict) -> tuple[int, dict]:
    try:
        data = base64.b64decode(payload.get('data') or '', validate=True)
    except (binascii.Error, ValueError):
        return 400, ec.body('image_base64_invalid')
    try:
        event = server.covers.set_manual(payload.get('album'), payload.get('artist'), payload.get('content_type'),
                                         data, note=str(payload.get('note') or ''))
    except PhotoError as exc:
        return 400, ec.from_text(exc)
    return 200, {'saved': True, 'event_id': event}


def clear(server, payload: dict) -> tuple[int, dict]:
    try:
        return 200, {'saved': True, 'event_id': server.covers.clear_manual(payload.get('album'), payload.get('artist'))}
    except PhotoError as exc:
        return 400, ec.from_text(exc)


def undo(server, payload: dict) -> tuple[int, dict]:
    event_id = payload.get('event_id')
    if type(event_id) is not int:
        return 400, ec.body('correction_event_id_invalid')
    try:
        return 200, {'saved': True, 'event_id': server.covers.undo(event_id)}
    except PhotoError as exc:
        return 400, ec.from_text(exc)


def annotate(server, albums: list[dict]) -> list[dict]:
    """Add each album's cover state; a URL only where a cover applies (verified release or a person's choice)."""
    pairs = [(a['title'], a.get('artist') or '') for a in albums]
    summaries = server.covers.summaries(pairs)
    for album, pair in zip(albums, pairs):
        cover = dict(summaries[pair])
        if cover.pop('has_cover'):
            cover['has_cover'] = True
            cover['url'] = '/api/album-cover?' + urlencode({'album': pair[0], 'artist': pair[1]})
        album_art = getattr(server, 'album_art', None)
        art = album_art.describe(pair[0], pair[1], check_held=False) if (album_art is not None and pair[1]) else {'state': 'missing', 'source': ''}
        cover['art'] = art                       # the catalogue answer for this album: confirmed / plausible (suggested) / missing
        if not cover.get('url') and art['state'] in ('confirmed', 'plausible'):
            cover['url'] = '/api/album-art/image?' + urlencode({'album': pair[0], 'artist': pair[1]})
        album['cover'] = cover
    return albums


def annotate_tracks(server, tracks: list[dict]) -> list[dict]:
    """Say where each song's thumbnail comes from. A verified album cover (or the person's own choice) when one applies;
    otherwise the picture embedded in the file's tags, marked unverified. The page requests the image lazily."""
    from urllib.parse import quote
    pairs = [(t.get('album') or '', t.get('artist') or '') for t in tracks]
    summaries = server.covers.summaries([p for p in pairs if p[0]]) if any(p[0] for p in pairs) else {}
    for track, pair in zip(tracks, pairs):
        cover = summaries.get(pair)
        if cover and cover.get('has_cover'):
            track['art'] = {'source': 'album', 'verified': cover['provenance'] != 'user' and cover['state'] in (
                'verified', 'verified_group'), 'provenance': cover['provenance'],
                'url': '/api/album-cover?' + urlencode({'album': pair[0], 'artist': pair[1]})}
        elif track.get('embedded_art') == 1:
            # The file's tags were read and hold a picture entry; the endpoint still confirms it can be served.
            track['art'] = {'source': 'embedded', 'verified': False,
                            'url': '/api/track-artwork?path=' + quote(track['path'], safe='')}
        else:
            # Absent (0) or not yet read (NULL, cleared by migration 22 and filled by the scan's backfill): no request.
            track['art'] = None
    return tracks
