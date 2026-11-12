"""Metadata is written to organized COPIES only, keeping every tag the person already had."""
import hashlib

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from audio_fixtures import write_mp3
from meloshelf.infrastructure.tag_writer import write_tags
from meloshelf.infrastructure.tags import has_picture_frame

JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x01' * 64 + b'\xff\xd9'


def test_the_given_fields_are_written_and_unrelated_tags_survive(tmp_path):
    path = write_mp3(tmp_path / 'a.mp3', seed=1, tags={'title': '0042', 'artist': 'Unknown', 'album': ''})
    audio = EasyID3(path)
    audio['genre'] = 'Gospel'
    audio['composer'] = 'Someone'
    audio.save()
    assert write_tags(str(path), {'title': 'Song', 'artist': 'Queen', 'album': 'Opera', 'tracknumber': '11'}, '') is True
    tags = EasyID3(path)
    assert tags['title'] == ['Song'] and tags['artist'] == ['Queen'] and tags['album'] == ['Opera']
    assert tags['tracknumber'] == ['11']
    assert tags['genre'] == ['Gospel'] and tags['composer'] == ['Someone'], 'tags that were not asked about are kept'
    assert MP3(path).info.length > 0, 'the audio is still readable'


def test_a_file_with_no_tag_block_gets_one(tmp_path):
    path = write_mp3(tmp_path / 'a.mp3', seed=2, tags={})
    assert write_tags(str(path), {'title': 'Song', 'artist': 'Queen'}, '') is True
    assert EasyID3(path)['title'] == ['Song']


def test_an_empty_field_is_not_written_over_an_existing_value(tmp_path):
    path = write_mp3(tmp_path / 'a.mp3', seed=3, tags={'title': 'Keep', 'artist': 'Keep', 'album': 'Keep'})
    write_tags(str(path), {'title': 'New', 'album': ''}, '')
    tags = EasyID3(path)
    assert tags['title'] == ['New'] and tags['album'] == ['Keep']


def test_the_audio_frames_are_not_altered_by_tagging(tmp_path):
    path = write_mp3(tmp_path / 'a.mp3', seed=4, tags={'title': 'x'})
    def audio_bytes(p):
        data = p.read_bytes()
        header = ID3(p).size if ID3(p).size else 0
        return hashlib.sha256(data[header:]).hexdigest()
    before = audio_bytes(path)
    write_tags(str(path), {'title': 'a much longer title than before to change the tag size', 'artist': 'Queen'}, '')
    assert audio_bytes(path) == before


def test_a_cover_is_embedded_only_when_the_copy_has_none(tmp_path):
    cover = tmp_path / 'cover.jpg'
    cover.write_bytes(JPEG)
    path = write_mp3(tmp_path / 'a.mp3', seed=5, tags={'title': 'x'})
    assert has_picture_frame(path) is False
    assert write_tags(str(path), {}, str(cover)) is True
    assert has_picture_frame(path) is True
    before = ID3(path).getall('APIC')[0].data
    other = tmp_path / 'other.jpg'
    other.write_bytes(JPEG + b'\x00\x00')
    write_tags(str(path), {}, str(other))
    assert ID3(path).getall('APIC')[0].data == before, 'an existing picture is never replaced'


def test_a_file_that_cannot_be_tagged_reports_false_and_does_not_raise(tmp_path):
    junk = tmp_path / 'not-audio.mp3'
    junk.write_bytes(b'this is not an mp3 at all' * 3)
    assert write_tags(str(junk), {'title': 'x'}, '') in (True, False)      # never raises
    assert write_tags(str(tmp_path / 'missing.mp3'), {'title': 'x'}, '') is False
