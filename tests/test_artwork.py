"""Album artwork: verified release IDs only, persistent bounded cache, safe failures, served locally."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.artwork import ArtworkCache, ArtworkError
from meloshelf.infrastructure.database import Database

RELEASE = '11111111-2222-4333-8444-555555555555'
OTHER = '66666666-7777-4888-8999-000000000000'
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 2000
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 2000


class FakeCAA:
    """Stands in for coverartarchive.org; records every request."""
    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def __call__(self, url):
        self.requests.append(url)
        answer = self.answers.get(url, ('missing', '', b''))
        if isinstance(answer, Exception):
            raise answer
        return answer


def make_cache(tmp_path, fetch, **options):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    return ArtworkCache(database, tmp_path / 'artwork', fetch=fetch, **options)


def url_for(mbid):
    return f'https://coverartarchive.org/release/{mbid}/front-500'


def test_only_well_formed_release_ids_are_requested(tmp_path):
    caa = FakeCAA({})
    cache = make_cache(tmp_path, caa)
    with pytest.raises(ArtworkError):
        cache.cover('../../etc/passwd')
    assert caa.requests == []


def test_a_cover_is_fetched_once_and_then_served_from_the_cache(tmp_path):
    caa = FakeCAA({url_for(RELEASE): ('image', 'image/jpeg', JPEG)})
    cache = make_cache(tmp_path, caa)
    assert cache.cover(RELEASE) == ('image/jpeg', JPEG)
    assert cache.cover(RELEASE) == ('image/jpeg', JPEG)
    assert len(caa.requests) == 1


def test_the_cache_survives_a_restart(tmp_path):
    caa = FakeCAA({url_for(RELEASE): ('image', 'image/png', PNG)})
    make_cache(tmp_path, caa).cover(RELEASE)
    fresh = make_cache(tmp_path, FakeCAA({}))
    assert fresh.cover(RELEASE) == ('image/png', PNG)


def test_a_release_without_a_cover_is_remembered_and_not_requested_again(tmp_path):
    caa = FakeCAA({})
    cache = make_cache(tmp_path, caa)
    assert cache.cover(OTHER) is None
    assert cache.cover(OTHER) is None
    assert len(caa.requests) == 1


def test_only_real_image_data_is_accepted(tmp_path):
    caa = FakeCAA({url_for(RELEASE): ('html', 'text/html', b'<html>not a picture</html>')})
    cache = make_cache(tmp_path, caa)
    assert cache.cover(RELEASE) is None
    assert not (tmp_path / 'artwork').exists() or not any((tmp_path / 'artwork').iterdir())


def test_an_image_whose_bytes_do_not_match_its_type_is_refused(tmp_path):
    caa = FakeCAA({url_for(RELEASE): ('image', 'image/jpeg', b'GIF89a' + b'\x00' * 100)})
    assert make_cache(tmp_path, caa).cover(RELEASE) is None


def test_oversized_images_are_refused(tmp_path):
    caa = FakeCAA({url_for(RELEASE): ('image', 'image/jpeg', JPEG + b'\x00' * 5000)})
    assert make_cache(tmp_path, caa, max_image_bytes=4000).cover(RELEASE) is None


def test_network_failures_are_not_remembered_as_missing_covers(tmp_path):
    caa = FakeCAA({url_for(RELEASE): ConnectionError('offline')})
    cache = make_cache(tmp_path, caa)
    assert cache.cover(RELEASE) is None
    caa.answers[url_for(RELEASE)] = ('image', 'image/jpeg', JPEG)
    assert cache.cover(RELEASE) == ('image/jpeg', JPEG), 'a later online request must succeed'


def test_the_cache_stays_within_its_size_limit_by_removing_the_oldest(tmp_path):
    big = JPEG + b'\x00' * 3000
    caa = FakeCAA({url_for(RELEASE): ('image', 'image/jpeg', big),
                   url_for(OTHER): ('image', 'image/jpeg', big)})
    cache = make_cache(tmp_path, caa, max_total_bytes=len(big) + 100)
    cache.cover(RELEASE)
    cache.cover(OTHER)
    assert cache.total_bytes() <= len(big) + 100
    assert cache.cover(OTHER) is not None


def test_the_web_service_serves_cached_covers_locally_and_refuses_bad_ids(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    caa = FakeCAA({url_for(RELEASE): ('image', 'image/jpeg', JPEG)})
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, artwork_fetch=caa)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{running.server_address[1]}'
    try:
        with urllib.request.urlopen(f'{base}/api/artwork?release={RELEASE}', timeout=10) as response:
            assert response.status == 200
            assert response.headers['Content-Type'] == 'image/jpeg'
            assert response.read() == JPEG
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f'{base}/api/artwork?release=not-a-release', timeout=10)
        assert caught.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f'{base}/api/artwork?release={OTHER}', timeout=10)
        assert caught.value.code == 404
    finally:
        running.shutdown()
        running.server_close()
