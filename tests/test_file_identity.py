"""Stable local file-instance identity.

A file instance is one physical file, identified by a local id that is NOT its path and NOT a MusicBrainz ID.
Evidence is cheap: size and mtime from the scan, plus a sampled fingerprint (the size and the first and last
SAMPLE bytes) read once per new file. Reconciliation is conservative: when the evidence does not single out one
earlier file, a new instance is created and no decision is transferred.
All reads are read-only; nothing here writes to, renames or moves a music file.
"""
import hashlib
import os

import pytest

from meloshelf.domain.models import Track
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.file_identity import SAMPLE, FileRegistry
from meloshelf.infrastructure.repository import LibraryRepository


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / 'lib.sqlite3')
    database.initialize()
    music = tmp_path / 'music'
    music.mkdir()
    return database, music


def content(tag: str, size: int = 4000) -> bytes:
    return (tag.encode() * (size // len(tag) + 1))[:size]


def put(music, name, data):
    path = music / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def rescan(db, paths):
    """What a scan leaves in the tracks table: exactly these files, with their current size and mtime."""
    with db.write() as connection:
        connection.execute('DELETE FROM tracks')
    rows = []
    for p in paths:
        st = os.stat(p)
        rows.append(Track(p, st.st_size, '.mp3', st.st_mtime_ns, 'T', 'Artist', 'Album', '', 'tagged'))
    LibraryRepository(db).save_many(rows)


def ids(db):
    return FileRegistry(db).instance_ids_by_path()


def test_the_first_reconcile_gives_every_file_its_own_stable_id(env):
    db, music = env
    a, b = put(music, 'a.mp3', content('A')), put(music, 'b.mp3', content('B'))
    rescan(db, [a, b])
    summary = FileRegistry(db).reconcile()
    first = ids(db)
    assert summary['new'] == 2 and len(set(first.values())) == 2
    assert FileRegistry(db).reconcile()['new'] == 0
    assert ids(db) == first


def test_a_rename_keeps_the_same_instance(env):
    db, music = env
    a = put(music, 'old name.mp3', content('A'))
    rescan(db, [a])
    FileRegistry(db).reconcile()
    before = ids(db)[a]
    new = str(music / 'new name.mp3')
    os.rename(a, new)
    rescan(db, [new])
    summary = FileRegistry(db).reconcile()
    assert summary['moved'] == 1 and summary['new'] == 0
    assert ids(db)[new] == before


def test_a_move_to_another_folder_keeps_the_same_instance(env):
    db, music = env
    a = put(music, 'x/song.mp3', content('A'))
    rescan(db, [a])
    FileRegistry(db).reconcile()
    before = ids(db)[a]
    new = str(music / 'y' / 'song.mp3')
    os.makedirs(os.path.dirname(new))
    os.rename(a, new)
    rescan(db, [new])
    FileRegistry(db).reconcile()
    assert ids(db)[new] == before


def test_the_history_records_where_an_instance_has_been(env):
    db, music = env
    a = put(music, 'one.mp3', content('A'))
    rescan(db, [a])
    registry = FileRegistry(db)
    registry.reconcile()
    instance = ids(db)[a]
    new = str(music / 'two.mp3')
    os.rename(a, new)
    rescan(db, [new])
    registry.reconcile()
    paths = [h['path'] for h in registry.history(instance)]
    assert paths == [a, new]


def test_duplicate_copies_are_separate_instances_and_reported_as_duplicates(env):
    db, music = env
    a, b = put(music, 'copy1.mp3', content('SAME')), put(music, 'copy2.mp3', content('SAME'))
    rescan(db, [a, b])
    registry = FileRegistry(db)
    registry.reconcile()
    assert ids(db)[a] != ids(db)[b]
    groups = registry.duplicate_groups()
    assert len(groups) == 1 and sorted(groups[0]) == sorted([ids(db)[a], ids(db)[b]])


def test_an_ambiguous_relocation_creates_a_new_instance_and_transfers_nothing(env):
    db, music = env
    a, b = put(music, 'copy1.mp3', content('SAME')), put(music, 'copy2.mp3', content('SAME'))
    rescan(db, [a, b])
    registry = FileRegistry(db)
    registry.reconcile()
    old_ids = set(ids(db).values())
    os.remove(a)
    os.remove(b)
    c = put(music, 'elsewhere.mp3', content('SAME'))
    rescan(db, [c])
    summary = registry.reconcile()
    assert summary['ambiguous'] == 1 and summary['moved'] == 0
    assert ids(db)[c] not in old_ids, 'two earlier copies fit equally well, so neither one is assumed'


def test_changed_content_at_the_same_path_is_a_new_instance(env):
    db, music = env
    a = put(music, 'song.mp3', content('A', 4000))
    rescan(db, [a])
    registry = FileRegistry(db)
    registry.reconcile()
    before = ids(db)[a]
    (music / 'song.mp3').write_bytes(content('B', 5000))
    rescan(db, [a])
    summary = registry.reconcile()
    assert summary['changed'] == 1
    assert ids(db)[a] != before
    assert registry.get(before)['state'] == 'superseded'


def test_path_reuse_by_a_different_file_is_a_new_instance(env):
    db, music = env
    a = put(music, 'song.mp3', content('A'))
    rescan(db, [a])
    registry = FileRegistry(db)
    registry.reconcile()
    before = ids(db)[a]
    os.remove(a)
    rescan(db, [])
    registry.reconcile()
    put(music, 'song.mp3', content('Z', 6000))
    rescan(db, [a])
    registry.reconcile()
    assert ids(db)[a] != before
    assert registry.get(before)['state'] in ('missing', 'superseded')


def test_a_missing_file_keeps_its_instance_and_a_returning_file_reclaims_it(env):
    db, music = env
    a = put(music, 'song.mp3', content('A'))
    rescan(db, [a])
    registry = FileRegistry(db)
    registry.reconcile()
    before = ids(db)[a]
    rescan(db, [])
    assert registry.reconcile()['missing'] == 1
    assert registry.get(before)['state'] == 'missing'
    rescan(db, [a])
    registry.reconcile()
    assert ids(db)[a] == before


def test_instances_survive_a_restart(env, tmp_path):
    db, music = env
    a = put(music, 'song.mp3', content('A'))
    rescan(db, [a])
    FileRegistry(db).reconcile()
    before = ids(db)[a]
    reopened = Database(tmp_path / 'lib.sqlite3')
    reopened.initialize()
    assert FileRegistry(reopened).instance_ids_by_path()[a] == before


def test_each_new_file_is_read_once_and_only_a_bounded_sample(env):
    db, music = env
    big = put(music, 'big.mp3', content('Q', SAMPLE * 6))
    rescan(db, [big])
    reads = []

    def opener(path, mode='rb'):
        handle = open(path, mode)
        original = handle.read

        def counting(n=-1):
            data = original(n)
            reads.append(len(data))
            return data
        handle.read = counting
        return handle

    registry = FileRegistry(db, opener=opener)
    registry.reconcile()
    assert 0 < sum(reads) <= 2 * SAMPLE
    reads.clear()
    registry.reconcile()
    assert reads == [], 'an unchanged library is not read again'


def test_the_read_cap_per_run_defers_fingerprints_without_losing_instances(env):
    db, music = env
    paths = [put(music, f'{n}.mp3', content(f'T{n}')) for n in range(6)]
    rescan(db, paths)
    registry = FileRegistry(db, max_fingerprints_per_run=2)
    registry.reconcile()
    assert len(ids(db)) == 6
    assert registry.pending_fingerprints() == 4
    registry.reconcile()
    registry.reconcile()
    assert registry.pending_fingerprints() == 0


def test_reconcile_never_changes_a_music_file(env):
    db, music = env
    a = put(music, 'song.mp3', content('A'))
    digest = hashlib.sha256((music / 'song.mp3').read_bytes()).hexdigest()
    mtime = os.stat(a).st_mtime_ns
    rescan(db, [a])
    FileRegistry(db).reconcile()
    assert hashlib.sha256((music / 'song.mp3').read_bytes()).hexdigest() == digest
    assert os.stat(a).st_mtime_ns == mtime


def test_an_unreadable_file_still_gets_an_instance_without_a_fingerprint(env):
    db, music = env
    ghost = str(music / 'never-existed.mp3')
    with db.write() as connection:
        connection.execute('DELETE FROM tracks')
    LibraryRepository(db).save_many([Track(ghost, 100, '.mp3', 1, 'T', 'A', 'B', '', 'tagged')])
    FileRegistry(db).reconcile()
    assert ghost in ids(db)
