"""CSV import of human reference labels: validated row by row, previewed before anything is written, never
overwriting an existing judgement unless that row is named, atomic, and the replaced judgement is kept in history."""
import csv
import io
import json
import threading

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.qa_import import MAX_ROWS, QaImporter
from meloshelf.infrastructure.qa_store import QaStore
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request
from test_qa_labels import candidate, seed

HEADER = ['path', 'label', 'corrected_recording_id', 'corrected_title', 'corrected_artist']


def make_csv(rows, header=HEADER):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed(db, 'D:/Library/a.mp3', [candidate('rec-a', 'Song A', 'Artist A')], 'AUTO_APPROVED', title='Song A', artist='Artist A')
    seed(db, 'D:/Library/b.mp3', [candidate('rec-b', 'Song B', 'Artist B'), candidate('rec-b2', 'Song B (live)', 'Artist B')],
         'NEEDS_REVIEW')
    seed(db, 'D:/Library/Sub/c.mp3', [candidate('rec-c', 'Song C', 'Artist C')], 'NEEDS_REVIEW')
    seed(db, 'G:/Other/c.mp3', [candidate('rec-c2', 'Song C', 'Artist C')], 'NEEDS_REVIEW')
    return db


def labels(database):
    with database.read() as db:
        return {r['path']: (r['label'], r['corrected_recording_id'], r['corrected_title'])
                for r in db.execute('SELECT * FROM human_labels')}


# ── preview never writes ─────────────────────────────────────────────────────────

def test_a_preview_reports_what_would_happen_and_writes_nothing(database):
    text = make_csv([['D:/Library/a.mp3', 'correct', '', '', ''], ['D:/Library/b.mp3', 'incorrect', 'rec-b2', '', '']])
    plan = QaImporter(database).plan(text)
    assert plan['counts']['new'] == 2 and plan['counts']['rejected'] == 0
    assert labels(database) == {}


def test_apply_writes_new_labels_with_the_current_proposal_recorded(database):
    text = make_csv([['D:/Library/a.mp3', 'correct', '', '', ''], ['D:/Library/b.mp3', 'incorrect', 'rec-b2', 'Right', 'Them']])
    result = QaImporter(database).apply(text)
    assert result['written'] == 2
    assert labels(database)['D:/Library/b.mp3'] == ('incorrect', 'rec-b2', 'Right')
    assert QaStore(database).get_label('D:/Library/a.mp3')['proposal_recording_id'] == 'rec-a'
    assert MatchStore(database).recording_status('D:/Library/a.mp3') == 'AUTO_APPROVED'


# ── validation row by row ────────────────────────────────────────────────────────

def test_bad_rows_are_rejected_with_a_reason_and_good_rows_are_still_planned(database):
    text = make_csv([
        ['D:/Library/a.mp3', 'correct', '', '', ''],
        ['G:/Nowhere/x.mp3', 'correct', '', '', ''],                  # not analysed
        ['D:/Library/b.mp3', 'maybe', '', '', ''],                       # unknown label
        ['D:/Library/Sub/c.mp3', 'correct', 'rec-c', '', ''],            # correct cannot carry a correction
        ['G:/Other/c.mp3', 'incorrect', 'rec-zzz', '', ''],            # not one of its candidates
    ])
    plan = QaImporter(database).plan(text)
    assert plan['counts'] == {'new': 1, 'unchanged': 0, 'conflicts': 0, 'rejected': 4, 'skipped': 0}
    reasons = {r['path']: r['reason'] for r in plan['rejected']}
    assert 'not been analysed' in reasons['G:/Nowhere/x.mp3']
    assert 'correct, incorrect or unsure' in reasons['D:/Library/b.mp3']
    assert 'cannot carry a correction' in reasons['D:/Library/Sub/c.mp3']
    assert 'not one of the candidates' in reasons['G:/Other/c.mp3']


def test_a_blank_label_is_skipped_not_rejected(database):
    plan = QaImporter(database).plan(make_csv([['D:/Library/a.mp3', '', '', '', '']]))
    assert plan['counts']['skipped'] == 1 and plan['counts']['rejected'] == 0


def test_labels_are_case_insensitive_and_wrong_means_incorrect(database):
    QaImporter(database).apply(make_csv([['D:/Library/a.mp3', ' Wrong ', '', '', ''], ['D:/Library/b.mp3', 'UNSURE', '', '', '']]))
    assert labels(database)['D:/Library/a.mp3'][0] == 'incorrect' and labels(database)['D:/Library/b.mp3'][0] == 'unsure'


def test_overlong_text_is_rejected(database):
    plan = QaImporter(database).plan(make_csv([['D:/Library/a.mp3', 'incorrect', '', 'x' * 201, '']]))
    assert plan['counts']['rejected'] == 1


def test_the_export_neutralisation_prefix_is_removed_on_the_way_back(database):
    QaImporter(database).apply(make_csv([['D:/Library/a.mp3', 'incorrect', '', "'=SUM(1)", "'@x"]]))
    row = QaStore(database).get_label('D:/Library/a.mp3')
    assert row['corrected_title'] == '=SUM(1)' and row['corrected_artist'] == '@x'


def test_a_normal_leading_apostrophe_is_kept(database):
    QaImporter(database).apply(make_csv([['D:/Library/a.mp3', 'incorrect', '', "'Til Tuesday", '']]))
    assert QaStore(database).get_label('D:/Library/a.mp3')['corrected_title'] == "'Til Tuesday"


# ── the worksheet layout and relative paths ──────────────────────────────────────

WORKSHEET = ['category', 'file', 'tag_title', 'verdict: correct / wrong / unsure (fill in)']


def test_the_worksheet_layout_is_understood_and_a_unique_relative_path_is_matched(database):
    text = make_csv([['x', 'Sub/c.mp3', 'Song C', 'wrong'], ['x', 'a.mp3', 'Song A', 'correct']], header=WORKSHEET)
    plan = QaImporter(database).plan(text)
    assert plan['counts']['new'] == 2
    QaImporter(database).apply(text)
    assert labels(database)['D:/Library/Sub/c.mp3'][0] == 'incorrect'
    assert labels(database)['D:/Library/a.mp3'][0] == 'correct'


def test_an_ambiguous_relative_path_is_rejected_never_guessed(database):
    plan = QaImporter(database).plan(make_csv([['x', 'c.mp3', '', 'correct']], header=WORKSHEET))
    assert plan['counts']['rejected'] == 1 and 'more than one' in plan['rejected'][0]['reason']
    assert labels(database) == {}


def test_a_backslash_path_matches_the_same_file(database):
    plan = QaImporter(database).plan(make_csv([['D:\\Library\\a.mp3', 'correct', '', '', '']]))
    assert plan['counts']['new'] == 1


# ── conflicts keep the existing judgement ────────────────────────────────────────

def test_an_existing_different_label_is_a_conflict_and_is_not_overwritten_by_default(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'correct')
    text = make_csv([['D:/Library/a.mp3', 'incorrect', '', '', '']])
    plan = QaImporter(database).plan(text)
    assert plan['counts']['conflicts'] == 1
    assert plan['conflicts'][0]['existing']['label'] == 'correct' and plan['conflicts'][0]['incoming']['label'] == 'incorrect'
    result = QaImporter(database).apply(text)
    assert result['written'] == 0 and result['kept_existing'] == 1
    assert labels(database)['D:/Library/a.mp3'][0] == 'correct'


def test_the_same_label_again_is_unchanged(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'correct')
    plan = QaImporter(database).plan(make_csv([['D:/Library/a.mp3', 'correct', '', '', '']]))
    assert plan['counts']['unchanged'] == 1 and plan['counts']['conflicts'] == 0


def test_only_the_named_conflicts_are_overwritten_and_the_old_label_is_kept_in_history(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'correct')
    QaStore(database).set_label('D:/Library/b.mp3', 'unsure')
    text = make_csv([['D:/Library/a.mp3', 'incorrect', '', '', ''], ['D:/Library/b.mp3', 'correct', '', '', '']])
    result = QaImporter(database).apply(text, overwrite=['D:/Library/a.mp3'])
    assert result['written'] == 1 and result['kept_existing'] == 1
    assert labels(database)['D:/Library/a.mp3'][0] == 'incorrect' and labels(database)['D:/Library/b.mp3'][0] == 'unsure'
    with database.read() as db:
        history = db.execute('SELECT path,label,source FROM human_label_history').fetchall()
    assert [(h['path'], h['label'], h['source']) for h in history] == [('D:/Library/a.mp3', 'correct', 'csv-import')]


def test_naming_a_path_that_is_not_a_conflict_does_not_make_it_one(database):
    result = QaImporter(database).apply(make_csv([['D:/Library/a.mp3', 'correct', '', '', '']]), overwrite=['D:/Library/b.mp3'])
    assert result['written'] == 1
    assert 'D:/Library/b.mp3' not in labels(database)


def test_changing_a_label_in_the_normal_way_also_keeps_the_previous_one(database):
    store = QaStore(database)
    store.set_label('D:/Library/a.mp3', 'correct')
    store.set_label('D:/Library/a.mp3', 'incorrect', corrected_title='Other')
    with database.read() as db:
        rows = db.execute('SELECT label,source FROM human_label_history WHERE path=?', ('D:/Library/a.mp3',)).fetchall()
    assert [(r['label'], r['source']) for r in rows] == [('correct', 'manual')]


# ── duplicates inside the file ───────────────────────────────────────────────────

def test_identical_duplicate_rows_count_once_and_contradictory_ones_are_both_rejected(database):
    plan = QaImporter(database).plan(make_csv([
        ['D:/Library/a.mp3', 'correct', '', '', ''], ['D:/Library/a.mp3', 'correct', '', '', ''],
        ['D:/Library/b.mp3', 'correct', '', '', ''], ['D:/Library/b.mp3', 'incorrect', '', '', '']]))
    assert plan['counts']['new'] == 1 and plan['counts']['rejected'] == 2
    assert all('contradict' in r['reason'] for r in plan['rejected'])


# ── file-level refusals ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,fragment', [
    ('', 'empty'),
    ('foo,bar\n1,2\n', 'path'),
    ('path,note\nD:/Library/a.mp3,x\n', 'label'),
    ('path,label\nD:/Library/a.mp3,correct\x00\n', 'binary'),
])
def test_a_file_that_is_not_a_usable_label_sheet_is_refused_whole(database, text, fragment):
    with pytest.raises(ValueError) as error:
        QaImporter(database).plan(text)
    assert fragment in str(error.value).lower()


def test_too_many_rows_are_refused(database):
    rows = [[f'G:/x/{i}.mp3', 'correct', '', '', ''] for i in range(MAX_ROWS + 1)]
    with pytest.raises(ValueError) as error:
        QaImporter(database).plan(make_csv(rows))
    assert 'rows' in str(error.value)


def test_a_byte_order_mark_from_a_spreadsheet_is_accepted(database):
    plan = QaImporter(database).plan('\ufeff' + make_csv([['D:/Library/a.mp3', 'correct', '', '', '']]))
    assert plan['counts']['new'] == 1


def test_the_semicolon_layout_some_spreadsheets_write_is_accepted(database):
    plan = QaImporter(database).plan('path;label\nD:/Library/a.mp3;correct\n')
    assert plan['counts']['new'] == 1


# ── atomic ───────────────────────────────────────────────────────────────────────

def test_a_failure_part_way_writes_nothing(database, monkeypatch):
    importer = QaImporter(database)
    original = importer._write_row
    calls = []

    def flaky(db, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('disk trouble')
        return original(db, *args, **kwargs)

    monkeypatch.setattr(importer, '_write_row', flaky)
    with pytest.raises(RuntimeError):
        importer.apply(make_csv([['D:/Library/a.mp3', 'correct', '', '', ''], ['D:/Library/b.mp3', 'correct', '', '', '']]))
    assert labels(database) == {}


# ── round trip with the real export ──────────────────────────────────────────────

def test_an_exported_sheet_imports_into_a_copy_with_the_same_labels(database, tmp_path):
    QaStore(database).set_label('D:/Library/a.mp3', 'correct')
    QaStore(database).set_label('D:/Library/b.mp3', 'incorrect', corrected_recording_id='rec-b2')
    rows = QaStore(database).export_rows()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[k for k in rows[0] if k != 'candidates'])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: v for k, v in row.items() if k != 'candidates'})
    fresh = Database(tmp_path / 'copy.sqlite3')
    fresh.initialize()
    seed(fresh, 'D:/Library/a.mp3', [candidate('rec-a', 'Song A', 'Artist A')], 'AUTO_APPROVED')
    seed(fresh, 'D:/Library/b.mp3', [candidate('rec-b', 'Song B', 'Artist B'), candidate('rec-b2', 'Song B (live)', 'Artist B')],
         'NEEDS_REVIEW')
    result = QaImporter(fresh).apply(out.getvalue())
    assert result['written'] == 2
    assert labels(fresh)['D:/Library/b.mp3'][:2] == ('incorrect', 'rec-b2')


# ── the API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def served(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    seed(running.database, 'D:/Library/a.mp3', [candidate('rec-a', 'Song A', 'Artist A')], 'AUTO_APPROVED')
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def test_the_import_endpoint_needs_the_token(served):
    _, base = served
    assert request(base, 'POST', '/api/qa/import', {'csv': 'path,label\n', 'mode': 'preview'})[0] == 403


def test_preview_then_apply_through_the_api(served):
    running, base = served
    text = make_csv([['D:/Library/a.mp3', 'correct', '', '', '']])
    status, _, body = request(base, 'POST', '/api/qa/import', {'csv': text, 'mode': 'preview'}, token=running.token)
    assert status == 200 and json.loads(body)['counts']['new'] == 1
    assert labels(running.database) == {}
    status, _, body = request(base, 'POST', '/api/qa/import', {'csv': text, 'mode': 'apply'}, token=running.token)
    assert status == 200 and json.loads(body)['written'] == 1 and json.loads(body)['progress']['labelled'] == 1


@pytest.mark.parametrize('payload', [
    {'csv': 5, 'mode': 'preview'}, {'csv': 'path,label\n', 'mode': 'delete'}, {'mode': 'apply'},
    {'csv': 'path,label\n', 'mode': 'apply', 'overwrite': 'all'}, {'csv': 'path,label\n', 'mode': 'apply', 'overwrite': [1]},
    {'csv': 'not a sheet', 'mode': 'preview'},
])
def test_malformed_import_requests_are_refused(served, payload):
    running, base = served
    assert request(base, 'POST', '/api/qa/import', payload, token=running.token)[0] == 400
    assert labels(running.database) == {}


def test_the_page_offers_the_import_with_a_preview_step_and_named_checkboxes():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'id="qaImport"' in page and "mode:'preview'" in page and "mode:'apply'" in page
    assert 'data-overwrite' in page and 'aria-label="Replace my label for' in page
    assert 'Your existing labels are never replaced unless you tick them' in page


def test_an_oversize_field_is_a_refused_sheet_not_a_server_error(database):
    """csv raises its own error type for a field over its default limit; it must reach the user as a 400."""
    with pytest.raises(ValueError) as error:
        QaImporter(database).plan('path,label\nD:/Library/a.mp3,' + 'x' * 200_000 + '\n')
    assert 'field' in str(error.value).lower()
