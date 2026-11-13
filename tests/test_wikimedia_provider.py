"""The Wikimedia photo provider: MusicBrainz url-rels -> Wikidata P18 -> Commons metadata and thumbnail.

Mocked transports only. These tests prove parsing and refusals; they are not live-service evidence.
"""
import pytest

from meloshelf.infrastructure.music_api import APIError
from meloshelf.infrastructure.wikimedia import WikimediaPhotoProvider

MB = 'cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493'


class NoWait:
    def __init__(self):
        self.waits = 0

    def wait(self, *a, **k):
        self.waits += 1

    def dispatch(self, action, *a, **k):
        self.waits += 1
        return action()


def chain(mb=None, wd=None, commons=None):
    urls = []

    def get(url):
        urls.append(url)
        if 'musicbrainz.org' in url:
            return mb if mb is not None else {'relations': [
                {'type': 'official homepage', 'url': {'resource': 'https://example.org'}},
                {'type': 'wikidata', 'url': {'resource': 'https://www.wikidata.org/wiki/Q18000'}}]}
        if 'wikidata.org' in url:
            return wd if wd is not None else {'entities': {'Q18000': {'claims': {'P18': [
                {'mainsnak': {'datavalue': {'value': 'Adele in 2016.jpg'}}}]}}}}
        return commons if commons is not None else {'query': {'pages': [{'imageinfo': [{
            'thumburl': 'https://upload.wikimedia.org/thumb/Adele_256px.jpg',
            'descriptionurl': 'https://commons.wikimedia.org/wiki/File:Adele_in_2016.jpg', 'mime': 'image/jpeg',
            'extmetadata': {'LicenseShortName': {'value': 'CC BY-SA 4.0'},
                            'Artist': {'value': '<a href="https://x">Some &amp; Photographer</a>'}}}]}]}}

    return get, urls


def provider(get, **kw):
    return WikimediaPhotoProvider('MeloShelf/0.2.0 ( https://example.invalid/project )', json_get=get,
                                  mb_limiter=NoWait(), wiki_limiter=NoWait(), **kw)


def test_the_chain_returns_the_file_licence_author_page_and_thumbnail():
    get, urls = chain()
    found = provider(get).find(MB)
    assert found['status'] == 'found'
    assert found['license'] == 'CC BY-SA 4.0'
    assert found['author'] == 'Some & Photographer', 'markup is removed from the attribution'
    assert found['source_url'] == 'https://commons.wikimedia.org/wiki/File:Adele_in_2016.jpg'
    assert found['thumb_url'].startswith('https://upload.wikimedia.org/')
    assert len(urls) == 3 and 'inc=url-rels' in urls[0]
    assert 'Q18000' in urls[1] and 'File%3AAdele+in+2016.jpg' in urls[2]


def test_an_artist_without_a_wikidata_relation_has_no_photo_and_nothing_is_searched_by_name():
    get, urls = chain(mb={'relations': [{'type': 'official homepage', 'url': {'resource': 'https://e.org'}}]})
    assert provider(get).find(MB)['status'] == 'none'
    assert len(urls) == 1


def test_a_wikidata_item_without_an_image_has_no_photo():
    get, _ = chain(wd={'entities': {'Q18000': {'claims': {}}}})
    assert provider(get).find(MB)['status'] == 'none'


def test_missing_commons_information_has_no_photo():
    get, _ = chain(commons={'query': {'pages': [{'missing': True}]}})
    assert provider(get).find(MB)['status'] == 'none'


def test_a_file_with_no_licence_metadata_reports_an_empty_licence_so_it_will_be_refused():
    get, _ = chain(commons={'query': {'pages': [{'imageinfo': [{'thumburl': 'https://upload.wikimedia.org/t.jpg',
                                                                'extmetadata': {}}]}]}})
    assert provider(get).find(MB)['license'] == ''


def test_a_provider_error_is_raised_not_swallowed():
    def failing(url):
        raise APIError('down', retryable=True)
    with pytest.raises(APIError):
        provider(failing).find(MB)


def test_only_wikimedia_hosts_are_downloaded():
    p = provider(chain()[0], bytes_get=lambda url: ('image/jpeg', b'x'))
    assert p.download('https://upload.wikimedia.org/a.jpg') == ('image/jpeg', b'x')
    with pytest.raises(APIError):
        p.download('https://evil.example/a.jpg')


def test_every_remote_call_waits_on_its_limiter():
    get, _ = chain()
    mb, wiki = NoWait(), NoWait()
    WikimediaPhotoProvider('MeloShelf/0.2.0 ( https://example.invalid/project )', json_get=get, mb_limiter=mb,
                           wiki_limiter=wiki).find(MB)
    assert mb.waits == 1 and wiki.waits == 2


def test_a_user_agent_with_a_contact_is_required():
    with pytest.raises(ValueError):
        WikimediaPhotoProvider('')


def test_the_thumbnail_host_that_commons_really_returns_is_allowed():
    """Found by the live check: Commons serves thumbnails from thumb.wikimedia.org."""
    real_shape = ('https://thumb.wikimedia.org/wikipedia/commons/thumb/7/7c/Adele_2016.jpg/330px-Adele_2016.jpg'
                  '?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=thumbnail')
    p = provider(chain()[0], bytes_get=lambda url: ('image/jpeg', b'x'))
    assert p.download(real_shape) == ('image/jpeg', b'x')
    with pytest.raises(APIError):
        p.download('https://thumb.wikimedia.org.evil.example/a.jpg')
