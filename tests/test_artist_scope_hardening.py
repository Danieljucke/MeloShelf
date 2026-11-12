"""Identity-scope hardening: legacy unscoped decisions, track-level scope and the resolution hierarchy.

Hierarchy (highest first) for one recording:
  1. explicit human decision for this exact recording (track scope)
  2. explicit human decision for this album (album scope)
  3. a historical reject with no scope (blocks a link, never creates one)
  4. a strong, unambiguous provider link
  5. otherwise unresolved

A historical ACCEPT with no scope no longer applies to every recording with the name: it is kept, shown, and needs a
person to scope it again. A narrower decision always beats a broader one. Nothing here changes an audio file.
"""
import pytest

from meloshelf.domain.artist_identity import ArtistCandidate, decide_link, effective_link
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions, SuggestionError
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository

CANDIDATES = (ArtistCandidate('mb-one', 'Prince', 100), ArtistCandidate('mb-two', 'Prince', 100))
A = '/music/purple-rain/01.mp3'
B = '/music/purple-rain/02.mp3'
C = '/music/compilation/01.mp3'
D = '/music/compilation/02.mp3'


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    LibraryRepository(database).save_many([
        Track(A, 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged'),
        Track(B, 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged'),
        Track(C, 1, '.mp3', 1, 'T', 'Prince', 'Compilation', '', 'tagged'),
        Track(D, 1, '.mp3', 1, 'T', 'Prince', 'Compilation', '', 'tagged'),
    ])
    ArtistLinks(database).record('Prince', decide_link('Prince', list(CANDIDATES)), looked_up_at=1.0)
    return database


def tracks_under(db, mb_id):
    artists, _ = LibraryRepository(db).artist_identities(limit=300)
    return sum(a['tracks'] for a in artists if a['mb_artist_id'] == mb_id)


def legacy(action, mb, ref_id=1):
    return {'id': ref_id, 'action': action, 'mb_artist_id': mb, 'ref': None, 'scope': [], 'tracks': [],
            'fingerprint': ''}


# ── 1.1 historical unscoped decisions ───────────────────────────────────────────

def test_a_historical_unscoped_accept_no_longer_applies_to_every_recording():
    row = {'status': 'ambiguous', 'mb_artist_id': None}
    assert effective_link(row, [legacy('accept', 'mb-two')], album_key='purple rain') == (None, None)


def test_a_historical_unscoped_accept_is_kept_in_the_log_and_listed_for_scope_review(db):
    ArtistLinks(db).record('Prince', decide_link('Prince', list(CANDIDATES)), looked_up_at=2.0)
    with db.write() as connection:
        connection.execute("INSERT INTO artist_link_decisions(credit_key,action,mb_artist_id,ref,fingerprint,at,scope) "
                           "VALUES('prince','accept','mb-two',NULL,'',1.0,'[]')")
    decisions = SuggestionDecisions(db)
    assert len(decisions.events()) == 1, 'the historical decision is preserved, not deleted or rewritten'
    item = next(s for s in decisions.open_suggestions() if s['credit'] == 'Prince')
    assert item['legacy_accept'] is True


def test_a_historical_unscoped_reject_still_blocks_its_link_because_blocking_is_the_safe_direction():
    row = {'status': 'linked', 'mb_artist_id': 'mb-two'}
    assert effective_link(row, [legacy('reject', 'mb-two')], album_key='purple rain') == ('rejected', 'mb-two')


def test_a_strong_unambiguous_provider_link_still_applies_without_any_decision():
    row = {'status': 'linked', 'mb_artist_id': 'mb-one'}
    assert effective_link(row, [], album_key='purple rain') == ('linked', 'mb-one')


# ── 1.2 track-level scope and the hierarchy ─────────────────────────────────────

def test_a_track_accept_applies_only_to_that_exact_recording(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[C])
    assert tracks_under(db, 'mb-two') == 1


def test_two_homonyms_on_one_compilation_are_separated_by_recording(db):
    decisions = SuggestionDecisions(db)
    decisions.decide('Prince', 'accept', mb_artist_id='mb-one', tracks=[C])
    decisions.decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[D])
    assert tracks_under(db, 'mb-one') == 1
    assert tracks_under(db, 'mb-two') == 1


def test_a_narrow_reject_beats_a_broad_album_accept_for_that_recording(db):
    decisions = SuggestionDecisions(db)
    decisions.decide('Prince', 'accept', mb_artist_id='mb-two', albums=['Purple Rain'])
    decisions.decide('Prince', 'reject', mb_artist_id='mb-two', tracks=[B])
    assert tracks_under(db, 'mb-two') == 1, 'the album accept still covers A; the narrow reject removes B only'


def test_the_hierarchy_order_is_track_then_album_then_historical_reject_then_provider():
    album_accept = {'id': 1, 'action': 'accept', 'mb_artist_id': 'mb-two', 'ref': None, 'scope': ['purple rain'],
                    'tracks': [], 'fingerprint': ''}
    track_reject = {'id': 2, 'action': 'reject', 'mb_artist_id': 'mb-two', 'ref': None, 'scope': [],
                    'tracks': [A], 'fingerprint': ''}
    row = {'status': 'linked', 'mb_artist_id': 'mb-one'}
    assert effective_link(row, [album_accept, track_reject], album_key='purple rain', track_path=A) == \
        ('rejected', 'mb-two')
    assert effective_link(row, [album_accept, track_reject], album_key='purple rain', track_path=B) == \
        ('accepted', 'mb-two')


def test_a_track_decision_needs_recordings_that_really_carry_this_credit(db):
    with pytest.raises(SuggestionError):
        SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=['/not/in/library.mp3'])


def test_a_track_decision_on_a_recording_without_album_information_still_applies(tmp_path):
    database = Database(tmp_path / 'no-album.sqlite3')
    database.initialize()
    LibraryRepository(database).save_many([Track('/loose.mp3', 1, '.mp3', 1, 'T', 'Prince', '', '', 'tagged'),
                                           Track('/loose2.mp3', 1, '.mp3', 1, 'T', 'Prince', '', '', 'tagged')])
    ArtistLinks(database).record('Prince', decide_link('Prince', list(CANDIDATES)), looked_up_at=1.0)
    SuggestionDecisions(database).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=['/loose.mp3'])
    assert tracks_under(database, 'mb-two') == 1


def test_undo_of_a_track_decision_returns_that_recording_to_the_album_level(db):
    decisions = SuggestionDecisions(db)
    event = decisions.decide('Prince', 'reject', mb_artist_id='mb-one', tracks=[A])
    decisions.undo(event)
    assert decisions.effective('Prince', 'Purple Rain', track_path=A) == (None, None)


def test_a_track_decision_survives_a_reindex_that_rewrites_the_tracks(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[C])
    with db.write() as connection:
        connection.execute('DELETE FROM tracks')
    LibraryRepository(db).save_many([Track(C, 1, '.mp3', 1, 'T', 'Prince', 'Compilation', '', 'tagged')])
    assert tracks_under(db, 'mb-two') == 1


def test_a_track_decision_survives_reopening_the_database(tmp_path):
    path = tmp_path / 'reopen.sqlite3'
    first = Database(path)
    first.initialize()
    LibraryRepository(first).save_many([Track(C, 1, '.mp3', 1, 'T', 'Prince', 'Compilation', '', 'tagged')])
    ArtistLinks(first).record('Prince', decide_link('Prince', list(CANDIDATES)), looked_up_at=1.0)
    SuggestionDecisions(first).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[C])
    reopened = Database(path)
    reopened.initialize()
    assert SuggestionDecisions(reopened).effective('Prince', 'Compilation', track_path=C) == ('accepted', 'mb-two')


# ── newly imported recordings are never covered by old decisions ────────────────

def test_a_newly_imported_recording_with_a_previously_accepted_name_is_not_attributed(db):
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', albums=['Purple Rain'])
    LibraryRepository(db).save_many([Track('/new/recording.mp3', 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain Deluxe',
                                           '', 'tagged')])
    assert tracks_under(db, 'mb-two') == 2, 'the new recording is not covered; only the decided ones are'


def test_a_keep_separate_decision_still_wins_over_a_track_accept(db):
    from meloshelf.infrastructure.artist_corrections import ArtistCorrections
    LibraryRepository(db).save_many([Track('/x.mp3', 1, '.mp3', 1, 'T', 'Prince Fan', 'Other', '', 'tagged')])
    ArtistLinks(db).record('Prince Fan', decide_link('Prince Fan', [ArtistCandidate('mb-two', 'Prince', 100)]),
                           looked_up_at=1.0)
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[C])
    ArtistCorrections(db).keep_separate('Prince', 'Prince Fan')
    artists, _ = LibraryRepository(db).artist_identities(limit=300)
    assert len([a for a in artists if a['mb_artist_id'] == 'mb-two']) <= 1


def test_migration_fifteen_keeps_existing_decisions_and_gives_them_no_recording_scope(tmp_path):
    path = tmp_path / 'schema14.sqlite3'
    database = Database(path)
    database.initialize()
    LibraryRepository(database).save_many([Track(A, 1, '.mp3', 1, 'T', 'Prince', 'Purple Rain', '', 'tagged')])
    with database.write() as connection:
        connection.execute("INSERT INTO artist_link_decisions(credit_key,action,mb_artist_id,ref,fingerprint,at,scope) "
                           "VALUES('prince','accept','mb-two',NULL,'',1.0,'[\"purple rain\"]')")
        connection.execute('DROP TABLE artwork_decisions')
        connection.execute('DROP TABLE artist_images')
        connection.execute('DROP TABLE human_label_history')  # created by the later migration 18
        connection.execute('DROP TABLE human_label_batch_items')  # created by the later migration 19
        connection.execute('DROP TABLE human_label_batches')
        connection.execute('DROP TABLE human_version_label_history')
        connection.execute('DROP TABLE human_version_labels')  # created by the later migration 20
        connection.execute('DROP TABLE file_instance_history')
        connection.execute('DROP TABLE file_instances')
        connection.execute('ALTER TABLE artist_link_decisions DROP COLUMN instances')
        connection.execute('ALTER TABLE artist_link_decisions DROP COLUMN tracks')
        connection.execute('PRAGMA user_version=14')
    reopened = Database(path)
    reopened.initialize()
    events = SuggestionDecisions(reopened).events()
    assert len(events) == 1
    assert events[0]['action'] == 'accept' and events[0]['mb_artist_id'] == 'mb-two'
    assert events[0]['scope'] == ['purple rain'], 'the album scope the person chose is preserved'
    assert events[0]['tracks'] == []
