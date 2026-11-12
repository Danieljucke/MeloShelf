"""Bulk human-reference judgement: a person-chosen, previewed selection of unlabelled proposals gets one label.
It never replaces an existing label, never lets a version conflict be bulk-approved, is bounded, atomic and undoable."""
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.qa_bulk import MAX_SELECTION, QaBulk
from meloshelf.infrastructure.qa_store import QaError, QaStore
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request
from test_qa_labels import candidate, seed


def conflicting(recording_id, title, artist):
    c = candidate(recording_id, title, artist)
    c['evidence']['version_conflict'] = True
    return c


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed(db, 'G:/M/a.mp3', [candidate('rec-a', 'Song A', 'Artist A')], 'AUTO_APPROVED', title='Song A', artist='Artist A')
    seed(db, 'G:/M/b.mp3', [candidate('rec-b', 'Song B', 'Artist B')], 'AUTO_APPROVED', title='Song B', artist='Artist B')
    seed(db, 'G:/M/c.mp3', [candidate('rec-c', 'Song C', 'Other')], 'NEEDS_REVIEW', title='Song C', artist='Artist C')
    seed(db, 'G:/M/live.mp3', [conflicting('rec-l', 'Song L (Live)', 'Artist L')], 'AUTO_APPROVED', title='Song L', artist='Artist L')
    seed(db, 'G:/M/none.mp3', [], 'UNIDENTIFIED')
    return db


def labels(database):
    with database.read() as db:
        return {r['path']: r['label'] for r in db.execute('SELECT path,label FROM human_labels')}


# ── the list to choose from ──────────────────────────────────────────────────────

def test_the_list_holds_only_unlabelled_proposals_with_what_is_needed_to_judge_them(database):
    QaStore(database).set_label('G:/M/a.mp3', 'correct')
    page = QaBulk(database).candidates()
    paths = [r['path'] for r in page['items']]
    assert 'G:/M/a.mp3' not in paths and 'G:/M/none.mp3' not in paths
    assert page['total'] == 3
    row = next(r for r in page['items'] if r['path'] == 'G:/M/b.mp3')
    assert row['tags'] == {'title': 'Song B', 'artist': 'Artist B'} and row['proposed'] == {'title': 'Song B', 'artist': 'Artist B'}
    assert row['status'] == 'AUTO_APPROVED' and row['filename'] == 'b.mp3'


def test_the_list_can_be_filtered_by_status_and_text_and_paged(database):
    bulk = QaBulk(database)
    assert {r['path'] for r in bulk.candidates(status='NEEDS_REVIEW')['items']} == {'G:/M/c.mp3'}
    assert {r['path'] for r in bulk.candidates(query='song b')['items']} == {'G:/M/b.mp3'}
    first = bulk.candidates(limit=2)
    second = bulk.candidates(limit=2, offset=2)
    assert len(first['items']) == 2 and len(second['items']) == 2 and first['total'] == 4
    assert not {r['path'] for r in first['items']} & {r['path'] for r in second['items']}


def test_the_text_filter_treats_wildcards_as_plain_text(database):
    assert QaBulk(database).candidates(query='%')['total'] == 0
    assert QaBulk(database).candidates(query="'; DROP TABLE human_labels;--")['total'] == 0


def test_the_page_size_is_bounded(database):
    assert len(QaBulk(database).candidates(limit=10_000)['items']) <= 4
    with pytest.raises(ValueError):
        QaBulk(database).candidates(limit=0)


# ── preview and apply ────────────────────────────────────────────────────────────

def test_a_preview_says_who_would_be_labelled_and_who_would_not_and_writes_nothing(database):
    QaStore(database).set_label('G:/M/b.mp3', 'unsure')
    plan = QaBulk(database).preview(['G:/M/a.mp3', 'G:/M/b.mp3', 'G:/M/live.mp3', 'G:/M/none.mp3', 'G:/M/ghost.mp3'], 'correct')
    assert plan['eligible'] == ['G:/M/a.mp3'] and plan['counts'] == {'eligible': 1, 'ineligible': 4}
    reasons = {r['path']: r['reason'] for r in plan['ineligible']}
    assert 'already has your label' in reasons['G:/M/b.mp3']
    assert 'version' in reasons['G:/M/live.mp3']
    assert 'no proposal' in reasons['G:/M/none.mp3']
    assert 'not been analysed' in reasons['G:/M/ghost.mp3']
    assert labels(database) == {'G:/M/b.mp3': 'unsure'}


def test_apply_labels_the_eligible_ones_like_a_hand_label_and_leaves_the_automatic_status_alone(database):
    result = QaBulk(database).apply(['G:/M/a.mp3', 'G:/M/c.mp3'], 'correct')
    assert result['written'] == 2 and result['batch_id']
    assert labels(database) == {'G:/M/a.mp3': 'correct', 'G:/M/c.mp3': 'correct'}
    assert QaStore(database).get_label('G:/M/a.mp3')['proposal_recording_id'] == 'rec-a'
    assert MatchStore(database).recording_status('G:/M/a.mp3') == 'AUTO_APPROVED'


def test_bulk_never_replaces_an_existing_label(database):
    QaStore(database).set_label('G:/M/a.mp3', 'incorrect')
    result = QaBulk(database).apply(['G:/M/a.mp3', 'G:/M/b.mp3'], 'correct')
    assert result['written'] == 1 and result['skipped'] == 1
    assert labels(database) == {'G:/M/a.mp3': 'incorrect', 'G:/M/b.mp3': 'correct'}


def test_a_version_conflict_can_be_bulk_marked_incorrect_or_unsure_but_never_correct(database):
    bulk = QaBulk(database)
    assert bulk.apply(['G:/M/live.mp3'], 'correct')['written'] == 0
    assert bulk.apply(['G:/M/live.mp3'], 'incorrect')['written'] == 1


def test_a_label_outside_the_three_is_refused(database):
    for bad in ('maybe', '', None, 3):
        with pytest.raises(QaError):
            QaBulk(database).apply(['G:/M/a.mp3'], bad)
    assert labels(database) == {}


def test_the_selection_is_bounded_and_repeats_count_once(database):
    with pytest.raises(ValueError):
        QaBulk(database).preview([f'G:/M/{i}.mp3' for i in range(MAX_SELECTION + 1)], 'correct')
    result = QaBulk(database).apply(['G:/M/a.mp3'] * 5, 'correct')
    assert result['written'] == 1


def test_an_empty_selection_is_refused(database):
    with pytest.raises(ValueError):
        QaBulk(database).apply([], 'correct')


def test_a_failure_part_way_writes_nothing_and_records_no_batch(database, monkeypatch):
    bulk = QaBulk(database)
    import meloshelf.infrastructure.qa_bulk as module
    original, calls = module.write_label, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('disk trouble')
        return original(*args, **kwargs)

    monkeypatch.setattr(module, 'write_label', flaky)
    with pytest.raises(RuntimeError):
        bulk.apply(['G:/M/a.mp3', 'G:/M/b.mp3'], 'correct')
    assert labels(database) == {} and bulk.batches() == []


# ── undo ─────────────────────────────────────────────────────────────────────────

def test_undo_removes_the_labels_a_batch_added(database):
    bulk = QaBulk(database)
    batch = bulk.apply(['G:/M/a.mp3', 'G:/M/b.mp3'], 'correct')['batch_id']
    result = bulk.undo(batch)
    assert result == {'removed': 2, 'kept': 0}
    assert labels(database) == {}
    assert bulk.batches()[0]['undone'] is True


def test_undo_keeps_a_label_the_person_changed_after_the_batch(database):
    bulk = QaBulk(database)
    batch = bulk.apply(['G:/M/a.mp3', 'G:/M/b.mp3'], 'correct')['batch_id']
    QaStore(database).set_label('G:/M/a.mp3', 'incorrect')
    result = bulk.undo(batch)
    assert result == {'removed': 1, 'kept': 1}
    assert labels(database) == {'G:/M/a.mp3': 'incorrect'}


def test_undoing_twice_or_an_unknown_batch_changes_nothing(database):
    bulk = QaBulk(database)
    batch = bulk.apply(['G:/M/a.mp3'], 'correct')['batch_id']
    bulk.undo(batch)
    QaStore(database).set_label('G:/M/a.mp3', 'unsure')
    with pytest.raises(QaError):
        bulk.undo(batch)
    with pytest.raises(QaError):
        bulk.undo(9999)
    assert labels(database) == {'G:/M/a.mp3': 'unsure'}


def test_the_batch_list_shows_recent_batches_newest_first(database):
    bulk = QaBulk(database)
    one = bulk.apply(['G:/M/a.mp3'], 'correct')['batch_id']
    two = bulk.apply(['G:/M/b.mp3'], 'unsure')['batch_id']
    listed = bulk.batches()
    assert [b['id'] for b in listed] == [two, one]
    assert listed[0]['label'] == 'unsure' and listed[0]['count'] == 1 and listed[0]['undone'] is False


# ── the API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def served(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    seed(running.database, 'G:/M/a.mp3', [candidate('rec-a', 'Song A', 'Artist A')], 'AUTO_APPROVED', title='Song A', artist='Artist A')
    seed(running.database, 'G:/M/b.mp3', [candidate('rec-b', 'Song B', 'Artist B')], 'NEEDS_REVIEW', title='Song B', artist='Artist B')
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def test_the_list_is_readable_without_a_token_but_every_write_needs_one(served):
    _, base = served
    status, _, body = request(base, 'GET', '/api/qa/bulk/candidates?limit=10')
    assert status == 200 and json.loads(body)['total'] == 2
    for path, payload in (('/api/qa/bulk', {'paths': ['G:/M/a.mp3'], 'label': 'correct', 'mode': 'apply'}),
                          ('/api/qa/bulk/undo', {'batch_id': 1})):
        assert request(base, 'POST', path, payload)[0] == 403


def test_preview_apply_and_undo_through_the_api(served):
    running, base = served
    payload = {'paths': ['G:/M/a.mp3', 'G:/M/b.mp3'], 'label': 'correct'}
    status, _, body = request(base, 'POST', '/api/qa/bulk', {**payload, 'mode': 'preview'}, token=running.token)
    assert status == 200 and json.loads(body)['counts']['eligible'] == 2 and labels(running.database) == {}
    status, _, body = request(base, 'POST', '/api/qa/bulk', {**payload, 'mode': 'apply'}, token=running.token)
    applied = json.loads(body)
    assert status == 200 and applied['written'] == 2 and applied['progress']['labelled'] == 2
    status, _, body = request(base, 'POST', '/api/qa/bulk/undo', {'batch_id': applied['batch_id']}, token=running.token)
    assert status == 200 and json.loads(body)['removed'] == 2 and labels(running.database) == {}
    assert json.loads(request(base, 'GET', '/api/qa/bulk/batches')[2])['batches'][0]['undone'] is True


@pytest.mark.parametrize('path,payload', [
    ('/api/qa/bulk', {'paths': 'G:/M/a.mp3', 'label': 'correct', 'mode': 'apply'}),
    ('/api/qa/bulk', {'paths': [1], 'label': 'correct', 'mode': 'apply'}),
    ('/api/qa/bulk', {'paths': [], 'label': 'correct', 'mode': 'apply'}),
    ('/api/qa/bulk', {'paths': ['G:/M/a.mp3'], 'label': 'maybe', 'mode': 'apply'}),
    ('/api/qa/bulk', {'paths': ['G:/M/a.mp3'], 'label': 'correct', 'mode': 'delete'}),
    ('/api/qa/bulk', {'paths': ['G:/M/a.mp3'], 'label': 'correct'}),
    ('/api/qa/bulk/undo', {'batch_id': 'one'}),
    ('/api/qa/bulk/undo', {'batch_id': 424242}),
])
def test_malformed_bulk_requests_are_refused_and_change_nothing(served, path, payload):
    running, base = served
    assert request(base, 'POST', path, payload, token=running.token)[0] == 400
    assert labels(running.database) == {}


def test_the_page_makes_the_selection_explicit_and_offers_undo():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'id="qaBulk"' in page and "mode:'preview'" in page and '/api/qa/bulk/undo' in page
    assert 'are never replaced' in page and 'cannot be bulk-marked correct' in page
