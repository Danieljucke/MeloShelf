"""Metadata cleanup API: the server recomputes proposals itself, requires the session token, and never touches audio."""
import hashlib
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_metadata_store import seed_track
from test_qa_api import request


@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(),
                            environ={}, connection_tester=lambda target, config: {'ok': True, 'state': 'valid', 'message': 'x'})
    audio = tmp_path / 'song.mp3'
    audio.write_bytes(b'AUDIO' * 200)
    seed_track(running.database, str(audio), title='Song', artist='Wrong Artist', album='Other', cand_artist='Right Artist')
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}', audio
    running.shutdown()
    running.server_close()


def test_the_groups_endpoint_lists_correction_groups_with_their_keys(server):
    running, base, audio = server
    groups = json.loads(request(base, 'GET', '/api/metadata/groups')[2])['groups']
    artist = [g for g in groups if g['original'] == 'Wrong Artist'][0]
    assert artist['proposed'] == 'Right Artist' and artist['key']


def test_a_decision_needs_the_session_token(server):
    running, base, audio = server
    key = json.loads(request(base, 'GET', '/api/metadata/groups')[2])['groups'][0]['key']
    assert request(base, 'POST', '/api/metadata/decision', {'key': key, 'action': 'approve'})[0] == 403


def test_the_server_uses_its_own_proposal_not_one_supplied_by_the_browser(server):
    running, base, audio = server
    group = [g for g in json.loads(request(base, 'GET', '/api/metadata/groups')[2])['groups'] if g['original'] == 'Wrong Artist'][0]
    status, _, body = request(base, 'POST', '/api/metadata/decision',
                              {'key': group['key'], 'action': 'approve', 'proposed': 'Forged Artist'}, token=running.token)
    assert status == 200
    stored = json.loads(request(base, 'GET', '/api/metadata/decisions')[2])['decisions'][0]
    assert stored['proposed'] == 'Right Artist' and stored['status'] == 'approved'


def test_an_unknown_group_key_is_refused(server):
    running, base, audio = server
    status, _, body = request(base, 'POST', '/api/metadata/decision', {'key': 'nope', 'action': 'approve'}, token=running.token)
    assert status == 404


def test_approving_a_shared_pattern_without_a_replacement_is_refused_with_a_reason(server):
    running, base, audio = server
    folder = str(audio.parent).replace('\\', '/') + '/SS19'
    for i, cand in enumerate(['CASS', 'KB', 'DJ Mykael V', '1K Phew']):
        seed_track(running.database, f'{folder}/t{i}.mp3', title=f't{i}', artist='Tedashii/Steven Malcom',
                   album='SS19', cand_artist=cand)
    groups = json.loads(request(base, 'GET', '/api/metadata/groups')[2])['groups']
    shared = [g for g in groups if g['folder'].endswith('/SS19') and g['field'] == 'artist'][0]
    assert shared['proposed'] == ''
    status, _, body = request(base, 'POST', '/api/metadata/decision', {'key': shared['key'], 'action': 'approve'}, token=running.token)
    assert status == 400 and 'individually' in json.loads(body)['error']


def test_approving_never_changes_the_audio_file(server):
    running, base, audio = server
    before = hashlib.sha256(audio.read_bytes()).hexdigest()
    group = [g for g in json.loads(request(base, 'GET', '/api/metadata/groups')[2])['groups'] if g['original'] == 'Wrong Artist'][0]
    request(base, 'POST', '/api/metadata/decision', {'key': group['key'], 'action': 'approve'}, token=running.token)
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == before


def test_a_confirmed_correction_is_offered_for_a_new_track_in_the_same_folder_through_the_api(server):
    running, base, audio = server
    group = [g for g in json.loads(request(base, 'GET', '/api/metadata/groups')[2])['groups'] if g['original'] == 'Wrong Artist'][0]
    request(base, 'POST', '/api/metadata/decision', {'key': group['key'], 'action': 'approve'}, token=running.token)
    seed_track(running.database, str(audio.parent / 'new.mp3'), title='New', artist='Wrong Artist', album='Other', cand_artist=None)
    groups = json.loads(request(base, 'GET', '/api/metadata/groups')[2])['groups']
    again = [g for g in groups if g['original'] == 'Wrong Artist'][0]
    assert again['previously_confirmed'] == 'Right Artist' and again['individual_review'] is True
