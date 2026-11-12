"""Identity suggestions: human accept, reject, review and undo, kept apart from provider evidence.

* Provider evidence (artist_links) is never edited by a person. Human choices live in their own append-only log.
* The effective link is the latest active human decision if there is one, otherwise the provider's strong link.
* Rejected suggestions are not presented again unless the provider's answer changes materially.
* A keep-separate decision still wins over any accepted or provider link.
* A name-match score of 100 alone is not treated as proof for ambiguous names: suggestions show the evidence.
Deterministic: candidates are fixtures. Nothing here is live-service evidence.
"""
import json
import threading

import pytest

from meloshelf.application.artist_lookup import ArtistLookup
from meloshelf.application.webapp import create_server
from meloshelf.domain.artist_identity import ArtistCandidate, LinkDecision, decide_link, effective_link
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_corrections import ArtistCorrections
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions, SuggestionError
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

PRINCE_ALBUMS = ['Purple Rain', 'Sign o the Times']
PRINCE_CANDIDATES = (ArtistCandidate('mb-p1', 'Prince', 100), ArtistCandidate('mb-p2', 'Prince', 100))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    LibraryRepository(database).save_many([
        Track('/a.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged'),
        Track('/b.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Sign o the Times', '', 'tagged'),
        Track('/c.mp3', 1, '.mp3', 1, 'T', 'Tauren Wells', 'Hills', '', 'tagged'),
    ])
    ArtistLinks(database).record('Prince', decide_link('Prince', list(PRINCE_CANDIDATES)), looked_up_at=1.0)
    return database


def ambiguous_row(db):
    return ArtistLinks(db).get('Prince')


# ── pure rules ──────────────────────────────────────────────────────────────────

def test_with_no_human_decision_the_provider_link_is_the_effective_link():
    assert effective_link({'status': 'linked', 'mb_artist_id': 'mb-a'}, []) == ('linked', 'mb-a')


def test_an_accepted_suggestion_becomes_the_effective_link_for_its_album_without_a_provider_link():
    """Changed rule (identity-scope hardening): an accept applies to the albums it names, not to every recording."""
    decisions = [{'id': 1, 'action': 'accept', 'mb_artist_id': 'mb-p2', 'ref': None, 'scope': ['purple rain'],
                  'tracks': []}]
    assert effective_link({'status': 'ambiguous', 'mb_artist_id': None}, decisions,
                          album_key='purple rain') == ('accepted', 'mb-p2')


def test_a_rejection_removes_the_provider_link_for_that_artist():
    decisions = [{'id': 1, 'action': 'reject', 'mb_artist_id': 'mb-a', 'ref': None}]
    assert effective_link({'status': 'linked', 'mb_artist_id': 'mb-a'}, decisions) == ('rejected', 'mb-a')


def test_review_later_changes_nothing():
    decisions = [{'id': 1, 'action': 'review', 'mb_artist_id': None, 'ref': None}]
    assert effective_link({'status': 'ambiguous', 'mb_artist_id': None}, decisions) == (None, None)


def test_an_undone_decision_no_longer_counts():
    decisions = [{'id': 1, 'action': 'accept', 'mb_artist_id': 'mb-p2', 'ref': None},
                 {'id': 2, 'action': 'undo', 'mb_artist_id': None, 'ref': 1}]
    assert effective_link({'status': 'ambiguous', 'mb_artist_id': None}, decisions) == (None, None)


# ── store: decisions are persisted, validated and reversible ────────────────────

def test_accept_requires_a_candidate_that_the_provider_actually_returned(db):
    decisions = SuggestionDecisions(db)
    with pytest.raises(SuggestionError):
        decisions.decide('Prince', 'accept', mb_artist_id='mb-invented')


def test_accept_is_stored_as_a_human_decision_and_the_provider_row_is_untouched(db):
    decisions = SuggestionDecisions(db)
    decisions.decide('Prince', 'accept', mb_artist_id='mb-p2', albums=PRINCE_ALBUMS)
    assert ambiguous_row(db)['status'] == 'ambiguous' and ambiguous_row(db)['mb_artist_id'] is None
    assert decisions.effective('Prince', 'Purple Rain') == ('accepted', 'mb-p2')
    assert decisions.effective('Prince') == (None, None), 'without an album, the scoped decision does not apply'


def test_undo_reverses_the_last_decision_and_is_itself_recorded(db):
    decisions = SuggestionDecisions(db)
    event = decisions.decide('Prince', 'accept', mb_artist_id='mb-p2', albums=PRINCE_ALBUMS)
    decisions.undo(event)
    assert decisions.effective('Prince', 'Purple Rain') == (None, None)
    assert [e['action'] for e in decisions.events()] == ['accept', 'undo']


def test_a_decision_survives_reopening_the_database(tmp_path):
    path = tmp_path / 'keep.sqlite3'
    first = Database(path)
    first.initialize()
    LibraryRepository(first).save_many([Track('/a.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged'),
                                        Track('/b.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Sign o the Times', '', 'tagged')])
    ArtistLinks(first).record('Prince', decide_link('Prince', list(PRINCE_CANDIDATES)), looked_up_at=1.0)
    SuggestionDecisions(first).decide('Prince', 'reject', mb_artist_id='mb-p1', albums=PRINCE_ALBUMS)
    reopened = Database(path)
    reopened.initialize()
    assert SuggestionDecisions(reopened).effective('Prince', 'Purple Rain') == ('rejected', 'mb-p1')


def test_the_candidates_and_their_aliases_are_stored_with_the_provider_answer(db):
    ArtistLinks(db).record('Tauren W.', decide_link('Tauren W.', [
        ArtistCandidate('mb-tw', 'Tauren Wells', 100, aliases=('Tauren W.',))]), looked_up_at=1.0)
    stored = ArtistLinks(db).get('Tauren W.')
    assert stored['candidates'][0]['mb_artist_id'] == 'mb-tw'
    assert stored['candidates'][0]['aliases'] == ['Tauren W.']


# ── no repeated suggestion after a rejection ────────────────────────────────────

def test_a_rejected_name_is_not_looked_up_again_by_the_normal_run(db):
    SuggestionDecisions(db).decide('Prince', 'reject', mb_artist_id='mb-p1', albums=PRINCE_ALBUMS)
    calls = []

    class Client:
        def search_artists(self, name, *, limit=5):
            calls.append(name)
            return []

    ArtistLookup(db, Client()).run()
    assert 'Prince' not in calls


def test_a_rejected_suggestion_is_not_listed_again_when_the_same_answer_comes_back(db):
    decisions = SuggestionDecisions(db)
    decisions.decide('Prince', 'reject', mb_artist_id='mb-p1', albums=PRINCE_ALBUMS)
    ArtistLinks(db).record('Prince', decide_link('Prince', list(PRINCE_CANDIDATES)), looked_up_at=2.0)
    assert 'Prince' not in [s['credit'] for s in decisions.open_suggestions()]


def test_a_changed_provider_answer_reopens_the_suggestion_for_review(db):
    decisions = SuggestionDecisions(db)
    decisions.decide('Prince', 'reject', mb_artist_id='mb-p1', albums=PRINCE_ALBUMS)
    new_answer = (ArtistCandidate('mb-p9', 'Prince', 100), ArtistCandidate('mb-p2', 'Prince', 100))
    ArtistLinks(db).record('Prince', decide_link('Prince', list(new_answer)), looked_up_at=3.0)
    assert 'Prince' in [s['credit'] for s in decisions.open_suggestions()]


# ── grouping: human decisions and keep-separate precedence ───────────────────────

def test_an_accepted_identity_groups_under_the_chosen_artist_id(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-p2', albums=PRINCE_ALBUMS)
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    prince = next(a for a in artists if a['name'] == 'Prince')
    assert prince['mb_artist_id'] == 'mb-p2' and prince['link_status'] == 'accepted'
    assert prince['tracks'] == 2


def test_a_rejected_identity_is_not_grouped_by_id(db):
    SuggestionDecisions(db).decide('Prince', 'reject', mb_artist_id='mb-p1', albums=PRINCE_ALBUMS)
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    prince = next(a for a in artists if a['name'] == 'Prince')
    assert prince['mb_artist_id'] is None


def test_a_keep_separate_decision_still_wins_over_an_accepted_link(db):
    LibraryRepository(db).save_many([Track('/x.mp3', 1, '.mp3', 1, 'T', 'Prince Fan', 'X', '', 'tagged')])
    ArtistLinks(db).record('Prince Fan', LinkDecision('linked', mb_id='mb-p2', name='Prince', reason='fixture'),
                           looked_up_at=1.0)
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-p2', albums=PRINCE_ALBUMS)
    ArtistCorrections(db).keep_separate('Prince', 'Prince Fan')
    artists, _ = LibraryRepository(db).artist_identities(limit=60)
    grouped = [a for a in artists if a['mb_artist_id'] == 'mb-p2']
    assert len(grouped) <= 1, 'the human keep-separate decision keeps the two credits apart'


# ── the suggestions list carries the evidence a person needs ────────────────────

def test_an_open_suggestion_shows_candidates_tracks_and_the_reason(db):
    rows = SuggestionDecisions(db).open_suggestions()
    prince = next(s for s in rows if s['credit'] == 'Prince')
    assert prince['status'] == 'ambiguous'
    assert {c['mb_artist_id'] for c in prince['candidates']} == {'mb-p1', 'mb-p2'}
    assert prince['tracks'] == 2
    assert prince['reason'] and 'different artists' in prince['reason']
    assert {r['album'] for r in prince['recordings']} == {'Purple Rain', 'Sign o the Times'}


def test_a_suggestion_with_a_recording_but_no_candidates_is_not_offered(db):
    assert 'Tauren Wells' not in [s['credit'] for s in SuggestionDecisions(db).open_suggestions()]


# ── HTTP: token-protected decisions ──────────────────────────────────────────────

def _server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'http.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={})
    LibraryRepository(running.database).save_many([Track('/a.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged')])
    LibraryRepository(running.database).save_many([
        Track('/b.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Sign o the Times', '', 'tagged')])
    ArtistLinks(running.database).record('Prince', decide_link('Prince', list(PRINCE_CANDIDATES)), looked_up_at=1.0)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return running, f'http://127.0.0.1:{running.server_address[1]}'


def test_the_suggestions_endpoint_lists_open_suggestions(tmp_path):
    running, base = _server(tmp_path)
    try:
        status, _, body = request(base, 'GET', '/api/artist-suggestions')
        assert status == 200 and [s['credit'] for s in json.loads(body)['suggestions']] == ['Prince']
    finally:
        running.shutdown()
        running.server_close()


def test_a_decision_needs_the_token_and_a_real_candidate(tmp_path):
    running, base = _server(tmp_path)
    try:
        body = {'credit': 'Prince', 'action': 'accept', 'mb_artist_id': 'mb-p2', 'albums': PRINCE_ALBUMS}
        assert request(base, 'POST', '/api/artist-suggestions/decide', body)[0] == 403
        assert request(base, 'POST', '/api/artist-suggestions/decide',
                       {**body, 'mb_artist_id': 'mb-forged'}, token=running.token)[0] == 400
        assert request(base, 'POST', '/api/artist-suggestions/decide', body, token=running.token)[0] == 200
        listed = json.loads(request(base, 'GET', '/api/artist-suggestions')[2])['suggestions']
        assert listed == [], 'an accepted suggestion leaves the open list'
    finally:
        running.shutdown()
        running.server_close()


def test_undo_through_the_api_reopens_the_suggestion(tmp_path):
    running, base = _server(tmp_path)
    try:
        request(base, 'POST', '/api/artist-suggestions/decide',
                {'credit': 'Prince', 'action': 'reject', 'mb_artist_id': 'mb-p1', 'albums': PRINCE_ALBUMS}, token=running.token)
        event = SuggestionDecisions(running.database).events()[0]['id']
        assert request(base, 'POST', '/api/artist-suggestions/decide',
                       {'action': 'undo', 'event_id': event}, token=running.token)[0] == 200
        listed = json.loads(request(base, 'GET', '/api/artist-suggestions')[2])['suggestions']
        assert [s['credit'] for s in listed] == ['Prince']
    finally:
        running.shutdown()
        running.server_close()


def test_the_page_offers_accept_reject_review_and_undo_for_suggestions():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    for hook in ('id="suggestList"', 'id="suggestUndo"', 'data-decide="accept"', 'data-decide="reject"',
                 'data-decide="review"', "'/api/artist-suggestions/decide'"):
        assert hook in page, hook
