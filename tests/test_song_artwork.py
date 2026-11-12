"""Song thumbnails: a verified album cover when one applies, otherwise the picture embedded in the file's own tags.

* Embedded pictures are read-only: the file is never opened for writing and is byte-identical afterwards.
* Only real JPEG or PNG data within a size limit is returned; anything else is treated as 'no picture'.
* The endpoint serves pictures only for files that are in the library index, never for an arbitrary path.
* Embedded art is not a verified release: the tracks API says which source a thumbnail comes from.
"""
import hashlib
import json
import threading

import pytest
from mutagen.id3 import APIC, ID3

from audio_fixtures import write_mp3
from meloshelf.application.webapp import create_server
from meloshelf.domain.models import Track
from meloshelf.infrastructure.embedded_art import read_embedded_art
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request

JPEG = b'\xff\xd8\xff\xe0' + b'J' * 700 + b'\xff\xd9'
PNG = b'\x89PNG\r\n\x1a\n' + b'P' * 700 + b'IEND\xaeB`\x82'
R1 = '11111111-1111-4111-8111-111111111111'


def with_picture(path, data, mime='image/jpeg', kind=3):
    tags = ID3(path)
    tags.add(APIC(encoding=3, mime=mime, type=kind, desc='cover', data=data))
    tags.save(path)


# ── reading ─────────────────────────────────────────────────────────────────────

def test_the_front_cover_picture_is_read_from_the_file_tags(tmp_path):
    song = write_mp3(tmp_path / 'a.mp3', tags={'title': 'T', 'artist': 'A'})
    with_picture(song, JPEG)
    assert read_embedded_art(song) == ('image/jpeg', JPEG)


def test_a_front_cover_is_preferred_over_other_picture_types(tmp_path):
    song = write_mp3(tmp_path / 'a.mp3', tags={'title': 'T'})
    with_picture(song, PNG, 'image/png', kind=4)     # back cover
    tags = ID3(song)
    tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='front', data=JPEG))
    tags.save(song)
    assert read_embedded_art(song) == ('image/jpeg', JPEG)


def test_a_file_without_a_picture_gives_nothing(tmp_path):
    assert read_embedded_art(write_mp3(tmp_path / 'a.mp3', tags={'title': 'T'})) is None


def test_a_file_that_is_not_audio_gives_nothing(tmp_path):
    junk = tmp_path / 'x.mp3'
    junk.write_bytes(b'this is not audio at all' * 50)
    assert read_embedded_art(junk) is None
    assert read_embedded_art(tmp_path / 'missing.mp3') is None


def test_picture_data_that_is_not_an_image_is_refused(tmp_path):
    song = write_mp3(tmp_path / 'a.mp3', tags={'title': 'T'})
    with_picture(song, b'<html>not an image</html>')
    assert read_embedded_art(song) is None


def test_a_declared_type_that_does_not_match_the_bytes_uses_the_real_type(tmp_path):
    song = write_mp3(tmp_path / 'a.mp3', tags={'title': 'T'})
    with_picture(song, PNG, mime='image/jpeg')
    assert read_embedded_art(song) == ('image/png', PNG)


def test_an_oversized_picture_is_refused(tmp_path):
    song = write_mp3(tmp_path / 'a.mp3', tags={'title': 'T'})
    with_picture(song, JPEG + b'x' * 5000)
    assert read_embedded_art(song, max_bytes=1000) is None


def test_reading_never_changes_the_file(tmp_path):
    song = write_mp3(tmp_path / 'a.mp3', tags={'title': 'T'})
    with_picture(song, JPEG)
    before = hashlib.sha256(song.read_bytes()).hexdigest()
    mtime = song.stat().st_mtime_ns
    read_embedded_art(song)
    assert hashlib.sha256(song.read_bytes()).hexdigest() == before and song.stat().st_mtime_ns == mtime


# ── HTTP ────────────────────────────────────────────────────────────────────────

def rel(release_id, title):
    return {'id': release_id, 'title': title, 'group_id': ''}


@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, artwork_fetch=lambda url: ('image', 'image/jpeg', JPEG))
    with_art = write_mp3(tmp_path / 'music' / 'with_art.mp3', tags={'title': 'With Art', 'artist': 'A', 'album': 'Loose'})
    with_picture(with_art, PNG, 'image/png')
    no_art = write_mp3(tmp_path / 'music' / 'no_art.mp3', tags={'title': 'No Art'})
    verified = write_mp3(tmp_path / 'music' / 'verified.mp3', tags={'title': 'Verified'})
    outside = write_mp3(tmp_path / 'outside' / 'not_indexed.mp3', tags={'title': 'Outside'})
    with_picture(outside, PNG, 'image/png')
    LibraryRepository(running.database).save_many([
        Track(str(with_art), 1, '.mp3', 1, 'With Art', 'A', 'Loose', '', 'tagged', has_picture=True),
        Track(str(no_art), 1, '.mp3', 1, 'No Art', 'A', 'Other', '', 'tagged'),
        Track(str(verified), 1, '.mp3', 1, 'Verified', 'B', 'Purple Rain', '', 'tagged')])
    MatchStore(running.database).put_result(str(verified), 1, 1, 'matched', {'candidates': [
        {'recording_id': 'r', 'title': 'T', 'release_details': [rel(R1, 'Purple Rain')]}]}, recording_status='AUTO_APPROVED')
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}', {'with_art': with_art, 'no_art': no_art,
                                                                     'outside': outside, 'verified': verified}
    running.shutdown()
    running.server_close()


def get_art(base, path):
    from urllib.parse import quote
    return request(base, 'GET', '/api/track-artwork?path=' + quote(str(path), safe=''))


def test_the_endpoint_serves_an_indexed_files_embedded_picture(server):
    _, base, files = server
    status, headers, data = get_art(base, files['with_art'])
    assert status == 200 and data == PNG and headers['Content-Type'] == 'image/png'
    assert headers['X-Content-Type-Options'] == 'nosniff'


def test_an_indexed_file_without_a_picture_is_a_404(server):
    _, base, files = server
    assert get_art(base, files['no_art'])[0] == 404


def test_a_file_outside_the_index_is_never_read_even_if_it_has_a_picture(server):
    _, base, files = server
    assert get_art(base, files['outside'])[0] == 404
    assert get_art(base, '../../etc/passwd')[0] == 404


def test_the_path_parameter_is_required(server):
    _, base, _ = server
    assert request(base, 'GET', '/api/track-artwork')[0] == 400


def test_the_tracks_api_tells_the_page_where_each_thumbnail_comes_from(server):
    _, base, files = server
    tracks = {t['title']: t for t in json.loads(request(base, 'GET', '/api/tracks?limit=50')[2])['tracks']}
    assert tracks['Verified']['art']['source'] == 'album' and tracks['Verified']['art']['verified'] is True
    assert tracks['Verified']['art']['url'].startswith('/api/album-cover?')
    assert tracks['With Art']['art']['source'] == 'embedded' and tracks['With Art']['art']['verified'] is False
    assert tracks['With Art']['art']['url'].startswith('/api/track-artwork?path=')


def test_the_page_lazy_loads_song_thumbnails_and_falls_back_to_nothing():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'class="thumb"' in page and 'loading="lazy"' in page
    assert "img.closest('.thumb')" in page, 'a thumbnail that cannot load is removed rather than shown broken'
    assert 'from the file tags' in page, 'embedded art is labelled as unverified'
