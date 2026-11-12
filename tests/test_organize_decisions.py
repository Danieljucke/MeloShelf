"""What to do with each file: keep reliable tags, use a confident recognition for generic ones, otherwise ask a person.

Never invent anything: an album is used only when the evidence names exactly one, an edition is never chosen, and a
file whose identity is not reliable keeps its name and is placed in À vérifier with no speculative metadata.
"""
from meloshelf.domain.classify import Recognized, decide, fields_to_write, needs_recognition


def tags(title='', artist='', album='', tracknumber=''):
    return {'title': title, 'artist': artist, 'album': album, 'tracknumber': tracknumber}


def recognized(title='Give In', artist='Lecrae', releases=('Anomaly',), approved=True, verdict='probable_agreement', **extra):
    return Recognized(approved=approved, attempted=True, title=title, artist=artist, releases=tuple(releases),
                      version_verdict=verdict, **extra)


# ── reliable tags are kept ──────────────────────────────────────────────────────

def test_reliable_tags_are_preserved_and_need_no_recognition():
    t = tags('Bohemian Rhapsody', 'Queen', 'A Night at the Opera', '11/12')
    assert needs_recognition(t, 'Queen - Bohemian Rhapsody.mp3') == (False, '')
    d = decide(t, 'Queen - Bohemian Rhapsody.mp3', None)
    assert (d.status, d.artist, d.album, d.title, d.track_no, d.origin) == (
        'organize', 'Queen', 'A Night at the Opera', 'Bohemian Rhapsody', 11, 'tags')
    assert fields_to_write(t, d) == {}, 'a correctly tagged file is not retagged'


def test_a_missing_album_is_an_unknown_album_not_a_reason_to_doubt_the_song():
    d = decide(tags('Hello', 'Queen', '', '3'), 'x.mp3', None)
    assert d.status == 'organize' and d.album == '' and d.track_no is None, 'no number without an album'


def test_a_featured_credit_is_kept_in_the_decision():
    d = decide(tags('Give In', 'Lecrae Feat. Crystal Nicole', 'Anomaly'), 'x.mp3', None)
    assert d.artist == 'Lecrae Feat. Crystal Nicole'


# ── detecting generic or incomplete metadata ────────────────────────────────────

def test_generic_titles_missing_artists_and_download_ids_need_recognition():
    cases = [tags('', '', ''), tags('0042', 'Queen', 'Opera'), tags('Track 01', 'Queen', 'Opera'),
             tags('Song', 'Unknown Artist', 'Opera'), tags('Song', '', ''), tags('dQw4w9WgXcQ', 'Queen', 'Opera'),
             tags('Untitled', 'Queen', 'Opera')]
    for t in cases:
        wanted, reason = needs_recognition(t, '0042.mp3')
        assert wanted is True and reason, t


def test_tags_that_contradict_the_file_name_need_a_second_opinion():
    t = tags('Imagine', 'John Lennon', 'Imagine')
    wanted, reason = needs_recognition(t, 'Queen - Bohemian Rhapsody.mp3')
    assert wanted is True and reason == 'contradictory_tags'


def test_a_file_name_without_an_artist_is_not_a_contradiction():
    assert needs_recognition(tags('Imagine', 'John Lennon', 'Imagine'), '03 - Imagine.mp3') == (False, '')


# ── a confident recognition completes generic metadata ──────────────────────────

def test_a_recognized_song_with_no_tags_is_restored_from_the_recognition():
    t = tags('', '', '')
    d = decide(t, '0042.mp3', recognized())
    assert (d.status, d.artist, d.title, d.album, d.origin) == ('organize', 'Lecrae', 'Give In', 'Anomaly', 'recognition')
    assert fields_to_write(t, d) == {'title': 'Give In', 'artist': 'Lecrae', 'album': 'Anomaly'}


def test_only_the_changed_fields_are_written():
    t = tags('0042', 'Lecrae', 'Anomaly', '5')
    d = decide(t, '0042.mp3', recognized())
    assert fields_to_write(t, d) == {'title': 'Give In'}


def test_several_different_albums_are_never_chosen_between():
    d = decide(tags('', '', ''), 'x.mp3', recognized(releases=('Anomaly', 'Best Of Lecrae', 'Now 54')))
    assert d.status == 'organize' and d.album == '', 'the recording is known; its album is not'


def test_editions_with_the_same_title_do_not_change_the_album_name():
    d = decide(tags('', '', ''), 'x.mp3', recognized(releases=('Anomaly', 'anomaly', 'Anomaly')))
    assert d.album == 'Anomaly'


def test_an_existing_album_tag_that_matches_one_release_is_kept():
    d = decide(tags('', 'Lecrae', 'Best Of Lecrae'), 'x.mp3', recognized(releases=('Anomaly', 'Best Of Lecrae')))
    assert d.album == 'Best Of Lecrae'


# ── when it is not reliable ─────────────────────────────────────────────────────

def test_an_unrecognized_generic_file_goes_to_review_with_no_speculative_metadata():
    d = decide(tags('0042', '', ''), 'Lecrae - Give In.mp3',
               Recognized(approved=False, attempted=True, reason_code='not_recognised'))
    assert d.status == 'review' and d.reason_code == 'not_recognised'
    assert (d.artist, d.title, d.album) == ('', '', '')
    assert {'title': 'Give In', 'artist': 'Lecrae', 'source': 'filename'}.items() <= dict(d.candidates[0]).items()


def test_a_generic_file_whose_recognition_was_not_possible_says_so():
    d = decide(tags('', '', ''), '0042.mp3', Recognized(approved=False, attempted=False))
    assert d.status == 'review' and d.reason_code == 'not_checked'
    assert decide(tags('', '', ''), '0042.mp3', None).reason_code == 'not_checked'


def test_a_weak_recognition_is_a_review_not_a_guess():
    d = decide(tags('', '', ''), 'x.mp3', Recognized(approved=False, attempted=True, title='Maybe', artist='Someone',
                                                    reason_code='weak_evidence'))
    assert d.status == 'review' and d.reason_code == 'weak_evidence' and d.title == ''
    assert any(c['title'] == 'Maybe' for c in map(dict, d.candidates)), 'the person sees what the evidence suggests'


def test_a_live_or_studio_version_that_cannot_be_told_apart_waits_for_a_person():
    for verdict in ('possible_conflict', 'confirmed_conflict'):
        d = decide(tags('', '', ''), 'x.mp3', recognized(verdict=verdict))
        assert d.status == 'review' and d.reason_code == 'version_unclear', verdict


def test_contradictory_tags_without_a_confident_recognition_wait_for_a_person():
    t = tags('Imagine', 'John Lennon', 'Imagine')
    d = decide(t, 'Queen - Bohemian Rhapsody.mp3', Recognized(approved=False, attempted=True, reason_code='not_recognised'))
    assert d.status == 'review' and d.reason_code == 'contradictory_tags'
    approved = decide(t, 'Queen - Bohemian Rhapsody.mp3', recognized(title='Bohemian Rhapsody', artist='Queen', releases=()))
    assert approved.status == 'organize' and approved.origin == 'recognition' and approved.title == 'Bohemian Rhapsody'
