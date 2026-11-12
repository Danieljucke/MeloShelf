"""Artist photographs from Wikimedia Commons, reached through MusicBrainz and Wikidata.

Chain: MusicBrainz artist (url-rels) -> Wikidata item -> its image (P18) -> Commons file metadata and a 256 px thumbnail.
Only a file that the artist's own Wikidata item names is ever requested; nothing is searched by name. The Commons
metadata supplies the licence name, author and file page, which the caller stores as attribution. Requests identify the
application with the public contact and are spaced (the MusicBrainz call shares that provider's one-per-second budget).
Transports are injectable so tests never touch the network.
"""
import html
import http.client
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from meloshelf.infrastructure.music_api import APIError, RateLimiter, _request_json, provider_limiter, read_complete
from meloshelf.infrastructure.redirect_policy import WIKIMEDIA_IMAGE_HOST, open_restricted

MB_URL = 'https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels&fmt=json'
WIKIDATA_URL = 'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json'
COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
THUMB_WIDTH = 256
MAX_BYTES = 1_000_000
_QID = re.compile(r'/(Q\d+)(?:$|[?#])')
_TAG = re.compile(r'<[^>]+>')
_wikimedia_limiter = RateLimiter(1.0)


def _plain(value) -> str:
    return ' '.join(html.unescape(_TAG.sub('', str(value or ''))).split())


def _fetch_bytes(url: str, user_agent: str):
    request = urllib.request.Request(url, headers={'User-Agent': user_agent, 'Accept': 'image/jpeg,image/png'})
    try:
        with open_restricted(request, WIKIMEDIA_IMAGE_HOST) as response:
            content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            data = read_complete(response, MAX_BYTES)
    except urllib.error.HTTPError as exc:
        raise APIError(f'Wikimedia answered HTTP {exc.code}', retryable=exc.code >= 500, status=exc.code) from exc
    except OSError as exc:
        raise APIError(f'Network error: {type(exc).__name__}', retryable=True) from exc
    except http.client.HTTPException as exc:  # the photograph stopped part-way
        raise APIError(f'Network error: {type(exc).__name__}', retryable=True, kind='connection') from exc
    return content_type, data


class WikimediaPhotoProvider:
    def __init__(self, user_agent: str, *, json_get=None, bytes_get=None, mb_limiter=None, wiki_limiter=None):
        if not user_agent or not user_agent.strip():
            raise ValueError('A meaningful User-Agent with an application contact is required')
        self.user_agent = user_agent.strip()
        self._json = json_get or (lambda url: _request_json(url, headers={'User-Agent': self.user_agent,
                                                                           'Accept': 'application/json'}))
        self._bytes = bytes_get or (lambda url: _fetch_bytes(url, self.user_agent))
        self._mb_limiter = mb_limiter if mb_limiter is not None else provider_limiter('musicbrainz')
        self._wiki_limiter = wiki_limiter if wiki_limiter is not None else _wikimedia_limiter

    def find(self, mb_id: str) -> dict:
        """{'status': 'found', ...attribution, thumb_url} or {'status': 'none', 'reason': ...}. Raises APIError."""
        self._mb_limiter.wait()
        artist = self._json(MB_URL.format(mbid=mb_id))
        qid = None
        for relation in (artist.get('relations') or []) if isinstance(artist, dict) else []:
            if isinstance(relation, dict) and relation.get('type') == 'wikidata':
                match = _QID.search((relation.get('url') or {}).get('resource', '') + '#')
                if match:
                    qid = match.group(1)
                    break
        if not qid:
            return {'status': 'none', 'reason': 'MusicBrainz lists no Wikidata item for this artist'}
        self._wiki_limiter.wait()
        entity = self._json(WIKIDATA_URL.format(qid=qid))
        try:
            claims = entity['entities'][qid]['claims'].get('P18') or []
            name = claims[0]['mainsnak']['datavalue']['value']
        except (KeyError, IndexError, TypeError):
            return {'status': 'none', 'reason': 'the Wikidata item has no image'}
        if not isinstance(name, str) or not name.strip():
            return {'status': 'none', 'reason': 'the Wikidata item has no image'}
        self._wiki_limiter.wait()
        query = urlencode({'action': 'query', 'titles': 'File:' + name, 'prop': 'imageinfo',
                           'iiprop': 'url|extmetadata|mime', 'iiurlwidth': THUMB_WIDTH, 'format': 'json',
                           'formatversion': 2})
        commons = self._json(COMMONS_API + '?' + query)
        try:
            info = commons['query']['pages'][0]['imageinfo'][0]
        except (KeyError, IndexError, TypeError):
            return {'status': 'none', 'reason': 'Commons has no information for that file'}
        meta = info.get('extmetadata') or {}
        thumb = info.get('thumburl') or ''
        if not thumb:
            return {'status': 'none', 'reason': 'Commons gave no thumbnail for that file'}
        return {'status': 'found', 'file': name,
                'license': _plain((meta.get('LicenseShortName') or {}).get('value')),
                'author': _plain((meta.get('Artist') or {}).get('value')),
                'source_url': info.get('descriptionurl') or ('https://commons.wikimedia.org/wiki/File:' + quote(name)),
                'thumb_url': thumb, 'mime': info.get('mime', '')}

    def download(self, url: str):
        """(content_type, bytes) for a thumbnail the caller will verify. Only Wikimedia hosts are fetched."""
        if not re.match(r'^https://(?:upload|thumb|commons)\.wikimedia\.org/', url or ''):
            raise APIError('Refusing to fetch an image from a host other than Wikimedia', retryable=False)
        self._wiki_limiter.wait()
        return self._bytes(url)
