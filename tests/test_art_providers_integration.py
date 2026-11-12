"""Optional providers behind the primary ones: photographs and album covers, with provenance, safety and retry rules."""
import pytest

from meloshelf.application.album_art import AlbumArtService
from meloshelf.application.artist_photos import ArtistPhotoService
from meloshelf.domain.artist_identity import ArtistCandidate, decide_link
from meloshelf.domain.organize import Decision
from meloshelf.infrastructure.art_sources import SourceError
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.music_api import APIError
from test_album_art import FakeCache, FakeCovers, Searcher, hit
from test_artist_photos import FakeProvider

MB = 'cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493'
GROUP = '11111111-2222-3333-4444-555555555555'
JPEG = bytes.fromhex('ffd8ffe000104a46494600') + bytes([7]) * 80 + bytes.fromhex('ffd9')
OTHER_JPEG = bytes.fromhex('ffd8ffe000104a46494600') + bytes([9]) * 80 + bytes.fromhex('ffd9')


class Extra:
    """A stand-in for a Fanart.tv / TheAudioDB client."""

    def __init__(self, name, *, thumb=True, cover=True, error=None, data=JPEG, url='https://assets.fanart.tv/x.jpg'):
        self.name, self.label = name, name
        self.thumb, self.cover, self.error, self.data, self.url = thumb, cover, error, data, url
        self.artist_calls, self.album_calls = [], []

    def _found(self):
        return {'provider': self.name, 'url': self.url, 'source_url': f'https://{self.name}.example/page', 'terms_url': f'https://{self.name}.example/terms',
                'license': f'{self.name} terms: personal use', 'author': f'{self.name} community', 'embeddable': False}

    def artist_thumb(self, mbid):
        self.artist_calls.append(mbid)
        if self.error:
            raise self.error
        return self._found() if self.thumb else None

    def album_cover(self, group):
        self.album_calls.append(group)
        if self.error:
            raise self.error
        return self._found() if self.cover else None

    def download(self, url):
        if not url.startswith('https://assets.fanart.tv/') and not url.startswith('https://r2.theaudiodb.com/'):
            raise SourceError('image_host_not_allowed')
        return 'image/jpeg', self.data


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    ArtistLinks(database).record('Kygo', decide_link('Kygo', [ArtistCandidate(MB, 'Kygo', 100)]), looked_up_at=1.0)
    return database


def photos(db, tmp_path, wiki, extras):
    return ArtistPhotoService(db, tmp_path / 'photos', wiki, extras=lambda: extras)


NO_WIKI = {'status': 'none', 'reason': 'no image on Wikidata'}


def test_providers_that_are_off_are_never_asked(db, tmp_path):
    wiki = FakeProvider(found=NO_WIKI)
    assert photos(db, tmp_path, wiki, []).ensure(MB)['status'] == 'none'


def test_a_fallback_supplies_the_photograph_when_commons_has_none_and_keeps_its_provenance(db, tmp_path):
    fanart = Extra('fanart.tv')
    service = photos(db, tmp_path, FakeProvider(found=NO_WIKI), [fanart])
    assert service.ensure(MB)['status'] == 'available'
    info = service.info(MB)
    assert info['provenance'] == 'fanart.tv' and info['author'] == 'fanart.tv community' and 'personal use' in info['license']
    assert info['source_url'] == 'https://fanart.tv.example/page'
    assert service.image(MB) == ('image/jpeg', JPEG)


def test_commons_stays_primary_and_the_fallbacks_are_not_consulted_when_it_has_a_licensed_photo(db, tmp_path):
    fanart = Extra('fanart.tv')
    service = photos(db, tmp_path, FakeProvider(), [fanart])
    assert service.ensure(MB)['status'] == 'available' and service.info(MB)['provenance'] == 'wikimedia'
    assert fanart.artist_calls == []


def test_a_photo_with_a_licence_commons_refuses_may_still_come_from_a_fallback(db, tmp_path):
    wiki = FakeProvider(found={'status': 'found', 'file': 'x.jpg', 'license': 'All rights reserved', 'author': 'x', 'source_url': 'https://commons.wikimedia.org/wiki/File:x.jpg', 'thumb_url': 'u'})
    assert photos(db, tmp_path, wiki, [Extra('fanart.tv')]).ensure(MB)['status'] == 'available'


def test_the_first_provider_without_a_usable_answer_is_skipped_for_the_next(db, tmp_path):
    bad_host = Extra('fanart.tv', url='https://evil.example/x.jpg')
    audiodb = Extra('theaudiodb', url='https://r2.theaudiodb.com/x.jpg', data=OTHER_JPEG)
    service = photos(db, tmp_path, FakeProvider(found=NO_WIKI), [bad_host, audiodb])
    assert service.ensure(MB)['status'] == 'available' and service.info(MB)['provenance'] == 'theaudiodb'
    assert service.image(MB) == ('image/jpeg', OTHER_JPEG)


def test_content_that_is_not_an_image_is_never_stored(db, tmp_path):
    service = photos(db, tmp_path, FakeProvider(found=NO_WIKI), [Extra('fanart.tv', data=b'<html>nope</html>')])
    assert service.ensure(MB)['status'] == 'none' and service.image(MB) is None


def test_a_rate_limit_or_outage_is_not_stored_so_a_later_attempt_can_succeed(db, tmp_path):
    limited = Extra('fanart.tv', error=APIError('slow', status=429, kind='rate_limited', retryable=True))
    assert photos(db, tmp_path, FakeProvider(found=NO_WIKI), [limited]).ensure(MB)['status'] == 'failed'
    assert photos(db, tmp_path, FakeProvider(found=NO_WIKI), [Extra('fanart.tv')]).ensure(MB)['status'] == 'available'


def test_a_persons_own_photo_always_wins_over_every_provider(db, tmp_path):
    service = photos(db, tmp_path, FakeProvider(found=NO_WIKI), [Extra('fanart.tv')])
    service.ensure(MB)
    service.set_manual(MB, 'image/jpeg', OTHER_JPEG, note='mine')
    assert service.image(MB) == ('image/jpeg', OTHER_JPEG) and service.info(MB)['provenance'] == 'user'


def test_a_previously_missing_photo_is_asked_again_when_a_new_provider_is_turned_on_but_not_otherwise(db, tmp_path):
    wiki = FakeProvider(found=NO_WIKI)
    assert photos(db, tmp_path, wiki, []).ensure(MB)['status'] == 'none'
    assert photos(db, tmp_path, wiki, []).ensure(MB)['status'] == 'none' and len(wiki.find_calls) == 1, 'unchanged sources: not retried'
    fanart = Extra('fanart.tv')
    assert photos(db, tmp_path, wiki, [fanart]).ensure(MB)['status'] == 'available', 'a new provider gets its chance'
    assert fanart.artist_calls == [MB]


def test_a_negative_answer_given_with_the_same_providers_is_not_asked_again(db, tmp_path):
    fanart = Extra('fanart.tv', thumb=False)
    service = photos(db, tmp_path, FakeProvider(found=NO_WIKI), [fanart])
    assert service.ensure(MB)['status'] == 'none' and service.ensure(MB)['status'] == 'none'
    assert fanart.artist_calls == [MB]


def test_only_verified_artists_get_a_photograph_from_any_provider(tmp_path):
    database = Database(tmp_path / 'other.sqlite3')
    database.initialize()
    fanart = Extra('fanart.tv')
    assert photos(database, tmp_path, FakeProvider(found=NO_WIKI), [fanart]).ensure(MB)['status'] == 'unverified'
    assert fanart.artist_calls == []


# ── album covers ──────────────────────────────────────────────────────────────

def albums(tmp_path, *, cache=None, hits=None, extras=(), covers=None):
    database = Database(tmp_path / 'albums.sqlite3')
    database.initialize()
    store = MatchStore(database)
    return AlbumArtService(store, covers or FakeCovers(), cache or FakeCache({}), tmp_path / 'art',
                           search=Searcher(hits if hits is not None else [hit()]), fallbacks=lambda: list(extras))


def test_a_confirmed_release_group_without_a_cover_art_archive_image_gets_a_provider_cover_that_is_display_only(tmp_path):
    fanart = Extra('fanart.tv')
    service = albums(tmp_path, extras=[fanart])
    found = service.lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'confirmed' and found['source'] == 'fanart.tv' and found['group_id'] == GROUP and found['embeddable'] is False
    assert service.cover_for(Decision('organize', artist='Lecrae', album='Anomaly', title='Fear'), {'paths': []}) == '', 'never silently embedded'
    info = service.describe('Anomaly', 'Lecrae')
    assert info['provenance'] == 'fanart.tv' and info['source_url'] == 'https://fanart.tv.example/page' and info['terms_url']


def test_cover_art_archive_stays_primary_for_the_same_release_group(tmp_path):
    fanart = Extra('fanart.tv')
    service = albums(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', OTHER_JPEG)}), extras=[fanart])
    found = service.lookup('Anomaly', 'Lecrae', [])
    assert found['source'] == 'musicbrainz_release_group' and found['embeddable'] is True and fanart.album_calls == []


def test_an_uncertain_album_identity_only_ever_yields_a_suggested_provider_cover(tmp_path):
    service = albums(tmp_path, hits=[hit(), hit(group='99999999-2222-3333-4444-555555555555', primary='EP')], extras=[Extra('fanart.tv')])
    assert service.lookup('Anomaly', 'Lecrae', [])['state'] == 'plausible'


def test_no_provider_image_leaves_the_cover_missing_and_never_borrows_another_albums(tmp_path):
    service = albums(tmp_path, extras=[Extra('fanart.tv', cover=False), Extra('theaudiodb', cover=False)])
    assert service.lookup('Anomaly', 'Lecrae', [])['state'] == 'missing'
    (tmp_path / 'x').mkdir()
    assert albums(tmp_path / 'x', hits=[], extras=[Extra('fanart.tv')]).lookup('Nothing', 'Nobody', [])['state'] == 'missing'


LECRAE = Decision('organize', artist='Lecrae', album='Anomaly', title='Fear')


def test_accepting_a_provider_cover_is_a_choice_by_the_person_but_keeps_the_source_and_stays_display_only(tmp_path):
    covers = FakeCovers()
    service = albums(tmp_path, extras=[Extra('fanart.tv')], covers=covers)
    service.lookup('Anomaly', 'Lecrae', [])
    assert service.accept('Anomaly', 'Lecrae') is True and covers.accepted[0][:3] == ('Anomaly', 'Lecrae', 'image/jpeg')
    found = service.lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'confirmed' and found['chosen'] is True and found['embeddable'] is False
    info = service.describe('Anomaly', 'Lecrae')
    assert info['chosen'] is True and info['embeddable'] is False
    assert (info['provenance'], info['source_url'], info['terms_url'], info['license'], info['author']) == (
        'fanart.tv', 'https://fanart.tv.example/page', 'https://fanart.tv.example/terms', 'fanart.tv terms: personal use', 'fanart.tv community')
    assert service.cover_for(LECRAE, {'paths': []}) == '', 'a choice is not proof the provider allows reuse: never embedded'


def test_the_attribution_of_an_accepted_cover_survives_a_restart_and_another_pass(tmp_path):
    covers = FakeCovers()
    service = albums(tmp_path, extras=[Extra('fanart.tv')], covers=covers)
    service.lookup('Anomaly', 'Lecrae', [])
    service.accept('Anomaly', 'Lecrae')
    again = AlbumArtService(service.store, covers, FakeCache({}), tmp_path / 'art', search=Searcher([hit()]), fallbacks=lambda: [Extra('fanart.tv')])
    again.enrich([('Anomaly', 'Lecrae', '2014')])
    info = again.describe('Anomaly', 'Lecrae')
    assert info['chosen'] is True and info['provenance'] == 'fanart.tv' and info['terms_url'] and again.cover_for(LECRAE, {'paths': []}) == ''
    assert again.accept('Anomaly', 'Lecrae') is True and len(covers.accepted) == 1, 'accepting twice records nothing new'


def test_a_different_image_the_person_chooses_later_carries_no_provider_attribution_and_is_their_own_file(tmp_path):
    covers = FakeCovers()
    service = albums(tmp_path, extras=[Extra('fanart.tv')], covers=covers)
    service.lookup('Anomaly', 'Lecrae', [])
    service.accept('Anomaly', 'Lecrae')
    covers.found = ('image/jpeg', OTHER_JPEG)                       # they upload their own image afterwards
    found = service.lookup('Anomaly', 'Lecrae', [])
    assert found['embeddable'] is True and not found.get('chosen') and not service.describe('Anomaly', 'Lecrae').get('chosen')
    assert service.cover_for(LECRAE, {'paths': []})


def test_a_cover_art_archive_suggestion_accepted_by_the_person_is_still_embedded(tmp_path):
    covers = FakeCovers()
    service = albums(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', OTHER_JPEG)}), hits=[hit(), hit(group='99999999-2222-3333-4444-555555555555', primary='EP')], covers=covers)
    assert service.lookup('Anomaly', 'Lecrae', [])['state'] == 'plausible' and service.accept('Anomaly', 'Lecrae')
    assert service.lookup('Anomaly', 'Lecrae', [])['embeddable'] is True and service.cover_for(LECRAE, {'paths': []})


def test_a_provider_that_failed_is_not_remembered_as_missing(tmp_path):
    flaky = Extra('fanart.tv', error=APIError('slow', status=429, kind='rate_limited', retryable=True))
    service = albums(tmp_path, extras=[flaky])
    assert service.lookup('Anomaly', 'Lecrae', [])['state'] == 'missing'
    good = Extra('fanart.tv')
    again = AlbumArtService(service.store, FakeCovers(), FakeCache({}), tmp_path / 'art', search=Searcher([hit()]), fallbacks=lambda: [good])
    assert again.lookup('Anomaly', 'Lecrae', [])['source'] == 'fanart.tv'


def test_a_missing_provider_cover_is_asked_again_when_a_new_provider_is_enabled(tmp_path):
    first = Extra('fanart.tv', cover=False)
    service = albums(tmp_path, extras=[first])
    assert service.lookup('Anomaly', 'Lecrae', [])['state'] == 'missing' and first.album_calls == [GROUP]
    service.lookup('Anomaly', 'Lecrae', [])
    assert first.album_calls == [GROUP], 'same providers: not asked again'
    audiodb = Extra('theaudiodb', url='https://r2.theaudiodb.com/y.jpg')
    again = AlbumArtService(service.store, FakeCovers(), FakeCache({}), tmp_path / 'art', search=Searcher([hit()]), fallbacks=lambda: [first, audiodb])
    assert again.lookup('Anomaly', 'Lecrae', [])['source'] == 'theaudiodb'
