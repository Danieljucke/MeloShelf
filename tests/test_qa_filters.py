"""Qualification filters and sample categories: the validation queue, the bulk list and the accuracy report can be
narrowed by status, category, confidence, artist, album and version. Categories are derived from the current
files and proposals each time, so they never go stale; the accuracy report can be broken down by category."""
import json
import threading
import time

import pytest

from meloshelf.application.webapp import create_server
from meloshelf.domain.sample_category import CATEGORIES, categorize
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.qa_bulk import QaBulk
from meloshelf.infrastructure.qa_filters import parse_filter
from meloshelf.infrastructure.qa_store import QaStore
from meloshelf.infrastructure.secrets import MemorySecretStore
from test_qa_api import request
from test_qa_labels import candidate, seed


# ── categories ───────────────────────────────────────────────────────────────────

def cat(path, title='', artist='', album='', proposal=None):
    return categorize(path=path, title=title, artist=artist, album=album, proposed_title=(proposal or ('', ''))[0],
                      proposed_artist=(proposal or ('', ''))[1], version_conflict=False)


def test_the_five_categories_have_stable_keys_and_labels():
    assert [c.key for c in CATEGORIES] == ['tagged_and_named', 'named_no_metadata', 'numeric_generic_names',
                                           'conflicting_tags', 'singles_collabs_rare_live', 'other']
    assert all(c.label for c in CATEGORIES)


def test_a_file_with_title_and_artist_tags_and_a_plain_name_is_tagged_and_named():
    assert cat('G:/M/Danny Gokey - Fighter.mp3', 'Fighter', 'Danny Gokey') == 'tagged_and_named'


def test_a_readable_name_without_title_or_artist_tags_is_named_no_metadata():
    assert cat('G:/M/Darius_James_-_Flower_Pots.mp3') == 'named_no_metadata'


@pytest.mark.parametrize('name', ['AUD-20200124-WA0037.mp3', '03 Whom Shall I Fear.mp3', 'track 07.mp3', '0012.mp3',
                                  'IMG_4410.mp3', 'Untitled.mp3'])
def test_numeric_or_generic_file_names(name):
    assert cat('G:/M/' + name) == 'numeric_generic_names'


def test_tags_that_disagree_with_the_proposal_are_conflicting():
    assert cat('G:/M/x.mp3', 'Big Wave', 'Tedashii/Steven Malcom', proposal=('Big Wave', 'Lecrae')) == 'conflicting_tags'


def test_tags_that_agree_with_or_contain_the_proposal_are_not_conflicting():
    assert cat('G:/M/Song.mp3', 'Song', 'The Band', proposal=('Song (Remastered)', 'The Band')) != 'conflicting_tags'
    assert cat('G:/M/Song.mp3', 'SONG!', 'the band', proposal=('Song', 'The Band')) == 'tagged_and_named'


def test_no_conflict_is_claimed_without_a_proposal_or_without_tags():
    assert cat('G:/M/Song.mp3', 'Song', 'The Band') == 'tagged_and_named'
    assert cat('G:/M/Song.mp3', '', '', proposal=('Other', 'Else')) == 'named_no_metadata'


@pytest.mark.parametrize('name', ['A - B (feat. C).mp3', 'Band - Song (Live).mp3', 'Band - Song (Remix).mp3',
                                  'Band - Song ft Someone.mp3', 'A & B - Song.mp3', 'Band - Song (Official Music Video).mp3'])
def test_collaborations_live_and_remix_markers(name):
    assert cat('G:/M/' + name) == 'singles_collabs_rare_live'


def test_a_version_conflict_on_the_proposal_counts_as_rare_live():
    assert categorize(path='G:/M/Song.mp3', title='Song', artist='Band', album='', proposed_title='Song',
                      proposed_artist='Band', version_conflict=True) == 'singles_collabs_rare_live'


def test_the_order_is_generic_then_conflict_then_markers_then_tagged():
    assert cat('G:/M/03 Song (Live).mp3', 'Song', 'Band') == 'numeric_generic_names'
    assert cat('G:/M/Song (Live).mp3', 'Song', 'Band', proposal=('Other', 'Else')) == 'conflicting_tags'
    assert cat('G:/M/Song (Live).mp3', 'Song', 'Band') == 'singles_collabs_rare_live'


def test_a_file_that_fits_nothing_is_other():
    assert cat('G:/M/x.mp3', '', '', '') == 'other'


# ── filter parsing ───────────────────────────────────────────────────────────────

def test_an_empty_query_is_the_empty_filter():
    flt = parse_filter({})
    assert flt.is_empty() and flt.category == ''


@pytest.mark.parametrize('query', [
    {'category': ['nonsense']}, {'score_min': ['abc']}, {'score_min': ['-1']}, {'score_max': ['101']},
    {'score_min': ['80'], 'score_max': ['20']}, {'provider_min': ['2']}, {'version': ['maybe']},
    {'artist': ['x' * 201]},
])
def test_invalid_filter_values_are_refused(query):
    with pytest.raises(ValueError):
        parse_filter(query)


def test_valid_values_are_read():
    flt = parse_filter({'category': ['conflicting_tags'], 'score_min': ['40'], 'score_max': ['90'], 'provider_min': ['0.9'],
                        'artist': [' Lecrae '], 'album': ['Anchor'], 'version': ['conflict'], 'status': ['NEEDS_REVIEW']})
    assert (flt.category, flt.score_min, flt.score_max, flt.provider_min, flt.artist, flt.album, flt.version, flt.status) == (
        'conflicting_tags', 40, 90, 0.9, 'Lecrae', 'Anchor', 'conflict', 'NEEDS_REVIEW')


# ── the library under test ───────────────────────────────────────────────────────

def scored(recording_id, title, artist, score, provider=0.97, conflict=False):
    c = candidate(recording_id, title, artist, provider=provider, score=score)
    c['evidence']['version_conflict'] = conflict
    return c


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed(db, 'G:/M/Anchor/Danny Gokey - Fighter.mp3', [scored('r1', 'Fighter', 'Danny Gokey', 95)], 'AUTO_APPROVED',
         title='Fighter', artist='Danny Gokey')
    seed(db, 'G:/M/Anchor/Darius_James_-_Flower_Pots.mp3', [scored('r2', 'Flower Pots', 'Darius James', 60, provider=0.7)],
         'NEEDS_REVIEW')
    seed(db, 'G:/M/Other/03 Whom Shall I Fear.mp3', [scored('r3', 'Whom Shall I Fear', 'Chris Tomlin', 85)], 'NEEDS_REVIEW')
    seed(db, 'G:/M/Other/Big Wave.mp3', [scored('r4', 'Big Wave', 'Lecrae', 50, provider=0.6)], 'NEEDS_REVIEW',
         title='Big Wave', artist='Tedashii/Steven Malcom')
    seed(db, 'G:/M/Other/CASS - Control (feat. X).mp3', [scored('r5', 'Control', 'CASS', 79, conflict=True)], 'NEEDS_REVIEW')
    with db.write() as conn:
        conn.execute("UPDATE tracks SET album='Anchor' WHERE path LIKE 'G:/M/Anchor/%'")
        conn.execute("UPDATE tracks SET album='Misc' WHERE path LIKE 'G:/M/Other/%'")
    return db


def listed(database, **query):
    flt = parse_filter({k: [str(v)] for k, v in query.items()})
    return [r['filename'] for r in QaBulk(database).candidates(filters=flt, limit=100)['items']]


def test_each_filter_narrows_the_bulk_list(database):
    assert len(listed(database)) == 5
    assert listed(database, status='AUTO_APPROVED') == ['Danny Gokey - Fighter.mp3']
    assert listed(database, category='numeric_generic_names') == ['03 Whom Shall I Fear.mp3']
    assert listed(database, category='conflicting_tags') == ['Big Wave.mp3']
    assert listed(database, category='singles_collabs_rare_live') == ['CASS - Control (feat. X).mp3']
    assert sorted(listed(database, score_min=80)) == ['03 Whom Shall I Fear.mp3', 'Danny Gokey - Fighter.mp3']
    assert sorted(listed(database, score_max=60)) == ['Big Wave.mp3', 'Darius_James_-_Flower_Pots.mp3']
    assert sorted(listed(database, provider_min=0.9)) == ['03 Whom Shall I Fear.mp3', 'CASS - Control (feat. X).mp3',
                                                          'Danny Gokey - Fighter.mp3']
    assert sorted(listed(database, album='anchor')) == ['Danny Gokey - Fighter.mp3', 'Darius_James_-_Flower_Pots.mp3']
    assert listed(database, artist='tedashii') == ['Big Wave.mp3']
    assert listed(database, version='conflict') == ['CASS - Control (feat. X).mp3']
    assert len(listed(database, version='clean')) == 4


def test_filters_combine(database):
    assert listed(database, album='Misc', score_min=80) == ['03 Whom Shall I Fear.mp3']
    assert listed(database, album='Misc', category='tagged_and_named') == []


def test_filter_text_is_matched_literally(database):
    assert listed(database, artist='%') == []
    assert listed(database, album="x' OR '1'='1") == []


def test_the_total_and_paging_count_the_filtered_set_even_for_a_category(database):
    flt = parse_filter({'album': ['Misc']})
    page = QaBulk(database).candidates(filters=flt, limit=2)
    assert page['total'] == 3 and len(page['items']) == 2
    flt = parse_filter({'category': ['named_no_metadata']})
    assert QaBulk(database).candidates(filters=flt)['total'] == 1


def test_the_validation_queue_honours_the_filters_and_still_skips_labelled_ones(database):
    qa = QaStore(database)
    flt = parse_filter({'category': ['numeric_generic_names']})
    assert qa.next_item(filters=flt) == 'G:/M/Other/03 Whom Shall I Fear.mp3'
    qa.set_label('G:/M/Other/03 Whom Shall I Fear.mp3', 'correct')
    assert qa.next_item(filters=flt) is None
    assert qa.next_item(filters=parse_filter({'version': ['conflict']})) == 'G:/M/Other/CASS - Control (feat. X).mp3'


def test_the_queue_without_filters_behaves_as_before(database):
    assert QaStore(database).next_item() == 'G:/M/Anchor/Danny Gokey - Fighter.mp3'
    assert QaStore(database).next_item(status='NEEDS_REVIEW', skip=['G:/M/Anchor/Darius_James_-_Flower_Pots.mp3']) == \
        'G:/M/Other/03 Whom Shall I Fear.mp3'


# ── the report by category ───────────────────────────────────────────────────────

def test_the_accuracy_report_is_broken_down_by_category(database):
    qa = QaStore(database)
    qa.set_label('G:/M/Anchor/Danny Gokey - Fighter.mp3', 'correct')
    qa.set_label('G:/M/Other/Big Wave.mp3', 'incorrect')
    qa.set_label('G:/M/Other/03 Whom Shall I Fear.mp3', 'unsure')
    by = qa.report()['by_category']
    assert by['tagged_and_named']['judged'] == 1 and by['tagged_and_named']['precision'] == 1.0
    assert by['conflicting_tags']['incorrect'] == 1 and by['conflicting_tags']['precision'] == 0.0
    assert by['numeric_generic_names']['unsure'] == 1 and by['numeric_generic_names']['precision'] is None
    assert by['named_no_metadata']['labelled'] == 0
    assert set(by) == {c.key for c in CATEGORIES}


def test_a_small_category_sample_is_flagged_as_insufficient(database):
    QaStore(database).set_label('G:/M/Anchor/Danny Gokey - Fighter.mp3', 'correct')
    assert QaStore(database).report()['by_category']['tagged_and_named']['sufficient_sample'] is False


# ── the API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def served(tmp_path):
    html = tmp_path / 'index.html'
    html.write_text('TOKEN: __MELOSHELF_TOKEN__', encoding='utf-8')
    running = create_server(db_path=tmp_path / 'lib.sqlite3', html_path=html, secret_store=MemorySecretStore(), environ={})
    seed(running.database, 'G:/M/Danny Gokey - Fighter.mp3', [scored('r1', 'Fighter', 'Danny Gokey', 95)], 'AUTO_APPROVED',
         title='Fighter', artist='Danny Gokey')
    seed(running.database, 'G:/M/03 Song.mp3', [scored('r3', 'Song', 'Band', 85)], 'NEEDS_REVIEW')
    thread = threading.Thread(target=running.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    yield running, f'http://127.0.0.1:{running.server_address[1]}'
    running.shutdown()
    running.server_close()


def test_the_queue_and_the_bulk_list_accept_filters_through_the_api(served):
    _, base = served
    assert json.loads(request(base, 'GET', '/api/qa/next?category=numeric_generic_names')[2])['filename'] == '03 Song.mp3'
    page = json.loads(request(base, 'GET', '/api/qa/bulk/candidates?score_min=90')[2])
    assert page['total'] == 1 and page['items'][0]['filename'] == 'Danny Gokey - Fighter.mp3'
    assert page['items'][0]['category'] == 'tagged_and_named'


@pytest.mark.parametrize('path', ['/api/qa/next?category=nope', '/api/qa/bulk/candidates?score_min=x',
                                  '/api/qa/bulk/candidates?version=maybe'])
def test_bad_filters_are_a_400_not_a_crash(served, path):
    _, base = served
    assert request(base, 'GET', path)[0] == 400


def test_the_category_list_is_served_for_the_page(served):
    _, base = served
    data = json.loads(request(base, 'GET', '/api/qa/categories')[2])
    assert [c['key'] for c in data['categories']] == [c.key for c in CATEGORIES]


# ── scale ────────────────────────────────────────────────────────────────────────

def test_a_category_filter_over_ten_thousand_proposals_stays_interactive(tmp_path):
    db = Database(tmp_path / 'big.sqlite3')
    db.initialize()
    with db.write() as conn:          # one transaction: seeding 10,000 rows one commit at a time would dominate the test
        for i in range(10_000):
            path = f'G:/B/{i % 100:02d} Song {i}.mp3' if i % 3 else f'G:/B/Artist - Song {i}.mp3'
            conn.execute('INSERT INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status) VALUES(?,?,?,?,?,?,?,?,?)',
                         (path, 10, '.mp3', 1, 'T' if i % 2 else '', 'A' if i % 2 else '', 'Alb', '', 'review'))
            data = {'candidates': [scored(f'r{i}', f'Song {i}', 'A', 50 + i % 50)], 'assessment': '', 'reason': '', 'code': ''}
            conn.execute("INSERT INTO match_results(path,file_size,file_mtime_ns,status,data,created_at,decision,recording_status) "
                         "VALUES(?,?,?,?,?,?,'pending','NEEDS_REVIEW')", (path, 10, 1, 'review', json.dumps(data), 1))
    flt = parse_filter({'category': ['numeric_generic_names'], 'score_min': ['60']})
    started = time.perf_counter()
    page = QaBulk(db).candidates(filters=flt, limit=25)
    elapsed = time.perf_counter() - started
    assert page['total'] > 0 and len(page['items']) == 25
    assert elapsed < 5.0, f'{elapsed:.2f}s'


def test_the_page_offers_the_filters_and_the_category_breakdown():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')
    assert 'id="qaFilters"' in page and 'data-qf="category"' in page and 'data-qf="score_min"' in page
    assert 'qfQuery()' in page and 'By kind of file' in page and 'not the whole library' in page


def test_a_file_with_only_one_of_title_and_artist_and_a_readable_name_is_in_the_incomplete_tags_category():
    assert cat('G:/M/Do You Believe.mp3', '', '7eventh Time Down') == 'named_no_metadata'
    assert cat('G:/M/Do You Believe.mp3', 'Do You Believe', '') == 'named_no_metadata'
