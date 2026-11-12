"""Provider HTTP behaviour: retries, backoff, 429 handling, rate limits, cancellation.

All timing is driven by injected clocks and sleepers; no test waits in real time
except the one that checks the limiter across real threads.
"""
import io
import threading
import time
from urllib.error import HTTPError, URLError

import pytest

from meloshelf.infrastructure import music_api
from meloshelf.infrastructure.music_api import (
    AcoustIDClient, APIError, MusicBrainzClient, OperationCancelled, RateLimiter, RetryPolicy,
    call_with_retry, provider_limiter,
)

MBID = '11111111-1111-4111-8111-111111111111'
OK_ACOUSTID = {'status': 'ok', 'results': []}


class Recorder:
    """Sleeper that records delays instead of waiting."""
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def failing_then(outcomes):
    """Transport that raises/returns the given outcomes in order."""
    calls = []

    def transport(url, *, data=None, headers=None, timeout=None):
        calls.append(timeout)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    transport.calls = calls
    return transport


def policy(sleeper, **overrides):
    settings = {'max_attempts': 4, 'base_delay': 1.0, 'max_delay': 30.0, 'sleeper': sleeper, 'rng': lambda: 1.0}
    settings.update(overrides)
    return RetryPolicy(**settings)


def no_limit():
    return RateLimiter(0)


# ── retry policy ─────────────────────────────────────────────────────────────

def test_transient_failures_are_retried_with_exponential_backoff():
    sleeper = Recorder()
    transport = failing_then([APIError('down', retryable=True), APIError('down', retryable=True), OK_ACOUSTID])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(), retry=policy(sleeper))
    assert client.lookup('fp', 100) == []
    assert len(transport.calls) == 3
    assert sleeper.delays == [1.0, 2.0]


def test_jitter_scales_each_delay_by_the_injected_random_factor():
    sleeper = Recorder()
    factors = iter([0.0, 0.5])
    transport = failing_then([APIError('x', retryable=True), APIError('x', retryable=True), OK_ACOUSTID])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(),
                            retry=policy(sleeper, rng=lambda: next(factors)))
    client.lookup('fp', 100)
    assert sleeper.delays == [0.5, 1.5], 'delay = backoff * (0.5 + rng/2): never below half the backoff'


def test_backoff_is_capped_by_max_delay():
    sleeper = Recorder()
    transport = failing_then([APIError('x', retryable=True)] * 5 + [OK_ACOUSTID])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(),
                            retry=policy(sleeper, max_attempts=6, max_delay=3.0))
    client.lookup('fp', 100)
    assert sleeper.delays == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_gives_up_after_max_attempts_and_reports_the_last_error():
    sleeper = Recorder()
    transport = failing_then([APIError('still down', retryable=True)])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(), retry=policy(sleeper, max_attempts=3))
    with pytest.raises(APIError, match='still down'):
        client.lookup('fp', 100)
    assert len(transport.calls) == 3
    assert len(sleeper.delays) == 2


def test_permanent_errors_are_not_retried():
    sleeper = Recorder()
    transport = failing_then([APIError('bad request', retryable=False, status=400)])
    client = MusicBrainzClient('MeloShelf-tests/1 (dev@example.org)', transport=transport,
                               limiter=no_limit(), retry=policy(sleeper))
    with pytest.raises(APIError):
        client.lookup_recording(MBID)
    assert len(transport.calls) == 1
    assert sleeper.delays == []


def test_invalid_payload_is_permanent_and_not_retried():
    sleeper = Recorder()
    transport = failing_then([{'status': 'error', 'error': {'message': 'invalid API key'}}])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(), retry=policy(sleeper))
    with pytest.raises(APIError):
        client.lookup('fp', 100)
    assert len(transport.calls) == 1


# ── 429 / Retry-After ────────────────────────────────────────────────────────

def test_429_waits_at_least_retry_after():
    now = [0.0]
    delays = []

    def sleeper(seconds):  # one fake clock for both the retry policy and the limiter
        delays.append(seconds)
        now[0] += seconds

    transport = failing_then([APIError('slow down', retryable=True, status=429, retry_after=7.0), OK_ACOUSTID])
    client = AcoustIDClient('k', transport=transport, retry=policy(sleeper),
                            limiter=RateLimiter(0, clock=lambda: now[0], sleeper=sleeper))
    client.lookup('fp', 100)
    assert delays == [7.0], 'waits once for Retry-After, not once per mechanism'


def test_retry_after_longer_than_the_ceiling_fails_instead_of_hanging():
    sleeper = Recorder()
    transport = failing_then([APIError('banned', retryable=True, status=429, retry_after=3600.0)])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(),
                            retry=policy(sleeper, max_retry_after=120.0))
    with pytest.raises(APIError, match='3600'):
        client.lookup('fp', 100)
    assert sleeper.delays == []


def test_429_pushes_back_the_shared_limiter_for_every_worker():
    now = [100.0]
    limiter = RateLimiter(1.0, clock=lambda: now[0], sleeper=lambda s: now.__setitem__(0, now[0] + s))
    transport = failing_then([APIError('slow', retryable=True, status=429, retry_after=10.0), OK_ACOUSTID])
    client = AcoustIDClient('k', transport=transport, limiter=limiter,
                            retry=policy(lambda s: now.__setitem__(0, now[0] + s)))
    client.lookup('fp', 100)
    assert limiter.reserve() >= 0.9, 'a second worker must not fire immediately after a 429'
    assert now[0] >= 110.0


# ── transport classification ────────────────────────────────────────────────

def http_error(code, headers=None):
    return HTTPError('https://example.invalid', code, 'error', headers or {}, io.BytesIO(b''))


@pytest.mark.parametrize('raised, retryable, status', [
    (http_error(500), True, 500),
    (http_error(502), True, 502),
    (http_error(503), True, 503),
    (http_error(504), True, 504),
    (http_error(429), True, 429),
    (http_error(400), False, 400),
    (http_error(401), False, 401),
    (http_error(404), False, 404),
    (URLError('name resolution failed'), True, None),
    (TimeoutError('timed out'), True, None),
    (ConnectionResetError('reset'), True, None),
])
def test_transport_classifies_failures(monkeypatch, raised, retryable, status):
    def fake_urlopen(request, timeout=None):
        raise raised
    monkeypatch.setattr(music_api, 'urlopen', fake_urlopen)
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://example.invalid')
    assert caught.value.retryable is retryable
    assert caught.value.status == status


def test_transport_parses_retry_after_seconds(monkeypatch):
    monkeypatch.setattr(music_api, 'urlopen',
                        lambda request, timeout=None: (_ for _ in ()).throw(http_error(429, {'Retry-After': '12'})))
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://example.invalid')
    assert caught.value.retry_after == 12.0


def test_transport_ignores_unparseable_retry_after(monkeypatch):
    monkeypatch.setattr(music_api, 'urlopen',
                        lambda request, timeout=None: (_ for _ in ()).throw(http_error(503, {'Retry-After': 'soon'})))
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://example.invalid')
    assert caught.value.retry_after is None


def test_malformed_json_body_is_a_permanent_error(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _n): return b'<html>gateway</html>'
    monkeypatch.setattr(music_api, 'urlopen', lambda request, timeout=None: Response())
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://example.invalid')
    assert caught.value.retryable is False


# ── timeouts ─────────────────────────────────────────────────────────────────

def test_request_timeout_is_configurable_per_client():
    transport = failing_then([OK_ACOUSTID])
    AcoustIDClient('k', transport=transport, limiter=no_limit(), timeout=3.5).lookup('fp', 100)
    assert transport.calls == [3.5]
    mb_transport = failing_then([{'id': MBID}])
    MusicBrainzClient('MeloShelf-tests/1 (dev@example.org)', transport=mb_transport,
                      limiter=no_limit(), timeout=9).lookup_recording(MBID)
    assert mb_transport.calls == [9]


# ── rate limiting ────────────────────────────────────────────────────────────

def test_every_attempt_including_retries_passes_through_the_limiter():
    waits = []

    class CountingLimiter(RateLimiter):
        def wait(self, should_cancel=None):
            waits.append(1)

    transport = failing_then([APIError('x', retryable=True), OK_ACOUSTID])
    AcoustIDClient('k', transport=transport, limiter=CountingLimiter(0), retry=policy(Recorder())).lookup('fp', 1)
    assert len(waits) == 2


def test_clients_share_one_limiter_per_provider_by_default():
    first, second = AcoustIDClient('a'), AcoustIDClient('b')
    assert first.limiter is second.limiter is provider_limiter('acoustid')
    agent = 'MeloShelf-tests/1 (dev@example.org)'
    assert MusicBrainzClient(agent).limiter is provider_limiter('musicbrainz')
    assert provider_limiter('acoustid') is not provider_limiter('musicbrainz')
    assert provider_limiter('acoustid').interval == pytest.approx(1 / 3)
    assert provider_limiter('musicbrainz').interval >= 1.0


def test_limiter_spaces_requests_from_concurrent_workers():
    limiter = RateLimiter(0.05)
    stamps = []
    guard = threading.Lock()

    def worker():
        for _ in range(4):
            limiter.wait()
            with guard:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 16
    assert min(gaps) >= 0.035, f'requests fired {min(gaps) * 1000:.0f} ms apart'


def test_limiter_does_not_hold_its_lock_while_sleeping():
    """A sleeping worker must not prevent others from reserving their slot."""
    now = [0.0]
    inside = []

    def sleeper(seconds):
        inside.append(limiter.reserve())  # would deadlock if the lock were held
        now[0] += seconds

    limiter = RateLimiter(1.0, clock=lambda: now[0], sleeper=sleeper)
    limiter.wait()
    limiter.wait()
    assert inside == [2.0]


# ── cancellation ─────────────────────────────────────────────────────────────

def test_cancellation_before_a_request_sends_nothing():
    transport = failing_then([OK_ACOUSTID])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(), should_cancel=lambda: True)
    with pytest.raises(OperationCancelled):
        client.lookup('fp', 100)
    assert transport.calls == []


def test_cancellation_during_backoff_stops_retrying():
    cancelled = []
    sleeper = lambda seconds: cancelled.append(True)  # the user cancels while we wait
    transport = failing_then([APIError('x', retryable=True)])
    client = AcoustIDClient('k', transport=transport, limiter=no_limit(),
                            retry=policy(sleeper), should_cancel=lambda: bool(cancelled))
    with pytest.raises(OperationCancelled):
        client.lookup('fp', 100)
    assert len(transport.calls) == 1


def test_cancellation_is_not_an_api_error():
    assert not issubclass(OperationCancelled, APIError)


def test_call_with_retry_returns_the_operation_result():
    assert call_with_retry(lambda: 42, policy(Recorder())) == 42
