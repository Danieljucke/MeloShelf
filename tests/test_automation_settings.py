"""Automation settings: policy options, persistence, API validation and controlled reevaluation."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.domain.decisions import (AUTO_APPROVED, NEEDS_REVIEW, REJECTED, UNIDENTIFIED, USER_APPROVED,
                                        decide, status_for)
from meloshelf.domain.matching import Evidence
from meloshelf.infrastructure.match_store import MatchStore


def ev(score, *, title='match', artist='match', duration='match'):
    return Evidence(provider_score=0.97, score=score, title=title, artist=artist, duration=duration,
                    duration_delta=0.0 if duration == 'match' else None)


# ── policy options ───────────────────────────────────────────────────────────

def test_automatic_approval_can_be_switched_off():
    assert status_for([ev(95)], auto_enabled=False) == NEEDS_REVIEW


def test_strict_mode_requires_an_agreeing_tag_or_duration():
    fingerprint_only = ev(90, title='unknown', artist='unknown', duration='unknown')
    assert status_for([fingerprint_only], strict=True) == NEEDS_REVIEW
    assert status_for([fingerprint_only], strict=False) == AUTO_APPROVED


def test_contradictions_block_approval_in_both_modes():
    contradicted = ev(95, title='conflict')
    assert status_for([contradicted], strict=True) == NEEDS_REVIEW
    assert status_for([contradicted], strict=False) == NEEDS_REVIEW


def test_ambiguous_results_can_be_kept_out_of_the_review_queue():
    ambiguous = [ev(90), ev(86, title='partial')]
    assert status_for(ambiguous, review_ambiguous=True) == NEEDS_REVIEW
    assert status_for(ambiguous, review_ambiguous=False) == UNIDENTIFIED


def test_user_decisions_are_unaffected_by_automation_options():
    assert decide(ev(10), 'insufficient', user_decision='accepted', auto_enabled=False) == USER_APPROVED
    assert decide(ev(95), 'strong', user_decision='rejected', strict=False) == REJECTED


# ── persistence and API ──────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html)
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def call(server, base, method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(base + path, data=body, method=method, headers={
        'Content-Type': 'application/json', 'X-MeloShelf-Token': server.token})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_defaults_match_the_documented_policy(server):
    running, base = server
    status, body = call(running, base, 'GET', '/api/settings')
    assert status == 200
    assert body == {'auto_approve_enabled': True, 'threshold': 80, 'strict_checks': True,
                    'review_ambiguous': True}


def test_settings_are_validated(server):
    running, base = server
    assert call(running, base, 'POST', '/api/settings', {'threshold': 49})[0] == 400
    assert call(running, base, 'POST', '/api/settings', {'threshold': 101})[0] == 400
    assert call(running, base, 'POST', '/api/settings', {'auto_approve_enabled': 'yes'})[0] == 400
    assert call(running, base, 'POST', '/api/settings', {'unknown_option': True})[0] == 400


def test_settings_persist_across_restart(server, tmp_path):
    running, base = server
    status, body = call(running, base, 'POST', '/api/settings', {'threshold': 88, 'strict_checks': False})
    assert status == 200 and body['settings']['threshold'] == 88
    reopened = MatchStore(tmp_path / 'lib.sqlite3')
    assert reopened.get_setting('auto_approve_threshold', 80) == 88
    assert reopened.get_setting('strict_checks', True) is False


def test_changing_settings_reevaluates_without_touching_decisions(server):
    running, base = server
    store = running.store
    candidate = {'recording_id': '1', 'title': 'Song', 'artist': 'A', 'acoustid_score': .97, 'releases': [],
                 'duration': None, 'release_details': [],
                 'evidence': {'provider_score': .97, 'score': 84, 'title': 'match', 'artist': 'match',
                              'duration': 'match', 'duration_delta': 0.0, 'notes': []}}
    for name in ('kept.mp3', 'mine.mp3'):
        store.put_result(name, 1, 1, 'review', {'candidates': [candidate], 'reason': 'r'},
                         recording_status=AUTO_APPROVED)
    store.set_decision('mine.mp3', 'accepted', recording_id='1')

    status, body = call(running, base, 'POST', '/api/settings', {'threshold': 90})
    assert status == 200 and body['reevaluated'] == 2
    assert store.recording_status('kept.mp3') == NEEDS_REVIEW
    assert store.recording_status('mine.mp3') == USER_APPROVED
    assert store.get_result('mine.mp3')['decision'] == 'accepted'


def test_disabling_automation_reevaluates_existing_automatic_approvals(server):
    running, base = server
    running.store.put_result('auto.mp3', 1, 1, 'review', {'candidates': [{
        'recording_id': '1', 'title': 'S', 'artist': 'A', 'acoustid_score': .97, 'releases': [], 'duration': None,
        'release_details': [], 'evidence': {'provider_score': .97, 'score': 95, 'title': 'match', 'artist': 'match',
                                           'duration': 'match', 'duration_delta': 0.0, 'notes': []}}],
        'reason': 'r'}, recording_status=AUTO_APPROVED)
    call(running, base, 'POST', '/api/settings', {'auto_approve_enabled': False})
    assert running.store.recording_status('auto.mp3') == NEEDS_REVIEW


def test_duration_agreement_alone_does_not_auto_approve_in_strict_mode():
    """A matching length is common to many recordings; only a tag agreement confirms the identity."""
    duration_only = ev(92, title='unknown', artist='unknown', duration='match')
    assert status_for([duration_only], strict=True) == NEEDS_REVIEW
    tag_agreement = ev(92, title='match', artist='unknown', duration='unknown')
    assert status_for([tag_agreement], strict=True) == AUTO_APPROVED


def test_artist_alone_does_not_identify_a_recording_in_strict_mode():
    """An artist's name matches every track they made; it needs a matching length to narrow it."""
    artist_only = ev(92, title='unknown', artist='match', duration='unknown')
    assert status_for([artist_only], strict=True) == NEEDS_REVIEW
    artist_and_length = ev(92, title='unknown', artist='match', duration='match')
    assert status_for([artist_and_length], strict=True) == AUTO_APPROVED


def test_results_stored_before_automatic_statuses_are_evaluated_when_read(tmp_path):
    """Results written before the status column existed carry an empty status; reading them must
    still show the decision the current policy gives, without writing anything."""
    store = MatchStore(tmp_path / 'lib.sqlite3')
    strong = {'candidates': [{'recording_id': '1', 'title': 'Song', 'artist': 'A', 'acoustid_score': .97,
                              'releases': [], 'duration': None, 'release_details': [],
                              'evidence': {'provider_score': .97, 'score': 90, 'title': 'match', 'artist': 'match',
                                           'duration': 'match', 'duration_delta': 0.0, 'notes': []}}],
              'reason': 'r'}
    store.put_result('old.mp3', 1, 1, 'review', strong)  # no recording_status supplied
    row = next(r for r in store.list_results() if r['path'] == 'old.mp3')
    assert row['recording_status'] == AUTO_APPROVED
    assert store.recording_status('old.mp3') == '', 'reading must not write'
    empty = {'candidates': [], 'reason': 'none'}
    store.put_result('none.mp3', 1, 1, 'unmatched', empty)
    unmatched = next(r for r in store.list_results() if r['path'] == 'none.mp3')
    assert unmatched['recording_status'] == UNIDENTIFIED


def test_a_user_decision_replaces_the_stored_automatic_status(tmp_path):
    """Rejecting an automatic approval must show as rejected, and reconsidering must restore the policy."""
    store = MatchStore(tmp_path / 'lib.sqlite3')
    strong = {'candidates': [{'recording_id': '1', 'title': 'Song', 'artist': 'A', 'acoustid_score': .97,
                              'releases': [], 'duration': None, 'release_details': [],
                              'evidence': {'provider_score': .97, 'score': 95, 'title': 'match', 'artist': 'match',
                                           'duration': 'match', 'duration_delta': 0.0, 'notes': []}}],
              'reason': 'r'}
    store.put_result('auto.mp3', 1, 1, 'review', strong, recording_status=AUTO_APPROVED)
    store.set_decision('auto.mp3', 'rejected')
    assert store.recording_status('auto.mp3') == REJECTED
    assert next(r for r in store.list_results() if r['path'] == 'auto.mp3')['recording_status'] == REJECTED
    store.set_decision('auto.mp3', 'pending')
    assert store.recording_status('auto.mp3') == AUTO_APPROVED, 'reconsidering returns to the policy decision'
    store.set_decision('auto.mp3', 'accepted', recording_id='1')
    assert store.recording_status('auto.mp3') == USER_APPROVED
