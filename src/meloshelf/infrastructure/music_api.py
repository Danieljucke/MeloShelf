"""Strictly read-only fingerprint and catalog lookups. Never upload audio files."""
from collections.abc import Callable
from dataclasses import dataclass
import http.client
import json
import random
import re
import socket
import ssl
import time
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class APIError(RuntimeError):
    """A provider request failed. ``retryable`` tells transient from permanent failures.

    ``kind`` names the cause (dns, tls, timeout, connection, rate_limited, provider_error,
    invalid_key, request_rejected, malformed, http_rejected). ``provider_code`` and
    ``provider_message`` carry AcoustID's own error, already scrubbed of the client key.
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None,
                 retry_after: float | None = None, kind: str = '', provider_code: int | None = None,
                 provider_message: str = '', payload=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after
        self.kind = kind
        self.provider_code = provider_code
        self.provider_message = provider_message
        self.payload = payload


class OperationCancelled(Exception):
    """The user stopped the work; not a provider failure."""


@dataclass(frozen=True)
class RecordingCandidate:
    recording_id: str
    title: str
    artist: str
    acoustid_score: float
    releases: tuple[str, ...] = ()
    duration: float | None = None
    release_details: tuple[dict, ...] = ()


class RateLimiter:
    """At most one request per interval, shared by every thread using the instance."""

    def __init__(self, interval: float, *, clock=time.monotonic, sleeper=time.sleep):
        self.interval = max(0.0, interval)
        self.clock = clock
        self.sleeper = sleeper
        self._next = None
        self._lock = Lock()

    def reserve(self) -> float:
        """Book the next free slot and return how long the caller must wait for it."""
        with self._lock:
            now = self.clock()
            slot = now if self._next is None or self._next < now else self._next
            self._next = slot + self.interval
            return slot - now

    def defer(self, seconds: float) -> None:
        """Push every later request back, e.g. after the provider answered 429."""
        with self._lock:
            earliest = self.clock() + max(0.0, seconds)
            if self._next is None or self._next < earliest:
                self._next = earliest

    def wait(self, should_cancel: Callable[[], bool] | None = None) -> None:
        delay = self.reserve()  # the lock is released before sleeping
        if delay > 0:
            self.sleeper(delay)
        if should_cancel and should_cancel():
            raise OperationCancelled('Cancelled while waiting for the rate limit')


_PROVIDER_INTERVALS = {'acoustid': 1 / 3, 'musicbrainz': 1.1}
_provider_limiters: dict[str, RateLimiter] = {}
_provider_guard = Lock()


def provider_limiter(provider: str) -> RateLimiter:
    """The process-wide limiter of a provider, so concurrent workers share one budget."""
    with _provider_guard:
        if provider not in _provider_limiters:
            _provider_limiters[provider] = RateLimiter(_PROVIDER_INTERVALS[provider])
        return _provider_limiters[provider]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    max_retry_after: float = 120.0
    sleeper: Callable[[float], None] = time.sleep
    rng: Callable[[], float] = random.random

    def delay_before(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with jitter in [50%, 100%], never below Retry-After."""
        backoff = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return max(backoff * (0.5 + self.rng() / 2), retry_after or 0.0)


def call_with_retry(operation, policy: RetryPolicy, *, should_cancel=None, on_throttle=None):
    """Run ``operation``; retry transient APIErrors only, within the policy's bounds."""
    for attempt in range(1, policy.max_attempts + 1):
        if should_cancel and should_cancel():
            raise OperationCancelled('Cancelled before the request was sent')
        try:
            return operation()
        except APIError as exc:
            if not exc.retryable or attempt == policy.max_attempts:
                raise
            if exc.retry_after is not None and exc.retry_after > policy.max_retry_after:
                raise APIError(f'{exc} (provider asked to wait {exc.retry_after:.0f} s; giving up)',
                               status=exc.status, retry_after=exc.retry_after, kind=exc.kind) from exc
            delay = policy.delay_before(attempt, exc.retry_after)
            if exc.status == 429 and on_throttle:
                on_throttle(delay)
            policy.sleeper(delay)
    raise AssertionError('unreachable')


def _parse_retry_after(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _network_kind(exc: BaseException) -> str:
    """Name the cause of a transport failure without echoing the request."""
    reason = exc.reason if isinstance(exc, URLError) and isinstance(exc.reason, BaseException) else exc
    if isinstance(reason, socket.gaierror):
        return 'dns'
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        return 'tls'
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return 'timeout'
    return 'connection'


def _decode_error_body(exc: HTTPError):
    try:
        body = exc.read(3_000_001)
        return json.loads(body.decode('utf-8'))
    except (OSError, ValueError, AttributeError):
        return None


def read_complete(response, limit: int) -> bytes:
    """Read at most ``limit`` + 1 bytes. A body shorter than its declared Content-Length was interrupted: the
    connection closed part-way, and urllib returns the partial bytes without complaint, so it is checked here."""
    data = response.read(limit + 1)
    headers = getattr(response, 'headers', None)
    declared = (headers.get('Content-Length') or '').strip() if headers else ''
    if declared.isdigit() and len(data) <= limit and len(data) < int(declared):
        raise http.client.IncompleteRead(data, int(declared) - len(data))
    return data


def _request_json(url, *, data=None, headers=None, timeout=15):
    req = Request(url, data=data, headers=headers or {}, method='POST' if data is not None else 'GET')
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = read_complete(response, 3_000_000)
    except HTTPError as exc:
        # AcoustID answers rejected requests with HTTP 400 and a JSON error body; keep that body.
        payload = _decode_error_body(exc)
        retry_after = _parse_retry_after(exc.headers.get('Retry-After')) if exc.headers else None
        if exc.code == 429:
            kind = 'rate_limited'
        elif exc.code in RETRYABLE_STATUS:
            kind = 'provider_error'
        else:
            kind = 'http_rejected'
        raise APIError(f'Provider answered HTTP {exc.code}', retryable=exc.code in RETRYABLE_STATUS,
                       status=exc.code, retry_after=retry_after, kind=kind, payload=payload) from exc
    except OSError as exc:  # URLError, timeouts, resets, DNS and certificate failures
        kind = _network_kind(exc)
        raise APIError(f'Network error ({kind}): {type(exc).__name__}', retryable=kind in ('timeout', 'connection'),
                       kind=kind) from exc
    except http.client.HTTPException as exc:  # the answer stopped part-way (IncompleteRead) or broke the protocol
        raise APIError(f'Network error (connection): {type(exc).__name__}', retryable=True, kind='connection') from exc
    if len(raw) > 3_000_000:
        raise APIError('API response too large', kind='malformed')
    try:
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise APIError('Provider returned a malformed response', kind='malformed') from exc


class _ProviderClient:
    provider = ''

    def __init__(self, *, transport, limiter, retry, timeout, should_cancel):
        self.transport = transport
        self.limiter = limiter or provider_limiter(self.provider)
        self.retry = retry or RetryPolicy()
        self.timeout = timeout
        self.should_cancel = should_cancel

    def _send(self, url, **request):
        def attempt():
            self.limiter.wait(self.should_cancel)
            return self.transport(url, timeout=self.timeout, **request)
        return call_with_retry(attempt, self.retry, should_cancel=self.should_cancel,
                               on_throttle=self.limiter.defer)


class AcoustIDClient(_ProviderClient):
    ENDPOINT = 'https://api.acoustid.org/v2/lookup'
    # Documented by AcoustID: code 4 is "invalid API key" (verified against the live service).
    # Other codes are not classified as authentication errors until they are verified.
    INVALID_KEY_CODE = 4
    # Code 2 (missing parameter) and 3 (invalid fingerprint) mean MeloShelf sent a bad request.
    REQUEST_CODES = frozenset({2, 3})
    provider = 'acoustid'

    def _scrub(self, text) -> str:
        """Provider text can never carry the client key back to the user, a log or the browser."""
        return str(text or '').replace(self.client_key, '[key]')

    def _error_from_payload(self, payload, status=None) -> APIError:
        error = payload.get('error') if isinstance(payload, dict) and isinstance(payload.get('error'), dict) else {}
        code = error.get('code') if isinstance(error.get('code'), int) else None
        message = self._scrub(error.get('message', ''))[:200]
        if code == self.INVALID_KEY_CODE:
            return APIError(f'AcoustID rejected the key (code {code}).', status=401, kind='invalid_key',
                            provider_code=code, provider_message=message, payload=payload)
        if code in self.REQUEST_CODES:
            return APIError(f'AcoustID rejected the request (code {code}): {message}', status=status or 400,
                            kind='request_rejected', provider_code=code, provider_message=message, payload=payload)
        return APIError(f'AcoustID returned an error (code {code}): {message}', status=status,
                        kind='provider_error', provider_code=code, provider_message=message, payload=payload)

    def __init__(self, client_key: str, *, transport=_request_json, limiter=None, retry=None,
                 timeout: float = 20, should_cancel=None):
        if not client_key or not client_key.strip():
            raise ValueError('AcoustID application client key is required')
        self.client_key = client_key.strip()
        super().__init__(transport=transport, limiter=limiter, retry=retry, timeout=timeout,
                         should_cancel=should_cancel)

    def lookup(self, fingerprint: str, duration: int) -> list[RecordingCandidate]:
        data = urlencode({'client':self.client_key,'fingerprint':fingerprint,'duration':duration,
                          'meta':'recordings','format':'json'}).encode('utf-8')
        try:
            payload = self._send(self.ENDPOINT, data=data, headers={'Content-Type':'application/x-www-form-urlencoded',
                                              'User-Agent':'MeloShelf/0.2 (local music organizer)'})
        except APIError as exc:
            if isinstance(exc.payload, dict) and exc.payload.get('status') == 'error':
                raise self._error_from_payload(exc.payload, status=exc.status) from exc
            raise
        if isinstance(payload, dict) and payload.get('status') == 'error':
            raise self._error_from_payload(payload)
        if not isinstance(payload, dict) or payload.get('status') != 'ok' or not isinstance(payload.get('results'), list):
            raise APIError('AcoustID sent a response MeloShelf cannot read.', kind='malformed')
        candidates = {}
        for match in payload['results']:
            if not isinstance(match, dict):
                continue
            score = max(0.0,min(1.0,float(match.get('score',0))))
            for rec in match.get('recordings',[]) or []:
                if not isinstance(rec,dict) or not isinstance(rec.get('id'),str):
                    continue
                artists = rec.get('artists') or []
                name = ', '.join(a.get('name','') for a in artists if isinstance(a,dict) and a.get('name'))
                length = rec.get('duration')
                item = RecordingCandidate(rec['id'],str(rec.get('title','')),name,score,
                                          duration=float(length) if isinstance(length,(int,float)) and length>0 else None)
                if item.recording_id not in candidates or score > candidates[item.recording_id].acoustid_score:
                    candidates[item.recording_id] = item
        return sorted(candidates.values(),key=lambda c:c.acoustid_score,reverse=True)


class MusicBrainzClient(_ProviderClient):
    ROOT = 'https://musicbrainz.org/ws/2/recording/'
    ARTIST_ROOT = 'https://musicbrainz.org/ws/2/artist/'
    RELEASE_GROUP_ROOT = 'https://musicbrainz.org/ws/2/release-group/'
    UUID = re.compile(r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$', re.I)
    provider = 'musicbrainz'

    def __init__(self, user_agent: str, *, transport=_request_json, limiter=None, retry=None,
                 timeout: float = 20, should_cancel=None):
        if not user_agent or not user_agent.strip():
            raise ValueError('A meaningful MusicBrainz User-Agent with application contact is required')
        self.user_agent = user_agent.strip()
        super().__init__(transport=transport, limiter=limiter, retry=retry, timeout=timeout,
                         should_cancel=should_cancel)

    def search_recordings(self, title: str, artist: str = '', *, limit: int = 5) -> list[dict]:
        """Catalogue text search. Results are suggestions for review; they are never acoustic matches."""
        def phrase(text: str) -> str:
            return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'
        query = 'recording:' + phrase(title) + (' AND artist:' + phrase(artist) if artist else '')
        url = self.ROOT + '?' + urlencode({'query': query, 'limit': max(1, min(int(limit), 25)), 'fmt': 'json'})
        payload = self._send(url, headers={'User-Agent': self.user_agent, 'Accept': 'application/json'})
        if not isinstance(payload, dict) or not isinstance(payload.get('recordings'), list):
            raise APIError('MusicBrainz sent a search response MeloShelf cannot read.', kind='malformed')
        results = []
        for item in payload['recordings']:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            credits = item.get('artist-credit') or []
            name = ''.join((c.get('name') or '') + (c.get('joinphrase') or '') for c in credits if isinstance(c, dict))
            length = item.get('length')
            results.append({
                'recording_id': item['id'], 'title': str(item.get('title') or ''), 'artist': name.strip(),
                'search_score': int(item.get('score') or 0),
                'duration': length / 1000 if isinstance(length, (int, float)) and length > 0 else None,
                'releases': [r.get('title', '') for r in (item.get('releases') or []) if isinstance(r, dict)][:5],
            })
        return results

    def search_artists(self, name: str, *, limit: int = 5) -> list[dict]:
        """Artist search by name. Each hit is a candidate; the caller decides whether any of them is a link."""
        def phrase(text: str) -> str:
            return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'
        url = (self.ARTIST_ROOT + '?' +
               urlencode({'query': 'artist:' + phrase(name), 'limit': max(1, min(int(limit), 10)), 'fmt': 'json'}))
        payload = self._send(url, headers={'User-Agent': self.user_agent, 'Accept': 'application/json'})
        if not isinstance(payload, dict) or not isinstance(payload.get('artists'), list):
            raise APIError('MusicBrainz sent an artist response MeloShelf cannot read.', kind='malformed')
        hits = []
        for item in payload['artists']:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            aliases = [a.get('name') for a in (item.get('aliases') or []) if isinstance(a, dict) and a.get('name')]
            hits.append({'mb_artist_id': item['id'], 'name': str(item.get('name') or ''),
                         'score': int(item.get('score') or 0), 'aliases': aliases,
                         'disambiguation': str(item.get('disambiguation') or '')})
        return hits

    def search_release_groups(self, album: str, artist: str = '', *, limit: int = 5) -> list[dict]:
        """Catalogue search for an album. A hit is a suggestion about which album this is; it is never proof."""
        def phrase(text: str) -> str:
            return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'
        query = 'releasegroup:' + phrase(album) + (' AND artist:' + phrase(artist) if artist else '')
        url = self.RELEASE_GROUP_ROOT + '?' + urlencode({'query': query, 'limit': max(1, min(int(limit), 10)), 'fmt': 'json'})
        payload = self._send(url, headers={'User-Agent': self.user_agent, 'Accept': 'application/json'})
        if not isinstance(payload, dict) or not isinstance(payload.get('release-groups'), list):
            raise APIError('MusicBrainz sent an album response MeloShelf cannot read.', kind='malformed')
        hits = []
        for item in payload['release-groups']:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not self.UUID.fullmatch(item['id']):
                continue
            credits = item.get('artist-credit') or []
            name = ''.join((c.get('name') or '') + (c.get('joinphrase') or '') for c in credits if isinstance(c, dict))
            hits.append({'group_id': item['id'].lower(), 'title': str(item.get('title') or ''), 'artist': name.strip(),
                         'score': int(item.get('score') or 0), 'primary_type': str(item.get('primary-type') or ''),
                         'secondary_types': [str(s) for s in (item.get('secondary-types') or [])],
                         'first_release_date': str(item.get('first-release-date') or '')})
        return hits

    def lookup_recording(self, recording_id: str) -> dict:
        if not self.UUID.fullmatch(recording_id):
            raise ValueError('Invalid MusicBrainz recording ID')
        payload = self._send(self.ROOT + recording_id + '?' + urlencode({'inc':'artist-credits+releases+release-groups','fmt':'json'}),
                             headers={'User-Agent':self.user_agent,'Accept':'application/json'})
        if not isinstance(payload,dict) or payload.get('id','').lower()!=recording_id.lower():
            raise APIError('Invalid MusicBrainz recording response')
        return payload
