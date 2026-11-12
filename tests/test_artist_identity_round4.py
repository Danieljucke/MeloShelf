"""Round 4: stable MusicBrainz artist identities.

Rules under test:
* A link to a MusicBrainz artist ID needs an exact name or alias match with a strong score, and exactly one
  distinct artist ID. Similar names never link, and two distinct IDs never merge because of their names.
* Links keep their provenance and are stored apart from user decisions. A user's merge or keep-separate decision
  still wins over a provider link.
* Credits keep their original display text. The identity's display name comes from the provider only when linked.
* Unlinked artists are grouped conservatively by normalised spelling, exactly as before.
* Provider failure stores nothing and never breaks the Artists view.
Deterministic: every provider response below is a mocked fixture. No live provider is contacted here.
"""
import json

import pytest

from meloshelf.application.artist_lookup import ArtistLookup
from meloshelf.domain.artist_identity import ArtistCandidate, decide_link
from meloshelf.domain.artists import canonical_key
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_corrections import ArtistCorrections
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.music_api import APIError, MusicBrainzClient
from meloshelf.infrastructure.repository import LibraryRepository


def cand(mb_id, name, score=100, aliases=(), disambiguation=''):
    return ArtistCandidate(mb_id=mb_id, name=name, score=score, aliases=tuple(aliases), disambiguation=disambiguation)


# ── pure rules: when a link is allowed ─────────────────────────────────────────

def test_an_exact_name_with_a_strong_score_and_one_id_links():
    decision = decide_link('Tauren Wells', [cand('mb-tw', 'Tauren Wells')])
    assert decision.status == 'linked' and decision.mb_id == 'mb-tw'
    assert decision.provenance == 'musicbrainz'


def test_two_distinct_ids_with_the_same_name_are_ambiguous_and_neither_is_chosen():
    decision = decide_link('Prince', [cand('mb-1', 'Prince'), cand('mb-2', 'Prince')])
    assert decision.status == 'ambiguous' and decision.mb_id is None


def test_a_similar_but_different_name_never_links_however_high_the_score():
    decision = decide_link('Queen', [cand('mb-q', 'Queen Band', score=100)])
    assert decision.status == 'no_match' and decision.mb_id is None


def test_a_weak_score_does_not_link_even_on_an_exact_name():
    decision = decide_link('Queen', [cand('mb-q', 'Queen', score=60)])
    assert decision.status == 'no_match'


def test_an_alias_match_is_only_a_suggestion_for_review_and_never_an_automatic_link():
    decision = decide_link('Tauren W.', [cand('mb-tw', 'Tauren Wells', aliases=('Tauren W.',))])
    assert decision.status == 'suggested' and decision.mb_id == 'mb-tw'
    assert 'alias' in decision.reason


def test_a_primary_name_below_the_automatic_threshold_is_only_a_suggestion():
    decision = decide_link('Bobby Prince', [cand('mb-bp', 'Bobby Prince', score=92)])
    assert decision.status == 'suggested' and decision.mb_id == 'mb-bp'


def test_a_name_that_ties_with_another_strong_candidate_is_not_automatic_either():
    decision = decide_link('Queen', [cand('mb-q', 'Queen', score=100), cand('mb-x', 'Queen', score=88)])
    assert decision.status == 'ambiguous'


def test_capitalisation_and_accent_variants_of_an_exact_name_link_to_the_same_id():
    decision = decide_link('beyonce', [cand('mb-b', 'Beyoncé')])
    assert decision.status == 'linked' and decision.mb_id == 'mb-b'


def test_the_same_id_returned_twice_is_one_link_not_an_ambiguity():
    decision = decide_link('Adele', [cand('mb-a', 'Adele', score=100), cand('mb-a', 'Adele', score=98)])
    assert decision.status == 'linked' and decision.mb_id == 'mb-a'


def test_an_empty_credit_is_never_linked():
    assert decide_link('', [cand('mb-x', 'Anything')]).status == 'no_match'


# ── store: provenance is kept ──────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    return database


def test_a_link_is_stored_with_its_provenance_and_original_name(db):
    links = ArtistLinks(db)
    links.record('Tauren Wells', decide_link('Tauren Wells', [cand('mb-tw', 'Tauren Wells')]), looked_up_at=1700000000.0)
    stored = links.get('Tauren Wells')
    assert stored['mb_artist_id'] == 'mb-tw'
    assert stored['mb_name'] == 'Tauren Wells'
    assert stored['provenance'] == 'musicbrainz'
    assert stored['status'] == 'linked'
    assert stored['credit'] == 'Tauren Wells'


def test_an_ambiguous_result_is_stored_without_an_id_so_it_is_not_retried_blindly(db):
    links = ArtistLinks(db)
    links.record('Prince', decide_link('Prince', [cand('mb-1', 'Prince'), cand('mb-2', 'Prince')]), looked_up_at=1.0)
    stored = links.get('Prince')
    assert stored['status'] == 'ambiguous' and stored['mb_artist_id'] is None


def test_a_provider_that_later_names_a_different_id_is_a_conflict_not_a_silent_switch(db):
    links = ArtistLinks(db)
    links.record('Adele', decide_link('Adele', [cand('mb-old', 'Adele')]), looked_up_at=1.0)
    links.record('Adele', decide_link('Adele', [cand('mb-new', 'Adele')]), looked_up_at=2.0)
    stored = links.get('Adele')
    assert stored['status'] == 'conflict'
    assert stored['mb_artist_id'] == 'mb-old', 'the stored link is kept until a person decides'
    assert 'mb-new' in stored['reason']


def test_the_same_id_again_refreshes_the_stored_link(db):
    links = ArtistLinks(db)
    links.record('Adele', decide_link('Adele', [cand('mb-a', 'Adele')]), looked_up_at=1.0)
    links.record('Adele', decide_link('Adele', [cand('mb-a', 'Adele')]), looked_up_at=2.0)
    stored = links.get('Adele')
    assert stored['status'] == 'linked' and stored['looked_up_at'] == 2.0


def test_a_suggestion_is_stored_and_does_not_group_artists(db):
    seed(db, [('/a.mp3', 'Tauren W.', 'B')])
    ArtistLinks(db).record('Tauren W.', decide_link('Tauren W.', [cand('mb-tw', 'Tauren Wells', aliases=('Tauren W.',))]),
                           looked_up_at=1.0)
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    assert artists[0]['name'] == 'Tauren W.'
    assert artists[0]['mb_artist_id'] is None and artists[0]['link_status'] == 'suggested'


# ── lookup: calls are bounded, failures are harmless ───────────────────────────

class FakeArtistClient:
    def __init__(self, table, *, fail=False):
        self.table = table          # normalised name key -> list of candidate dicts (mocked provider response)
        self.fail = fail
        self.calls = []

    def search_artists(self, name, *, limit=5):
        self.calls.append(name)
        if self.fail:
            raise APIError('MusicBrainz is unreachable', retryable=True)
        return self.table.get(canonical_key(name), [])


def artist_hit(mb_id, name, score=100, aliases=()):
    return {'mb_artist_id': mb_id, 'name': name, 'score': score, 'aliases': list(aliases), 'disambiguation': ''}


def seed(db, rows):
    LibraryRepository(db).save_many([Track(p, 1, '.mp3', 1, 'T', a, al, '', 'tagged') for p, a, al in rows])


def test_each_distinct_credit_is_looked_up_once_and_reused_on_the_next_run(db):
    seed(db, [('/a.mp3', 'Adele', 'A'), ('/b.mp3', 'Adele', 'B'), ('/c.mp3', 'Queen', 'C')])
    client = FakeArtistClient({'adele': [artist_hit('mb-a', 'Adele')], 'queen': [artist_hit('mb-q', 'Queen')]})
    ArtistLookup(db, client).run()
    ArtistLookup(db, client).run()
    assert sorted(client.calls) == ['Adele', 'Queen']


def test_featured_performers_are_looked_up_too(db):
    seed(db, [('/c.mp3', 'Tauren Wells Feat. Jenn Johnson', 'C')])
    client = FakeArtistClient({'jenn johnson': [artist_hit('mb-j', 'Jenn Johnson')],
                               'tauren wells': [artist_hit('mb-t', 'Tauren Wells')]})
    summary = ArtistLookup(db, client).run()
    assert 'Jenn Johnson' in client.calls
    assert summary['linked'] == 2


def test_a_provider_failure_stores_nothing_and_reports_the_failure(db):
    seed(db, [('/a.mp3', 'Adele', 'A')])
    summary = ArtistLookup(db, FakeArtistClient({}, fail=True)).run()
    assert summary['failed'] == 1 and summary['linked'] == 0
    assert ArtistLinks(db).get('Adele') is None


# ── grouping: identities follow links, names come from the provider only when linked ──

def test_linked_variants_group_under_one_identity_named_by_the_provider_and_keep_their_credits(db):
    seed(db, [('/a.mp3', 'Tauren Wells', 'A'), ('/b.mp3', 'tauren WELLS', 'B')])
    ArtistLookup(db, FakeArtistClient({'tauren wells': [artist_hit('mb-t', 'Tauren Wells')]})).run()
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    assert len(artists) == 1
    assert artists[0]['name'] == 'Tauren Wells' and artists[0]['mb_artist_id'] == 'mb-t'
    assert {v['credit'] for v in artists[0]['variants']} == {'Tauren Wells', 'tauren WELLS'}


def test_an_alias_credit_is_a_suggestion_and_stays_out_of_the_identity_until_a_person_accepts(db):
    seed(db, [('/a.mp3', 'Tauren Wells', 'A'), ('/b.mp3', 'Tauren W.', 'B')])
    client = FakeArtistClient({'tauren wells': [artist_hit('mb-t', 'Tauren Wells', aliases=['Tauren W.'])],
                               'tauren w': [artist_hit('mb-t', 'Tauren Wells', aliases=['Tauren W.'])]})
    ArtistLookup(db, client).run()
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    assert sorted(a['name'] for a in artists) == ['Tauren W.', 'Tauren Wells']
    suggested = next(a for a in artists if a['name'] == 'Tauren W.')
    assert suggested['link_status'] == 'suggested' and suggested['mb_artist_id'] is None


def test_every_looked_up_name_is_counted_in_exactly_one_outcome_including_suggestions(db):
    seed(db, [('/a.mp3', 'Tauren Wells', 'A'), ('/b.mp3', 'Tauren W.', 'B'), ('/c.mp3', 'Queen', 'C')])
    client = FakeArtistClient({'tauren wells': [artist_hit('mb-t', 'Tauren Wells', aliases=['Tauren W.'])],
                               'tauren w': [artist_hit('mb-t', 'Tauren Wells', aliases=['Tauren W.'])]})
    summary = ArtistLookup(db, client).run()
    outcomes = summary['linked'] + summary['suggested'] + summary['ambiguous'] + summary['no_match']
    assert summary['looked_up'] == outcomes == 3
    assert summary['suggested'] == 1, 'the alias credit is a suggestion, and it is reported as one'


def test_a_name_and_an_alias_of_different_ids_never_merge_by_name(db):
    seed(db, [('/a.mp3', 'Prince', 'A'), ('/b.mp3', 'The Prince', 'B')])
    ArtistLookup(db, FakeArtistClient({
        'prince': [artist_hit('mb-p1', 'Prince')],
        'the prince': [artist_hit('mb-p2', 'Prince', aliases=['The Prince'])],
    })).run()
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    linked = [a for a in artists if a['mb_artist_id']]
    assert [a['mb_artist_id'] for a in linked] == ['mb-p1']
    assert len(artists) == 2, 'the alias suggestion is a separate, reviewable identity'


def test_an_unlinked_name_is_not_merged_into_a_linked_artist_by_similarity(db):
    seed(db, [('/a.mp3', 'Queen', 'A'), ('/b.mp3', 'Queen Band', 'B')])
    ArtistLookup(db, FakeArtistClient({'queen': [artist_hit('mb-q', 'Queen')]})).run()
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    names = sorted(a['name'] for a in artists)
    assert names == ['Queen', 'Queen Band']
    band = next(a for a in artists if a['name'] == 'Queen Band')
    assert band['mb_artist_id'] is None


def test_an_ambiguous_name_stays_unlinked_and_separate(db):
    seed(db, [('/a.mp3', 'Prince', 'A')])
    ArtistLookup(db, FakeArtistClient({'prince': [artist_hit('mb-1', 'Prince'), artist_hit('mb-2', 'Prince')]})).run()
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    assert artists[0]['mb_artist_id'] is None and artists[0]['link_status'] == 'ambiguous'


def test_a_user_merge_still_wins_over_a_provider_link(db):
    seed(db, [('/a.mp3', 'Tauren Wells', 'A'), ('/b.mp3', 'Tauren W.', 'B')])
    ArtistLookup(db, FakeArtistClient({'tauren wells': [artist_hit('mb-t', 'Tauren Wells')]})).run()
    ArtistCorrections(db).merge('Tauren W.', 'Tauren Wells')
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    assert len(artists) == 1 and artists[0]['tracks'] == 2


def test_the_identity_record_lists_its_featured_collaborators(db):
    seed(db, [('/c.mp3', 'Paul Simon Feat. The Beatles', 'Collab')])
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    simon = next(a for a in artists if a['name'] == 'Paul Simon')
    assert 'The Beatles' in simon['collaborators']


# ── corrections: the lowercase display-name fix ────────────────────────────────

def test_a_saved_merge_keeps_the_original_spelling_of_both_names(db):
    ArtistCorrections(db).merge('The Beatles', 'Beatles')
    event = [e for e in ArtistCorrections(db).events() if e['action'] == 'merge'][0]
    assert event['source_name'] == 'The Beatles'
    assert event['target_name'] == 'Beatles'


def test_a_keep_separate_keeps_both_original_names(db):
    ArtistCorrections(db).keep_separate('Ghost', 'Ghost Town')
    event = [e for e in ArtistCorrections(db).events() if e['action'] == 'split'][0]
    assert event['source_name'] == 'Ghost' and event['target_name'] == 'Ghost Town'


def test_migration_ten_keeps_existing_decisions_and_their_keys(tmp_path):
    database = Database(tmp_path / 'old.sqlite3')
    database.initialize()
    ArtistCorrections(database).merge('The Beatles', 'Beatles')
    with database.write() as c:
        c.execute('DROP TABLE artwork_decisions')
        c.execute('DROP TABLE artist_images')
        c.execute('DROP TABLE human_label_history')  # created by the later migration 18
        c.execute('DROP TABLE human_label_batch_items')  # created by the later migration 19
        c.execute('DROP TABLE human_label_batches')
        c.execute('DROP TABLE human_version_label_history')
        c.execute('DROP TABLE human_version_labels')  # created by the later migration 20
        c.execute('DROP TABLE file_instance_history')
        c.execute('DROP TABLE file_instances')
        c.execute('DROP TABLE artist_link_decisions')
        c.execute('DROP TABLE artist_enrichment_items')
        c.execute('DROP TABLE artist_enrichment_jobs')
        c.execute('ALTER TABLE artist_corrections DROP COLUMN source_name')
        c.execute('DROP TABLE artist_links')
        c.execute('PRAGMA user_version=9')
    database.initialize()
    events = ArtistCorrections(database).events()
    assert len(events) == 1 and events[0]['source_key'] == 'the beatles'
    assert events[0]['source_name'] == ''


# ── provider client: parsing a mocked MusicBrainz artist search ───────────────────

def test_the_musicbrainz_artist_search_parses_ids_names_and_aliases():
    payload = {'artists': [{'id': '4d5b3b9b-1111-4f3b-8e0e-000000000001', 'name': 'Tauren Wells', 'score': 100,
                            'disambiguation': 'US singer',
                            'aliases': [{'name': 'Tauren W.'}, {'name': 'T. Wells'}]}]}
    seen = {}

    def transport(url, **kwargs):
        seen['url'] = url
        return payload

    client = MusicBrainzClient('MeloShelf/test (https://example.invalid/contact)', transport=transport)
    hits = client.search_artists('Tauren Wells')
    assert '/ws/2/artist/' in seen['url']
    assert hits == [{'mb_artist_id': '4d5b3b9b-1111-4f3b-8e0e-000000000001', 'name': 'Tauren Wells', 'score': 100,
                     'aliases': ['Tauren W.', 'T. Wells'], 'disambiguation': 'US singer'}]


def test_a_malformed_artist_search_is_refused_not_guessed():
    client = MusicBrainzClient('MeloShelf/test (https://example.invalid/contact)', transport=lambda url, **k: {'x': 1})
    with pytest.raises(APIError):
        client.search_artists('Anyone')


# ── HTTP: the lookup is an explicit, token-protected action ──────────────────────

def test_the_lookup_endpoint_needs_the_session_token_and_stores_results(tmp_path):
    import threading
    from pathlib import Path
    from meloshelf.application.webapp import create_server
    from meloshelf.infrastructure.secrets import MemorySecretStore
    from test_qa_api import request

    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, artist_client=FakeArtistClient({'adele': [artist_hit('mb-a', 'Adele')]}))
    seed(running.database, [('/a.mp3', 'Adele', 'A')])
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{running.server_address[1]}'
    try:
        assert request(base, 'POST', '/api/artist-links/lookup', {'limit': 10})[0] == 403
        status, _, body = request(base, 'POST', '/api/artist-links/lookup', {'limit': 10}, token=running.token)
        assert status == 202 and json.loads(body)['linked'] == 1
        assert ArtistLinks(running.database).get('Adele')['mb_artist_id'] == 'mb-a'
        status, _, body = request(base, 'POST', '/api/artist-links/lookup', {'limit': 500}, token=running.token)
        assert status == 400
    finally:
        running.shutdown()
        running.server_close()


def test_the_page_offers_the_lookup_and_shows_original_spellings_in_saved_decisions():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'id="lookupArtists"' in page and "'/api/artist-links/lookup'" in page
    assert 'e.source_name||e.source_key' in page
    assert 'MusicBrainz ID linked' in page
