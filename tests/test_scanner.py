from pathlib import Path
import os
import pytest
from meloshelf.application.scanner import LibraryScanner
from meloshelf.domain.models import Track, classify_track


def test_classification_requires_real_tags():
    assert classify_track(title='000123', artist='', album='') == 'unidentified'
    assert classify_track(title='Unknown Artist - Track 01', artist='', album='') == 'unidentified'
    assert classify_track(title='Hello', artist='Adele', album='') == 'review'
    assert classify_track(title='Hello', artist='Adele', album='25') == 'tagged'
    assert classify_track(title='Hello', artist='Unknown Artist', album='25') == 'review'


def test_scans_supported_files_recursively_without_writing(tmp_path):
    (tmp_path / 'sub').mkdir()
    mp3 = tmp_path / 'sub' / '000123.mp3'
    mp3.write_bytes(b'not a real mp3')
    (tmp_path / 'sub' / 'notes.txt').write_text('do not read')
    result = list(LibraryScanner(tag_reader=lambda path: {}).iter_tracks(tmp_path))
    assert len(result) == 1
    assert result[0].path == str(mp3.resolve())
    assert result[0].status == 'unidentified'
    assert mp3.read_bytes() == b'not a real mp3'


def test_ignores_symlink_to_avoid_cycles(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'one.flac').write_bytes(b'test')
    try:
        (tmp_path / 'a' / 'loop').symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unsupported here')
    assert len(list(LibraryScanner(tag_reader=lambda path: {}).iter_tracks(tmp_path))) == 1


def test_destination_tree_can_be_excluded(tmp_path):
    (tmp_path / 'new').mkdir()
    (tmp_path / 'source.mp3').write_bytes(b'a')
    (tmp_path / 'new' / 'already.flac').write_bytes(b'b')
    tracks = list(LibraryScanner(tag_reader=lambda path: {}).iter_tracks(tmp_path, excluded_paths=[tmp_path / 'new']))
    assert [Path(t.path).name for t in tracks] == ['source.mp3']


def test_invalid_root_raises(tmp_path):
    with pytest.raises((NotADirectoryError, FileNotFoundError)):
        list(LibraryScanner(tag_reader=lambda path: {}).iter_tracks(tmp_path / 'missing'))


def test_tags_are_kept_separate_from_filename_guess(tmp_path):
    path = tmp_path / '812382.mp3'
    path.write_bytes(b'x')
    tags = {'title':'Real Title', 'artist':'Real Artist','album':'Original Album','date':'2008'}
    track = next(LibraryScanner(tag_reader=lambda _: tags).iter_tracks(tmp_path))
    assert track.title == 'Real Title'
    assert track.status == 'tagged'
    assert track.year == '2008'


def test_scan_can_be_cancelled_between_files(tmp_path):
    for i in range(6):
        (tmp_path / f'{i}.mp3').write_bytes(b'x')
    seen = 0
    def reading(path):
        nonlocal seen
        seen += 1
        return {}
    scanner = LibraryScanner(tag_reader=reading)
    tracks = list(scanner.iter_tracks(tmp_path, should_stop=lambda: seen >= 2))
    assert len(tracks) == 2
