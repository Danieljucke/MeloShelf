"""Correction memory: an append-only, scope-aware log of human confirmations, reversals and conflicts.

Only a person can confirm or reverse a correction. An analysis run may raise a conflict; it never confirms,
and an unverified suggestion can never become evidence for a later automatic decision.
"""
import pytest

from meloshelf.application.metadata_service import MetadataService
from meloshelf.infrastructure.correction_memory import CorrectionMemory, MemoryError
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.metadata_store import MetadataStore
from test_metadata_store import seed_track

FOLDER = 'D:/Library/Folder---01'


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed_track(db, 'D:/Library/Other/Song.mp3', title='Song', artist='Wrong Artist', album='Other', cand_artist='Right Artist')
    seed_track(db, f'{FOLDER}/CASS.mp3', title='CASS', artist='Tedashii/Steven Malcom', album='SS19', cand_artist='CASS')
    return db


def group_for(db, original):
    return [g for g in MetadataService(db).groups() if g['original'] == original][0]


def decide(db, group, action):
    MetadataStore(db).decide(group['key'], action, folder=group['folder'], field=group['field'],
                             original=group['original'], proposed=group['proposed'],
                             classification=group['classification'], paths=group['paths'])


def test_a_confirmation_is_recorded_as_an_event_with_its_source(library):
    decide(library, group_for(library, 'Wrong Artist'), 'approve')
    events = CorrectionMemory(library).history(group_for(library, 'Wrong Artist')['key'])
    assert [e['event'] for e in events] == ['confirm']
    assert events[0]['source'] == 'human' and events[0]['corrected'] == 'Right Artist'


def test_a_reversal_is_a_new_event_so_the_full_history_is_kept(library):
    group = group_for(library, 'Wrong Artist')
    decide(library, group, 'approve')
    decide(library, group, 'revert')
    events = CorrectionMemory(library).history(group['key'])
    assert [e['event'] for e in events] == ['confirm', 'revert']
    assert CorrectionMemory(library).active_for(group['folder'], group['field'], group['original']) is None


def test_a_confirmed_correction_is_reused_only_inside_its_own_folder(library):
    group = group_for(library, 'Wrong Artist')
    decide(library, group, 'approve')
    memory = CorrectionMemory(library)
    assert memory.active_for('D:/Library/Other', 'artist', 'Wrong Artist')['corrected'] == 'Right Artist'
    assert memory.active_for('D:/Library/Elsewhere', 'artist', 'Wrong Artist') is None


def test_a_later_analysis_that_changes_the_proposal_raises_a_conflict_and_does_not_apply_the_old_value(library):
    group = group_for(library, 'Wrong Artist')
    decide(library, group, 'approve')
    changed = dict(group, proposed='Different Artist')
    raised = CorrectionMemory(library).reconcile([changed])
    assert raised == 1
    events = CorrectionMemory(library).history(group['key'])
    assert events[-1]['event'] == 'conflict' and events[-1]['source'] == 'analysis'
    assert CorrectionMemory(library).active_for(group['folder'], 'artist', 'Wrong Artist') is None


def test_an_analysis_run_alone_never_creates_a_confirmation(library):
    group = group_for(library, 'Wrong Artist')
    CorrectionMemory(library).reconcile([dict(group, proposed='Right Artist')])
    assert CorrectionMemory(library).history(group['key']) == []


def test_only_human_sources_can_confirm_or_revert(library):
    group = group_for(library, 'Wrong Artist')
    with pytest.raises(MemoryError):
        CorrectionMemory(library).append(group['key'], 'confirm', source='suggestion', folder=group['folder'],
                                         field='artist', original='Wrong Artist', corrected='Right Artist')


def test_a_suggestion_from_memory_for_a_new_track_is_offered_but_stays_pending_review(library):
    decide(library, group_for(library, 'Wrong Artist'), 'approve')
    seed_track(library, 'D:/Library/Other/New.mp3', title='New', artist='Wrong Artist', album='Other', cand_artist=None)
    fresh = [g for g in MetadataService(library).groups(folder='D:/Library/Other')]
    annotated = CorrectionMemory(library).annotate(fresh)
    for group in annotated:
        if group['original'] == 'Wrong Artist':
            assert group['previously_confirmed'] == 'Right Artist'
            assert group['individual_review'] is True


def test_the_history_survives_a_restart(library, tmp_path):
    group = group_for(library, 'Wrong Artist')
    decide(library, group, 'approve')
    reopened = Database(tmp_path / 'lib.sqlite3')
    reopened.initialize()
    assert [e['event'] for e in CorrectionMemory(reopened).history(group['key'])] == ['confirm']


def test_migration_seven_adds_the_correction_log_to_a_schema_six_library(tmp_path):
    db = Database(tmp_path / 'old.sqlite3')
    db.initialize()
    with db.write() as c:
        # A schema-6 library has none of the tables added by later migrations.
        c.execute('DROP TABLE artwork_decisions')
        c.execute('DROP TABLE artist_images')
        c.execute('DROP TABLE human_label_history')  # created by the later migration 18
        c.execute('DROP TABLE human_label_batch_items')  # created by the later migration 19
        c.execute('DROP TABLE human_label_batches')
        c.execute('DROP TABLE human_version_label_history')
        c.execute('DROP TABLE human_version_labels')  # created by the later migration 20
        c.execute('DROP TABLE file_instance_history')
        c.execute('DROP TABLE file_instances')
        c.execute('DROP TABLE artist_link_decisions')
        c.execute('DROP TABLE artist_enrichment_items')
        c.execute('DROP TABLE artist_enrichment_jobs')
        c.execute('DROP TABLE artist_links')
        c.execute('DROP TABLE artist_corrections')
        c.execute('DROP TABLE fallback_results')
        c.execute('DROP TABLE correction_events')
        c.execute('PRAGMA user_version=6')
    db.initialize()
    from meloshelf.infrastructure.database import SCHEMA_VERSION
    with db.read() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert c.execute("SELECT name FROM sqlite_master WHERE name='correction_events'").fetchone() is not None
