"""The first credited artist is the primary artist; genuine group names and ambiguous duos are not split or guessed."""
import pytest

from meloshelf.domain.artists import canonical_key
from meloshelf.domain.primary_artist import analyse_credit, known_artists

KYGO = ['Kygo', 'Kygo, Selena Gomez', 'Kygo, John Legend', 'Kygo, Conrad Sewell', 'Kygo, Donna Summer', 'Kygo & Tina Turner',
        'Kygo feat. Imagine Dragons', 'Kygo ft. Gryffin', 'Kygo x Sasha Alex Sloan', 'Kygo vs. U2', 'Kygo with Zara Larsson',
        'Kygo, Valerie Broussard', 'Kygo & Sandro Cavazza', 'Kygo, Parson James', 'Kygo, Ellie Goulding', 'Kygo, Rita Ora',
        'Kygo, Maty Noyes', 'Kygo, OneRepublic', 'Kygo, Chelsea Cutler', 'Kygo, Miguel', 'Kygo, X Ambassadors', 'Kygo, Julia Michaels',
        'Kygo, Lemaitre', 'Kygo & Whitney Houston', 'Kygo, Tom Odell', 'Kygo, Dean Lewis']


def test_twenty_six_kygo_credits_all_have_one_primary_artist():
    assert len(KYGO) == 26
    for credit in KYGO:
        found = analyse_credit(credit, known=known_artists(KYGO))
        assert found.primary == 'Kygo', credit
        assert found.display == credit, 'the complete original credit is preserved'
        assert not found.review


def test_lecrae_credits_are_grouped_under_lecrae():
    credits = ['Lecrae', 'Lecrae & Andy Mineo', 'Lecrae Feat. E-40', 'Lecrae, Ty Dolla $ign']
    known = known_artists(credits)
    for credit in credits:
        assert analyse_credit(credit, known=known).primary == 'Lecrae', credit
    assert analyse_credit('Lecrae & Andy Mineo', known=known).collaborators == ('Andy Mineo',)


@pytest.mark.parametrize('credit,primary,others', [
    ('Kygo, Selena Gomez', 'Kygo', ('Selena Gomez',)),
    ('Lecrae & Andy Mineo', 'Lecrae', ('Andy Mineo',)),
    ('U2 vs. Kygo', 'U2', ('Kygo',)),
    ('Lecrae Feat. E-40', 'Lecrae', ('E-40',)),
    ('Drake x Future x Lil Baby', 'Drake', ('Future', 'Lil Baby')),
])
def test_the_first_credited_artist_is_the_primary_one(credit, primary, others):
    found = analyse_credit(credit, known=frozenset({'lecrae'}))
    assert (found.primary, found.collaborators) == (primary, others)


@pytest.mark.parametrize('group', ['Simon & Garfunkel', 'Earth, Wind & Fire', 'Hall & Oates', 'Florence + the Machine',
                                   'Of Monsters and Men', 'Crosby, Stills & Nash', 'Peter, Paul and Mary'])
def test_genuine_group_names_are_never_split(group):
    found = analyse_credit(group)
    assert found.primary == group and found.collaborators == () and found.confidence == 'protected'


def test_a_group_name_declared_whole_by_a_person_or_musicbrainz_is_protected():
    assert analyse_credit('Dev, Dee').primary == 'Dev'
    assert analyse_credit('Dev, Dee', together=frozenset({'dev dee'})).primary == 'Dev, Dee'
    assert analyse_credit('Tyler, The Creator').primary == 'Tyler, The Creator'
    assert analyse_credit('Nothing But Thieves & Co', groups=frozenset({'nothing but thieves co'})).primary == 'Nothing But Thieves & Co'


def test_an_unknown_duo_or_pair_is_not_guessed_and_asks_for_review():
    found = analyse_credit('Alpha Wolves & Beta Foxes')
    assert found.review and found.confidence == 'ambiguous'
    assert found.primary == 'Alpha Wolves & Beta Foxes' and found.collaborators == ()
    known = known_artists(['Alpha Wolves', 'Alpha Wolves feat. Someone'])
    assert analyse_credit('Alpha Wolves & Beta Foxes', known=known).primary == 'Alpha Wolves'


def test_featured_credits_do_not_become_the_primary():
    found = analyse_credit('Selena Gomez feat. Kygo')
    assert found.primary == 'Selena Gomez' and found.collaborators == ('Kygo',)


def test_a_credit_with_no_artist_is_empty_and_a_plain_name_is_single():
    assert analyse_credit('').primary == ''
    found = analyse_credit('  Adele ')
    assert found.primary == 'Adele' and found.confidence == 'single' and found.display == 'Adele'


def test_a_slash_between_multi_word_names_is_the_id3_list_separator_and_the_first_artist_is_primary():
    found = analyse_credit('DJ Standout/Byron Juane/Canon/Evan Ford')
    assert found.primary == 'DJ Standout' and found.collaborators == ('Byron Juane', 'Canon', 'Evan Ford')
    assert found.display == 'DJ Standout/Byron Juane/Canon/Evan Ford', 'the credit as tagged is never rewritten'
    assert analyse_credit('DJ Standout/Evan & Eris').primary == 'DJ Standout'
    assert analyse_credit('DJ Standout/Kennis Clark').primary == 'DJ Standout'


def test_a_known_slash_group_or_a_compact_slash_name_stays_whole():
    assert analyse_credit('AC/DC').primary == 'AC/DC'
    assert analyse_credit('Mumford & Sons').primary == 'Mumford & Sons'
    assert analyse_credit('AC/DC feat. Someone Else').primary == 'AC/DC'
    assert analyse_credit('Band Name/Other', together=frozenset({canonical_key('Band Name/Other')})).primary == 'Band Name/Other'


def test_names_with_slashes_or_dots_are_not_split():
    assert analyse_credit('AC/DC').primary == 'AC/DC'
    assert analyse_credit('Jay-Z').primary == 'Jay-Z'
    assert analyse_credit('P.O.D.').primary == 'P.O.D.'
