"""AcoustID diagnostics: the Test button must report what AcoustID actually answered, never a bare type name."""
import io
import json
import socket
import ssl
import urllib.error
from urllib.error import HTTPError, URLError

import pytest

from meloshelf.application.config import AppConfig
from meloshelf.application.webapp import run_connection_test
from meloshelf.infrastructure import music_api
from meloshelf.infrastructure.music_api import APIError, AcoustIDClient, RetryPolicy
from meloshelf.infrastructure.secrets import MemorySecretStore, SecretStoreError

KEY = 'AbCdEf1234_xyz-SECRETVALUE'
# A fingerprint that AcoustID accepts as well-formed; the connection test must send a real one.
PROBE_FINGERPRINT = lambda: ('AQADtEmUaEqSJMgG', 30)


class FakeStore:
    def __init__(self):
        self.values = {}

    def get_setting(self, name, default=''):
        return self.values.get(name, default)

    def set_setting(self, name, value):
        self.values[name] = value


def no_retry():
    return RetryPolicy(max_attempts=1, sleeper=lambda _s: None)


def http_error(code, payload=None):
    body = io.BytesIO(json.dumps(payload).encode() if payload is not None else b'')
    return HTTPError('https://api.acoustid.org/v2/lookup', code, 'msg', {}, body)


def transport_raising(exc):
    def transport(url, **kwargs):
        raise exc
    return transport


def config_with(secrets=None, env=None):
    secrets = secrets if secrets is not None else MemorySecretStore()
    return AppConfig(FakeStore(), secrets, env=env or {}, which=lambda name: None), secrets


# ── 1–2. saving a key and reading its status back, without exposing it ──────────

def test_saved_key_is_read_back_from_the_secure_store_and_reported_without_its_value():
    config, _ = config_with()
    config.set_acoustid_key(KEY)
    assert config.acoustid_key() == (KEY, 'secure store')
    summary = repr(config.summary())
    assert KEY not in summary and config.summary()['acoustid'] == {'configured': True, 'source': 'secure store'}


# ── 3. testing right after saving, without restart ──────────────────────────────

def test_test_immediately_after_saving_uses_the_saved_key(monkeypatch):
    config, _ = config_with()
    config.set_acoustid_key(KEY)
    seen = []

    def transport(url, **kwargs):
        seen.append(kwargs['data'])
        return {'status': 'ok', 'results': []}

    result = run_connection_test('acoustid', config, transport=transport, fingerprint=PROBE_FINGERPRINT)
    assert result['state'] == 'valid' and result['ok'] is True
    assert KEY.encode() in seen[0]  # the lookup really carries the saved key
    assert KEY not in json.dumps(result)


# ── 4. restart and reuse of the stored configuration ───────────────────────────

def test_stored_key_survives_a_restart_because_it_lives_in_the_secure_store():
    secrets = MemorySecretStore()
    config, _ = config_with(secrets)
    config.set_acoustid_key(KEY)
    restarted, _ = config_with(secrets)  # a new process sees the same credential store
    result = run_connection_test('acoustid', restarted, transport=lambda url, **k: {'status': 'ok', 'results': []},
                                 fingerprint=PROBE_FINGERPRINT)
    assert result['state'] == 'valid'


def test_an_unavailable_secure_store_is_reported_not_treated_as_a_missing_key():
    class BrokenStore(MemorySecretStore):
        def get(self, name):
            from meloshelf.infrastructure.secrets import SecretStoreError
            raise SecretStoreError('vault locked')

    config, _ = config_with(BrokenStore())
    result = run_connection_test('acoustid', config, transport=lambda url, **k: {'status': 'ok', 'results': []},
                                 fingerprint=PROBE_FINGERPRINT)
    assert result['state'] == 'store_unavailable' and result['ok'] is False


# ── 7. AcoustID reporting an invalid client key (documented: code 4) ──────────────

def test_invalid_client_key_is_reported_as_invalid_not_unreachable():
    client = AcoustIDClient(KEY, transport=lambda url, **k: {
        'status': 'error', 'error': {'code': 4, 'message': 'invalid API key'}}, retry=no_retry())
    with pytest.raises(APIError) as caught:
        client.lookup('AQAAAA==', 10)
    assert caught.value.kind == 'invalid_key'
    assert caught.value.provider_code == 4


def test_invalid_key_message_states_the_key_was_rejected():
    config, _ = config_with()
    config.set_acoustid_key(KEY)
    result = run_connection_test('acoustid', config, transport=lambda url, **k: {
        'status': 'error', 'error': {'code': 4, 'message': 'invalid API key'}},
        fingerprint=PROBE_FINGERPRINT)
    assert result['state'] == 'invalid_key' and 'rejected' in result['message'].lower()
    assert result['provider_code'] == 4 and KEY not in json.dumps(result)


# ── 8. DNS, TLS and connection failures are distinct ───────────────────────────

@pytest.mark.parametrize('reason, kind', [
    (socket.gaierror(11001, 'getaddrinfo failed'), 'dns'),
    (ssl.SSLError('CERTIFICATE_VERIFY_FAILED'), 'tls'),
    (socket.timeout('timed out'), 'timeout'),
    (ConnectionResetError('reset by peer'), 'connection'),
])
def test_network_failures_are_classified_by_cause(monkeypatch, reason, kind):
    def fake_urlopen(request, timeout):
        raise URLError(reason)
    monkeypatch.setattr(music_api, 'urlopen', fake_urlopen)
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://api.acoustid.org/v2/lookup', data=b'x')
    assert caught.value.kind == kind and caught.value.retryable is (kind in ('timeout', 'connection'))


# ── 9. HTTP 429, 4xx and 5xx ───────────────────────────────────────────────────

def test_http_429_is_a_retryable_rate_limit_with_retry_after(monkeypatch):
    error = HTTPError('u', 429, 'Too Many', {'Retry-After': '7'}, io.BytesIO(b''))
    monkeypatch.setattr(music_api, 'urlopen', lambda request, timeout: (_ for _ in ()).throw(error))
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://api.acoustid.org/v2/lookup', data=b'x')
    assert caught.value.kind == 'rate_limited' and caught.value.retryable and caught.value.retry_after == 7


def test_http_5xx_is_a_retryable_provider_error(monkeypatch):
    monkeypatch.setattr(music_api, 'urlopen', lambda request, timeout: (_ for _ in ()).throw(http_error(503)))
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://api.acoustid.org/v2/lookup', data=b'x')
    assert caught.value.kind == 'provider_error' and caught.value.retryable


def test_http_400_keeps_the_json_error_body_instead_of_discarding_it(monkeypatch):
    body = {'status': 'error', 'error': {'code': 3, 'message': 'invalid fingerprint'}}
    monkeypatch.setattr(music_api, 'urlopen', lambda request, timeout: (_ for _ in ()).throw(http_error(400, body)))
    with pytest.raises(APIError) as caught:
        music_api._request_json('https://api.acoustid.org/v2/lookup', data=b'x')
    assert caught.value.status == 400 and caught.value.payload == body and not caught.value.retryable


# ── 10. status: error in JSON with a request-level code ───────────────────────

def test_a_fingerprint_rejected_by_acoustid_is_not_reported_as_unreachable():
    config, _ = config_with()
    config.set_acoustid_key(KEY)
    result = run_connection_test('acoustid', config, transport=transport_raising(
        APIError('x', status=400, retryable=False)), fingerprint=PROBE_FINGERPRINT)
    assert result['state'] != 'unreachable'


def test_request_level_error_codes_are_reported_as_a_bad_request_not_a_key_problem():
    client = AcoustIDClient(KEY, transport=lambda url, **k: {
        'status': 'error', 'error': {'code': 2, 'message': 'missing required parameter "fingerprint"'}},
        retry=no_retry())
    with pytest.raises(APIError) as caught:
        client.lookup('', 10)
    assert caught.value.kind == 'request_rejected' and caught.value.provider_code == 2
    assert 'missing' in str(caught.value).lower()


def test_the_connection_test_never_sends_the_rejected_placeholder_fingerprint():
    # AQAAAA== is answered with code 3 "invalid fingerprint": the request is wrong, so it proves nothing about the key.
    sent = []

    def transport(url, **kwargs):
        sent.append(kwargs['data'])
        return {'status': 'ok', 'results': []}
    config, _ = config_with()
    config.set_acoustid_key(KEY)
    run_connection_test('acoustid', config, transport=transport, fingerprint=PROBE_FINGERPRINT)
    assert b'AQAAAA' not in sent[0]


# ── 11. valid API responses ─────────────────────────────────────────────────────

def test_a_valid_lookup_with_no_matches_is_a_usable_configuration():
    config, _ = config_with()
    config.set_acoustid_key(KEY)
    result = run_connection_test('acoustid', config, transport=lambda url, **k: {'status': 'ok', 'results': []},
                                 fingerprint=PROBE_FINGERPRINT)
    assert result['ok'] is True and result['state'] == 'valid' and result['provider_code'] is None


# ── 12. unreachable provider does not crash the application ─────────────────────

def test_an_unreachable_provider_is_reported_and_never_raises():
    config, _ = config_with()
    config.set_acoustid_key(KEY)

    def broken(url, **kwargs):
        raise APIError('Network error: gaierror', retryable=True, kind='dns')
    result = run_connection_test('acoustid', config, transport=broken, fingerprint=PROBE_FINGERPRINT)
    assert result['state'] == 'unreachable' and result['ok'] is False
    assert 'DNS' in result['message'] or 'name' in result['message'].lower()


def test_missing_key_and_missing_fingerprint_tool_have_distinct_states():
    config, _ = config_with()
    assert run_connection_test('acoustid', config, transport=lambda url, **k: {})['state'] == 'missing'
    config.set_acoustid_key(KEY)
    result = run_connection_test('acoustid', config, transport=lambda url, **k: {},
                                 fingerprint=lambda: (_ for _ in ()).throw(RuntimeError('no fpcalc')))
    assert result['state'] == 'fingerprint_unavailable'


def test_the_key_never_appears_in_a_logged_or_returned_message(caplog):
    config, _ = config_with()
    config.set_acoustid_key(KEY)
    caplog.set_level('DEBUG')
    result = run_connection_test('acoustid', config, transport=transport_raising(
        URLError(f'failed for client={KEY}')), fingerprint=PROBE_FINGERPRINT)
    assert KEY not in json.dumps(result) and KEY not in caplog.text


def test_settings_page_offers_retry_and_explains_a_failed_fetch_instead_of_showing_it_raw():
    from pathlib import Path
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'id="retryAcoustid"' in page and "err instanceof TypeError" in page
    assert 'Connected to MeloShelf process' in page


def test_identify_page_points_to_settings_and_refreshes_after_a_save():
    # Real case: after saving the MusicBrainz contact in Settings, Identify still said "not set" and told the
    # user to set an environment variable, because its configuration was loaded once and never refreshed.
    from pathlib import Path
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'to load release details' not in page or 'Settings' in page.split('to load release details')[0][-200:]
    assert 'Set MELOSHELF_MUSICBRAINZ_USER_AGENT to load' not in page
    assert 'Set MELOSHELF_ACOUSTID_KEY and restart' not in page
    save_block = page.split('async function saveSetup(')[1].split('\n  }')[0]
    assert 'renderConfig()' in save_block
