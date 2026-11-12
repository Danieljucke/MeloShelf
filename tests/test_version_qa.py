"""Dedicated recording-version QA: a person judges whether the proposed recording is the same version as the file
(live, remix, remaster...), separately from whether it is the right recording. The judgement is stored apart from
every automatic decision and from the recording labels, and is used only to measure MeloShelf's version verdicts."""
import json
import threading
import time

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.qa_store import QaError, QaStore
from meloshelf.infrastructure.secrets import MemorySecretStore
from meloshelf.infrastructure.version_qa import VersionQa
from test_qa_api import request
from test_qa_labels import candidate, seed


def version_candidate(recording_id, title, duration):
    c = candidate(recording_id, title, 'Band')
    c['evidence']['duration'] = duration
    return c


CASES = {                                   # path -> (local title, candidate title, duration state, expected verdict)
    'G:/V/confirmed.mp3': ('Song (Live)', 'Song (Remix)', 'mismatch', 'confirmed_conflict'),
    'G:/V/possible.mp3': ('Song (Live)', 'Song', 'match', 'possible_conflict'),
    'G:/V/insufficient.mp3': ('Song', 'Song', 'unknown', 'insufficient'),
    'G:/V/probable.mp3': ('Song', 'Song', 'match', 'probable_agreement'),
    'G:/V/verified.mp3': ('Song (Live)', 'Song (Live)', 'match', 'verified_agreement'),
}


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    for index, (path, (local, remote, duration, _)) in enumerate(CASES.items()):
        seed(db, path, [version_candidate(f'rec-{index}', remote, duration)], 'NEEDS_REVIEW', title=local, artist='Band')
    seed(db, 'G:/V/none.mp3', [], 'UNIDENTIFIED')
    return db


def order(database, **kwargs):
    seen, out = [], []
    while True:
        item = VersionQa(database).next_item(skip=seen, **kwargs)
        if item is None:
            return out
        out.append(item['path'])
        seen.append(item['path'])


# ── the queue ────────────────────────────────────────────────────────────────────

def test_the_queue_shows_the_most_doubtful_versions_first(database):
    assert [CASES[p][3] for p in order(database)] == ['confirmed_conflict', 'possible_conflict', 'insufficient',
                                                      'probable_agreement', 'verified_agreement']


def test_the_item_carries_what_a_person_needs_to_judge_the_version(database):
    item = VersionQa(database).next_item()
    assert item['path'] == 'G:/V/confirmed.mp3' and item['filename'] == 'confirmed.mp3'
    assert item['local_title'] == 'Song (Live)' and item['candidate']['title'] == 'Song (Remix)'
    assert item['version']['verdict'] == 'confirmed_conflict' and item['version']['reasons']
    assert item['duration'] == 'mismatch' and item['label'] is None


def test_the_queue_can_be_limited_to_one_verdict(database):
    assert [CASES[p][3] for p in order(database, verdict='possible_conflict')] == ['possible_conflict']
    with pytest.raises(ValueError):
        VersionQa(database).next_item(verdict='nonsense')


def test_files_without_a_proposal_are_never_queued(database):
    assert 'G:/V/none.mp3' not in order(database)


def test_a_judged_file_leaves_the_queue_and_skipping_does_not_judge(database):
    qa = VersionQa(database)
    qa.set_label('G:/V/confirmed.mp3', 'different')
    assert 'G:/V/confirmed.mp3' not in order(database)
    first = qa.next_item(skip=['G:/V/possible.mp3'])
    assert first['path'] == 'G:/V/insufficient.mp3'
    assert qa.progress()['labelled'] == 1


# ── labels ───────────────────────────────────────────────────────────────────────

def test_a_label_records_the_verdict_meloshelf_gave_at_the_time(database):
    VersionQa(database).set_label('G:/V/possible.mp3', 'same')
    stored = VersionQa(database).get_label('G:/V/possible.mp3')
    assert stored['verdict'] == 'same' and stored['assessed_verdict'] == 'possible_conflict'
    assert stored['stale'] is False


@pytest.mark.parametrize('bad', ['correct', 'SAME', '', None, 3])
def test_only_same_different_or_unsure_is_accepted(database, bad):
    with pytest.raises(QaError):
        VersionQa(database).set_label('G:/V/possible.mp3', bad)


def test_a_file_without_a_proposal_cannot_be_judged(database):
    with pytest.raises(QaError):
        VersionQa(database).set_label('G:/V/none.mp3', 'same')
    with pytest.raises(QaError):
        VersionQa(database).set_label('G:/V/ghost.mp3', 'same')


def test_a_version_label_changes_no_automatic_decision_and_no_recording_label(database):
    VersionQa(database).set_label('G:/V/possible.mp3', 'different')
    assert MatchStore(database).recording_status('G:/V/possible.mp3') == 'NEEDS_REVIEW'
    assert QaStore(database).get_label('G:/V/possible.mp3') is None


def test_changing_a_label_keeps_the_earlier_one_in_history(database):
    qa = VersionQa(database)
    qa.set_label('G:/V/possible.mp3', 'same')
    qa.set_label('G:/V/possible.mp3', 'different')
    with database.read() as db:
        history = db.execute('SELECT verdict FROM human_version_label_history WHERE path=?', ('G:/V/possible.mp3',)).fetchall()
    assert [h['verdict'] for h in history] == ['same'] and qa.get_label('G:/V/possible.mp3')['verdict'] == 'different'


def test_a_label_is_flagged_stale_when_the_proposal_changes(database):
    VersionQa(database).set_label('G:/V/possible.mp3', 'same')
    MatchStore(database).put_result('G:/V/possible.mp3', 10, 1, 'review',
                                    {'candidates': [version_candidate('rec-new', 'Song', 'match')], 'assessment': '',
                                     'reason': '', 'code': ''}, recording_status='NEEDS_REVIEW')
    assert VersionQa(database).get_label('G:/V/possible.mp3')['stale'] is True


def test_labels_survive_a_restart(database, tmp_path):
    VersionQa(database).set_label('G:/V/possible.mp3', 'unsure')
    again = Database(tmp_path / 'lib.sqlite3')
    again.initialize()
    assert VersionQa(again).get_label('G:/V/possible.mp3')['verdict'] == 'unsure'


# ── progress and report ──────────────────────────────────────────────────────────

def test_progress_counts_proposals_and_labels(database):
    qa = VersionQa(database)
    qa.set_label('G:/V/confirmed.mp3', 'different')
    qa.set_label('G:/V/verified.mp3', 'same')
    qa.set_label('G:/V/probable.mp3', 'unsure')
    assert qa.progress() == {'proposals': 5, 'labelled': 3, 'same': 1, 'different': 1, 'unsure': 1, 'remaining': 2}


def test_the_report_measures_meloshelfs_version_verdicts_against_the_judgements(database):
    qa = VersionQa(database)
    qa.set_label('G:/V/confirmed.mp3', 'different')       # conflict flagged, human agrees
    qa.set_label('G:/V/possible.mp3', 'same')             # conflict flagged, human disagrees
    qa.set_label('G:/V/probable.mp3', 'different')        # a missed conflict
    qa.set_label('G:/V/verified.mp3', 'same')             # agreement, human agrees
    qa.set_label('G:/V/insufficient.mp3', 'unsure')
    report = qa.report()
    assert report['matrix']['confirmed_conflict'] == {'same': 0, 'different': 1, 'unsure': 0}
    assert report['matrix']['possible_conflict']['same'] == 1
    flagged = report['conflicts_flagged']
    assert (flagged['judged'], flagged['confirmed_by_person'], flagged['precision'], flagged['sufficient_sample']) == (2, 1, 0.5, False)
    low, high = flagged['interval95']
    assert 0 <= low < 0.5 < high <= 1
    assert report['agreements_claimed']['judged'] == 2 and report['agreements_claimed']['precision'] == 0.5
    assert report['missed_conflicts'] == 1
    assert report['unsure'] == 1 and report['labelled'] == 5


def test_an_empty_report_makes_no_claims(database):
    report = VersionQa(database).report()
    assert report['labelled'] == 0 and report['conflicts_flagged']['precision'] is None
    assert report['agreements_claimed']['precision'] is None and report['missed_conflicts'] == 0


def test_stale_labels_are_left_out_of_the_report(database):
    VersionQa(database).set_label('G:/V/possible.mp3', 'same')
    MatchStore(database).put_result('G:/V/possible.mp3', 10, 1, 'review',
                                    {'candidates': [version_candidate('rec-new', 'Song', 'match')], 'assessment': '',
                                     'reason': '', 'code': ''}, recording_status='NEEDS_REVIEW')
    report = VersionQa(database).report()
    assert report['labelled'] == 0 and report['stale'] == 1


# ── the API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def served(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    for index, (path, (local, remote, duration, _)) in enumerate(CASES.items()):
        seed(running.database, path, [version_candidate(f'rec-{index}', remote, duration)], 'NEEDS_REVIEW', title=local, artist='Band')
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def test_read_routes_are_open_and_the_write_route_needs_the_token(served):
    _, base = served
    assert json.loads(request(base, 'GET', '/api/qa/version/next')[2])['version']['verdict'] == 'confirmed_conflict'
    assert json.loads(request(base, 'GET', '/api/qa/version/progress')[2])['proposals'] == 5
    assert 'matrix' in json.loads(request(base, 'GET', '/api/qa/version/report')[2])
    assert request(base, 'POST', '/api/qa/version/label', {'path': 'G:/V/possible.mp3', 'verdict': 'same'})[0] == 403


def test_a_label_can_be_saved_and_the_queue_moves_on(served):
    running, base = served
    status, _, body = request(base, 'POST', '/api/qa/version/label', {'path': 'G:/V/confirmed.mp3', 'verdict': 'different'},
                              token=running.token)
    assert status == 200 and json.loads(body)['progress']['labelled'] == 1
    assert json.loads(request(base, 'GET', '/api/qa/version/next')[2])['path'] == 'G:/V/possible.mp3'
    assert json.loads(request(base, 'GET', '/api/qa/version/next?verdict=verified_agreement')[2])['path'] == 'G:/V/verified.mp3'


def test_exhausting_the_queue_is_not_an_error(served):
    running, base = served
    for path in CASES:
        request(base, 'POST', '/api/qa/version/label', {'path': path, 'verdict': 'same'}, token=running.token)
    assert json.loads(request(base, 'GET', '/api/qa/version/next')[2]) == {'path': None}


@pytest.mark.parametrize('payload', [{'path': 'G:/V/possible.mp3', 'verdict': 'maybe'}, {'path': 5, 'verdict': 'same'},
                                     {'verdict': 'same'}, {'path': 'G:/V/ghost.mp3', 'verdict': 'same'}])
def test_bad_label_requests_are_refused(served, payload):
    running, base = served
    assert request(base, 'POST', '/api/qa/version/label', payload, token=running.token)[0] == 400
    assert json.loads(request(base, 'GET', '/api/qa/version/progress')[2])['labelled'] == 0


def test_an_unknown_verdict_filter_is_a_400(served):
    _, base = served
    assert request(base, 'GET', '/api/qa/version/next?verdict=nope')[0] == 400


def test_the_page_has_a_version_check_with_audio_and_three_verdict_buttons():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'id="qaVersion"' in page and '/api/qa/version/label' in page
    assert 'Same version' in page and 'Different version' in page and 'Not sure' in page
    assert 'does not change any decision' in page


# ── scale ────────────────────────────────────────────────────────────────────────

def test_the_version_queue_over_ten_thousand_proposals_stays_interactive(tmp_path):
    db = Database(tmp_path / 'big.sqlite3')
    db.initialize()
    with db.write() as conn:
        for i in range(10_000):
            path = f'G:/B/{i}.mp3'
            conn.execute('INSERT INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status) VALUES(?,?,?,?,?,?,?,?,?)',
                         (path, 10, '.mp3', 1, 'Song (Live)' if i % 7 == 0 else 'Song', 'A', '', '', 'review'))
            data = {'candidates': [version_candidate(f'r{i}', 'Song', 'match')], 'assessment': '', 'reason': '', 'code': ''}
            conn.execute("INSERT INTO match_results(path,file_size,file_mtime_ns,status,data,created_at,decision,recording_status) "
                         "VALUES(?,?,?,?,?,?,'pending','NEEDS_REVIEW')", (path, 10, 1, 'review', json.dumps(data), 1))
    started = time.perf_counter()
    item = VersionQa(db).next_item()
    elapsed = time.perf_counter() - started
    assert item['version']['verdict'] == 'possible_conflict'
    assert elapsed < 5.0, f'{elapsed:.2f}s'
