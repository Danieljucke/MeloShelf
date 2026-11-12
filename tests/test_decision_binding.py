"""Human recognition decisions follow the physical file, never a path alone.

Real temporary audio files are used throughout (never the music library). A decision moves with its file instance
when the same instance reappears at exactly one new path with the same size and modification time. A decision whose
file cannot be identified is kept and counted for review, and an archived decision is never applied to other content."""
import json
import os
import shutil
import sqlite3

import pytest

from audio_fixtures import write_mp3
from meloshelf.application.jobs import JobService
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.file_identity import FileRegistry
from meloshelf.infrastructure.match_store import MatchStore

CANDIDATE = {'recording_id': 'rec-1', 'title': 'Song', 'artist': 'Band', 'acoustid_score': 0.99, 'duration': 4.0,
             'releases': [], 'release_details': [],
             'evidence': {'score': 90, 'title': 'match', 'artist': 'match', 'duration': 'match', 'duration_delta': 0.0,
                          'notes': [], 'version_conflict': False, 'provider_score': 0.99}}


@pytest.fixture
def world(tmp_path):
    lib = tmp_path / 'lib'
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    return {'lib': lib, 'db': db, 'store': MatchStore(db)}


def scan(db, folder):
    """Runs a real scan job and returns the summary the job stored."""
    from meloshelf.infrastructure.job_store import JobStore
    service = JobService(db, tag_reader=lambda p: {'title': p.stem, 'artist': 'Band', 'album': 'Q'})
    job = service.start_scan(str(folder))
    assert service.wait(job['job_id'], 60)
    return JobStore(db).get(job['job_id'])['summary']


def identify(world, path, decision='accepted'):
    """Stands in for recognition: stores a result for the current version of the file, then records the decision."""
    store, path = world['store'], path
    stat = os.stat(path)
    store.put_result(path, stat.st_size, stat.st_mtime_ns, 'matched', {'candidates': [CANDIDATE], 'reason': 'x'},
                     recording_status='NEEDS_REVIEW')
    if decision != 'pending':
        store.set_decision(str(path), decision, recording_id='rec-1')


def status(world, path):
    row = world['store'].get_result(str(path))
    return None if row is None else (row['decision'], row['recording_status'])


def history(world):
    with world['db'].read() as c:
        return [dict(r) for r in c.execute('SELECT * FROM match_decision_history ORDER BY id')]


def make(world, name, seed, title='Song'):
    return write_mp3(world['lib'] / name, seed=seed, tags={'title': title, 'artist': 'Band', 'album': 'Q'})


# ── the decision follows the file ────────────────────────────────────────────────

def test_a_rename_in_the_same_folder_keeps_the_accepted_decision_on_the_new_path(world):
    a = make(world, 'a.mp3', seed=1)
    scan(world['db'], world['lib'])
    identify(world, a)
    a.rename(world['lib'] / 'b.mp3')
    summary = scan(world['db'], world['lib'])
    b = world['lib'] / 'b.mp3'
    assert status(world, b) == ('accepted', 'USER_APPROVED')
    assert status(world, a) is None, 'the old path no longer carries the decision'
    assert summary['decisions_transferred'] == 1 and summary['decisions_awaiting_review'] == 0


def test_a_move_to_another_folder_keeps_the_decision(world):
    a = make(world, 'a.mp3', seed=2)
    scan(world['db'], world['lib'])
    identify(world, a, decision='rejected')
    target = world['lib'] / 'elsewhere' / 'a.mp3'
    target.parent.mkdir(parents=True)
    shutil.move(str(a), str(target))
    scan(world['db'], world['lib'])
    assert status(world, target) == ('rejected', 'REJECTED')


def test_a_copy_does_not_inherit_the_decision_and_the_original_keeps_its_own(world):
    a = make(world, 'a.mp3', seed=3)
    scan(world['db'], world['lib'])
    identify(world, a)
    shutil.copyfile(str(a), str(world['lib'] / 'copy.mp3'))
    scan(world['db'], world['lib'])
    assert status(world, world['lib'] / 'copy.mp3') is None
    assert status(world, a) == ('accepted', 'USER_APPROVED')


def test_identical_duplicates_keep_their_decisions_apart(world):
    a = make(world, 'a.mp3', seed=4)
    b = make(world, 'b.mp3', seed=4)
    assert a.read_bytes() == b.read_bytes()
    scan(world['db'], world['lib'])
    identify(world, a)
    identify(world, b, decision='rejected')
    scan(world['db'], world['lib'])
    assert status(world, a) == ('accepted', 'USER_APPROVED')
    assert status(world, b) == ('rejected', 'REJECTED')


def test_a_file_deleted_and_recreated_at_the_same_path_never_inherits_the_decision(world):
    a = make(world, 'a.mp3', seed=5)
    scan(world['db'], world['lib'])
    identify(world, a)
    a.unlink()
    make(world, 'a.mp3', seed=6, title='Different')
    scan(world['db'], world['lib'])
    assert status(world, a) is None
    assert [h['decision'] for h in history(world)] == ['accepted']
    assert history(world)[0]['reason'] == 'file changed'


def test_an_in_place_change_archives_the_decision_instead_of_applying_it(world):
    a = make(world, 'a.mp3', seed=7)
    scan(world['db'], world['lib'])
    identify(world, a)
    a.write_bytes(a.read_bytes() + b'\x00' * 64)
    os.utime(a, ns=(a.stat().st_atime_ns, a.stat().st_mtime_ns + 7_000_000_000))
    scan(world['db'], world['lib'])
    assert status(world, a) is None
    assert history(world)[-1]['decision'] == 'accepted'


def test_a_decision_whose_file_cannot_be_identified_is_kept_and_counted_for_review(world):
    a = make(world, 'a.mp3', seed=8)
    scan(world['db'], world['lib'])
    identify(world, a)
    a.unlink()                                     # gone, with no file of the same identity anywhere
    summary = scan(world['db'], world['lib'])
    with world['db'].read() as c:
        kept = c.execute('SELECT path, decision FROM match_results WHERE decision IN (?,?)', ('accepted', 'rejected')).fetchall()
    assert [tuple(r) for r in kept] == [(str(a), 'accepted')], 'kept, not transferred or deleted'
    assert summary['decisions_awaiting_review'] == 1 and summary['decisions_transferred'] == 0


def test_reindexing_after_a_move_is_idempotent(world):
    a = make(world, 'a.mp3', seed=9)
    scan(world['db'], world['lib'])
    identify(world, a)
    a.rename(world['lib'] / 'b.mp3')
    for _ in range(3):
        scan(world['db'], world['lib'])
    with world['db'].read() as c:
        rows = c.execute("SELECT path FROM match_results WHERE decision='accepted'").fetchall()
    assert [r[0] for r in rows] == [str(world['lib'] / 'b.mp3')]


def test_the_decision_survives_a_restart(world, tmp_path):
    a = make(world, 'a.mp3', seed=10)
    scan(world['db'], world['lib'])
    identify(world, a)
    a.rename(world['lib'] / 'b.mp3')
    scan(world['db'], world['lib'])
    again = Database(tmp_path / 'lib.sqlite3')
    again.initialize()
    assert MatchStore(again).get_result(str(world['lib'] / 'b.mp3'))['decision'] == 'accepted'


def test_a_new_identification_of_a_changed_file_archives_the_earlier_human_decision(world):
    a = make(world, 'a.mp3', seed=11)
    scan(world['db'], world['lib'])
    identify(world, a)
    a.write_bytes(a.read_bytes() + b'\x01' * 32)
    os.utime(a, ns=(a.stat().st_atime_ns, a.stat().st_mtime_ns + 5_000_000_000))
    # No scan in between: the decision is still attached when the file is identified again, so it is archived then.
    identify(world, a, decision='pending')
    assert any(h['decision'] == 'accepted' and h['reason'] == 'replaced by a new identification' for h in history(world))
    assert status(world, a) == ('pending', 'NEEDS_REVIEW')


# ── migration of legacy path-keyed decisions ─────────────────────────────────────

def test_migration_23_binds_existing_decisions_to_their_present_instances(tmp_path):
    from meloshelf.infrastructure.database import SCHEMA_VERSION
    db = Database(tmp_path / 'old.sqlite3')
    db.initialize()
    a = make_plain(tmp_path, 'a.mp3', 12)
    scan(db, tmp_path / 'music')
    MatchStore(db).put_result(str(a), a.stat().st_size, a.stat().st_mtime_ns, 'matched',
                              {'candidates': [CANDIDATE], 'reason': 'x'}, recording_status='NEEDS_REVIEW')
    MatchStore(db).set_decision(str(a), 'accepted', recording_id='rec-1')
    with sqlite3.connect(db.path) as c:
        c.execute('ALTER TABLE match_results DROP COLUMN instance_id')
        c.execute('DROP TABLE match_decision_history')
        c.execute('PRAGMA user_version=22')
    db.initialize()
    with sqlite3.connect(db.path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        instance = c.execute('SELECT instance_id FROM file_instances WHERE path=?', (str(a),)).fetchone()[0]
        assert c.execute('SELECT instance_id FROM match_results WHERE path=?', (str(a),)).fetchone()[0] == instance


def make_plain(tmp_path, name, seed):
    return write_mp3(tmp_path / 'music' / name, seed=seed, tags={'title': 'x', 'artist': 'Band', 'album': 'Q'})
