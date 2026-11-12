"""Human reference labels: stored separately from automatic decisions, validated, resumable, never overwritten by scans."""
import json

import pytest

from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.qa_store import QaError, QaStore


def candidate(recording_id, title, artist, provider=0.97, score=90):
    return {'recording_id': recording_id, 'title': title, 'artist': artist, 'acoustid_score': provider,
            'duration': 200.0, 'releases': [], 'release_details': [],
            'evidence': {'provider_score': provider, 'score': score, 'title': 'match', 'artist': 'match',
                         'duration': 'match', 'duration_delta': 0.0, 'notes': [], 'version_conflict': False}}


def seed(database, path, candidates, recording_status, *, title='', artist=''):
    """A track and its stored proposal, written through the same stores the application uses."""
    with database.write() as db:
        db.execute('INSERT OR REPLACE INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (path, 10, '.mp3', 1, title, artist, '', '', 'review'))
    MatchStore(database).put_result(path, 10, 1, 'review', {'candidates': candidates, 'assessment': 'strong',
                                    'reason': '', 'code': ''}, recording_status=recording_status)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed(db, 'D:/Library/a.mp3', [candidate('rec-a', 'Song A', 'Artist A')], 'AUTO_APPROVED', title='Song A', artist='Artist A')
    seed(db, 'D:/Library/b.mp3', [candidate('rec-b', 'Song B', 'Artist B', provider=0.6, score=55),
                                candidate('rec-b2', 'Song B (live)', 'Artist B')], 'NEEDS_REVIEW')
    seed(db, 'D:/Library/c.mp3', [], 'UNIDENTIFIED')
    return db


# ── separation from automatic decisions and persistence ───────────────────────

def test_a_label_is_stored_separately_from_the_automatic_status(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'correct')
    assert MatchStore(database).recording_status('D:/Library/a.mp3') == 'AUTO_APPROVED'
    assert QaStore(database).get_label('D:/Library/a.mp3')['label'] == 'correct'


def test_a_user_decision_does_not_change_the_human_label_and_back(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'incorrect')
    MatchStore(database).set_decision('D:/Library/a.mp3', 'rejected', recording_id='rec-a')
    assert QaStore(database).get_label('D:/Library/a.mp3')['label'] == 'incorrect'


def test_a_rescan_that_rewrites_the_proposal_never_erases_the_human_label(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'incorrect', corrected_title='Other song')
    MatchStore(database).put_result('D:/Library/a.mp3', 10, 1, 'review', {'candidates': [], 'assessment': '',
                                    'reason': 'no_match', 'code': 'no_match'}, recording_status='UNIDENTIFIED')
    kept = QaStore(database).get_label('D:/Library/a.mp3')
    assert kept['label'] == 'incorrect' and kept['corrected_title'] == 'Other song'


def test_labels_survive_a_restart(database, tmp_path):
    QaStore(database).set_label('D:/Library/a.mp3', 'unsure')
    reopened = Database(tmp_path / 'lib.sqlite3')
    reopened.initialize()
    assert QaStore(reopened).get_label('D:/Library/a.mp3')['label'] == 'unsure'


def test_a_label_records_which_proposal_it_judged(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'correct')
    assert QaStore(database).get_label('D:/Library/a.mp3')['proposal_recording_id'] == 'rec-a'


# ── validation ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('bad', ['maybe', '', 'CORRECT ', None])
def test_only_correct_incorrect_or_unsure_are_accepted(database, bad):
    with pytest.raises(QaError):
        QaStore(database).set_label('D:/Library/a.mp3', bad)


def test_an_unanalysed_file_cannot_be_labelled(database):
    with pytest.raises(QaError):
        QaStore(database).set_label('D:/Library/unknown.mp3', 'correct')


def test_an_alternative_must_be_one_of_the_stored_candidates(database):
    with pytest.raises(QaError):
        QaStore(database).set_label('D:/Library/b.mp3', 'incorrect', corrected_recording_id='not-a-candidate')
    QaStore(database).set_label('D:/Library/b.mp3', 'incorrect', corrected_recording_id='rec-b2')
    assert QaStore(database).get_label('D:/Library/b.mp3')['corrected_recording_id'] == 'rec-b2'


def test_a_human_entered_correction_is_kept_as_text_and_length_limited(database):
    QaStore(database).set_label('D:/Library/c.mp3', 'incorrect', corrected_title='Real Title', corrected_artist='Real Artist')
    assert QaStore(database).get_label('D:/Library/c.mp3')['corrected_title'] == 'Real Title'
    with pytest.raises(QaError):
        QaStore(database).set_label('D:/Library/c.mp3', 'incorrect', corrected_title='x' * 300)


def test_a_correct_label_cannot_carry_a_correction(database):
    with pytest.raises(QaError):
        QaStore(database).set_label('D:/Library/a.mp3', 'correct', corrected_title='Something else')


# ── progress, resume and priority ─────────────────────────────────────────────

def test_progress_counts_proposals_and_labels(database):
    qa = QaStore(database)
    qa.set_label('D:/Library/a.mp3', 'correct')
    progress = qa.progress()
    assert progress['proposals'] == 2  # the unidentified file is not a proposal
    assert progress['labelled'] == 1 and progress['correct'] == 1 and progress['remaining'] == 1


def test_next_item_prefers_automatic_approvals_then_review_and_skips_labelled_items(database):
    qa = QaStore(database)
    assert qa.next_item() == 'D:/Library/a.mp3'
    qa.set_label('D:/Library/a.mp3', 'correct')
    assert qa.next_item() == 'D:/Library/b.mp3'
    qa.set_label('D:/Library/b.mp3', 'unsure')
    assert qa.next_item() is None


def test_a_status_filter_restricts_the_queue(database):
    assert QaStore(database).next_item(status='NEEDS_REVIEW') == 'D:/Library/b.mp3'


def test_a_label_is_flagged_stale_when_the_proposal_changes(database):
    QaStore(database).set_label('D:/Library/a.mp3', 'correct')
    MatchStore(database).put_result('D:/Library/a.mp3', 10, 1, 'review',
                                    {'candidates': [candidate('rec-new', 'Song A', 'Artist A')], 'assessment': 'strong',
                                     'reason': '', 'code': ''}, recording_status='AUTO_APPROVED')
    assert QaStore(database).get_label('D:/Library/a.mp3')['stale'] is True
    assert QaStore(database).progress()['stale'] == 1


# ── export ──────────────────────────────────────────────────────────────────────

def test_export_rows_carry_the_evidence_needed_for_an_external_audit(database):
    QaStore(database).set_label('D:/Library/b.mp3', 'incorrect', corrected_recording_id='rec-b2')
    row = [r for r in QaStore(database).export_rows() if r['path'] == 'D:/Library/b.mp3'][0]
    for key in ('path', 'proposed_recording_id', 'meloshelf_status', 'provider_score', 'meloshelf_score',
                'label', 'corrected_recording_id', 'corrected_title', 'corrected_artist', 'candidates'):
        assert key in row
    assert row['label'] == 'incorrect' and row['corrected_recording_id'] == 'rec-b2'
    assert isinstance(json.loads(json.dumps(row['candidates'])), list)


def test_next_can_skip_items_without_judging_them(database):
    qa = QaStore(database)
    assert qa.next_item(skip=['D:/Library/a.mp3']) == 'D:/Library/b.mp3'
    assert qa.get_label('D:/Library/a.mp3') is None  # skipping is not judging


def test_the_item_flags_a_version_conflict_recorded_only_as_a_note(database):
    legacy = candidate('rec-live', 'Song (Live)', 'Artist')
    legacy['evidence'].pop('version_conflict')
    legacy['evidence']['notes'] = ['Version differs: live in the tag, standard in the candidate']
    seed(database, 'D:/Library/legacy.mp3', [legacy], 'NEEDS_REVIEW')
    assert QaStore(database).item('D:/Library/legacy.mp3')['candidates'][0]['version_conflict'] is True


def test_the_item_carries_a_structured_version_assessment_for_each_candidate(database):
    reckless = candidate('rec-rl', 'Reckless Love', 'Artist')
    reckless['evidence']['duration'] = 'match'
    seed(database, 'D:/Library/rl.mp3', [reckless], 'AUTO_APPROVED', title='Reckless Love (Spontaneous) [Live]', artist='Artist')
    version = QaStore(database).item('D:/Library/rl.mp3')['candidates'][0]['version']
    assert version['verdict'] == 'possible_conflict'
    assert version['local'] == ['live', 'spontaneous'] and version['candidate'] == []
    assert version['reasons']
