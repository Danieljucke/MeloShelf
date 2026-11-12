"""P7: controlled network-failure qualification.

Every failure here is produced locally: a test HTTP server on 127.0.0.1 answers with throttling, server errors,
malformed or interrupted bodies and slow replies; connection refusal uses a closed local port; TLS failure is an
HTTPS request to the plain-HTTP test server; DNS failure is a stubbed resolver. No real provider is contacted.

Each failure must become a named, bounded, recoverable outcome: never an uncaught exception, an endless retry or a
cached "missing" answer that would hide the item after connectivity returns.
"""
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from meloshelf.application.recognition import classify_api_error
from meloshelf.infrastructure import artwork, wikimedia
from meloshelf.infrastructure.artwork import ArtworkCache, fetch_from_archive
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.music_api import (AcoustIDClient, APIError, RateLimiter, RetryPolicy, _request_json,
                                                call_with_retry)
from test_jobs import WAIT, FakeRecognition, database, job_of, run_identify, scanned  # noqa: F401  (fixture reuse)

RELEASE = '11111111-2222-3333-4444-555555555555'


class Provider(BaseHTTPRequestHandler):
    hits: dict = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body=b'', *, content_type='application/json', headers=None, length=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body) if length is None else length))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        Provider.hits[path] = Provider.hits.get(path, 0) + 1
        if path == '/ok':
            return self._send(200, b'{"ok": true}')
        if path == '/429':
            return self._send(429, b'{}', headers={'Retry-After': '2'})
        if path in ('/500', '/502', '/503'):
            return self._send(int(path[1:]), b'{}')
        if path == '/malformed':
            return self._send(200, b'{not json')
        if path == '/truncated':                     # promises 4000 bytes, sends a few, then closes
            self.close_connection = True
            return self._send(200, b'{"partial":', length=4000)
        if path == '/slow':
            time.sleep(1.5)
            return self._send(200, b'{}')
        if path == '/flaky':                         # two server errors, then a normal answer
            return self._send(503, b'{}') if Provider.hits[path] <= 2 else self._send(200, b'{"ok": true}')
        if path == '/image-truncated':
            self.close_connection = True
            return self._send(200, b'\xff\xd8\xff' + b'\x00' * 50, content_type='image/jpeg', length=20000)
        if path == '/image-503':
            return self._send(503, b'', content_type='text/plain')
        return self._send(404, b'{}')


@pytest.fixture
def provider(monkeypatch):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NO_PROXY', '*')
    monkeypatch.setattr(urllib.request, '_opener', None)
    Provider.hits = {}
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


def failure_of(call):
    with pytest.raises(APIError) as caught:
        call()
    return caught.value


# ── each failure has a name, a retry decision and a job outcome ────────────────

@pytest.mark.parametrize('path,kind,retryable,status', [
    ('/429', 'rate_limited', True, 429),
    ('/500', 'provider_error', True, 500),
    ('/502', 'provider_error', True, 502),
    ('/503', 'provider_error', True, 503),
    ('/malformed', 'malformed', False, None),
    ('/truncated', 'connection', True, None),
])
def test_a_provider_answer_that_fails_is_named_and_marked_retryable_or_not(provider, path, kind, retryable, status):
    error = failure_of(lambda: _request_json(provider + path, timeout=5))
    assert (error.kind, error.retryable, error.status) == (kind, retryable, status)
    if path == '/429':
        assert error.retry_after == 2.0


def test_a_slow_provider_is_a_retryable_timeout(provider):
    error = failure_of(lambda: _request_json(provider + '/slow', timeout=0.4))
    assert (error.kind, error.retryable) == ('timeout', True)


def test_a_refused_connection_is_a_retryable_connection_failure():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()                                    # nothing listens on this port now
    error = failure_of(lambda: _request_json(f'http://127.0.0.1:{port}/ok', timeout=3))
    assert (error.kind, error.retryable) == ('connection', True)


def test_a_tls_failure_is_named_and_not_retried(provider):
    secure = provider.replace('http://', 'https://')  # TLS to a plain-HTTP server fails the handshake
    error = failure_of(lambda: _request_json(secure + '/ok', timeout=3))
    assert (error.kind, error.retryable) == ('tls', False)


def test_a_dns_failure_is_named_and_not_retried(monkeypatch):
    real = socket.getaddrinfo

    def resolver(host, *args, **kwargs):
        if host == 'provider.invalid':
            raise socket.gaierror(11001, 'getaddrinfo failed')
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', resolver)
    error = failure_of(lambda: _request_json('http://provider.invalid/ok', timeout=3))
    assert (error.kind, error.retryable) == ('dns', False)


@pytest.mark.parametrize('kind,status,retryable,code', [
    ('dns', None, False, 'network_error'), ('tls', None, False, 'network_error'),
    ('timeout', None, True, 'network_error'), ('connection', None, True, 'network_error'),
    ('rate_limited', 429, True, 'rate_limited'), ('provider_error', 503, True, 'provider_unavailable'),
    ('malformed', None, False, 'provider_error'),
])
def test_every_failure_maps_to_a_job_outcome_the_page_explains(kind, status, retryable, code):
    assert classify_api_error(APIError('x', kind=kind, status=status, retryable=retryable)) == code


# ── retries are bounded, honour Retry-After, and recover ───────────────────────

def test_throttling_is_retried_a_bounded_number_of_times_and_waits_for_retry_after(provider):
    delays = []
    policy = RetryPolicy(max_attempts=3, base_delay=0.01, sleeper=delays.append, rng=lambda: 0.0)
    error = failure_of(lambda: call_with_retry(lambda: _request_json(provider + '/429', timeout=5), policy))
    assert error.kind == 'rate_limited'
    assert Provider.hits['/429'] == 3, 'no more requests than the policy allows'
    assert delays == [2.0, 2.0], 'each wait honours Retry-After'


def test_giving_up_on_a_very_long_retry_after_keeps_the_rate_limit_explained():
    def throttled():
        raise APIError('Provider answered HTTP 429', retryable=True, status=429, retry_after=900, kind='rate_limited')

    error = failure_of(lambda: call_with_retry(throttled, RetryPolicy(max_attempts=4, sleeper=lambda s: None)))
    assert (error.kind, error.status) == ('rate_limited', 429)
    assert classify_api_error(error) == 'rate_limited'


def test_a_provider_that_recovers_answers_on_a_later_attempt(provider):
    policy = RetryPolicy(max_attempts=4, base_delay=0.01, sleeper=lambda s: None, rng=lambda: 0.0)
    assert call_with_retry(lambda: _request_json(provider + '/flaky', timeout=5), policy) == {'ok': True}
    assert Provider.hits['/flaky'] == 3


def test_an_interrupted_identification_lookup_is_a_retryable_network_error_for_the_file(provider):
    client = AcoustIDClient('a-test-key-0000', limiter=RateLimiter(0),
                            retry=RetryPolicy(max_attempts=2, base_delay=0.01, sleeper=lambda s: None),
                            transport=lambda url, **request: _request_json(provider + '/truncated', timeout=5))
    error = failure_of(lambda: client.lookup('AQAA', 120))
    assert classify_api_error(error) == 'network_error'
    assert Provider.hits['/truncated'] == 2, 'retried within the policy, then stopped'


# ── artwork: a failed download is not remembered as "no cover" ─────────────────

@pytest.fixture
def plain_open(monkeypatch):
    """The image fetchers restrict redirects to the providers' hosts; the local server needs a plain opener."""
    opener = lambda request, allowed: urllib.request.urlopen(request, timeout=5)  # noqa: E731
    monkeypatch.setattr(artwork, 'open_restricted', opener)
    monkeypatch.setattr(wikimedia, 'open_restricted', opener)


def test_an_interrupted_cover_download_is_a_network_failure(provider, plain_open):
    with pytest.raises(OSError):
        fetch_from_archive(provider + '/image-truncated')


@pytest.mark.parametrize('path', ['/image-truncated', '/image-503'])
def test_a_failed_cover_download_shows_no_cover_and_is_retried_later(provider, plain_open, tmp_path, path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    cache = ArtworkCache(db, tmp_path / 'covers', fetch=lambda url: fetch_from_archive(provider + path))
    assert cache.cover(RELEASE) is None
    with db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM artwork').fetchone()[0] == 0, 'nothing recorded: the next view asks again'
    assert cache.cover(RELEASE) is None
    assert Provider.hits[path] == 2


def test_an_interrupted_photo_download_is_a_retryable_provider_error(provider, plain_open):
    error = failure_of(lambda: wikimedia._fetch_bytes(provider + '/image-truncated', 'MeloShelf-test/1.0'))
    assert error.retryable is True


# ── a paused job resumes when the network returns; nothing is lost ──────────────

def test_a_paused_identification_resumes_and_completes_when_the_network_returns(tmp_path, database):
    recognition = FakeRecognition(default='network_error')
    service, _ = scanned(tmp_path, database, count=12, recognition=recognition)
    job_id = run_identify(service) or job_of(service, 'identify')['id']
    paused = job_of(service, 'identify')
    assert paused['state'] == 'PAUSED' and len(recognition.calls) == 5, 'bounded: no retry loop while offline'
    assert paused['counters']['error'] == 5 and paused['counters']['pending'] == 7, 'progress is kept'

    recognition.default = 'review'                   # connectivity is back
    resumed = service.resume(paused['id'])
    assert resumed['started'] is True
    assert service.wait(paused['id'], WAIT)
    retried = service.retry_failed(paused['id'])
    assert retried['started'] is True and retried['requeued'] == 5
    assert service.wait(paused['id'], WAIT)
    done = job_of(service, 'identify')
    assert done['counters']['done'] == 12 and done['counters']['error'] == 0
    assert len(recognition.calls) == 5 + 12, 'each file was attempted once after the network returned'
    assert done['errors'] == [], 'the recovered failures are no longer reported'
    assert done['can']['retry'] is False, 'nothing is left to retry'


def test_an_enrichment_paused_by_an_outage_finishes_when_the_network_returns_and_loses_no_name(tmp_path):
    from meloshelf.domain.models import Track
    from meloshelf.infrastructure.repository import LibraryRepository
    from test_artist_enrichment import FakeClient, service
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    names = ['Adele', 'Queen', 'Prince', 'Tauren Wells', 'Jenn Johnson']
    LibraryRepository(db).save_many([Track(f'/{i}.mp3', 1, '.mp3', 1, 'T', n, 'A', '', 'tagged')
                                     for i, n in enumerate(names)])
    client = FakeClient(hard_down=True)
    svc = service(db, client)
    job = svc.create()
    paused = svc.run_to_completion(job['id'])
    assert paused['status'] == 'paused' and len(client.calls) <= 6, 'bounded while the provider is down'

    client.hard_down = False                       # connectivity is back
    svc.resume(job['id'])
    finished = svc.run_to_completion(job['id'])
    assert finished['status'] == 'done'
    assert finished['linked'] + finished['failed'] == 5
    # the names that failed during the outage have no stored answer, so the next job asks for them again
    retry = svc.create()
    assert retry['planned'] == finished['failed']
    assert svc.run_to_completion(retry['id'])['linked'] == finished['failed']
