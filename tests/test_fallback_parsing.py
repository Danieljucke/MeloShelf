"""Filename parsing for metadata-only fallback. Parsing suggests text for a search; it never proves identity."""
import pytest

from meloshelf.domain.fallback import parse_filename


@pytest.mark.parametrize('name, artist, title, featured', [
    ('Darius_James_-_Flower_Pots.mp3', 'Darius James', 'Flower Pots', ()),
    ('1K_Phew_-_Dance_All_Night_[VIRAL_VIDEO].mp3', '1K Phew', 'Dance All Night', ()),
    ('Aaron_Cole_-_Making_History_(Official_Audio).mp3', 'Aaron Cole', 'Making History', ()),
    ('Ada - I Overcame Lyrics Medium.mp3.mp3', 'Ada', 'I Overcame', ()),
    ('ADA_-__JESUS_(_You_Are_Able).mp3', 'Ada', 'JESUS (You Are Able)', ()),
    ('Byron_Juane,_Canon,_Derek_Minor_-_Going_Up(128k).mp3', 'Byron Juane', 'Going Up', ('Canon', 'Derek Minor')),
    ('116_-_Big_Wave_feat._Lecrae,_Parris_Chariz,(128k).mp3', '116', 'Big Wave', ('Lecrae', 'Parris Chariz')),
    ('1K_Phew,_Aha_Gazelle_-_x10(128k).mp3', '1K Phew', 'x10', ('Aha Gazelle',)),
])
def test_artist_title_and_featured_artists_are_extracted_from_real_names(name, artist, title, featured):
    parsed = parse_filename(name)
    assert parsed.artist == artist
    assert parsed.title == title
    assert parsed.featured == featured


def test_a_name_without_a_separator_gives_a_title_and_no_artist_rather_than_a_guess():
    parsed = parse_filename('Bloc 5.mp3')
    assert parsed.title == 'Bloc 5' and parsed.artist == ''


def test_a_leading_track_number_is_recorded_and_removed_from_the_title():
    parsed = parse_filename('03 Whom Shall I Fear (God of Angel Armies).mp3')
    assert parsed.track_number == 3
    assert parsed.title == 'Whom Shall I Fear (God of Angel Armies)'
    assert parsed.artist == ''


def test_version_descriptors_are_kept_in_the_title_and_listed_as_structured_terms():
    parsed = parse_filename('1k_Phew,_WHATUPRG,_Tyree_-_401K_(Remix)-mc.m4a')
    assert '(Remix)' in parsed.title
    assert parsed.version_terms == ('remix',)
    assert parsed.extension == '.m4a'


def test_live_and_acoustic_descriptors_survive_normalisation():
    assert 'Live' in parse_filename('Band - Song (Live) (320).mp3').title
    assert parse_filename('Band - Song (Acoustic) (128k).mp3').version_terms == ('acoustic',)


def test_presentation_noise_is_removed_and_recorded_for_transparency():
    parsed = parse_filename('Andy_Mineo_-_Anxiety_(EP_Visual)(128k).mp3')
    assert parsed.title == 'Anxiety'
    assert '(128k)' in parsed.removed and any('EP' in r for r in parsed.removed)


def test_a_download_identifier_at_the_end_of_the_name_is_removed():
    parsed = parse_filename('Andy Mineo - Who Else (feat_ Social Club Misfits X7hb8rBKRNI.m4a')
    assert 'X7hb8rBKRNI' not in parsed.title
    assert parsed.title == 'Who Else'


def test_original_filename_is_kept_unchanged():
    name = 'ADA_-__JESUS_(_You_Are_Able).mp3'
    assert parse_filename(name).original == name


def test_unicode_names_are_normalised_without_losing_their_letters():
    parsed = parse_filename('Beyoncé - Café Song.mp3')
    assert parsed.artist == 'Beyoncé' and parsed.title == 'Café Song'


def test_a_garbled_artist_is_not_repaired_into_a_different_name():
    parsed = parse_filename('ada_i_m_rich_mp3_53841.mp3')
    assert parsed.artist == '' and parsed.title != ''
