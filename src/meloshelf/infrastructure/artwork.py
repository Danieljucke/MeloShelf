"""Album artwork from verified MusicBrainz releases, cached on disk and in the library database.

Only a release identifier that MusicBrainz itself returned is ever requested. Covers are
accepted only when the declared type matches the bytes. A genuinely missing cover is
remembered so it is not requested again; a network failure is not, so a later attempt can succeed.
The browser never contacts the archive: it asks this service, which serves the cached bytes.
"""
import hashlib
import http.client
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from meloshelf.infrastructure.music_api import read_complete
from meloshelf.infrastructure.redirect_policy import ARCHIVE_IMAGE_HOST, open_restricted

log = logging.getLogger(__name__)

RELEASE_ID = re.compile(r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$', re.IGNORECASE)
ARCHIVE_URL = 'https://coverartarchive.org/release/{release}/front-500'
GROUP_ARCHIVE_URL = 'https://coverartarchive.org/release-group/{release}/front-500'
USER_AGENT = 'MeloShelf/0.2.0 (local music organizer)'
MAX_IMAGE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 500_000_000
_MAGIC = {'image/jpeg': (b'\xff\xd8\xff',), 'image/png': (b'\x89PNG\r\n\x1a\n',)}
_EXTENSION = {'image/jpeg': 'jpg', 'image/png': 'png'}


class ArtworkError(ValueError):
    pass


def fetch_from_archive(url: str):
    """Return (status, content_type, bytes). status is 'image', 'missing' or 'other'."""
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'image/*'})
    try:
        with open_restricted(request, ARCHIVE_IMAGE_HOST) as response:
            content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            data = read_complete(response, MAX_IMAGE_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 'missing', '', b''
        raise OSError(f'Cover Art Archive answered HTTP {exc.code}') from exc
    except http.client.HTTPException as exc:  # the image stopped part-way: a network failure, not a missing cover
        raise OSError(f'Cover download interrupted: {type(exc).__name__}') from exc
    return ('image', content_type, data) if content_type in _MAGIC else ('other', content_type, data)


class ArtworkCache:
    def __init__(self, database, directory, *, fetch=None, max_total_bytes=MAX_TOTAL_BYTES,
                 max_image_bytes=MAX_IMAGE_BYTES, clock=time.time):
        self.database = database
        self.directory = Path(directory)
        self.fetch = fetch or fetch_from_archive
        self.max_total_bytes = max_total_bytes
        self.max_image_bytes = max_image_bytes
        self._clock = clock

    @staticmethod
    def _key(release_id: str, group: bool = False) -> str:
        # A release id keeps its original key; a release-group id gets its own, so the two can never collide.
        text = ('group:' if group else '') + release_id.lower()
        return hashlib.sha256(text.encode('ascii')).hexdigest()

    def cover(self, release_id: str, *, group: bool = False):
        """(content_type, bytes) for a verified release (or release group), or None when no cover can be shown."""
        if not RELEASE_ID.fullmatch(release_id or ''):
            raise ArtworkError('Invalid release identifier')
        key = self._key(release_id, group)
        url = (GROUP_ARCHIVE_URL if group else ARCHIVE_URL).format(release=release_id.lower())
        with self.database.read() as db:
            row = db.execute('SELECT status, content_type, path FROM artwork WHERE key=?', (key,)).fetchone()
        if row and row['status'] == 'missing':
            return None
        if row and row['status'] == 'ok':
            path = Path(row['path'])
            if path.is_file():
                return row['content_type'], path.read_bytes()
        try:
            status, content_type, data = self.fetch(url)
        except (OSError, ValueError) as exc:  # a network failure is not evidence that no cover exists
            log.warning('cover request failed release=%s detail=%s', release_id, type(exc).__name__)
            return None
        if status == 'missing':
            self._remember(key, release_id, 'missing', '', '', 0)
            return None
        if status != 'image' or not self._valid(content_type, data):
            log.info('cover refused release=%s type=%s', release_id, content_type)
            return None
        if len(data) > self.max_image_bytes:
            log.info('cover too large release=%s bytes=%d', release_id, len(data))
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f'{key}.{_EXTENSION[content_type]}'
        path.write_bytes(data)
        self._remember(key, release_id, 'ok', content_type, str(path), len(data))
        self._evict(keep=key)
        return content_type, data

    @staticmethod
    def _valid(content_type: str, data: bytes) -> bool:
        return any(data.startswith(magic) for magic in _MAGIC.get(content_type, ()))

    def _remember(self, key, release_id, status, content_type, path, size):
        with self.database.write() as db:
            db.execute('''INSERT OR REPLACE INTO artwork(key,release_id,status,content_type,path,bytes,fetched_at)
                          VALUES(?,?,?,?,?,?,?)''', (key, release_id.lower(), status, content_type, path, size, self._clock()))

    def total_bytes(self) -> int:
        with self.database.read() as db:
            return db.execute("SELECT COALESCE(SUM(bytes),0) FROM artwork WHERE status='ok'").fetchone()[0]

    def _evict(self, keep: str) -> None:
        """Remove the oldest covers until the cache is within its size limit. The newest is never removed."""
        while self.total_bytes() > self.max_total_bytes:
            with self.database.read() as db:
                row = db.execute("""SELECT key,path FROM artwork WHERE status='ok' AND key<>?
                                    ORDER BY fetched_at ASC LIMIT 1""", (keep,)).fetchone()
            if row is None:
                return
            Path(row['path']).unlink(missing_ok=True)
            with self.database.write() as db:
                db.execute("UPDATE artwork SET status='evicted', path='', bytes=0 WHERE key=?", (row['key'],))
