"""The organizer through the real HTTP layer: validation, token protection, a full run, review and resolve."""
import json
import threading
import time

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_organize_service import SCRIPT, ScriptedRecognition, library, listing  # noqa: F401  (fixtures and helpers)
from test_qa_api import request


JPEG = bytes.fromhex('ffd8ffe000104a46494600') + b'' * 64 + bytes.fromhex('ffd9')
GROUP = '11111111-2222-3333-4444-555555555555'


def search(album, artist):
    if album == 'Anomaly':
        return [{'group_id': GROUP, 'title': 'Anomaly', 'artist': 'Lecrae', 'score': 100}]
    return []


@pytest.fixture
def app(tmp_path, library):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    opened, holder = [], {}

    def factory(offline, should_cancel):
        return holder['recognition']

    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={},
                            recognition_factory=factory, preflight=lambda offline: '',
                            explorer_opener=opened.append, folder_picker=lambda kind: str(tmp_path / 'picked'),
                            artwork_fetch=lambda url: ('image', 'image/jpeg', JPEG), release_group_search=search)
    from meloshelf.infrastructure.match_store import MatchStore
    holder['recognition'] = ScriptedRecognition(MatchStore(running.database), SCRIPT)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield {'server': running, 'base': f'http://127.0.0.1:{running.server_address[1]}', 'opened': opened,
           'source': library, 'dest': tmp_path / 'Organized'}
    running.shutdown()
    running.server_close()


def call(app, method, path, body=None, *, token=True):
    status, _, raw = request(app['base'], method, path, body, token=app['server'].token if token else None)
    return status, json.loads(raw.decode('utf-8'))


def finish(app, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, status = call(app, 'GET', '/api/organize/status')
        if status['phase'] in ('completed', 'completed_with_errors', 'failed', 'paused', 'cancelled'):
            return status
        time.sleep(0.1)
    raise AssertionError('the workflow did not finish')


def begin(app):
    status, body = call(app, 'POST', '/api/organize/start', {'source': str(app['source']), 'dest': str(app['dest'])})
    assert status == 202 and body['started'] is True, body
    return finish(app)


def test_a_destination_inside_the_source_is_refused_with_a_stable_code(app):
    status, body = call(app, 'POST', '/api/organize/preview',
                        {'source': str(app['source']), 'dest': str(app['source'] / 'Organized')})
    assert status == 400 and body['code'] == 'destination_inside_source'
    status, body = call(app, 'POST', '/api/organize/start', {'source': str(app['source']), 'dest': str(app['source'])})
    assert status == 400 and body['code'] == 'destination_is_source'
    assert not (app['source'] / 'Organized').exists()


def test_starting_needs_the_session_token(app):
    status, body = call(app, 'POST', '/api/organize/start', {'source': str(app['source']), 'dest': str(app['dest'])}, token=False)
    assert status == 403 and body['code'] == 'token_invalid'
    assert not app['dest'].exists()


def test_a_missing_source_folder_is_a_clear_error(app, tmp_path):
    status, body = call(app, 'POST', '/api/organize/start', {'source': str(tmp_path / 'nowhere'), 'dest': str(app['dest'])})
    assert status == 400 and body['code'] == 'folder_not_found'


def test_a_full_run_organizes_the_files_and_reports_honest_results(app):
    status = begin(app)
    assert status['phase'] == 'completed'
    assert status['summary']['total'] == 9 and status['summary']['review'] == 2 and status['summary']['organized'] == 7
    files = listing(app['dest'])
    assert 'Lecrae/Anomaly/01 - Give In.mp3' in files and 'À vérifier/0043.mp3' in files


def test_the_organized_library_lists_what_was_copied_by_artist_and_album(app):
    begin(app)
    status, library_view = call(app, 'GET', '/api/organize/library')
    assert status == 200 and library_view['dest'] == str(app['dest'])
    artists = {a['name']: a for a in library_view['artists']}
    assert set(artists) == {'Duo', 'Lecrae', 'Queen'}
    assert [t['file'] for t in {a['name']: a for a in artists['Lecrae']['albums']}['Anomaly']['tracks']] == [
        '01 - Give In.mp3', '02 - Fear.mp3', '03 - Broken.mp3']
    assert library_view['review_count'] == 2


def test_the_review_list_explains_each_unresolved_file_and_what_the_evidence_suggests(app):
    begin(app)
    status, review = call(app, 'GET', '/api/organize/review')
    items = {i['name']: i for i in review['items']}
    assert set(items) == {'0043.mp3', '0044.mp3'}
    assert items['0043.mp3']['reason_code'] == 'not_recognised'
    assert items['0044.mp3']['reason_code'] == 'version_unclear'
    assert items['0044.mp3']['candidates'][0]['title'] == 'Maybe Live'
    assert items['0043.mp3']['audio_url'].startswith('/api/audio?path=')


def test_resolving_a_file_then_organizing_again_moves_it_into_the_library(app):
    begin(app)
    path = str(app['dest'] / 'À vérifier' / '0043.mp3')
    status, body = call(app, 'POST', '/api/organize/resolve', {'path': path, 'title': 'Known Now', 'artist': 'Someone', 'album': 'Disc'})
    assert status == 200
    status = begin(app)
    files = listing(app['dest'])
    assert 'Someone/Disc/Known Now.mp3' in files and 'À vérifier/0043.mp3' not in files
    assert status['summary']['review'] == 1


def test_a_resolution_without_a_title_or_artist_is_refused(app):
    path = str(app['dest'] / 'À vérifier' / '0043.mp3')
    status, body = call(app, 'POST', '/api/organize/resolve', {'path': path, 'title': '', 'artist': 'x'})
    assert status == 400 and body['code'] == 'identity_required'


def test_open_folder_opens_only_the_configured_destination(app):
    status, body = call(app, 'POST', '/api/organize/open', {})
    assert status == 409 and body['code'] == 'nothing_organized_yet' and app['opened'] == []
    begin(app)
    status, body = call(app, 'POST', '/api/organize/open', {'path': 'C:/Windows'})
    assert status == 200 and app['opened'] == [str(app['dest'])], 'the request cannot choose another folder'


def test_the_folder_picker_returns_the_chosen_folder(app, tmp_path):
    status, body = call(app, 'POST', '/api/organize/pick', {'kind': 'dest'})
    assert status == 200 and body['path'] == str(tmp_path / 'picked')


def test_the_status_explains_a_recogniser_that_could_not_run(app):
    app['server'].jobs.preflight = lambda offline: 'fpcalc_unavailable'
    status = begin(app)
    assert status['recognition_blocker'] == 'fpcalc_unavailable'
    assert status['blocker']['code'] == 'fpcalc_unavailable' and status['blocker']['label'] and status['blocker']['hint']
    assert status['summary']['review'] == 3, 'what could not be identified waits for review'


def album_of(view, artist, album):
    found = {a['name']: a for a in view['artists']}[artist]
    return {al['name']: al for al in found['albums']}[album]


def test_the_library_labels_each_album_cover_honestly(app):
    begin(app)
    _, view = call(app, 'GET', '/api/organize/library')
    assert album_of(view, 'Lecrae', 'Anomaly')['art']['state'] == 'plausible', 'found by a catalogue search, not verified'
    assert album_of(view, 'Queen', 'A Night at the Opera')['art']['state'] == 'missing'


def test_a_suggested_cover_can_be_previewed_then_accepted(app):
    begin(app)
    status, _, raw = request(app['base'], 'GET', '/api/organize/art/image?album=Anomaly&artist=Lecrae')
    assert status == 200 and raw == JPEG
    status, _, _ = request(app['base'], 'GET', '/api/organize/art/image?album=Nothing&artist=Nobody')
    assert status == 404
    status, body = call(app, 'POST', '/api/organize/art/accept', {'album': 'Anomaly', 'artist': 'Lecrae'})
    assert status == 200 and body['accepted'] is True
    _, view = call(app, 'GET', '/api/organize/library')
    assert album_of(view, 'Lecrae', 'Anomaly')['art']['state'] == 'confirmed'
    status, body = call(app, 'POST', '/api/organize/art/accept', {'album': 'Nothing', 'artist': 'Nobody'})
    assert status == 404 and body['code'] == 'no_album_cover'


def test_accepting_a_cover_needs_the_session_token(app):
    status, body = call(app, 'POST', '/api/organize/art/accept', {'album': 'Anomaly', 'artist': 'Lecrae'}, token=False)
    assert status == 403


def test_a_cover_request_cannot_read_outside_the_organized_folder(app, tmp_path):
    from mutagen.id3 import APIC, ID3
    from audio_fixtures import write_mp3
    begin(app)
    secret = write_mp3(tmp_path / 'Secret' / 'x.mp3', seed=9, tags={'title': 'T'})
    tags = ID3(secret)
    tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='c', data=JPEG))
    tags.save(secret)
    for album, artist in (('Secret', '..'), ('..', '..'), ('.', '.'), ('../Secret', 'x'), ('Secret', '../')):
        status, _, _ = request(app['base'], 'GET', '/api/organize/art/image?album=' + album.replace('/', '%2F') + '&artist=' + artist.replace('/', '%2F'))
        assert status == 404, (album, artist)


def test_an_accepted_cover_is_added_to_the_albums_copies(app):
    from mutagen.id3 import ID3
    begin(app)
    copy = app['dest'] / 'Lecrae' / 'Anomaly' / '01 - Give In.mp3'
    assert not ID3(copy).getall('APIC')
    status, body = call(app, 'POST', '/api/organize/art/accept', {'album': 'Anomaly', 'artist': 'Lecrae'})
    assert status == 200 and body['covers_added'] >= 3
    assert all(ID3(app['dest'] / 'Lecrae' / 'Anomaly' / name).getall('APIC') for name in ('01 - Give In.mp3', '02 - Fear.mp3', '03 - Broken.mp3'))
    assert not ID3(app['dest'] / 'Queen' / 'A Night at the Opera' / '11 - Bohemian Rhapsody.mp3').getall('APIC'), 'other albums are untouched'


def test_accepting_a_fallback_provider_cover_keeps_its_source_and_never_writes_it_into_the_copies(app, monkeypatch):
    from mutagen.id3 import ID3
    from test_art_providers_integration import Extra
    server = app['server']
    monkeypatch.setattr(server.artwork, 'cover', lambda *args, **kwargs: None)          # Cover Art Archive has nothing
    server.album_art._fallbacks = lambda: [Extra('fanart.tv', data=JPEG)]
    begin(app)
    status, body = call(app, 'POST', '/api/organize/art/accept', {'album': 'Anomaly', 'artist': 'Lecrae'})
    assert status == 200 and body['accepted'] is True and body['covers_added'] == 0
    assert not any(ID3(app['dest'] / 'Lecrae' / 'Anomaly' / name).getall('APIC') for name in ('01 - Give In.mp3', '02 - Fear.mp3', '03 - Broken.mp3'))
    _, view = call(app, 'GET', '/api/organize/library')
    art = album_of(view, 'Lecrae', 'Anomaly')['art']
    assert art['state'] == 'confirmed' and art['chosen'] is True and art['provenance'] == 'fanart.tv' and art['terms_url'] and art['license']


def test_bulk_resolve_validates_then_saves_and_moves_everything_valid_in_one_request(app):
    begin(app)
    items = [{'path': str(app['dest'] / 'À vérifier' / '0043.mp3'), 'title': 'Known Now', 'artist': 'Someone', 'album': 'Disc'},
             {'path': str(app['dest'] / 'À vérifier' / '0044.mp3'), 'title': 'Needs Artist', 'artist': ''}]
    status, preview = call(app, 'POST', '/api/organize/resolve-many', {'items': items, 'dry_run': True})
    assert status == 200 and preview['ready'] == 1 and preview['incomplete'] == 1
    assert 'Someone' in preview['items'][0]['to'] and not (app['dest'] / 'Someone').exists(), 'a preview moves nothing'
    status, body = call(app, 'POST', '/api/organize/resolve-many', {'items': items})
    assert status == 202 and body['started'] is True
    deadline = time.time() + 60
    while time.time() < deadline:
        _, st = call(app, 'GET', '/api/organize/status')
        if st['phase'] in ('completed', 'completed_with_errors', 'failed') and st['batch']:
            break
        time.sleep(0.2)
    assert st['batch']['saved'] == 1 and st['batch']['moved'] == 1
    assert [s['code'] for s in st['batch']['skipped']] == ['artist_required']
    assert 'Someone/Disc/Known Now.mp3' in listing(app['dest'])
    assert 'À vérifier/0044.mp3' in listing(app['dest'])


def test_bulk_resolve_needs_the_token_and_a_list(app):
    status, _ = call(app, 'POST', '/api/organize/resolve-many', {'items': [{'path': 'x'}]}, token=False)
    assert status == 403
    status, body = call(app, 'POST', '/api/organize/resolve-many', {'items': []})
    assert status == 400 and body['code'] == 'items_required'


def test_consolidation_endpoints_preview_refuse_without_confirmation_and_then_merge_folders(app):
    from mutagen.id3 import ID3
    from audio_fixtures import write_mp3
    begin(app)
    write_mp3(app['dest'] / 'Lecrae, Someone' / 'Anomaly' / '09 - Extra.mp3', seed=77,
              tags={'title': 'Extra', 'artist': 'Lecrae, Someone', 'album': 'Anomaly', 'tracknumber': '9'})
    status, plan = call(app, 'POST', '/api/organize/consolidate/preview', {'scan': True})
    assert status == 200 and plan['to_move'] == 1 and plan['moves'][0]['relative'] == 'Lecrae/Anomaly/09 - Extra.mp3'
    assert (app['dest'] / 'Lecrae, Someone').exists(), 'a preview moves nothing'
    status, body = call(app, 'POST', '/api/organize/consolidate/apply', {})
    assert status == 400 and body['code'] == 'confirmation_required'
    status, body = call(app, 'POST', '/api/organize/consolidate/apply', {'confirm': True})
    assert status == 200 and body['moved'] == 1
    assert (app['dest'] / 'Lecrae' / 'Anomaly' / '09 - Extra.mp3').exists() and not (app['dest'] / 'Lecrae, Someone').exists()
    status, undone = call(app, 'POST', '/api/organize/consolidate/undo', {'run_id': body['run_id']})
    assert status == 200 and undone['moved'] == 1 and (app['dest'] / 'Lecrae, Someone' / 'Anomaly' / '09 - Extra.mp3').exists()
    assert call(app, 'POST', '/api/organize/consolidate/undo', {'run_id': 'nope'})[0] == 404
    assert ID3(app['dest'] / 'Lecrae, Someone' / 'Anomaly' / '09 - Extra.mp3')['TPE1'].text[0] == 'Lecrae, Someone'
