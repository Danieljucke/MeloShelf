"""Album artwork is looked up for the ALBUM, separately from recognising a song, and is labelled honestly."""
import pytest
from mutagen.id3 import APIC, ID3

from audio_fixtures import write_mp3
from meloshelf.application.album_art import AlbumArtService
from meloshelf.domain.organize import Decision
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.tag_writer import read_cover

JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x01' * 64 + b'\xff\xd9'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x02' * 64
GROUP = '11111111-2222-3333-4444-555555555555'


class FakeCovers:
    """The existing manual/verified cover service, reduced to what the album-art lookup relies on."""

    def __init__(self, found=None):
        self.found, self.accepted = found, []

    def cover(self, album, artist):
        return self.found

    def set_manual(self, album, artist, content_type, data, note=''):
        self.accepted.append((album, artist, content_type, data, note))
        self.found = (content_type, data)
        return 1


class FakeCache:
    def __init__(self, images=None):
        self.images, self.requests = images or {}, []

    def cover(self, release_id, *, group=False):
        self.requests.append((release_id, group))
        return self.images.get(release_id)


class Searcher:
    def __init__(self, hits):
        self.hits, self.calls = hits, 0

    def __call__(self, album, artist):
        self.calls += 1
        if isinstance(self.hits, Exception):
            raise self.hits
        return self.hits


def service(tmp_path, *, covers=None, cache=None, search=None, **kw):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    return AlbumArtService(MatchStore(db), covers or FakeCovers(), cache or FakeCache(), tmp_path / 'art', search=search, **kw)


def with_art(tmp_path, name, data=JPEG, mime='image/jpeg'):
    path = write_mp3(tmp_path / name, seed=len(name), tags={'title': 'T', 'artist': 'A', 'album': 'B'})
    tags = ID3(path)
    tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=data))
    tags.save(path)
    return str(path)


def test_art_embedded_in_the_albums_own_files_is_confirmed(tmp_path):
    files = [str(write_mp3(tmp_path / 'plain.mp3', seed=1)), with_art(tmp_path, 'withart.mp3')]
    found = service(tmp_path).lookup('B', 'A', files)
    assert found['state'] == 'confirmed' and found['source'] == 'embedded' and found['data'] == JPEG
    assert found['content_type'] == 'image/jpeg'


def test_a_verified_release_cover_is_confirmed(tmp_path):
    found = service(tmp_path, covers=FakeCovers(('image/png', PNG))).lookup('B', 'A', [])
    assert found['state'] == 'confirmed' and found['source'] == 'coverartarchive' and found['data'] == PNG


def test_a_catalogue_match_is_only_plausible_and_never_written_to_a_copy(tmp_path):
    hits = [{'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 100}]
    svc = service(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', JPEG)}), search=Searcher(hits))
    found = svc.lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'plausible' and found['source'] == 'musicbrainz_search' and found['group_id'] == GROUP
    assert svc.cover_for(Decision('organize', artist='Lecrae', album='Anomaly', title='Fear'), {'paths': []}) == '', \
        'a cover that is not confirmed is never embedded'


def test_a_hit_for_a_different_album_is_not_even_plausible(tmp_path):
    hits = [{'group_id': GROUP, 'title': 'Anomaly (Deluxe Remixes)', 'artist': 'Lecrae', 'score': 100},
            {'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Someone Else', 'score': 100},
            {'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 40}]
    svc = service(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', JPEG)}), search=Searcher(hits))
    assert svc.lookup('Anomaly', 'Lecrae', [])['state'] == 'missing'


def test_an_unavailable_catalogue_leaves_the_cover_missing_without_failing(tmp_path):
    found = service(tmp_path, search=Searcher(OSError('offline'))).lookup('B', 'A', [])
    assert found['state'] == 'missing'
    assert service(tmp_path).lookup('B', 'A', [])['state'] == 'missing', 'no searcher configured'


def test_the_catalogue_is_asked_once_per_album(tmp_path):
    searcher = Searcher([{'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 100}])
    svc = service(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', JPEG)}), search=searcher)
    svc.lookup('Anomaly', 'Lecrae', [])
    svc.lookup('Anomaly', 'Lecrae', [])
    assert searcher.calls == 1


def test_accepting_a_suggestion_makes_it_the_persons_confirmed_choice(tmp_path):
    covers = FakeCovers()
    svc = service(tmp_path, covers=covers, cache=FakeCache({GROUP: ('image/jpeg', JPEG)}),
                  search=Searcher([{'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 100}]))
    assert svc.lookup('Anomaly', 'Lecrae', [])['state'] == 'plausible'
    assert svc.accept('Anomaly', 'Lecrae') is True
    assert covers.accepted and covers.accepted[0][:3] == ('Anomaly', 'Lecrae', 'image/jpeg')
    assert svc.lookup('Anomaly', 'Lecrae', [])['state'] == 'confirmed'


def test_a_confirmed_cover_is_handed_to_the_copy_as_a_file(tmp_path):
    svc = service(tmp_path, covers=FakeCovers(('image/jpeg', JPEG)))
    path = svc.cover_for(Decision('organize', artist='A', album='B', title='T'), {'paths': []})
    assert path and open(path, 'rb').read() == JPEG


def test_there_is_no_cover_lookup_for_a_track_without_an_album(tmp_path):
    svc = service(tmp_path, covers=FakeCovers(('image/jpeg', JPEG)))
    assert svc.cover_for(Decision('organize', artist='A', album='', title='T'), {'paths': []}) == ''


def test_embedded_art_can_be_read_back_from_mp3(tmp_path):
    assert read_cover(with_art(tmp_path, 'x.mp3')) == ('image/jpeg', JPEG)
    assert read_cover(str(write_mp3(tmp_path / 'y.mp3', seed=2))) is None


def test_the_release_group_search_reads_musicbrainz_answers_and_sends_a_phrase_query():
    from urllib.parse import parse_qs, urlparse
    from meloshelf.infrastructure.music_api import MusicBrainzClient, RateLimiter, RetryPolicy
    seen = []

    def transport(url, **options):
        seen.append(url)
        return {'release-groups': [
            {'id': GROUP.upper(), 'title': 'Anomaly', 'score': 100, 'primary-type': 'Album',
             'artist-credit': [{'name': 'Lecrae', 'joinphrase': ''}]},
            {'id': 'not-an-id', 'title': 'Bad', 'score': 100}]}

    client = MusicBrainzClient('MeloShelf/test (https://example.invalid/contact)', transport=transport,
                               limiter=RateLimiter(0), retry=RetryPolicy(max_attempts=1))
    hits = client.search_release_groups('Anomaly "Deluxe"', 'Lecrae')
    assert hits == [{'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 100, 'primary_type': 'Album',
                     'secondary_types': [], 'first_release_date': ''}]
    query = parse_qs(urlparse(seen[0]).query)['query'][0]
    assert query.startswith('releasegroup:"Anomaly \\"Deluxe\\""') and 'artist:"Lecrae"' in query


def test_a_listing_never_asks_the_catalogue_but_reuses_what_an_earlier_lookup_found(tmp_path):
    searcher = Searcher([{'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 100}])
    svc = service(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', JPEG)}), search=searcher)
    assert svc.lookup('Anomaly', 'Lecrae', [], network=False)['state'] == 'missing' and searcher.calls == 0
    svc.lookup('Anomaly', 'Lecrae', [])
    assert svc.lookup('Anomaly', 'Lecrae', [], network=False)['state'] == 'plausible' and searcher.calls == 1


def test_embedded_art_is_only_trusted_when_the_bytes_really_are_an_image(tmp_path):
    page = b'<script>alert(1)</script>'
    assert read_cover(with_art(tmp_path, 'evil.mp3', data=page, mime='text/html')) is None, 'HTML is never an image'
    declared_png = with_art(tmp_path, 'lies.mp3', data=JPEG, mime='image/png')
    assert read_cover(declared_png) == ('image/jpeg', JPEG), 'the real type wins over the declared one'


def hit(title='Anomaly', artist='Lecrae', *, score=100, primary='Album', secondary=(), date='2014-09-09', group=GROUP):
    return {'group_id': group, 'title': title, 'artist': artist, 'score': score, 'primary_type': primary,
            'secondary_types': list(secondary), 'first_release_date': date}


def catalogue(tmp_path, hits, **kw):
    return service(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', JPEG), GROUP2: ('image/png', PNG)}), search=Searcher(hits), **kw)


GROUP2 = '99999999-2222-3333-4444-555555555555'


def test_a_unique_studio_album_match_is_confirmed_and_may_be_embedded(tmp_path):
    svc = catalogue(tmp_path, [hit()])
    found = svc.lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'confirmed' and found['source'] == 'musicbrainz_release_group' and found['group_id'] == GROUP
    assert svc.cover_for(Decision('organize', artist='Lecrae', album='Anomaly', title='Fear'), {'paths': []}), 'confirmed: embedded'


@pytest.mark.parametrize('secondary', [['Live'], ['Compilation'], ['Remix'], ['DJ-mix'], ['Mixtape/Street']])
def test_a_live_compilation_or_remix_release_is_never_used_as_the_albums_cover(tmp_path, secondary):
    found = catalogue(tmp_path, [hit(secondary=secondary)]).lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'missing'


def test_a_deluxe_edition_with_another_title_is_not_the_same_album(tmp_path):
    found = catalogue(tmp_path, [hit(title='Anomaly (Deluxe Edition)')]).lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'missing'


def test_two_equally_good_groups_leave_the_edition_uncertain_so_the_cover_is_only_suggested(tmp_path):
    svc = catalogue(tmp_path, [hit(), hit(group=GROUP2, primary='EP', date='2015-01-01')])
    found = svc.lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'plausible' and found['group_id'] == GROUP
    assert svc.cover_for(Decision('organize', artist='Lecrae', album='Anomaly', title='Fear'), {'paths': []}) == ''


def test_a_release_year_that_disagrees_with_the_tags_rules_the_group_out(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    assert catalogue(tmp_path / 'a', [hit(date='2020-05-01')]).lookup('Anomaly', 'Lecrae', [], year='2014')['state'] == 'missing'
    assert catalogue(tmp_path / 'b', [hit(date='2014-09-09')]).lookup('Anomaly', 'Lecrae', [], year='2014')['state'] == 'confirmed'


NOW = 1_800_000_000          # mid-January 2027; the service's clock is injected so "the future" is deterministic


def judged(tmp_path, hits, year=''):
    return catalogue(tmp_path, hits, clock=lambda: NOW).lookup('Anomaly', 'Lecrae', [], year=year)


@pytest.mark.parametrize('date, year', [('2099-01-01', ''), ('2099-01-01', '2099'), ('2099-01-01', '2014')])
def test_a_release_dated_in_the_future_is_never_confirmed(tmp_path, date, year):
    assert judged(tmp_path, [hit(date=date)], year)['state'] != 'confirmed'


@pytest.mark.parametrize('year', ['2099', '0001', '1066', '99', 'abcd'])
def test_an_impossible_year_in_the_tags_never_confirms_a_match_even_when_the_catalogue_agrees(tmp_path, year):
    for date in (year, '2014-09-09', ''):
        (tmp_path / (date or 'blank') / year).mkdir(parents=True)
        assert judged(tmp_path / (date or 'blank') / year, [hit(date=date or '')], year)['state'] != 'confirmed', (year, date)


def test_a_tag_year_that_contradicts_the_catalogue_rules_the_release_out(tmp_path):
    assert judged(tmp_path, [hit(date='2014-09-09')], '2008')['state'] == 'missing'


def test_each_edition_is_judged_by_its_own_year_and_only_the_one_that_agrees_is_confirmed(tmp_path):
    other = '99999999-2222-3333-4444-555555555555'
    both = [hit(date='2008-09-30'), hit(date='2014-09-09', group=other)]
    assert judged(tmp_path, both, '2014')['group_id'] == other
    (tmp_path / 'u').mkdir()
    unknown = judged(tmp_path / 'u', both, '')
    assert unknown['state'] == 'plausible', 'two editions and no year in the tags: only suggested'


def test_two_editions_with_a_future_one_never_make_the_future_one_the_answer(tmp_path):
    other = '99999999-2222-3333-4444-555555555555'
    found = judged(tmp_path, [hit(date='2099-01-01', group=other), hit(date='2008-09-30')], '')
    assert found['state'] == 'plausible' and found['group_id'] == GROUP, 'the future-dated edition is not preferred'


@pytest.mark.parametrize('date', ['', '0000', '2014', 'unknown'])
def test_a_missing_or_partial_release_date_neither_rules_out_nor_contradicts(tmp_path, date):
    found = judged(tmp_path, [hit(date=date)], '2014')
    assert found['state'] in ('confirmed', 'missing')
    if date in ('', '2014', 'unknown'):
        assert found['state'] == 'confirmed', 'no usable date: judged on title and artist alone, as before'


def test_a_hit_without_a_release_type_is_only_suggested(tmp_path):
    assert catalogue(tmp_path, [hit(primary='')]).lookup('Anomaly', 'Lecrae', [])['state'] == 'plausible'


def test_a_low_score_or_other_artist_is_not_a_match(tmp_path):
    assert catalogue(tmp_path, [hit(score=60), hit(artist='Someone Else')]).lookup('Anomaly', 'Lecrae', [])['state'] == 'missing'


def test_the_decision_and_attribution_survive_a_restart_and_the_catalogue_is_not_asked_again(tmp_path):
    first = catalogue(tmp_path, [hit()])
    first.lookup('Anomaly', 'Lecrae', [])
    searcher = Searcher([hit()])
    reopened = AlbumArtService(MatchStore(Database(tmp_path / 'lib.sqlite3')), FakeCovers(), FakeCache({GROUP: ('image/jpeg', JPEG)}),
                               tmp_path / 'art', search=searcher)
    found = reopened.lookup('Anomaly', 'Lecrae', [])
    assert found['state'] == 'confirmed' and searcher.calls == 0
    info = reopened.describe('Anomaly', 'Lecrae')
    assert info['state'] == 'confirmed' and info['source_url'] == 'https://musicbrainz.org/release-group/' + GROUP
    assert info['provenance'] == 'coverartarchive'


def test_an_unavailable_catalogue_is_missing_and_is_asked_again_later_not_remembered_as_a_failure(tmp_path):
    svc = service(tmp_path, search=Searcher(OSError('offline')))
    assert svc.lookup('Anomaly', 'Lecrae', [])['state'] == 'missing'
    searcher = Searcher([hit()])
    again = AlbumArtService(svc.store, FakeCovers(), FakeCache({GROUP: ('image/jpeg', JPEG)}), tmp_path / 'art', search=searcher)
    assert again.lookup('Anomaly', 'Lecrae', [])['state'] == 'confirmed' and searcher.calls == 1


def test_bulk_enrichment_reports_progress_honours_cancel_and_resumes_from_the_cache(tmp_path):
    searcher = Searcher([hit()])
    svc = service(tmp_path, cache=FakeCache({GROUP: ('image/jpeg', JPEG)}), search=searcher)
    pairs = [('Anomaly', 'Lecrae', ''), ('Church Clothes', 'Lecrae', ''), ('Rebel', 'Lecrae', '')]
    progress = []
    stop = {'after': 1}

    def should_cancel():
        return len(progress) >= stop['after']

    summary = svc.enrich(pairs, should_cancel=should_cancel, on_progress=lambda done, total, counts: progress.append((done, total)))
    assert summary['cancelled'] is True and summary['done'] == 1 and progress == [(1, 3)]
    stop['after'] = 99
    summary = svc.enrich(pairs, should_cancel=should_cancel, on_progress=lambda *a: None)
    assert summary['done'] == 3 and summary['cancelled'] is False
    assert searcher.calls == 3, 'the first pair came from the cache; each other pair was asked once'
    assert summary['counts']['confirmed'] + summary['counts']['missing'] + summary['counts']['suggested'] == 3
