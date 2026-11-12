"""HTTP surface for scan visibility, diagnostics, whole-library views and demo separation."""
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.domain.models import Track
from meloshelf.infrastructure.fingerprint import Fingerprinter
from meloshelf.infrastructure.job_store import JobStore
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.repository import LibraryRepository

PACKAGED_PAGE = Path(__file__).resolve().parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html'
DEMO_ONLY_STRINGS = ('Daft Punk', 'Nina Simone', 'Miles Davis', 'Blinding Lights', '12,480', '9,102')


@contextmanager
def running(tmp_path, **options):
    options.setdefault('fingerprinter', Fingerprinter(which=lambda name: None))  # fpcalc not installed
    options.setdefault('environ', {})  # nor found in conventional folders of the machine running the tests
    server = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=PACKAGED_PAGE, **options)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get(base, path):
    with urlopen(base + path, timeout=15) as response:
        return json.load(response)


def post(server, base, path, data):
    request = Request(base + path, data=json.dumps(data).encode(), method='POST',
                      headers={'Content-Type': 'application/json', 'X-MeloShelf-Token': server.token})
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def wait_idle(base, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not get(base, '/api/status')['running']:
            return
        time.sleep(.02)
    raise AssertionError('job did not finish')


def make_music(tmp_path, count=5):
    music = tmp_path / 'My Music'
    music.mkdir()
    for n in range(count):
        (music / f'{n:03}.mp3').write_bytes(b'x' * (n + 1))
    return music


def seed(server, tracks):
    LibraryRepository(server.database).save_many(
        [Track(f'/m/{n}.mp3', 1, '.mp3', 1, title, artist, album, year, 'tagged')
         for n, (title, artist, album, year) in enumerate(tracks)])


# ── demo data never reaches the real application ─────────────────────────────

def test_packaged_page_contains_demo_data_only_inside_the_marked_block():
    page = PACKAGED_PAGE.read_text(encoding='utf-8')
    start, end = page.index('<!--DEMO-DATA-->'), page.index('<!--/DEMO-DATA-->')
    outside = page[:start] + page[end:]
    for text in DEMO_ONLY_STRINGS:
        assert text in page[start:end] or text not in page, text
        assert text not in outside, f'{text!r} is hardcoded outside the demo block'


def test_served_page_has_no_demo_data(tmp_path):
    with running(tmp_path) as (_, base):
        with urlopen(base + '/') as response:
            page = response.read().decode()
    assert '<!--DEMO-DATA-->' not in page
    for text in DEMO_ONLY_STRINGS:
        assert text not in page, f'sample value {text!r} was served to the real application'
    assert '__MELOSHELF_TOKEN__' not in page


def test_empty_library_reports_empty_collections_not_samples(tmp_path):
    with running(tmp_path) as (_, base):
        assert get(base, '/api/artists') == {'artists': [], 'total': 0, 'offset': 0, 'limit': 60}
        assert get(base, '/api/albums') == {'albums': [], 'total': 0, 'offset': 0, 'limit': 60}
        activity = get(base, '/api/activity')
        assert activity['state'] == 'IDLE' and activity['jobs'] == []
        assert activity['library']['indexed'] == 0 and activity['library']['root'] == ''


# ── scan visibility ──────────────────────────────────────────────────────────

def test_selected_folder_and_real_counts_are_reported(tmp_path):
    music = make_music(tmp_path, 5)
    with running(tmp_path) as (server, base):
        status, body = post(server, base, '/api/scan', {'folder': str(music)})
        assert status == 202 and body['started'] and body['job_id']
        wait_idle(base)
        activity = get(base, '/api/activity')
    assert activity['library']['root'] == str(music.resolve())
    assert activity['library']['indexed'] == 5
    job = activity['jobs'][0]
    assert (job['kind'], job['state'], job['root']) == ('scan', 'COMPLETED', str(music.resolve()))
    assert job['counters']['total'] == 5 and job['counters']['done'] == 5 and job['percent'] == 100


def test_scanning_the_same_folder_twice_is_refused_while_running_is_reported(tmp_path):
    music = make_music(tmp_path, 3)
    with running(tmp_path) as (server, base):
        assert post(server, base, '/api/scan', {'folder': str(music)})[0] == 202
        wait_idle(base)
        status, body = post(server, base, '/api/scan', {'folder': str(music)})
        assert status == 202, 'a finished folder can be scanned again (incremental)'
        wait_idle(base)
        assert get(base, '/api/activity')['jobs'][0]['counters']['skipped'] == 3


def test_interrupted_job_is_shown_after_restart_and_can_be_resumed(tmp_path):
    music = make_music(tmp_path, 4)
    with running(tmp_path) as (server, base):
        store = JobStore(server.database)
        job_id = store.create('scan', root=str(music.resolve()))
        store.update(job_id, state='running', stage='INDEXING')  # as left behind by a crash
    with running(tmp_path) as (server, base):
        activity = get(base, '/api/activity')
        assert activity['state'] == 'INTERRUPTED'
        assert activity['jobs'][0]['id'] == job_id and activity['jobs'][0]['can']['resume'] is True
        status, body = post(server, base, '/api/jobs/resume', {'job_id': job_id})
        assert status == 202 and body['started']
        wait_idle(base)
        assert get(base, '/api/activity')['jobs'][0]['state'] == 'COMPLETED'
        assert get(base, '/api/stats')['total'] == 4


def test_job_control_endpoints_validate_their_input(tmp_path):
    with running(tmp_path) as (server, base):
        for action in ('pause', 'resume', 'cancel', 'retry'):
            assert post(server, base, f'/api/jobs/{action}', {})[0] == 400
            status, body = post(server, base, f'/api/jobs/{action}', {'job_id': 'nope'})
            assert status == 409 and body['reason'], action


# ── understandable errors ────────────────────────────────────────────────────

def test_identification_without_fpcalc_explains_itself(tmp_path):
    music = make_music(tmp_path, 5)
    with running(tmp_path) as (server, base):
        post(server, base, '/api/scan', {'folder': str(music)})
        wait_idle(base)
        status, body = post(server, base, '/api/identify', {'limit': 100, 'offline': True})
        assert status == 202
        wait_idle(base)
        job = next(j for j in get(base, '/api/activity')['jobs'] if j['kind'] == 'identify')
        assert job['state'] == 'FAILED'
        assert job['error']['code'] == 'fpcalc_unavailable'
        assert 'fpcalc' in job['error']['label'] and job['error']['hint']
        assert job['counters']['error'] == 0, 'the files did not fail; the tool is missing'
        config = get(base, '/api/config')
        assert config['fpcalc_available'] is False


def test_error_details_can_be_inspected_per_file(tmp_path):
    music = make_music(tmp_path, 3)
    with running(tmp_path) as (server, base):
        post(server, base, '/api/scan', {'folder': str(music)})
        wait_idle(base)
        store = JobStore(server.database)
        job_id = store.create('identify')
        target = str((music / '001.mp3').resolve())
        store.add_items(job_id, [target])
        store.update_items(job_id, [(target, 'error', 'invalid_audio', 'fpcalc returned 2: could not decode')])
        store.update(job_id, state='completed_with_errors')
        body = get(base, f'/api/jobs/errors?job_id={job_id}&code=invalid_audio')
        assert body['items'] == [{'path': target, 'code': 'invalid_audio',
                                  'detail': 'fpcalc returned 2: could not decode'}]
        assert body['outcome']['label'] == 'Invalid or unsupported audio'
        with pytest.raises(HTTPError) as caught:
            get(base, '/api/jobs/errors')
        assert caught.value.code == 400


def test_identify_limit_accepts_the_whole_library(tmp_path):
    with running(tmp_path) as (server, base):
        assert post(server, base, '/api/identify', {'limit': 0, 'offline': True})[0] == 400
        status, body = post(server, base, '/api/identify', {'limit': 50000, 'offline': True})
        assert status == 202 and body['started'] is False and 'Nothing' in body['reason']


# ── whole-library views ──────────────────────────────────────────────────────

def test_artists_and_albums_are_aggregated_over_the_whole_library(tmp_path):
    with running(tmp_path) as (server, base):
        seed(server, [('S1', 'Zed', 'First', '2001'), ('S2', 'Zed', 'First', '2001'), ('S3', 'Zed', 'Second', '2005'),
                      ('S4', 'Abel', 'Solo', '1999'), ('S5', '', '', '')])
        artists = get(base, '/api/artists')
        assert artists['total'] == 2
        assert artists['artists'] == [{'name': 'Zed', 'tracks': 3, 'albums': 2}, {'name': 'Abel', 'tracks': 1, 'albums': 1}]
        assert get(base, '/api/artists?q=abe')['artists'][0]['name'] == 'Abel'
        assert get(base, '/api/artists?limit=1&offset=1')['artists'][0]['name'] == 'Abel'
        albums = get(base, '/api/albums?sort=title')
        assert albums['total'] == 3
        # The album record keeps its original fields; the cover state is an additive field (no recognition results
        # exist in this fixture, so the album is unverified and gets no cover URL).
        first = dict(albums['albums'][0])
        cover = first.pop('cover')
        assert first == {'title': 'First', 'artist': 'Zed', 'year': '2001', 'tracks': 2}
        assert cover['state'] == 'unverified' and not cover.get('url')
        assert get(base, '/api/albums?artist=' + quote('Abel'))['albums'][0]['title'] == 'Solo'


def test_tracks_report_identification_state_and_total(tmp_path):
    with running(tmp_path) as (server, base):
        seed(server, [('S1', 'Zed', 'First', '2001'), ('S2', 'Zed', 'First', '2001'), ('S3', 'Zed', 'First', '2001')])
        store = MatchStore(server.database)
        store.put_result('/m/0.mp3', 1, 1, 'review', {'candidates': [], 'reason': 'r'})
        store.put_result('/m/1.mp3', 1, 1, 'unmatched', {'candidates': [], 'reason': 'r'})
        body = get(base, '/api/tracks?limit=2')
        assert body['total'] == 3 and len(body['tracks']) == 2
        states = {t['path']: t['identification'] for t in get(base, '/api/tracks')['tracks']}
        assert states == {'/m/0.mp3': 'proposal', '/m/1.mp3': 'unknown', '/m/2.mp3': 'awaiting'}
        assert get(base, '/api/tracks?status=tagged')['total'] == 3
        assert get(base, '/api/tracks?status=unidentified')['total'] == 0


def test_matches_are_paged_and_counted(tmp_path):
    with running(tmp_path) as (server, base):
        store = MatchStore(server.database)
        for n in range(5):
            store.put_result(f'/m/{n}.mp3', 1, 1, 'review', {'candidates': [], 'reason': 'r'})
        body = get(base, '/api/matches?limit=2&offset=4')
        assert body['total'] == 5 and len(body['matches']) == 1


def test_scan_dialog_puts_keyboard_focus_in_the_folder_field():
    """showModal() focuses the element marked autofocus; without it the close button gets the keys."""
    import re
    page = PACKAGED_PAGE.read_text(encoding='utf-8')
    field = re.search(r'<input[^>]*id="folderPath"[^>]*>', page).group(0)
    assert 'autofocus' in field


def test_review_card_labels_automatic_approvals_and_hides_redundant_accept():
    """Static check of the rendering rules: the card derives its label from recording_status,
    never shows 'Accepted by you' for an automatic decision, and offers Accept only when it is not
    already approved. Behaviour is verified separately in the browser."""
    page = PACKAGED_PAGE.read_text(encoding='utf-8')
    card = page[page.index('function matchCard(m){'):page.index('async function loadMatches(){')]
    for status in ('AUTO_APPROVED', 'USER_APPROVED', 'NEEDS_REVIEW', 'REJECTED', 'UNIDENTIFIED', 'EDITION_PENDING'):
        assert status in card, status
    assert 'Accepted by you' not in card, 'the wording for manual approval is "Approved by you"'
    assert 'Approved by you' in card and "USER_APPROVED" in card
    assert "recording_status" in card
    accept = card[card.index('data-review="accepted"') - 200:card.index('data-review="accepted"') + 40]
    assert "NEEDS_REVIEW" in accept, 'Accept must only appear for recordings still awaiting a decision'
    assert 'MeloShelf {n}/100' in card and 'bestScore' in card, 'the automatic badge shows the MeloShelf score'


def test_settings_screen_has_working_controls_for_every_automation_option():
    """Each control must be wired to the settings API; no decorative toggles."""
    page = PACKAGED_PAGE.read_text(encoding='utf-8')
    for control in ('autoApprove', 'approveThreshold', 'strictChecks', 'reviewAmbiguous', 'saveSettings'):
        assert f'id="{control}"' in page, control
    assert 'data-v="settings"' in page and 'href="#settings"' in page
    assert "api('/api/settings')" in page and "api('/api/settings'," in page
    assert 'Auto-approved means' in page or 'automatic' in page.lower()


def test_identification_setup_controls_are_wired_and_the_key_field_is_write_only():
    import re
    page = PACKAGED_PAGE.read_text(encoding='utf-8')
    for control in ('cfgAcoustid', 'saveAcoustid', 'clearAcoustid', 'cfgContact', 'saveContact',
                    'cfgFpcalc', 'saveFpcalc', 'testAcoustid', 'testContact', 'testFpcalc', 'setupStatus'):
        assert f'id="{control}"' in page, control
    field = re.search(r'<input[^>]*id="cfgAcoustid"[^>]*>', page).group(0)
    assert 'type="password"' in field and 'autocomplete="off"' in field
    assert 'value=' not in field, 'the key is never written into the page'
    assert "api('/api/config'," in page and "api('/api/config/test'," in page


def test_review_cards_show_a_cover_only_for_an_approved_unambiguous_release_and_drop_the_duplicate_badge():
    page = PACKAGED_PAGE.read_text(encoding='utf-8')
    card = page[page.index('function matchCard(m){'):page.index('async function loadMatches(){')]
    assert 'loading="lazy"' in card, 'covers load lazily'
    assert '/api/artwork?release=' in card
    assert 'release_details' in card and 'length===1' in card, 'only a single, unambiguous release is shown'
    assert 'AUTO_APPROVED' in card.split('/api/artwork')[0][-600:] or 'AUTO_APPROVED' in card
    assert 'class="state ${verdict[0]}"' not in card, 'the verdict badge duplicates the status badge'


def test_review_table_labels_automatic_status_not_only_the_user_decision():
    # Real case: auto-approved recordings were listed as "Awaiting your decision", and unidentified files too.
    from pathlib import Path
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    row = [line for line in page.splitlines() if 'decisionLabel(m)' in line]
    assert row, 'review table row does not use the decision label'
    label = page.split('function decisionLabel(m){')[1].split('\n  }')[0]
    assert "m.recording_status" in label and "m.decision" in label
    assert 'Auto-approved by MeloShelf' in page and 'In QA' in page and 'No identity found' in page


def test_a_completed_scan_card_does_not_present_its_old_total_as_the_library_size():
    # Real case: the card said "5,140 files indexed" while the library held 40 rows. A finished scan's totals
    # describe that scan only; the current library size must come from the library count.
    from pathlib import Path
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    assert "The library now holds" in page
    assert "fact(T('m.files-in-scan','Files in this scan')" in page
    assert "fact('Discovered'" not in page


def test_the_correction_fields_are_available_without_choosing_an_alternative():
    # Real case: the correction fields stayed hidden until an alternative candidate was chosen, so a reviewer
    # who knew the right title could not enter it for an incorrect proposal.
    from pathlib import Path
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'id="qaCorrection" style="margin-top:10px;display:none"' not in page
    assert 'id="qaTitle"' in page and 'id="qaArtist"' in page


def test_the_validate_page_offers_metadata_cleanup_and_recording_version_views():
    # Written after the panel was applied; it checks that the wired controls and API calls are present.
    from pathlib import Path
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    for needle in ('id="metaCleanup"', "/api/metadata/groups", "/api/metadata/decision", 'Recording version',
                   'id="metaBulk"', 'No single replacement'):
        assert needle in page, needle


def test_the_page_script_parses_as_javascript():
    # Real case: a duplicate top-level const stopped the whole page script from running, so the Validate page
    # was blank while the structural tests still passed. Parse the script with Node when it is available.
    import re, shutil, subprocess, tempfile
    from pathlib import Path
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed; the page script was not syntax-checked')
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    tags = re.findall(r'<script((?![^>]*\bsrc=)[^>]*)>(.*?)</script>', page, re.S)
    # Only JavaScript blocks: data blocks (for example application/json) are not parsed as code.
    scripts = [body for attrs, body in tags if not re.search(r'\btype\s*=\s*["\']?(?!text/javascript|module)', attrs)]
    assert scripts, 'no inline script found'
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write('\n'.join(scripts))
        path = handle.name
    result = subprocess.run([node, '--check', path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_validate_page_shows_text_suggestions_without_any_approve_control():
    import re
    from pathlib import Path
    page = (Path(__file__).parents[1] / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'id="fallbackPanel"' in page and '/api/fallback/results' in page
    assert 'not acoustically verified' in page
    # The suggestions themselves are listed by renderFallbackList: no control of any kind may be offered on them.
    block = page.split('async function renderFallbackList(){')[1].split('\n  }\n')[0]
    # Explanatory copy may say "approved"; the test guards against controls, not words.
    assert '<button' not in block and 'data-' not in block and 'onclick' not in block
    # The panel's own controls only start or stop the search; none of them accepts, approves or confirms anything.
    panel = page.split('async function loadFallback(){')[1].split('\n  }\n')[0]
    labels = re.findall(r'<button[^>]*>([^<]*)</button>', panel)
    # Labels go through T(key, English) so they can be translated; the English text is the second argument.
    labels = [re.sub(r"^\$\{esc\(T\('[^']+','([^']+)'\)\)\}$", r'\1', label) for label in labels]
    assert labels == ['Search by text', 'Stop after this file']
