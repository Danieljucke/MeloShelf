"""File names come in both orders. The artist is chosen by evidence; when evidence is missing both readings are offered."""
from meloshelf.domain.classify import Recognized, decide
from meloshelf.domain.filename_order import OrderHints, order_candidates, recurring_sides

TITO_1 = 'Un_Fils_de_Roi_à_Paris_-_Tito_Prince.m4a'
TITO_2 = 'Une_Étoile_parmi_Nous_-_Tito_Prince_.m4a'


def pairs(candidates):
    return [(c['title'], c['artist']) for c in candidates]


def test_both_tito_prince_files_put_the_title_first_when_the_name_repeats_across_the_folder():
    hints = OrderHints(recurring_sides([TITO_1, TITO_2]))
    assert pairs(order_candidates(TITO_1, hints)) == [('Un Fils de Roi à Paris', 'Tito Prince')]
    assert pairs(order_candidates(TITO_2, hints)) == [('Une Étoile parmi Nous', 'Tito Prince')]
    assert order_candidates(TITO_1, hints)[0]['evidence'] == 'recurring'


def test_a_known_artist_on_the_right_makes_it_title_first_and_on_the_left_artist_first():
    hints = OrderHints({'tito prince': 'known_artist', 'queen': 'known_artist'})
    assert pairs(order_candidates(TITO_1, hints)) == [('Un Fils de Roi à Paris', 'Tito Prince')]
    assert pairs(order_candidates('Queen - Bohemian Rhapsody.mp3', hints)) == [('Bohemian Rhapsody', 'Queen')]
    assert pairs(order_candidates('Bohemian Rhapsody - Queen.mp3', hints)) == [('Bohemian Rhapsody', 'Queen')]
    assert order_candidates('Bohemian Rhapsody - Queen.mp3', hints)[0]['order'] == 'title_artist'
    assert order_candidates('Queen - Bohemian Rhapsody.mp3', hints)[0]['order'] == 'artist_title'


def test_the_files_own_artist_tag_and_the_folder_name_are_stronger_than_a_known_artist():
    hints = OrderHints({'alpha song': 'tag', 'beta name': 'known_artist'})
    assert pairs(order_candidates('Alpha Song - Beta Name.mp3', hints)) == [('Beta Name', 'Alpha Song')]
    hints = OrderHints({'beta name': 'folder'})
    assert pairs(order_candidates('Alpha Song - Beta Name.mp3', hints)) == [('Alpha Song', 'Beta Name')]


def test_an_ambiguous_name_offers_both_readings_and_chooses_neither():
    found = order_candidates('Alpha Song - Beta Name.mp3', OrderHints())
    assert pairs(found) == [('Beta Name', 'Alpha Song'), ('Alpha Song', 'Beta Name')]
    assert all(c['ambiguous'] and c['evidence'] == '' for c in found)
    assert {c['order'] for c in found} == {'artist_title', 'title_artist'}


def test_a_filename_suggestion_is_never_marked_verified():
    for c in order_candidates(TITO_1, OrderHints({'tito prince': 'known_artist'})):
        assert c['source'] == 'filename' and 'verified' not in c


def test_a_name_without_a_separator_is_only_a_title():
    assert pairs(order_candidates('Just A Title.mp3')) == [('Just A Title', '')]


def test_tags_that_match_the_title_first_reading_are_not_called_contradictory():
    tags = {'title': 'Un Fils de Roi à Paris', 'artist': 'Tito Prince', 'album': 'Roi'}
    decision = decide(tags, TITO_1, None)
    assert decision.status == 'organize' and decision.reason_code != 'contradictory_tags'


def test_the_review_candidates_use_the_evidence_for_the_order():
    unresolved = Recognized(False, True, reason_code='not_recognised')
    hints = OrderHints({'tito prince': 'known_artist'})
    decision = decide({}, TITO_1, unresolved, hints=hints)
    assert decision.status == 'review'
    first = decision.candidates[0]
    assert (first['title'], first['artist'], first['source']) == ('Un Fils de Roi à Paris', 'Tito Prince', 'filename')
    ambiguous = decide({}, 'Alpha Song - Beta Name.mp3', unresolved)
    assert len([c for c in ambiguous.candidates if c['source'] == 'filename']) == 2
