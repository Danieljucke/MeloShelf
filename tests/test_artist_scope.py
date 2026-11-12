"""Identity scope and provenance: a human decision applies only to the recordings it was made for.

Two different real artists can carry the same credit string. An accepted MusicBrainz ID must never absorb every
recording with that string: an accept or reject is scoped to the albums the person was shown, and the artist-credit
text stays the attribution while the canonical identity is decided per scope.
"""
import pytest

from meloshelf.domain.artist_identity import ArtistCandidate, decide_link, effective_link
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions, SuggestionError
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository

CANDIDATES = (ArtistCandidate('mb-one', 'Prince', 100), ArtistCandidate('mb-two', 'Prince', 100))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    LibraryRepository(database).save_many([
        Track('/a.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged'),
        Track('/b.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Sign o the Times', '', 'tagged'),
        Track('/c.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Lovesexy', '', 'tagged'),
    ])
    ArtistLinks(database).record('Prince', decide_link('Prince', list(CANDIDATES)), looked_up_at=1.0)
    return database


def identities(db):
    artists, _ = LibraryRepository(db).artist_identities(limit=300)
    return artists


def tracks_under(db, mb_id):
    return sum(a['tracks'] for a in identities(db) if a['mb_artist_id'] == mb_id)


# ── an accept covers only the albums it was made for ─────────────────────────────

def test_an_accept_for_one_album_does_not_absorb_the_same_credit_in_other_albums(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', albums=['Purple Rain'])
    assert tracks_under(db, 'mb-two') == 1, 'only the Purple Rain recording is attributed to mb-two'
    unattributed = [a for a in identities(db) if a['mb_artist_id'] is None and a['name'] == 'Prince']
    assert sum(a['tracks'] for a in unattributed) == 2


def test_an_accept_without_albums_is_refused_so_nothing_is_applied_to_every_recording(db):
    with pytest.raises(SuggestionError):
        SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two')


def test_an_accept_naming_an_album_the_credit_does_not_have_is_refused(db):
    with pytest.raises(SuggestionError):
        SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', albums=['Not In Library'])


def test_a_reject_for_one_album_leaves_another_album_of_the_same_string_open_to_linking(db):
    decisions = SuggestionDecisions(db)
    decisions.decide('Prince', 'reject', mb_artist_id='mb-one', albums=['Purple Rain'])
    decisions.decide('Prince', 'accept', mb_artist_id='mb-one', albums=['Sign o the Times'])
    assert tracks_under(db, 'mb-one') == 1


def test_two_real_artists_with_one_name_stay_separate_when_each_is_accepted_for_its_own_album(db):
    decisions = SuggestionDecisions(db)
    decisions.decide('Prince', 'accept', mb_artist_id='mb-one', albums=['Purple Rain'])
    decisions.decide('Prince', 'accept', mb_artist_id='mb-two', albums=['Sign o the Times', 'Lovesexy'])
    assert tracks_under(db, 'mb-one') == 1
    assert tracks_under(db, 'mb-two') == 2


# ── the effective link respects scope ───────────────────────────────────────────

def test_a_scoped_decision_applies_only_inside_its_scope():
    decisions = [{'id': 1, 'action': 'accept', 'mb_artist_id': 'mb-two', 'ref': None, 'scope': ['purple rain']}]
    assert effective_link({'status': 'ambiguous', 'mb_artist_id': None}, decisions, album_key='purple rain') == \
        ('accepted', 'mb-two')
    assert effective_link({'status': 'ambiguous', 'mb_artist_id': None}, decisions, album_key='lovesexy') == \
        (None, None)


def test_a_legacy_unscoped_accept_no_longer_applies_everywhere():
    """Changed rule (identity-scope hardening): an unscoped accept written before scopes existed was the homonym
    risk itself. It is no longer applied to every recording; see tests/test_artist_scope_hardening.py."""
    decisions = [{'id': 1, 'action': 'accept', 'mb_artist_id': 'mb-two', 'ref': None, 'scope': [], 'tracks': []}]
    assert effective_link({'status': 'ambiguous', 'mb_artist_id': None}, decisions, album_key='lovesexy') == \
        (None, None)


# ── suggestions stay open until every album of the string is decided ────────────

def test_a_suggestion_stays_open_while_some_albums_of_its_string_are_undecided(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', albums=['Purple Rain'])
    open_items = {s['credit']: s for s in SuggestionDecisions(db).open_suggestions()}
    assert 'Prince' in open_items
    assert open_items['Prince']['undecided_albums'] == ['Lovesexy', 'Sign o the Times']


def test_a_suggestion_closes_once_every_album_is_decided(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two',
                                   albums=['Purple Rain', 'Sign o the Times', 'Lovesexy'])
    assert 'Prince' not in [s['credit'] for s in SuggestionDecisions(db).open_suggestions()]


def test_the_suggestion_lists_the_albums_so_the_person_can_scope_the_choice(db):
    item = next(s for s in SuggestionDecisions(db).open_suggestions() if s['credit'] == 'Prince')
    assert item['albums'] == ['Lovesexy', 'Purple Rain', 'Sign o the Times']


# ── provenance: attribution text is kept, identity is decided per scope ──────────

def test_the_credit_text_is_kept_on_every_track_whatever_the_identity(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', albums=['Purple Rain'])
    with db.read() as connection:
        credits = {r[0] for r in connection.execute("SELECT artist FROM tracks")}
    assert credits == {'Prince'}
