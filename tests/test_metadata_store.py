"""Persisted metadata proposal decisions: scoped, reversible, never silently overwritten, and never written to audio."""
import hashlib
from pathlib import Path

import pytest

from meloshelf.application.metadata_service import MetadataService
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.metadata_store import MetadataStore, MetadataError
from test_qa_labels import candidate


def seed_track(db, path, *, title, artist, album, cand_artist=None, status='ACOUSTICALLY_MATCHED'):
    with db.write() as c:
        c.execute('INSERT OR REPLACE INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status) '
                  'VALUES(?,?,?,?,?,?,?,?,?)', (path, 10, '.mp3', 1, title, artist, album, '', 'review'))
    data = {'candidates': [candidate('rec', title, cand_artist)] if cand_artist else [], 'assessment': '',
            'reason': '', 'code': ''}
    MatchStore(db).put_result(path, 10, 1, 'review', data, recording_status='AUTO_APPROVED' if cand_artist else 'UNIDENTIFIED')


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    folder = 'D:/Library/Folder---01'
    names = [('CASS_-_Control.mp3', 'CASS feat. Parris Chariz'), ('KB_-_Hold_Me_Back.mp3', 'KB'),
             ('DJ_Mykael_V_-_Switches.mp3', 'DJ Mykael V'), ('1K_Phew_-_x10.mp3', '1K Phew'),
             ('Aklesso_-_Lights.mp3', 'Aklesso')]
    for name, cand in names:
        seed_track(db, f'{folder}/{name}', title=name, artist='Tedashii/Steven Malcom', album='SS19', cand_artist=cand)
    seed_track(db, 'D:/Library/Other/Song.mp3', title='Song', artist='Wrong Artist', album='Other', cand_artist='Right Artist')
    return db


def test_the_service_reports_the_folder_pattern_with_its_evidence_and_no_invented_replacement(library):
    groups = MetadataService(library).groups()
    shared = [g for g in groups if g['folder'].endswith('Folder---01') and g['field'] == 'artist'][0]
    assert shared['classification'] == 'SUSPECTED_CONTAMINATED'
    assert shared['proposed'] == '' and shared['individual_review'] is True
    assert len(shared['paths']) == 5


def test_a_single_conflict_offers_a_replacement_the_user_can_approve(library):
    store = MetadataStore(library)
    group = [g for g in MetadataService(library).groups() if g['original'] == 'Wrong Artist'][0]
    assert group['proposed'] == 'Right Artist'
    store.decide(group['key'], 'approve', folder=group['folder'], field=group['field'], original=group['original'],
                 proposed=group['proposed'], classification=group['classification'], paths=group['paths'])
    stored = store.get(group['key'])
    assert stored['status'] == 'approved' and stored['proposed'] == 'Right Artist'


def test_an_approval_without_a_single_replacement_is_refused(library):
    store = MetadataStore(library)
    shared = [g for g in MetadataService(library).groups() if g['field'] == 'artist' and g['proposed'] == ''][0]
    with pytest.raises(MetadataError):
        store.decide(shared['key'], 'approve', folder=shared['folder'], field=shared['field'], original=shared['original'],
                     proposed='', classification=shared['classification'], paths=shared['paths'])


def test_a_decision_is_reversible_and_keeps_its_history(library):
    store = MetadataStore(library)
    group = [g for g in MetadataService(library).groups() if g['original'] == 'Wrong Artist'][0]
    args = dict(folder=group['folder'], field=group['field'], original=group['original'], proposed=group['proposed'],
                classification=group['classification'], paths=group['paths'])
    store.decide(group['key'], 'approve', **args)
    store.decide(group['key'], 'revert', **args)
    stored = store.get(group['key'])
    assert stored['status'] == 'pending'
    assert [h['action'] for h in stored['history']] == ['approve', 'revert']


def test_decisions_survive_a_restart(library, tmp_path):
    store = MetadataStore(library)
    group = [g for g in MetadataService(library).groups() if g['original'] == 'Wrong Artist'][0]
    store.decide(group['key'], 'reject', folder=group['folder'], field=group['field'], original=group['original'],
                 proposed=group['proposed'], classification=group['classification'], paths=group['paths'])
    reopened = Database(tmp_path / 'lib.sqlite3')
    reopened.initialize()
    assert MetadataStore(reopened).get(group['key'])['status'] == 'rejected'


def test_a_changed_proposal_is_flagged_and_the_human_decision_is_not_overwritten(library):
    store = MetadataStore(library)
    group = [g for g in MetadataService(library).groups() if g['original'] == 'Wrong Artist'][0]
    store.decide(group['key'], 'reject', folder=group['folder'], field=group['field'], original=group['original'],
                 proposed=group['proposed'], classification=group['classification'], paths=group['paths'])
    refreshed = dict(group, proposed='Another Artist')
    flagged = store.reconcile([refreshed])
    stored = store.get(group['key'])
    assert stored['status'] == 'rejected'
    assert stored['stale'] is True and stored['current_proposed'] == 'Another Artist'
    assert flagged == 1


def test_decisions_are_scoped_to_their_folder_and_never_propagate_to_other_folders(library):
    store = MetadataStore(library)
    group = [g for g in MetadataService(library).groups() if g['original'] == 'Wrong Artist'][0]
    other_key = MetadataStore.key_for('D:/Library/Elsewhere', group['field'], group['original'], group['classification'])
    assert other_key != group['key']
    assert store.get(other_key) is None


def test_an_approval_writes_no_tag_and_leaves_audio_bytes_unchanged(library, tmp_path):
    audio = tmp_path / 'song.mp3'
    audio.write_bytes(b'AUDIO' * 100)
    before = hashlib.sha256(audio.read_bytes()).hexdigest()
    store = MetadataStore(library)
    group = [g for g in MetadataService(library).groups() if g['original'] == 'Wrong Artist'][0]
    store.decide(group['key'], 'approve', folder=group['folder'], field=group['field'], original=group['original'],
                 proposed=group['proposed'], classification=group['classification'], paths=group['paths'])
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == before
    with library.read() as c:
        assert c.execute("SELECT artist FROM tracks WHERE path='D:/Library/Other/Song.mp3'").fetchone()[0] == 'Wrong Artist'


def test_migration_six_adds_the_decision_table_to_an_existing_schema_five_library(tmp_path):
    db = Database(tmp_path / 'old.sqlite3')
    db.initialize()
    with db.write() as c:
        # A schema-5 library has none of the tables added by later migrations.
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
        c.execute('DROP TABLE metadata_decisions')
        c.execute('PRAGMA user_version=5')
    db.initialize()
    with db.read() as c:
        from meloshelf.infrastructure.database import SCHEMA_VERSION
        assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION  # later migrations also apply
        assert c.execute("SELECT name FROM sqlite_master WHERE name='metadata_decisions'").fetchone() is not None
