"""Image downloads follow redirects only to the hosts they are allowed to reach.

A permitted first URL is not enough: urllib follows redirects by default, so a redirect from a Wikimedia or
Cover Art Archive address to any other host would have been fetched. These tests cover the redirect policy itself
(no network): an allowed hop is followed, a disallowed hop is refused before any request is sent."""
import urllib.request

import pytest

from meloshelf.infrastructure.music_api import APIError
from meloshelf.infrastructure.redirect_policy import HostBoundRedirects, ARCHIVE_IMAGE_HOST, WIKIMEDIA_IMAGE_HOST


def redirect(handler, target):
    request = urllib.request.Request('https://upload.wikimedia.org/a.jpg')
    return handler.redirect_request(request, None, 302, 'Found', {}, target)


@pytest.mark.parametrize('target', [
    'https://upload.wikimedia.org/wikipedia/commons/x/y.jpg',
    'https://thumb.wikimedia.org/commons/thumb/x/y.jpg/256px-y.jpg',
    'https://commons.wikimedia.org/w/index.php?title=File:Y.jpg',
])
def test_a_redirect_to_another_wikimedia_host_is_followed(target):
    handler = HostBoundRedirects(WIKIMEDIA_IMAGE_HOST)
    assert redirect(handler, target).full_url == target


@pytest.mark.parametrize('target', [
    'http://upload.wikimedia.org/a.jpg',                 # not https
    'https://evil.example/a.jpg',
    'https://upload.wikimedia.org.evil.example/a.jpg',   # the allowed name is only a prefix
    'https://169.254.169.254/latest/meta-data/',         # cloud metadata address
    'https://localhost:8080/admin',
    'file:///C:/Users/someone/secret.txt',
])
def test_a_redirect_elsewhere_is_refused_before_any_request(target):
    handler = HostBoundRedirects(WIKIMEDIA_IMAGE_HOST)
    with pytest.raises(APIError) as error:
        redirect(handler, target)
    assert error.value.retryable is False and 'not allowed' in str(error.value)


def test_the_archive_policy_follows_the_cover_art_archive_hop_to_the_internet_archive():
    handler = HostBoundRedirects(ARCHIVE_IMAGE_HOST)
    assert redirect(handler, 'https://archive.org/download/mbid/front-500.jpg').full_url.startswith('https://archive.org/')
    assert redirect(handler, 'https://ia800100.us.archive.org/12/items/x/front.jpg')
    with pytest.raises(APIError):
        redirect(handler, 'https://archive.org.attacker.example/x.jpg')


def test_the_fetchers_use_the_policy_rather_than_the_default_opener():
    from pathlib import Path
    wiki = (Path(__file__).resolve().parents[1] / 'src/meloshelf/infrastructure/wikimedia.py').read_text(encoding='utf-8')
    art = (Path(__file__).resolve().parents[1] / 'src/meloshelf/infrastructure/artwork.py').read_text(encoding='utf-8')
    assert 'open_restricted(request, WIKIMEDIA_IMAGE_HOST)' in wiki
    assert 'open_restricted(request, ARCHIVE_IMAGE_HOST)' in art
    assert 'urllib.request.urlopen(request' not in wiki and 'urllib.request.urlopen(request' not in art
