"""Round 4: persistent artist merge and split corrections with undo.

Corrections live in their own append-only log. A correction never rewrites a raw tag. Undo appends an
event that points at the one it reverses; nothing is deleted. A variant with no correction keeps the
heuristic identity from domain/artists.py.
"""
import pytest

from meloshelf.infrastructure.artist_corrections import ArtistCorrectionError, ArtistCorrections
from meloshelf.infrastructure.database import Database
from meloshelf.domain.artist_corrections import replay


@pytest.fixture
def corrections(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    return ArtistCorrections(db)


def test_merge_resolves_a_variant_to_the_chosen_name(corrections):
    corrections.merge('The Beatles', 'Beatles')
    assert corrections.state().resolve('the beatles') == 'Beatles'


def test_a_variant_with_no_correction_resolves_to_nothing(corrections):
    corrections.merge('The Beatles', 'Beatles')
    assert corrections.state().resolve('Queen') is None


def test_the_latest_merge_for_the_same_variant_wins(corrections):
    corrections.merge('Beatles', 'The Beatles')
    corrections.merge('Beatles', 'Fab Four')
    assert corrections.state().resolve('beatles') == 'Fab Four'


def test_undo_restores_the_previous_mapping(corrections):
    corrections.merge('X Artist', 'Y Artist')
    second = corrections.merge('X Artist', 'Z Artist')
    corrections.undo(second)
    assert corrections.state().resolve('x artist') == 'Y Artist'


def test_undo_of_the_only_merge_returns_the_variant_to_its_own_identity(corrections):
    event = corrections.merge('The Beatles', 'Beatles')
    corrections.undo(event)
    assert corrections.state().resolve('the beatles') is None


def test_an_undo_cannot_be_undone_and_an_unknown_event_is_refused(corrections):
    event = corrections.merge('A', 'B')
    undo_event = corrections.undo(event)
    with pytest.raises(ArtistCorrectionError):
        corrections.undo(undo_event)
    with pytest.raises(ArtistCorrectionError):
        corrections.undo(999)


def test_the_same_merge_cannot_be_undone_twice(corrections):
    event = corrections.merge('A', 'B')
    corrections.undo(event)
    with pytest.raises(ArtistCorrectionError):
        corrections.undo(event)


def test_keep_separate_blocks_a_merge_in_either_direction(corrections):
    corrections.keep_separate('Ghost', 'Ghost Town')
    with pytest.raises(ArtistCorrectionError):
        corrections.merge('Ghost', 'Ghost Town')
    with pytest.raises(ArtistCorrectionError):
        corrections.merge('Ghost Town', 'Ghost')


def test_merging_a_name_into_its_own_key_is_refused(corrections):
    with pytest.raises(ArtistCorrectionError):
        corrections.merge('Abba', 'ABBA')


def test_empty_names_are_refused(corrections):
    with pytest.raises(ArtistCorrectionError):
        corrections.merge('', 'X')
    with pytest.raises(ArtistCorrectionError):
        corrections.merge('X', '   ')


def test_corrections_persist_after_reopening_the_database(tmp_path):
    path = tmp_path / 'lib.sqlite3'
    db = Database(path)
    db.initialize()
    ArtistCorrections(db).merge('The Beatles', 'Beatles')
    reopened = Database(path)
    reopened.initialize()
    assert ArtistCorrections(reopened).state().resolve('the beatles') == 'Beatles'


def test_corrections_never_change_the_raw_tags(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    with db.write() as connection:
        connection.execute('''INSERT INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status)
                              VALUES('D:/Library/a.mp3',1,'.mp3',1,'Song','The Beatles','Album',2000,'tagged')''')
    ArtistCorrections(db).merge('The Beatles', 'Beatles')
    with db.read() as connection:
        artist = connection.execute("SELECT artist FROM tracks WHERE path='D:/Library/a.mp3'").fetchone()[0]
    assert artist == 'The Beatles'


def test_the_log_is_append_only(corrections):
    event = corrections.merge('A', 'B')
    corrections.undo(event)
    events = corrections.events()
    assert [e['action'] for e in events] == ['merge', 'undo']
    assert events[0]['id'] == event


def test_replay_refuses_an_undo_that_points_at_nothing():
    events = [{'id': 1, 'action': 'undo', 'ref': 7, 'source_key': '', 'target_key': '', 'target_name': ''}]
    with pytest.raises(ArtistCorrectionError):
        replay(events)
