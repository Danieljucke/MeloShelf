"""Fanart.tv and TheAudioDB clients: identity, keys, rate limits, hosts and image verification, all against fake transports."""
import pytest

from meloshelf.infrastructure.art_sources import (AudioDbClient, FanartClient, SourceError, download_image, image_url_allowed)
from meloshelf.infrastructure.music_api import APIError, RateLimiter, RetryPolicy

ARTIST = 'cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493'
GROUP = '1dc4c347-a1db-32aa-b14f-bc9cc507b843'
JPEG = bytes.fromhex('ffd8ffe000104a46494600') + bytes([1]) * 64 + bytes.fromhex('ffd9')


def fast():
    return {'limiter': RateLimiter(0), 'retry': RetryPolicy(max_attempts=1)}


class Transport:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, *, headers=None, **_):
        self.calls.append((url, dict(headers or {})))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def fanart(transport, **kw):
    return FanartClient('PROJECT-KEY-123', transport=transport, **fast(), **kw)


def test_fanart_picks_the_most_liked_artist_thumb_and_keeps_source_and_terms():
    t = Transport({'name': 'Kygo', 'mbid_id': ARTIST, 'artistthumb': [{'url': 'https://assets.fanart.tv/a.jpg', 'likes': '2'}, {'url': 'https://assets.fanart.tv/b.jpg', 'likes': '9'}]})
    found = fanart(t).artist_thumb(ARTIST)
    assert found['url'] == 'https://assets.fanart.tv/b.jpg' and found['provider'] == 'fanart.tv'
    assert found['source_url'] == 'https://fanart.tv/artist/' + ARTIST and found['terms_url'] and 'non-commercial' in found['license']
    assert found['embeddable'] is False, 'third-party artwork is display-only'


def test_fanart_sends_the_project_key_and_an_optional_personal_key_in_headers_never_in_the_url():
    t = Transport({'mbid_id': ARTIST, 'artistthumb': []})
    fanart(t, client_key='PERSONAL-KEY-456').artist_thumb(ARTIST)
    url, headers = t.calls[0]
    assert headers['api-key'] == 'PROJECT-KEY-123' and headers['client-key'] == 'PERSONAL-KEY-456'
    assert 'PROJECT-KEY' not in url and 'PERSONAL-KEY' not in url
    t2 = Transport({'mbid_id': ARTIST})
    fanart(t2).artist_thumb(ARTIST)
    assert 'client-key' not in t2.calls[0][1]


def test_fanart_never_attaches_an_answer_that_names_another_artist():
    t = Transport({'name': 'Someone Else', 'mbid_id': '0383dadf-2a4e-4d10-a46a-e9e041da8eb3', 'artistthumb': [{'url': 'https://assets.fanart.tv/x.jpg', 'likes': '1'}]})
    assert fanart(t).artist_thumb(ARTIST) is None


def test_fanart_album_covers_come_from_the_release_group_entry_only():
    answer = {'albums': {GROUP: {'albumcover': [{'url': 'https://assets.fanart.tv/c.jpg', 'likes': '3'}], 'cdart': [{'url': 'https://assets.fanart.tv/cd.png'}]},
                         '99999999-1111-2222-3333-444444444444': {'albumcover': [{'url': 'https://assets.fanart.tv/other.jpg', 'likes': '50'}]}}}
    found = fanart(Transport(answer)).album_cover(GROUP)
    assert found['url'] == 'https://assets.fanart.tv/c.jpg' and found['source_url'] == 'https://fanart.tv/album/' + GROUP
    assert fanart(Transport({'albums': {}})).album_cover(GROUP) is None, 'no image: a normal answer, not an error'


def test_fanart_treats_404_as_no_artwork_and_429_as_a_rate_limit_to_back_off_from():
    assert fanart(Transport(APIError('nope', status=404, kind='http_rejected'))).artist_thumb(ARTIST) is None
    with pytest.raises(APIError) as caught:
        fanart(Transport(APIError('slow down', status=429, retry_after=30, kind='rate_limited', retryable=True))).artist_thumb(ARTIST)
    assert caught.value.kind == 'rate_limited' and caught.value.retry_after == 30


def test_fanart_needs_a_key_and_a_real_musicbrainz_id():
    with pytest.raises(SourceError) as caught:
        FanartClient('')
    assert caught.value.code == 'fanart_key_required'
    with pytest.raises(SourceError):
        fanart(Transport({})).artist_thumb('not-an-id')


def test_audiodb_refuses_the_shared_test_key_for_musicbrainz_lookups_and_needs_a_key():
    for key in ('123', '2'):
        with pytest.raises(SourceError) as caught:
            AudioDbClient(key)
        assert caught.value.code == 'audiodb_premium_key_required'
    with pytest.raises(SourceError) as caught:
        AudioDbClient('')
    assert caught.value.code == 'audiodb_key_required'


def audiodb(transport):
    return AudioDbClient('MY-PREMIUM-KEY', transport=transport, **fast())


def test_audiodb_looks_artists_and_albums_up_by_musicbrainz_id_and_checks_the_id_in_the_answer():
    t = Transport({'artists': [{'idArtist': '111', 'strMusicBrainzID': ARTIST, 'strArtistThumb': 'https://r2.theaudiodb.com/images/media/artist/thumb/x.jpg'}]})
    found = audiodb(t).artist_thumb(ARTIST)
    assert found['provider'] == 'theaudiodb' and found['url'].startswith('https://r2.theaudiodb.com/') and found['embeddable'] is False
    assert '/artist-mb.php?i=' + ARTIST in t.calls[0][0]
    wrong = Transport({'artists': [{'idArtist': '9', 'strMusicBrainzID': '0383dadf-2a4e-4d10-a46a-e9e041da8eb3', 'strArtistThumb': 'https://r2.theaudiodb.com/y.jpg'}]})
    assert audiodb(wrong).artist_thumb(ARTIST) is None
    album = Transport({'album': [{'idAlbum': '55', 'strMusicBrainzID': GROUP, 'strAlbumThumb': 'https://r2.theaudiodb.com/images/media/album/thumb/z.jpg'}]})
    assert audiodb(album).album_cover(GROUP)['source_url'] == 'https://www.theaudiodb.com/album/55'
    assert audiodb(Transport({'album': None})).album_cover(GROUP) is None


def test_audiodb_stays_inside_its_free_tier_request_rate_by_default():
    assert AudioDbClient('MY-PREMIUM-KEY', transport=Transport({})).limiter.interval >= 2.0


def test_only_the_providers_own_https_image_hosts_are_ever_fetched():
    assert image_url_allowed('fanart.tv', 'https://assets.fanart.tv/fanart/music/x.jpg')
    assert image_url_allowed('theaudiodb', 'https://r2.theaudiodb.com/images/x.jpg')
    for bad in ('http://assets.fanart.tv/x.jpg', 'https://assets.fanart.tv.evil.example/x.jpg', 'https://evil.example/assets.fanart.tv/x.jpg',
                'https://upload.wikimedia.org/x.jpg', 'file:///C:/x.jpg', ''):
        assert not image_url_allowed('fanart.tv', bad), bad
    assert not image_url_allowed('theaudiodb', 'https://assets.fanart.tv/x.jpg'), 'a provider cannot use another provider host'
    called = []
    with pytest.raises(SourceError):
        download_image('fanart.tv', 'https://evil.example/x.jpg', lambda url: called.append(url))
    assert called == [], 'no request is made to a host that is not allowed'


def test_downloaded_content_must_really_be_a_jpeg_or_png_within_the_size_limit():
    ok = download_image('fanart.tv', 'https://assets.fanart.tv/x.jpg', lambda u: ('image/png', JPEG))
    assert ok == ('image/jpeg', JPEG), 'the real type wins over the declared one'
    for payload in (b'<html>not an image</html>', JPEG + b'\0' * 3_000_000):
        with pytest.raises(SourceError) as caught:
            download_image('fanart.tv', 'https://assets.fanart.tv/x.jpg', lambda u, p=payload: ('image/jpeg', p))
        assert caught.value.code == 'image_invalid'
