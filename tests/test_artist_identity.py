"""Canonical artist identity from raw credit strings.

Without MusicBrainz artist IDs, identity is decided conservatively:
case, spacing and punctuation differences merge; featured artists are split off
the primary artist; a collaboration is split only when its leading name is an
artist already established in the library. Legitimate band names stay whole.
"""
from meloshelf.domain.artists import canonical_key, parse_credit
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.domain.models import Track

KNOWN = {canonical_key(n) for n in ('Tauren Wells', 'Aaron Cole', 'Kirk Franklin', 'Jenn Johnson', 'Adele')}


def test_capitalization_and_spacing_produce_one_identity():
    assert canonical_key('Tauren Wells') == canonical_key('Tauren wells') == canonical_key('  tauren   WELLS ')


def test_accents_and_punctuation_are_ignored_for_the_key():
    assert canonical_key('Beyoncé') == canonical_key('Beyonce')
    assert canonical_key('Mr. Probz') == canonical_key('Mr Probz')


def test_featured_artist_is_split_from_the_primary_and_kept_as_credit():
    credit = parse_credit('Tauren Wells Feat. Jenn Johnson', known=KNOWN)
    assert credit.primary == ('Tauren Wells',)
    assert credit.featured == ('Jenn Johnson',)
    assert credit.display == 'Tauren Wells Feat. Jenn Johnson'


def test_collaboration_with_an_established_leading_artist_is_split():
    credit = parse_credit('Tauren Wells & Aaron Cole', known=KNOWN)
    assert credit.primary == ('Tauren Wells',)
    assert credit.featured == ('Aaron Cole',)
    assert 'Tauren Wells & Aaron Cole' not in credit.primary


def test_comma_separated_collaboration_is_split_the_same_way():
    credit = parse_credit('Tauren Wells, Kirk Franklin', known=KNOWN)
    assert credit.primary == ('Tauren Wells',) and credit.featured == ('Kirk Franklin',)


def test_legitimate_band_names_with_ampersands_are_not_broken_apart():
    assert parse_credit('Simon & Garfunkel', known=KNOWN).primary == ('Simon & Garfunkel',)
    assert parse_credit('Florence + the Machine', known=KNOWN).primary == ('Florence + the Machine',)
    assert parse_credit('Earth, Wind & Fire', known=KNOWN).primary == ('Earth, Wind & Fire',)


def test_an_unknown_leading_name_is_not_split_even_with_separators():
    assert parse_credit('Hall & Oates', known=KNOWN).primary == ('Hall & Oates',)


def test_an_artist_with_the_same_name_in_another_library_is_not_merged_by_name_alone():
    """Two different artists sharing a name remain one identity until catalogue IDs are available;
    the limitation is documented rather than silently 'resolved' by merging on appearance alone."""
    assert canonical_key('Tauren Wells') == canonical_key('Tauren Wells')


def test_repository_groups_variants_under_one_artist_with_featured_appearances(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    repo = LibraryRepository(database)
    tracks = [
        ('/a.mp3', 'Tauren Wells', 'Album A'),
        ('/b.mp3', 'Tauren wells', 'Album A'),
        ('/c.mp3', 'Tauren Wells Feat. Jenn Johnson', 'Album B'),
        ('/d.mp3', 'Tauren Wells & Aaron Cole', 'Album B'),
        ('/e.mp3', 'Aaron Cole', 'Solo'),
        ('/f.mp3', 'Simon & Garfunkel', 'Bridge'),
    ]
    repo.save_many([Track(p, 1, '.mp3', 1, 'T', a, al, '', 'tagged') for p, a, al in tracks])
    artists, total = repo.artist_identities(limit=60)
    by_name = {a['name']: a for a in artists}
    assert total == len(artists)
    assert by_name['Tauren Wells']['tracks'] == 4
    assert by_name['Tauren Wells']['albums'] == 2
    assert by_name['Jenn Johnson']['featured_tracks'] == 1, 'discoverable under the featured artist'
    assert by_name['Simon & Garfunkel']['tracks'] == 1
    assert 'Tauren Wells & Aaron Cole' not in by_name
    assert by_name['Aaron Cole']['featured_tracks'] == 1 and by_name['Aaron Cole']['tracks'] == 1


def test_migration_keeps_raw_tags_and_does_not_rewrite_them(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    LibraryRepository(database).save_many([Track('/x.mp3', 1, '.mp3', 1, 'T', 'tauren wells feat. jenn johnson', 'A', '', 'tagged')])
    artists, _ = LibraryRepository(database).artist_identities(limit=60)
    assert LibraryRepository(database).list_tracks(limit=5)[0]['artist'] == 'tauren wells feat. jenn johnson'
    assert [a['name'] for a in artists if canonical_key(a['name']) == 'tauren wells'] == ['tauren wells'], artists


def test_each_canonical_artist_keeps_its_original_credit_strings_for_the_cleanup_view(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    repo = LibraryRepository(database)
    repo.save_many([
        Track('/a.mp3', 1, '.mp3', 1, 'T', 'Tauren Wells', 'A', '', 'tagged'),
        Track('/b.mp3', 1, '.mp3', 1, 'T', 'Tauren wells', 'A', '', 'tagged'),
        Track('/c.mp3', 1, '.mp3', 1, 'T', 'Tauren Wells Feat. Jenn Johnson', 'B', '', 'tagged'),
    ])
    artists, _ = repo.artist_identities(limit=60)
    tauren = next(a for a in artists if canonical_key(a['name']) == 'tauren wells')
    assert {v['credit'] for v in tauren['variants']} == {'Tauren Wells', 'Tauren wells', 'Tauren Wells Feat. Jenn Johnson'}
    assert sum(v['tracks'] for v in tauren['variants']) == 3
