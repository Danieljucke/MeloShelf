"""QA workspace API: token-protected writes, read-only audio for indexed files only, and CSV export."""
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_labels import candidate, seed


@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, connection_tester=lambda target, config: {'ok': True, 'state': 'valid', 'message': 'x'})
    audio = tmp_path / 'song.mp3'
    audio.write_bytes(bytes(range(256)) * 40)  # 10 KiB of stand-in audio bytes
    seed(running.database, str(audio), [candidate('rec-1', 'Song', 'Artist')], 'AUTO_APPROVED', title='Song', artist='Artist')
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}', audio
    running.shutdown()
    running.server_close()


def request(base, method, path, body=None, *, token=None, headers=None):
    import urllib.error
    import urllib.request
    all_headers = {'Content-Type': 'application/json', **(headers or {})}
    if token is not None:
        all_headers['X-MeloShelf-Token'] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_progress_and_next_item_describe_the_queue(server):
    running, base, audio = server
    status, _, body = request(base, 'GET', '/api/qa/progress')
    progress = json.loads(body)
    assert status == 200 and progress['proposals'] == 1 and progress['remaining'] == 1
    status, _, body = request(base, 'GET', '/api/qa/next')
    item = json.loads(body)
    assert item['path'] == str(audio) and item['candidates'][0]['recording_id'] == 'rec-1'
    assert item['label'] is None and item['filename'] == 'song.mp3'


def test_a_label_requires_the_session_token(server):
    running, base, audio = server
    status, _, _ = request(base, 'POST', '/api/qa/label', {'path': str(audio), 'label': 'correct'})
    assert status == 403


def test_a_valid_label_is_saved_and_the_queue_moves_on(server):
    running, base, audio = server
    status, _, body = request(base, 'POST', '/api/qa/label', {'path': str(audio), 'label': 'correct'}, token=running.token)
    assert status == 200 and json.loads(body)['saved'] is True
    assert json.loads(request(base, 'GET', '/api/qa/next')[2])['path'] is None


def test_an_invalid_label_is_rejected_with_a_reason(server):
    running, base, audio = server
    status, _, body = request(base, 'POST', '/api/qa/label', {'path': str(audio), 'label': 'perhaps'}, token=running.token)
    assert status == 400 and 'correct' in json.loads(body)['error']


def test_audio_is_streamed_for_indexed_library_files_with_seek_support(server):
    running, base, audio = server
    status, headers, body = request(base, 'GET', '/api/audio?path=' + str(audio).replace('\\', '/'),
                                    headers={'Range': 'bytes=100-199'})
    assert status == 206 and len(body) == 100 and body == audio.read_bytes()[100:200]
    assert headers['Accept-Ranges'] == 'bytes' and headers['Content-Range'] == 'bytes 100-199/10240'
    assert headers['Content-Type'] == 'audio/mpeg'


def test_audio_is_never_served_for_a_file_outside_the_index(server, tmp_path):
    running, base, audio = server
    secret = tmp_path / 'other.mp3'
    secret.write_bytes(b'not in the library')
    assert request(base, 'GET', '/api/audio?path=' + str(secret).replace('\\', '/'))[0] == 404


def test_audio_request_for_a_missing_range_returns_the_whole_file(server):
    running, base, audio = server
    status, _, body = request(base, 'GET', '/api/audio?path=' + str(audio).replace('\\', '/'))
    assert status == 200 and body == audio.read_bytes()


def test_csv_export_has_one_row_per_proposal_with_the_label_columns(server):
    running, base, audio = server
    status, headers, body = request(base, 'GET', '/api/qa/export')
    assert status == 200 and headers['Content-Type'].startswith('text/csv')
    header = body.decode('utf-8-sig').splitlines()[0]
    for column in ('path', 'proposed_recording_id', 'meloshelf_status', 'label', 'corrected_recording_id'):
        assert column in header


def test_statistics_count_the_library_not_a_scan_total(server):
    running, base, audio = server
    stats = json.loads(request(base, 'GET', '/api/stats')[2])
    assert stats['total'] == 1


def test_the_report_states_sample_sizes_and_never_counts_unlabelled_items_as_correct(server):
    running, base, audio = server
    report = json.loads(request(base, 'GET', '/api/qa/report')[2])
    assert report['auto']['labelled'] == 0 and report['auto']['precision'] is None
    assert report['auto']['sufficient_sample'] is False
    request(base, 'POST', '/api/qa/label', {'path': str(audio), 'label': 'correct'}, token=running.token)
    report = json.loads(request(base, 'GET', '/api/qa/report')[2])
    assert report['auto']['correct'] == 1 and report['auto']['precision'] == 1.0


def test_next_honours_skipped_items_so_next_moves_forward(server):
    running, base, audio = server
    other = audio.with_name('other.mp3')
    other.write_bytes(b'\x00' * 100)
    from test_qa_labels import seed as seed_row
    seed_row(running.database, str(other), [candidate('rec-2', 'Other', 'Artist')], 'NEEDS_REVIEW')
    first = json.loads(request(base, 'GET', '/api/qa/next')[2])['path']
    second = json.loads(request(base, 'GET', '/api/qa/next?skip=' + first.replace('\\', '/'))[2])['path']
    assert second != first and second is not None


def test_csv_export_neutralises_spreadsheet_formulas_in_user_supplied_text(server, tmp_path):
    # Tags come from users' audio files. A title such as =HYPERLINK(...) must not run when the CSV is opened.
    running, base, audio = server
    hostile = tmp_path / 'hostile.mp3'
    hostile.write_bytes(b'\x00' * 50)
    seed(running.database, str(hostile), [candidate('rec-h', '=HYPERLINK("http://x","a")', '+cmd|x')],
         'NEEDS_REVIEW', title='@SUM(1)', artist='-2+3')
    status, _, body = request(base, 'GET', '/api/qa/export')
    import csv, io
    rows = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
    row = [r for r in rows if r['path'].endswith('hostile.mp3')][0]
    for cell in (row['local_title'], row['local_artist'], row['proposed_title'], row['proposed_artist']):
        assert not cell.startswith(('=', '+', '-', '@')), cell
    assert row['local_title'] == "'@SUM(1)"


def test_a_listener_who_disconnects_mid_stream_does_not_produce_a_server_traceback(server):
    import socket
    import struct
    import time
    from urllib.parse import urlparse
    running, base, audio = server
    audio.write_bytes(b'\0' * (48 * 1024 * 1024))
    failures = []
    running.handle_error = lambda request, address: failures.append(address)
    host = urlparse(base)
    client = socket.create_connection((host.hostname, host.port), timeout=10)
    client.sendall(f'GET /api/audio?path={audio.as_posix()} HTTP/1.1\r\nHost: 127.0.0.1:{host.port}\r\n\r\n'.encode())
    assert client.recv(4096).startswith(b'HTTP/1.0 200')
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))  # close with a reset, like a seek
    client.close()
    time.sleep(1.5)
    assert failures == [], 'a closed player is normal, not an error'
    status, _, body = request(base, 'GET', '/api/audio?path=' + audio.as_posix(), headers={'Range': 'bytes=0-9'})
    assert status == 206 and len(body) == 10, 'the server keeps serving afterwards'


def test_changing_the_fpcalc_location_applies_without_restarting(server, tmp_path):
    running, base, _audio = server
    exe = tmp_path / 'tools' / 'fpcalc.exe'
    exe.parent.mkdir()
    exe.write_bytes(b'MZ')
    status, _, _ = request(base, 'POST', '/api/config', {'fpcalc_path': str(exe)}, token=running.token)
    assert status == 200
    assert running.fingerprinter.executable == str(exe), 'the running fingerprinter uses the new location at once'
