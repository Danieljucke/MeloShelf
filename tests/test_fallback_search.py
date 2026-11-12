"""MusicBrainz text search for fallback candidates: rate limited, cached, cancellable, offline-safe.

A search result is a catalogue suggestion. Nothing here produces an acoustic match.
"""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from meloshelf.application.fallback import FallbackService
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import MusicBrainzClient, OperationCancelled, RateLimiter, RetryPolicy

UA = 'MeloShelf/0.2 (test)'
SEARCH_BODY = {'recordings': [
    {'id': 'rec-1', 'title': 'Making History', 'score': 100, 'length': 188000,
     'artist-credit': [{'name': 'Aaron Cole', 'artist': {'name': 'Aaron Cole'}}],
     'releases': [{'id': 'rel-1', 'title': 'Making History'}]},
    {'id': 'rec-2', 'title': 'Making History (Live)', 'score': 61, 'length': 230000,
     'artist-credit': [{'name': 'Someone Else', 'artist': {'name': 'Someone Else'}}]},
]}


class Recorder:
    def __init__(self, body=SEARCH_BODY, error=None):
        self.calls = []
        self.body = body
        self.error = error

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.body


def client(transport, **kw):
    return MusicBrainzClient(UA, transport=transport, limiter=RateLimiter(0), retry=RetryPolicy(max_attempts=1), **kw)


def test_the_query_names_the_title_and_artist_and_escapes_quotes():
    recorder = Recorder()
    client(recorder).search_recordings('Making "History"', 'Aaron Cole', limit=5)
    query = parse_qs(urlparse(recorder.calls[0]).query)['query'][0]
    assert 'recording:"Making \\"History\\""' in query and 'artist:"Aaron Cole"' in query


def test_results_are_returned_as_structured_candidates_with_their_scores_and_lengths():
    results = client(Recorder()).search_recordings('Making History', 'Aaron Cole')
    first = results[0]
    assert first['recording_id'] == 'rec-1' and first['artist'] == 'Aaron Cole'
    assert first['duration'] == 188.0 and first['search_score'] == 100
    assert first['releases'] == ['Making History']


def test_a_malformed_response_is_an_error_not_an_empty_success():
    from meloshelf.infrastructure.music_api import APIError
    with pytest.raises(APIError):
        client(Recorder(body={'unexpected': True})).search_recordings('x', 'y')


def test_the_shared_rate_limiter_is_applied_before_every_search():
    waits = []
    limiter = RateLimiter(1.1, clock=lambda: 0.0, sleeper=lambda s: waits.append(s))
    transport = Recorder()
    cli = MusicBrainzClient(UA, transport=transport, limiter=limiter, retry=RetryPolicy(max_attempts=1))
    cli.search_recordings('a', 'b')
    cli.search_recordings('c', 'd')
    assert len(transport.calls) == 2 and any(w > 0 for w in waits)


def test_a_cancelled_operation_sends_no_request():
    transport = Recorder()
    with pytest.raises(OperationCancelled):
        client(transport, should_cancel=lambda: True).search_recordings('a', 'b')
    assert transport.calls == []


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    return db


def test_the_service_caches_identical_searches_and_does_not_repeat_the_request(database):
    transport = Recorder()
    service = FallbackService(database, client(transport))
    first = service.search('Making History', 'Aaron Cole')
    second = service.search('Making History', 'Aaron Cole')
    assert first == second and len(transport.calls) == 1


def test_offline_mode_makes_no_network_request_and_says_so(database):
    transport = Recorder()
    service = FallbackService(database, client(transport), offline=True)
    result = service.search('Making History', 'Aaron Cole')
    assert transport.calls == [] and result is None


def test_a_provider_failure_is_reported_with_a_reason_and_is_not_cached_as_an_answer(database):
    from meloshelf.infrastructure.music_api import APIError
    failing = Recorder(error=APIError('Provider answered HTTP 503', retryable=True, status=503, kind='provider_error'))
    service = FallbackService(database, client(failing))
    result = service.search('Making History', 'Aaron Cole')
    assert result is None and service.last_error == 'provider_error'
    healthy = Recorder()
    assert FallbackService(database, client(healthy)).search('Making History', 'Aaron Cole') is not None
