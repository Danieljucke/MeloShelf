"""HTTP-facing logic for album artwork enrichment and for serving a found cover."""
from pathlib import Path

from meloshelf.application import error_codes as ec


def status(server) -> tuple[int, dict]:
    return 200, server.album_enrichment.status()


def start(server) -> tuple[int, dict]:
    result = server.album_enrichment.start()
    return (202, result) if result['started'] else (409, ec.body(result['code']))


def control(server, payload: dict) -> tuple[int, dict]:
    if payload.get('action') != 'cancel':
        return 400, ec.body('organize_action_invalid')
    server.album_enrichment.cancel()
    return 200, {'ok': True}


def image(server, album: str, artist: str):
    """(content type, bytes) of the cover currently known for this album, or None. Reads the cache only."""
    if not album or not artist:
        return None
    found = server.album_art.lookup(album, artist, (), network=False)
    if found['state'] == 'missing' or found.get('content_type') not in ('image/jpeg', 'image/png'):
        return None
    return found['content_type'], found['data']
