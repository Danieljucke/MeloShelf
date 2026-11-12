"""The organization plan: where each file would go, decided without touching the filesystem.

Default layout:  Destination/Artist/Album/NN - Title.ext
Unknown album:   Destination/Artist/Unknown Album/Title.ext
Not reliable:    Destination/À vérifier/<original file name>
"""
import pytest

from meloshelf.domain.organize import (REVIEW_FOLDER, UNKNOWN_ALBUM, Decision, PlanEntry, canonical_spellings,
                                       plan_destinations, relative_destination, safe_component)


def organized(artist='Queen', album='A Night at the Opera', title='Bohemian Rhapsody', track_no=11, **extra):
    return Decision('organize', artist=artist, album=album, title=title, track_no=track_no, reason='tags reliable', **extra)


def review(reason='no reliable identity'):
    return Decision('review', reason=reason)


# ── Windows-safe names ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,expected', [
    ('AC/DC', 'AC-DC'),
    ('What? Why: Because*', 'What_ Why_ Because_'),
    ('  trailing dots...  ', 'trailing dots'),
    ('CON', 'CON_'),
    ('com1', 'com1_'),
    ('Nul.txt', 'Nul.txt_'),
    ('tab\tand\x00null', 'tab and null'),
    ('', 'Unknown'),
    ('...', 'Unknown'),
    ('Beyoncé – Déjà Vu', 'Beyoncé – Déjà Vu'),
    ('日本語のタイトル', '日本語のタイトル'),
])
def test_names_are_safe_for_windows_and_keep_real_characters(raw, expected):
    assert safe_component(raw) == expected


def test_a_long_name_is_shortened_without_cutting_the_extension_or_ending_in_a_space():
    name = safe_component('x' * 300, limit=100)
    assert len(name) <= 100 and not name.endswith((' ', '.'))


# ── where a file goes ───────────────────────────────────────────────────────────

def test_a_reliable_track_goes_to_artist_album_with_its_number():
    assert relative_destination(organized(), '.mp3', 'x.mp3') == 'Queen/A Night at the Opera/11 - Bohemian Rhapsody.mp3'


def test_the_track_number_is_two_digits_and_omitted_when_unknown():
    assert relative_destination(organized(track_no=3), '.flac', 'x').endswith('/03 - Bohemian Rhapsody.flac')
    assert relative_destination(organized(track_no=None), '.flac', 'x').endswith('/Bohemian Rhapsody.flac')


def test_an_unknown_album_is_a_named_category_and_never_an_invented_release():
    path = relative_destination(organized(album=''), '.mp3', 'x.mp3')
    assert path == f'Queen/{UNKNOWN_ALBUM}/11 - Bohemian Rhapsody.mp3' or path == f'Queen/{UNKNOWN_ALBUM}/Bohemian Rhapsody.mp3'
    assert UNKNOWN_ALBUM == 'Unknown Album'


def test_an_unresolved_file_keeps_its_original_name_under_the_review_folder():
    assert relative_destination(review(), '.mp3', '0042.mp3') == f'{REVIEW_FOLDER}/0042.mp3'
    assert REVIEW_FOLDER == 'À vérifier'


def test_featured_artists_do_not_create_a_second_artist_folder():
    path = relative_destination(organized(artist='Lecrae Feat. Crystal Nicole'), '.mp3', 'x')
    assert path.split('/')[0] == 'Lecrae'


def test_artist_spellings_of_one_artist_share_a_folder():
    spellings = canonical_spellings(['Lecrae', 'Lecrae', 'lecrae', 'LECRAE', 'Queen'])
    assert spellings['lecrae'] == 'Lecrae' and spellings['queen'] == 'Queen'


# ── the whole plan ──────────────────────────────────────────────────────────────

def entries(*items):
    return [PlanEntry(source=src, decision=decision, extension=ext) for src, decision, ext in items]


def test_two_different_files_with_the_same_destination_get_distinct_names_in_a_stable_order():
    plan = plan_destinations(entries(('/b/song.mp3', organized(), '.mp3'), ('/a/song.mp3', organized(), '.mp3')))
    by_source = {p.source: p.relative for p in plan}
    assert by_source['/a/song.mp3'] == 'Queen/A Night at the Opera/11 - Bohemian Rhapsody.mp3'
    assert by_source['/b/song.mp3'] == 'Queen/A Night at the Opera/11 - Bohemian Rhapsody (2).mp3'


def test_collisions_ignore_letter_case_because_windows_does():
    plan = plan_destinations(entries(('/a.mp3', organized(title='Song'), '.mp3'), ('/b.mp3', organized(title='SONG'), '.mp3')))
    assert len({p.relative.lower() for p in plan}) == 2


def test_review_files_with_the_same_original_name_do_not_overwrite_each_other():
    plan = plan_destinations(entries(('/x/0001.mp3', review(), '.mp3'), ('/y/0001.mp3', review(), '.mp3')))
    assert sorted(p.relative for p in plan) == [f'{REVIEW_FOLDER}/0001 (2).mp3', f'{REVIEW_FOLDER}/0001.mp3']


def test_the_plan_is_deterministic_whatever_the_input_order():
    items = [('/z/1.mp3', organized(title='A'), '.mp3'), ('/a/1.mp3', organized(title='A'), '.mp3'), ('/m/1.mp3', review(), '.mp3')]
    assert plan_destinations(entries(*items)) == plan_destinations(entries(*reversed(items)))


def test_a_destination_path_stays_within_the_windows_path_limit():
    long_decision = organized(artist='A' * 90, album='B' * 90, title='T' * 90)
    plan = plan_destinations(entries(('/a.mp3', long_decision, '.mp3')), destination_root='C:/Users/someone/Music/Organized')
    assert len('C:/Users/someone/Music/Organized/' + plan[0].relative) < 250
    assert plan[0].relative.endswith('.mp3')


def test_files_already_in_the_destination_are_not_reused_by_the_plan():
    taken = {'queen/a night at the opera/11 - bohemian rhapsody.mp3'}
    plan = plan_destinations(entries(('/a.mp3', organized(), '.mp3')), occupied=taken)
    assert plan[0].relative == 'Queen/A Night at the Opera/11 - Bohemian Rhapsody (2).mp3'
