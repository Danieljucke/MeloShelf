from meloshelf.infrastructure.tags import read_tags


def test_corrupted_audio_returns_empty_tags(tmp_path):
    sample = tmp_path / 'broken.mp3'
    sample.write_bytes(b'not audio')
    assert read_tags(sample) == {}
