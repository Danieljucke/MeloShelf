"""Collaboration folders in an already-organized destination are merged into one canonical artist folder, only on explicit
confirmation, through the journaled engine, with decisions and file identities following the files."""
import hashlib
import os

import pytest
from mutagen.easyid3 import EasyID3

from audio_fixtures import write_mp3
from meloshelf.application.jobs import JobService
from meloshelf.application.organize_service import OrganizeService
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.file_identity import FileRegistry
from meloshelf.infrastructure.tag_writer import write_tags


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listing(folder):
    return sorted(str(p.relative_to(folder)).replace(os.sep, '/') for p in folder.rglob('*') if p.is_file())


def folders(folder):
    return sorted(str(p.relative_to(folder)).replace(os.sep, '/') for p in folder.rglob('*') if p.is_dir())


def build(tmp_path, hooks=None):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    jobs = JobService(db, recognition_factory=lambda o, c: None)
    return db, jobs, OrganizeService(db, jobs, tagger=write_tags, free_space=lambda p: 10**12, hooks=hooks)


@pytest.fixture
def world(tmp_path):
    dest = tmp_path / 'Organized'

    def put(rel, seed, credit, title, album, number):
        write_mp3(dest / rel, seed=seed, tags={'title': title, 'artist': credit, 'album': album, 'tracknumber': str(number)})

    put('Kygo, Selena Gomez/Golden Hour/01 - It Ain’t Me.mp3', 1, 'Kygo, Selena Gomez', 'It Ain’t Me', 'Golden Hour', 1)
    put('Kygo, John Legend/Golden Hour/02 - Hymn.mp3', 2, 'Kygo, John Legend', 'Hymn', 'Golden Hour', 2)
    put('Kygo/Cloud Nine/03 - Firestone.mp3', 3, 'Kygo', 'Firestone', 'Cloud Nine', 3)
    put('Kygo, Rita Ora/Cloud Nine/03 - Firestone.mp3', 4, 'Kygo, Rita Ora', 'Firestone', 'Cloud Nine', 3)       # different audio, same name
    put('Kygo, Donna Summer/Golden Hour/04 - Same.mp3', 5, 'Kygo, Donna Summer', 'Same', 'Golden Hour', 4)
    put('Kygo/Golden Hour/04 - Same.mp3', 5, 'Kygo, Donna Summer', 'Same', 'Golden Hour', 4)                      # byte-identical duplicate
    put('Lecrae & Andy Mineo/Anomaly/02 - Say.mp3', 6, 'Lecrae & Andy Mineo', 'Say', 'Anomaly', 2)
    put('Lecrae/Anomaly/01 - Fear.mp3', 7, 'Lecrae', 'Fear', 'Anomaly', 1)
    put('Simon & Garfunkel/Bookends/01 - Save.mp3', 8, 'Simon & Garfunkel', 'Save', 'Bookends', 1)
    db, jobs, service = build(tmp_path)
    jobs.wait(jobs.start_scan(str(dest))['job_id'], 60)
    return {'tmp': tmp_path, 'dest': dest, 'db': db, 'jobs': jobs, 'service': service}


def test_the_preview_lists_every_move_and_every_conflict_and_changes_nothing(world):
    before = listing(world['dest'])
    plan = world['service'].consolidation_plan(str(world['dest']))
    assert listing(world['dest']) == before and plan['ok']
    moves = {os.path.basename(m['from']) + '|' + m['folder']: m for m in plan['moves']}
    assert plan['total'] == 5 and plan['to_move'] == 4 and plan['duplicates'] == 1 and plan['renamed'] == 1
    assert moves['01 - It Ain’t Me.mp3|Kygo, Selena Gomez']['relative'] == 'Kygo/Golden Hour/01 - It Ain’t Me.mp3'
    assert moves['03 - Firestone.mp3|Kygo, Rita Ora']['conflict'] == 'renamed'
    assert moves['04 - Same.mp3|Kygo, Donna Summer']['conflict'] == 'duplicate'
    assert moves['02 - Say.mp3|Lecrae & Andy Mineo']['relative'] == 'Lecrae/Anomaly/02 - Say.mp3'
    assert not any('Simon' in m['folder'] for m in plan['moves']), 'a genuine group name is never merged'
    assert {f['name'] for f in plan['folders']} == {'Kygo, Selena Gomez', 'Kygo, John Legend', 'Kygo, Rita Ora', 'Kygo, Donna Summer', 'Lecrae & Andy Mineo'}


def test_nothing_moves_without_explicit_confirmation(world):
    before = listing(world['dest'])
    with pytest.raises(PermissionError):
        world['service'].consolidate(str(world['dest']))
    assert listing(world['dest']) == before


def test_confirmed_consolidation_moves_files_preserves_credits_and_prunes_only_emptied_folders(world):
    keep_hymn = sha(world['dest'] / 'Kygo, John Legend' / 'Golden Hour' / '02 - Hymn.mp3')
    dup_a = sha(world['dest'] / 'Kygo' / 'Golden Hour' / '04 - Same.mp3')
    report = world['service'].consolidate(str(world['dest']), confirm=True)
    assert report['moved'] == 4 and report['failed'] == 0 and report['duplicates_left'] == 1
    files = listing(world['dest'])
    assert 'Kygo/Golden Hour/01 - It Ain’t Me.mp3' in files and 'Kygo/Golden Hour/02 - Hymn.mp3' in files
    assert 'Lecrae/Anomaly/02 - Say.mp3' in files
    assert 'Kygo/Cloud Nine/03 - Firestone.mp3' in files and 'Kygo/Cloud Nine/03 - Firestone (2).mp3' in files, 'no overwrite'
    assert sha(world['dest'] / 'Kygo' / 'Golden Hour' / '02 - Hymn.mp3') == keep_hymn
    assert EasyID3(world['dest'] / 'Kygo' / 'Golden Hour' / '01 - It Ain’t Me.mp3')['artist'] == ['Kygo, Selena Gomez'], 'the credit stays'
    assert sha(world['dest'] / 'Kygo' / 'Golden Hour' / '04 - Same.mp3') == dup_a
    assert 'Kygo, Donna Summer/Golden Hour/04 - Same.mp3' in files, 'a byte-identical duplicate is left for a person to judge'
    assert 'Simon & Garfunkel/Bookends/01 - Save.mp3' in files
    remaining = folders(world['dest'])
    assert 'Kygo, Selena Gomez' not in remaining and 'Kygo, John Legend' not in remaining and 'Kygo, Rita Ora' not in remaining
    assert 'Lecrae & Andy Mineo' not in remaining and 'Kygo, Donna Summer' in remaining, 'a folder that still holds a file stays'
    assert len(files) == 9, 'nothing lost, nothing duplicated'


def test_file_identity_and_human_decisions_follow_the_moved_files(world):
    old = str(world['dest'] / 'Kygo, John Legend' / 'Golden Hour' / '02 - Hymn.mp3')
    stat = os.stat(old)
    with world['db'].write() as db:
        db.execute("INSERT INTO manual_identities(path,size,mtime_ns,title,artist,album,created_at) VALUES(?,?,?,?,?,?,?)",
                   (old, stat.st_size, stat.st_mtime_ns, 'Hymn', 'Kygo, John Legend', 'Golden Hour', 1))
    registry = FileRegistry(world['db'])
    registry.reconcile()
    instance = registry.instance_ids_by_path()[old]
    world['service'].consolidate(str(world['dest']), confirm=True)
    new = str(world['dest'] / 'Kygo' / 'Golden Hour' / '02 - Hymn.mp3')
    ids = FileRegistry(world['db']).instance_ids_by_path()
    assert ids.get(new) == instance and old not in ids, 'the file keeps its instance id'
    with world['db'].read() as db:
        assert [r['path'] for r in db.execute('SELECT path FROM manual_identities')] == [new]
        tracks = {r['path'] for r in db.execute('SELECT path FROM tracks')}
    assert new in tracks and old not in tracks


def test_running_it_again_finds_nothing_more_to_move(world):
    world['service'].consolidate(str(world['dest']), confirm=True)
    plan = world['service'].consolidation_plan(str(world['dest']))
    assert plan['to_move'] == 0 and plan['duplicates'] == 1, 'only the duplicate awaiting a person remains'
    again = world['service'].consolidate(str(world['dest']), confirm=True)
    assert again['moved'] == 0 and len(listing(world['dest'])) == 9


def test_a_consolidation_can_be_undone_through_the_journal(world):
    before = listing(world['dest'])
    report = world['service'].consolidate(str(world['dest']), confirm=True)
    undone = world['service'].undo_consolidation(report['run_id'])
    assert undone['moved'] == 4 and undone['failed'] == 0
    assert listing(world['dest']) == before
    with world['db'].read() as db:
        paths = {r['path'] for r in db.execute('SELECT path FROM tracks')}
    assert all(os.path.isfile(p) for p in paths), 'the index follows the files back'


class Crash(BaseException):
    pass


def test_a_crash_during_consolidation_recovers_without_loss_or_duplicates(world):
    calls = {'n': 0}

    def boom(**_):
        calls['n'] += 1
        if calls['n'] == 2:
            raise Crash()

    world['service'].hooks = {'after_publish': boom}
    with pytest.raises(Crash):
        world['service'].consolidate(str(world['dest']), confirm=True)
    db, jobs, restarted = build(world['tmp'])
    report = restarted.consolidate(str(world['dest']), confirm=True)
    files = listing(world['dest'])
    assert len(files) == 9 and len(set(files)) == 9 and not [f for f in files if 'partial' in f]
    assert 'Kygo/Golden Hour/01 - It Ain’t Me.mp3' in files and 'Kygo/Golden Hour/02 - Hymn.mp3' in files
    assert report['failed'] == 0


def test_the_service_never_consolidates_on_its_own(tmp_path):
    dest = tmp_path / 'Organized'
    write_mp3(dest / 'Kygo, X' / 'A' / '01.mp3', seed=1, tags={'title': 'T', 'artist': 'Kygo, X', 'album': 'A'})
    build(tmp_path)
    assert listing(dest) == ['Kygo, X/A/01.mp3'], 'constructing the application moves nothing'
