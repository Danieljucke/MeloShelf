"""Recording decisions follow the physical file (its instance id), never merely a path.

* A decision on one file keeps applying after the file is renamed or moved.
* A different file that later occupies the same path does NOT inherit the decision.
* A duplicate copy of the file is a different instance and is not covered.
* A decision written before instance ids existed (path-keyed) is backfilled only when the file at that path shows no
  sign of having changed since the decision; otherwise it is kept, flagged and not applied.
All fixtures are mock candidates; no provider is contacted and no music file is modified.
"""
import os
import time

import pytest

from meloshelf.domain.artist_identity import ArtistCandidate, decide_link, effective_link
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.file_identity import FileRegistry
from meloshelf.infrastructure.repository import LibraryRepository

CANDIDATES = (ArtistCandidate('mb-one', 'Prince', 100), ArtistCandidate('mb-two', 'Prince', 100))


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    music = tmp_path / 'music'
    music.mkdir()
    return database, music


def put(music, name, data):
    path = music / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def rescan(db, paths):
    with db.write() as connection:
        connection.execute('DELETE FROM tracks')
    rows = []
    for p in paths:
        st = os.stat(p)
        rows.append(Track(p, st.st_size, '.mp3', st.st_mtime_ns, 'T', 'Prince', 'Album', '', 'tagged'))
    LibraryRepository(db).save_many(rows)


def link(db):
    ArtistLinks(db).record('Prince', decide_link('Prince', list(CANDIDATES)), looked_up_at=1.0)


def under(db, mb_id):
    artists, _ = LibraryRepository(db).artist_identities(limit=300)
    return sum(a['tracks'] for a in artists if a['mb_artist_id'] == mb_id)


# ── pure rule ──────────────────────────────────────────────────────────────────

def test_a_decision_applies_by_instance_id_and_not_by_path_when_instances_are_in_use():
    decision = {'id': 1, 'action': 'accept', 'mb_artist_id': 'mb-two', 'ref': None, 'scope': [], 'tracks': ['/p.mp3'],
                'instances': ['abc'], 'fingerprint': ''}
    row = {'status': 'ambiguous', 'mb_artist_id': None}
    assert effective_link(row, [decision], track_path='/p.mp3', instance_id='abc') == ('accepted', 'mb-two')
    assert effective_link(row, [decision], track_path='/p.mp3', instance_id='different') == (None, None)
    assert effective_link(row, [decision], track_path='/p.mp3', instance_id='') == (None, None)


def test_without_the_identity_layer_the_older_path_rule_still_holds_for_pure_domain_callers():
    decision = {'id': 1, 'action': 'accept', 'mb_artist_id': 'mb-two', 'ref': None, 'scope': [], 'tracks': ['/p.mp3'],
                'fingerprint': ''}
    assert effective_link({'status': 'ambiguous'}, [decision], track_path='/p.mp3') == ('accepted', 'mb-two')


# ── decisions follow the file ──────────────────────────────────────────────────

def test_a_track_decision_stores_the_instance_it_covers_and_keeps_the_path_as_provenance(env):
    db, music = env
    a = put(music, 'a.mp3', b'A' * 4000)
    rescan(db, [a])
    link(db)
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[a])
    event = SuggestionDecisions(db).events()[0]
    assert event['tracks'] == [a]
    assert event['instances'] == [FileRegistry(db).instance_ids_by_path()[a]]


def test_a_decision_still_applies_after_the_file_is_renamed(env):
    db, music = env
    a = put(music, 'old.mp3', b'A' * 4000)
    rescan(db, [a])
    link(db)
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[a])
    new = str(music / 'new.mp3')
    os.rename(a, new)
    rescan(db, [new])
    FileRegistry(db).reconcile()
    assert under(db, 'mb-two') == 1


def test_a_different_file_at_the_same_path_does_not_inherit_the_decision(env):
    db, music = env
    a = put(music, 'song.mp3', b'A' * 4000)
    rescan(db, [a])
    link(db)
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[a])
    os.remove(a)
    rescan(db, [])
    FileRegistry(db).reconcile()
    put(music, 'song.mp3', b'Z' * 7000)
    rescan(db, [a])
    FileRegistry(db).reconcile()
    assert under(db, 'mb-two') == 0


def test_a_duplicate_copy_is_not_covered_by_the_decision_on_the_original(env):
    db, music = env
    a, b = put(music, 'one.mp3', b'SAME' * 1000), put(music, 'two.mp3', b'SAME' * 1000)
    rescan(db, [a, b])
    link(db)
    SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[a])
    assert under(db, 'mb-two') == 1


def test_a_decision_for_a_file_the_library_has_not_indexed_is_refused(env):
    db, music = env
    put(music, 'a.mp3', b'A' * 4000)
    rescan(db, [])
    link(db)
    from meloshelf.infrastructure.artist_suggestions import SuggestionError
    with pytest.raises(SuggestionError):
        SuggestionDecisions(db).decide('Prince', 'accept', mb_artist_id='mb-two', tracks=[str(music / 'a.mp3')])


# ── older path-keyed decisions ─────────────────────────────────────────────────

def legacy_decision(db, path, at):
    import json
    with db.write() as connection:
        connection.execute("""INSERT INTO artist_link_decisions(credit_key,action,mb_artist_id,ref,fingerprint,at,scope,tracks)
                              VALUES('prince','accept','mb-two',NULL,'',?,'[]',?)""", (at, json.dumps([path])))


def test_an_older_path_decision_is_backfilled_when_the_file_has_not_changed_since(env):
    db, music = env
    a = put(music, 'a.mp3', b'A' * 4000)
    rescan(db, [a])
    link(db)
    FileRegistry(db).reconcile()
    legacy_decision(db, a, at=time.time() + 5)
    summary = SuggestionDecisions(db).backfill_instances()
    assert summary['backfilled'] == 1
    assert under(db, 'mb-two') == 1


def test_an_older_path_decision_is_not_applied_when_the_file_changed_after_it(env):
    db, music = env
    a = put(music, 'a.mp3', b'A' * 4000)
    rescan(db, [a])
    link(db)
    FileRegistry(db).reconcile()
    legacy_decision(db, a, at=1.0)          # decided long before this file's modification time
    summary = SuggestionDecisions(db).backfill_instances()
    assert summary['backfilled'] == 0 and summary['unresolved'] == 1
    assert under(db, 'mb-two') == 0


def test_an_older_path_decision_that_was_never_backfilled_applies_to_nothing(env):
    db, music = env
    a = put(music, 'a.mp3', b'A' * 4000)
    rescan(db, [a])
    link(db)
    FileRegistry(db).reconcile()
    legacy_decision(db, a, at=time.time() + 5)
    assert under(db, 'mb-two') == 0, 'until it is backfilled the path alone is not enough'


def test_the_history_of_an_older_decision_is_preserved_by_the_backfill(env):
    db, music = env
    a = put(music, 'a.mp3', b'A' * 4000)
    rescan(db, [a])
    link(db)
    FileRegistry(db).reconcile()
    legacy_decision(db, a, at=time.time() + 5)
    SuggestionDecisions(db).backfill_instances()
    event = SuggestionDecisions(db).events()[0]
    assert event['tracks'] == [a] and event['action'] == 'accept' and event['mb_artist_id'] == 'mb-two'


def test_migration_sixteen_keeps_existing_decisions(tmp_path):
    path = tmp_path / 'old.sqlite3'
    database = Database(path)
    database.initialize()
    with database.write() as c:
        c.execute("INSERT INTO artist_link_decisions(credit_key,action,mb_artist_id,ref,fingerprint,at,scope,tracks) "
                  "VALUES('prince','reject','mb-one',NULL,'',1.0,'[\"album\"]','[]')")
        c.execute('DROP TABLE artwork_decisions')
        c.execute('DROP TABLE artist_images')
        c.execute('DROP TABLE human_label_history')  # created by the later migration 18
        c.execute('DROP TABLE human_label_batch_items')  # created by the later migration 19
        c.execute('DROP TABLE human_label_batches')
        c.execute('DROP TABLE human_version_label_history')
        c.execute('DROP TABLE human_version_labels')  # created by the later migration 20
        c.execute('DROP TABLE file_instance_history')
        c.execute('DROP TABLE file_instances')
        c.execute('ALTER TABLE artist_link_decisions DROP COLUMN instances')
        c.execute('PRAGMA user_version=15')
    reopened = Database(path)
    reopened.initialize()
    events = SuggestionDecisions(reopened).events()
    assert len(events) == 1 and events[0]['scope'] == ['album'] and events[0]['instances'] == []


# ── the scan keeps instances current ────────────────────────────────────────────

def test_a_completed_scan_reconciles_file_instances(env):
    from meloshelf.application.jobs import JobService, _Control
    db, music = env
    a = put(music, 'a.mp3', b'A' * 4000)
    rescan(db, [a])
    service = JobService(db)
    job_id = service.store.create('scan', root=str(music))
    service._finish(job_id, _Control('scan', 0.0, time.time), None)
    assert a in FileRegistry(db).instance_ids_by_path()
