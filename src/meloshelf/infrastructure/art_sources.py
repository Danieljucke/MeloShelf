"""Optional artwork providers: Fanart.tv and TheAudioDB.

Cover Art Archive stays the primary source of release artwork and Wikimedia Commons the primary source of artist photographs.
These providers are fallbacks, OFF until the person turns them on and supplies their own key, and every image they return is
display-only: it is never embedded into a music file unless the person accepts it as their own choice.

What the providers' own pages say (checked when this module was written):
- Fanart.tv: an API key is required (a project key, plus an optional personal "client key"); its key page says the API may not be
  used commercially without written consent and that public programs must tell users about the site and the images used. Images are
  contributed by its community, which keeps the copyright. Hence: attribution everywhere, personal use wording, no embedding.
- TheAudioDB: the free v1 key is a shared test key with 30 requests a minute; lookup by MusicBrainz ID is a paid (premium) method, and
  the artwork is mostly fan created with no licence stated. Hence: the test key is never used for MusicBrainz lookups, a user's own
  premium key is required, requests stay under 30 a minute, and nothing is assumed about redistribution.

Both identify artists and albums by MusicBrainz ID and the answer must repeat that ID: an image is never attached on a name match.
"""
import re
from urllib.parse import quote

from meloshelf.domain.artwork import sniff_image
from meloshelf.infrastructure.music_api import (APIError, RateLimiter, RetryPolicy, _request_json, call_with_retry)

MAX_IMAGE_BYTES = 2_000_000
UUID = re.compile(r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$', re.IGNORECASE)
# Only these hosts are ever fetched, over https; a redirect to anything else is refused before a request is sent.
IMAGE_HOSTS = {
    'fanart.tv': re.compile(r'^https://assets\.fanart\.tv/'),
    'theaudiodb': re.compile(r'^https://(?:r2\.theaudiodb\.com|www\.theaudiodb\.com)/'),
}
SHARED_TEST_KEYS = {'1', '2', '3', '123'}      # TheAudioDB's shared free test keys


class SourceError(ValueError):
    """The provider cannot be used as configured. ``code`` is stable and safe to show."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def image_url_allowed(provider: str, url: str) -> bool:
    pattern = IMAGE_HOSTS.get(provider)
    return bool(pattern and isinstance(url, str) and pattern.match(url))


def download_image(provider: str, url: str, fetch, *, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[str, bytes]:
    """(content type, bytes) of a verified JPEG or PNG. ``fetch(url)`` returns (declared type, bytes) and is only called for an
    allowed https host. The real type, not the declared one, decides; oversize or unrecognised content is refused."""
    if not image_url_allowed(provider, url):
        raise SourceError('image_host_not_allowed')
    declared, data = fetch(url)
    real = sniff_image(data)
    if real is None or len(data) > max_bytes:
        raise SourceError('image_invalid')
    return real, data


def restricted_fetch(provider: str):
    """The default network fetch: redirects are followed only within the provider's own image hosts."""
    from urllib.request import Request
    from meloshelf.infrastructure.music_api import read_complete
    from meloshelf.infrastructure.redirect_policy import open_restricted

    def fetch(url: str):
        request = Request(url, headers={'User-Agent': 'MeloShelf (local music organizer)'})
        with open_restricted(request, IMAGE_HOSTS[provider], timeout=20) as response:
            return response.headers.get('Content-Type', '').split(';')[0].strip(), read_complete(response, MAX_IMAGE_BYTES + 1)
    return fetch


class _Client:
    name = ''
    label = ''
    terms_url = ''
    license = ''
    author = ''

    def __init__(self, *, transport=_request_json, fetch=None, limiter=None, retry=None, should_cancel=None):
        self.transport = transport
        self.fetch = fetch or restricted_fetch(self.name)
        self.limiter = limiter or RateLimiter(self.min_interval)
        self.retry = retry or RetryPolicy()
        self.should_cancel = should_cancel

    min_interval = 1.0

    def _json(self, url: str, headers: dict | None = None):
        """One request; HTTP 429 honours Retry-After through the shared retry policy and surfaces as APIError."""
        def attempt():
            return self.limiter.dispatch(
                lambda: self.transport(url, headers=headers or {}),
                self.should_cancel,
            )
        return call_with_retry(attempt, self.retry, should_cancel=self.should_cancel, on_throttle=self.limiter.defer)

    def download(self, url: str) -> tuple[str, bytes]:
        return self.limiter.dispatch(
            lambda: download_image(self.name, url, self.fetch),
            self.should_cancel,
        )

    def _found(self, url: str, source_url: str) -> dict:
        return {'provider': self.name, 'label': self.label, 'url': url, 'source_url': source_url, 'terms_url': self.terms_url,
                'license': self.license, 'author': self.author, 'embeddable': False}


class FanartClient(_Client):
    name = 'fanart.tv'
    label = 'fanart.tv'
    terms_url = 'https://fanart.tv/api-docs/api-v3/'
    license = 'fanart.tv community artwork: personal, non-commercial use; copyright stays with its authors'
    author = 'fanart.tv community'
    min_interval = 0.5
    ROOT = 'https://webservice.fanart.tv/v3/music/'

    def __init__(self, project_key: str, client_key: str = '', **options):
        if not project_key or not str(project_key).strip():
            raise SourceError('fanart_key_required')
        super().__init__(**options)
        self.project_key, self.client_key = str(project_key).strip(), str(client_key or '').strip()

    def _headers(self) -> dict:
        headers = {'api-key': self.project_key, 'Accept': 'application/json', 'User-Agent': 'MeloShelf (local music organizer)'}
        if self.client_key:
            headers['client-key'] = self.client_key              # a personal key; keys travel in headers, never in the URL
        return headers

    @staticmethod
    def _best(items):
        usable = [i for i in items or () if isinstance(i, dict) and isinstance(i.get('url'), str)]
        return max(usable, key=lambda i: int(i.get('likes') or 0), default=None)

    def artist_thumb(self, mb_artist_id: str) -> dict | None:
        if not UUID.match(mb_artist_id or ''):
            raise SourceError('musicbrainz_id_invalid')
        try:
            data = self._json(self.ROOT + quote(mb_artist_id.lower()), self._headers())
        except APIError as exc:
            if exc.status == 404:
                return None                                       # no artwork for this artist: a normal answer
            raise
        if not isinstance(data, dict) or str(data.get('mbid_id', '')).lower() != mb_artist_id.lower():
            return None                                           # the answer does not name this artist: never attach it
        best = self._best(data.get('artistthumb'))
        return self._found(best['url'], 'https://fanart.tv/artist/' + mb_artist_id.lower()) if best else None

    def album_cover(self, release_group_id: str) -> dict | None:
        if not UUID.match(release_group_id or ''):
            raise SourceError('musicbrainz_id_invalid')
        try:
            data = self._json(self.ROOT + 'albums/' + quote(release_group_id.lower()), self._headers())
        except APIError as exc:
            if exc.status == 404:
                return None
            raise
        albums = data.get('albums') if isinstance(data, dict) else None
        entry = next((v for k, v in (albums or {}).items() if str(k).lower() == release_group_id.lower()), None)
        best = self._best((entry or {}).get('albumcover'))
        return self._found(best['url'], 'https://fanart.tv/album/' + release_group_id.lower()) if best else None


class AudioDbClient(_Client):
    name = 'theaudiodb'
    label = 'TheAudioDB'
    terms_url = 'https://www.theaudiodb.com/free_music_api'
    license = 'TheAudioDB fan-created artwork; no licence stated: display only'
    author = 'TheAudioDB community'
    min_interval = 2.1                                           # its free tier allows 30 requests a minute
    ROOT = 'https://www.theaudiodb.com/api/v1/json/'

    def __init__(self, api_key: str, **options):
        key = str(api_key or '').strip()
        if not key:
            raise SourceError('audiodb_key_required')
        if key in SHARED_TEST_KEYS:
            raise SourceError('audiodb_premium_key_required')    # MusicBrainz lookups are a premium method; the shared key is not used
        super().__init__(**options)
        self.api_key = key

    def _call(self, endpoint: str, mbid: str):
        if not UUID.match(mbid or ''):
            raise SourceError('musicbrainz_id_invalid')
        try:
            return self._json(f'{self.ROOT}{quote(self.api_key, safe="")}/{endpoint}?i={quote(mbid.lower())}')
        except APIError as exc:
            if exc.status == 404:
                return None
            raise

    def artist_thumb(self, mb_artist_id: str) -> dict | None:
        data = self._call('artist-mb.php', mb_artist_id)
        rows = (data or {}).get('artists') if isinstance(data, dict) else None
        row = next((r for r in rows or () if isinstance(r, dict) and str(r.get('strMusicBrainzID', '')).lower() == mb_artist_id.lower()), None)
        url = (row or {}).get('strArtistThumb')
        return self._found(url, 'https://www.theaudiodb.com/artist/' + str(row.get('idArtist', ''))) if url else None

    def album_cover(self, release_group_id: str) -> dict | None:
        data = self._call('album-mb.php', release_group_id)
        rows = (data or {}).get('album') if isinstance(data, dict) else None
        row = next((r for r in rows or () if isinstance(r, dict) and str(r.get('strMusicBrainzID', '')).lower() == release_group_id.lower()), None)
        url = (row or {}).get('strAlbumThumb')
        return self._found(url, 'https://www.theaudiodb.com/album/' + str(row.get('idAlbum', ''))) if url else None
