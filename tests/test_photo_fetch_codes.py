"""A refused or failed photograph fetch carries a registered code; the provider's raw reason stays in the log."""
import json
from types import SimpleNamespace

import pytest

from meloshelf.application import photo_api
from meloshelf.application.error_codes import MESSAGES

RAW = 'Network error (dns): gaierror while fetching C:/Users/someone/cache'


def server_returning(result):
    return SimpleNamespace(photos=SimpleNamespace(ensure=lambda mb_id: dict(result)))


@pytest.mark.parametrize('status,http,code', [
    ('unverified', 409, 'photo_unverified'),
    ('no_contact', 409, 'photo_contact_required'),
    ('failed', 502, 'photo_fetch_failed'),
])
def test_a_refused_fetch_names_its_code_and_hides_the_raw_reason(status, http, code):
    got, body = photo_api.fetch(server_returning({'status': status, 'reason': RAW}), {'mb_artist_id': 'x'})
    assert got == http
    assert body['status'] == status, 'the page keeps keying its own messages on status'
    assert body['code'] == code and code in MESSAGES
    assert 'gaierror' not in json.dumps(body) and 'someone' not in json.dumps(body)


def test_a_successful_fetch_is_unchanged():
    got, body = photo_api.fetch(server_returning({'status': 'available'}), {'mb_artist_id': 'x'})
    assert (got, body) == (200, {'status': 'available'})
