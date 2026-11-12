"""MOVE semantics: the engine puts music into its classified folders and removes the original only after the
destination is published and verified. Nothing is ever lost to a crash, a collision or a failed transfer."""
import hashlib
import itertools
import os

import pytest

import meloshelf.infrastructure.organizer as organizer_module
from audio_fixtures import write_mp3
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.organizer import Organizer, WorkItem, stray_temp_files
from meloshelf.infrastructure.tag_writer import write_tags


class Crash(BaseException):
    """A simulated power cut: not an Exception, so no engine code can swallow it."""


def same(a, b):
    return True


def cross(a, b):
    return False


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    source, dest = tmp_path / 'source', tmp_path / 'organized'
    source.mkdir()
    return {'db': db, 'source': source, 'dest': dest, 'tmp': tmp_path}


def make(world, name, seed, **tags):
    return write_mp3(world['source'] / name, seed=seed, tags=tags or {'title': name, 'artist': 'A', 'album': 'B'})


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def items(world, *names, kind='organize', tags=None):
    return [WorkItem(source=str(world['source'] / n), relative=f'Artist/Album/{i + 1:02d} - {n}', kind=kind, tags=tags or {})
            for i, n in enumerate(names)]


def mover(world, volume=same, **options):
    options.setdefault('free_space', lambda path: 10**12)
    return Organizer(world['db'], mode='move', same_volume=volume, **options)


def start(world, work, volume=same, **options):
    organizer = mover(world, volume, **options)
    return organizer, organizer.create_run(str(world['source']), str(world['dest']), work)


def placed(world):
    root = world['dest'] / 'Artist' / 'Album'
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


VOLUMES = [pytest.param(same, id='same-volume'), pytest.param(cross, id='cross-volume')]


@pytest.mark.parametrize('volume', VOLUMES)
def test_a_move_puts_the_file_at_its_destination_and_removes_the_original(world, volume):
    original = make(world, 'a.mp3', 1)
    digest = sha(original)
    organizer, run_id = start(world, items(world, 'a.mp3'), volume)
    assert organizer.execute(run_id) == 'completed'
    assert placed(world) == ['01 - a.mp3'] and sha(world['dest'] / 'Artist' / 'Album' / '01 - a.mp3') == digest
    assert not original.exists(), 'the original location is empty after a successful move'
    assert not stray_temp_files(world['dest'])
    row = organizer.items(run_id)[0]
    assert row['state'] == 'done' and row['source_removed'] == 1
    assert organizer.counters(run_id)['done'] == 1


@pytest.mark.parametrize('volume', VOLUMES)
def test_metadata_is_written_to_the_moved_file_and_unrelated_tags_survive(world, volume):
    from mutagen.easyid3 import EasyID3
    original = make(world, 'a.mp3', 1, title='0042', artist='X', album='')
    audio = EasyID3(original)
    audio['genre'] = 'Gospel'
    audio.save()
    organizer, run_id = start(world, items(world, 'a.mp3', tags={'title': 'Song', 'artist': 'Queen', 'album': 'Opera'}), volume,
                              tagger=write_tags)
    assert organizer.execute(run_id) == 'completed'
    tags = EasyID3(world['dest'] / 'Artist' / 'Album' / '01 - a.mp3')
    assert tags['title'] == ['Song'] and tags['album'] == ['Opera'] and tags['genre'] == ['Gospel']
    assert not original.exists()


CRASHES = ['after_stage', 'after_verify', 'after_tag', 'after_publish', 'after_source_removed']


@pytest.mark.parametrize('volume', VOLUMES)
@pytest.mark.parametrize('crash_at', CRASHES)
def test_a_crash_at_any_step_loses_nothing_and_resumes_to_one_moved_file_each(world, volume, crash_at):
    digests = {}
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        digests[name] = sha(make(world, name, i + 1))

    def boom(**_):
        raise Crash()

    organizer, run_id = start(world, items(world, 'a.mp3', 'b.mp3', 'c.mp3'), volume, hooks={crash_at: boom})
    with pytest.raises(Crash):
        organizer.execute(run_id)
    survivors = [sha(p) for root in (world['source'], world['dest']) if root.exists() for p in root.rglob('*') if p.is_file()]
    assert set(digests.values()) <= set(survivors), 'a crash never loses a file'
    resumed = mover(world, volume)
    assert resumed.execute(run_id) == 'completed'
    assert placed(world) == ['01 - a.mp3', '02 - b.mp3', '03 - c.mp3']
    assert not list(world['source'].iterdir()), 'every original is gone after recovery'
    assert not stray_temp_files(world['dest'])
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        assert sha(world['dest'] / 'Artist' / 'Album' / f'{i + 1:02d} - {name}') == digests[name]


@pytest.mark.parametrize('volume', VOLUMES)
@pytest.mark.parametrize('crash_at', CRASHES)
def test_a_brand_new_run_after_a_crash_finishes_the_interrupted_move_instead_of_duplicating(world, volume, crash_at):
    for i, name in enumerate(('a.mp3', 'b.mp3')):
        make(world, name, i + 1)

    def boom(**_):
        raise Crash()

    organizer, run_id = start(world, items(world, 'a.mp3', 'b.mp3'), volume, hooks={crash_at: boom})
    with pytest.raises(Crash):
        organizer.execute(run_id)
    fresh = mover(world, volume)
    fresh.adopt_unfinished(str(world['dest']))                       # the app settles dead runs before it plans
    survivors = [i for i in items(world, 'a.mp3', 'b.mp3') if os.path.exists(i.source)]     # the app re-plans from what exists
    second = fresh.create_run(str(world['source']), str(world['dest']), survivors)
    assert fresh.execute(second) == 'completed'
    assert placed(world) == ['01 - a.mp3', '02 - b.mp3'], placed(world)
    assert not list(world['source'].iterdir()) and not stray_temp_files(world['dest'])


def test_retrying_a_completed_move_changes_nothing(world):
    make(world, 'a.mp3', 1)
    organizer, run_id = start(world, items(world, 'a.mp3'))
    assert organizer.execute(run_id) == 'completed'
    before = placed(world)
    assert organizer.execute(run_id) == 'completed' and placed(world) == before
    assert organizer.counters(run_id)['done'] == 1


def test_an_existing_different_file_is_never_overwritten_and_the_original_is_still_moved(world):
    original = make(world, 'a.mp3', 1)
    digest = sha(original)
    target = world['dest'] / 'Artist' / 'Album'
    target.mkdir(parents=True)
    existing = write_mp3(target / '01 - a.mp3', seed=99)
    keep = sha(existing)
    organizer, run_id = start(world, items(world, 'a.mp3'))
    assert organizer.execute(run_id) == 'completed'
    assert placed(world) == ['01 - a (2).mp3', '01 - a.mp3']
    assert sha(existing) == keep and sha(target / '01 - a (2).mp3') == digest and not original.exists()


def test_an_existing_destination_alone_never_causes_a_source_to_be_deleted(world, monkeypatch):
    original = make(world, 'a.mp3', 1)
    target = world['dest'] / 'Artist' / 'Album'
    target.mkdir(parents=True)
    write_mp3(target / '01 - a.mp3', seed=1)               # a byte-identical file already at the name
    counter = itertools.count()
    monkeypatch.setattr(organizer_module, 'file_sha256', lambda p: str(next(counter)))   # nothing can be verified
    organizer, run_id = start(world, items(world, 'a.mp3'), cross)
    organizer.execute(run_id)
    assert original.exists(), 'unverified: the source is preserved'
    assert organizer.items(run_id)[0]['state'] == 'failed'


def test_two_files_with_the_same_planned_name_both_arrive(world):
    make(world, 'x1.mp3', 1)
    make(world, 'x2.mp3', 2)
    work = [WorkItem(source=str(world['source'] / n), relative='Artist/Album/Same.mp3') for n in ('x1.mp3', 'x2.mp3')]
    organizer, run_id = start(world, work)
    assert organizer.execute(run_id) == 'completed'
    assert placed(world) == ['Same (2).mp3', 'Same.mp3'] and not list(world['source'].iterdir())


def test_a_source_that_disappeared_fails_that_file_and_the_rest_are_moved(world):
    make(world, 'a.mp3', 1)
    make(world, 'b.mp3', 2)
    organizer, run_id = start(world, items(world, 'a.mp3', 'b.mp3'))
    (world['source'] / 'a.mp3').unlink()
    assert organizer.execute(run_id) == 'completed_with_errors'
    states = {i['source']: (i['state'], i['error']) for i in organizer.items(run_id)}
    assert states[str(world['source'] / 'a.mp3')] == ('failed', 'source_missing')
    assert placed(world) == ['02 - b.mp3']


def test_a_source_changed_while_being_transferred_is_kept_and_nothing_is_published(world):
    original = make(world, 'a.mp3', 1)

    def change(**_):
        with open(original, 'ab') as handle:
            handle.write(b'more')

    organizer, run_id = start(world, items(world, 'a.mp3'), cross, hooks={'after_stage': change})
    organizer.execute(run_id)
    assert original.exists() and placed(world) == [] and not stray_temp_files(world['dest'])
    assert organizer.items(run_id)[0]['error'] == 'source_changed'


def test_a_locked_source_that_cannot_be_read_stays_where_it_is(world, monkeypatch):
    original = make(world, 'a.mp3', 1)
    real_open = open

    def locked(path, mode='r', *a, **k):
        if str(path) == str(original):
            raise PermissionError(13, 'locked')
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(organizer_module, 'open', locked, raising=False)
    organizer, run_id = start(world, items(world, 'a.mp3'), cross)
    assert organizer.execute(run_id) == 'completed_with_errors'
    assert original.exists() and placed(world) == [] and not stray_temp_files(world['dest'])
    assert organizer.items(run_id)[0]['state'] == 'failed'


def test_a_source_that_cannot_be_removed_keeps_a_verified_destination_and_asks_for_recovery(world, monkeypatch):
    original = make(world, 'a.mp3', 1)
    digest = sha(original)
    organizer, run_id = start(world, items(world, 'a.mp3'), cross)

    def denied(path):
        raise PermissionError(13, 'denied')

    monkeypatch.setattr(organizer_module.os, 'remove', denied)
    assert organizer.execute(run_id) == 'completed_with_errors'
    monkeypatch.undo()
    row = organizer.items(run_id)[0]
    assert row['state'] == 'failed' and row['error'] == 'source_not_removed'
    assert original.exists() and sha(world['dest'] / 'Artist' / 'Album' / '01 - a.mp3') == digest
    again = mover(world, cross)                                      # once the lock is gone, recovery finishes the job
    assert again.recover(run_id) == 1
    assert not original.exists() and again.items(run_id)[0]['state'] == 'done'
    assert placed(world) == ['01 - a.mp3']


def test_a_file_already_in_its_intended_place_is_left_alone(world):
    inside = write_mp3(world['dest'] / 'Artist' / 'Album' / '01 - a.mp3', seed=5)
    digest = sha(inside)
    work = [WorkItem(source=str(inside), relative='Artist/Album/01 - a.mp3')]
    organizer = mover(world)
    run_id = organizer.create_run(str(world['source']), str(world['dest']), work)
    assert organizer.execute(run_id) == 'completed'
    assert inside.exists() and sha(inside) == digest and placed(world) == ['01 - a.mp3']
    assert organizer.items(run_id)[0]['state'] == 'skipped'


def test_names_with_characters_windows_forbids_are_planned_safely_and_moved(world):
    from meloshelf.domain.organize import Decision, PlanEntry, plan_destinations
    make(world, 'a.mp3', 1)
    decision = Decision('organize', artist='AC/DC', album='Who Made Who?', title='Hells: Bells', track_no=1)
    planned = plan_destinations([PlanEntry(source=str(world['source'] / 'a.mp3'), decision=decision, extension='.mp3',
                                           original_name='a.mp3')], destination_root=str(world['dest']))
    organizer, run_id = start(world, [WorkItem(source=planned[0].source, relative=planned[0].relative)])
    assert organizer.execute(run_id) == 'completed'
    assert world['dest'].joinpath(*planned[0].relative.split('/')).is_file()


def test_pause_stops_between_moves_and_resume_finishes_the_rest(world):
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        make(world, name, i + 1)
    organizer, run_id = start(world, items(world, 'a.mp3', 'b.mp3', 'c.mp3'))
    calls = {'n': 0}

    def control():
        calls['n'] += 1
        return 'pause' if calls['n'] == 2 else None

    assert organizer.execute(run_id, control=control) == 'paused'
    assert len(placed(world)) == 1 and len(list(world['source'].iterdir())) == 2
    assert organizer.execute(run_id) == 'completed' and len(placed(world)) == 3 and not list(world['source'].iterdir())


def test_everything_known_about_a_file_follows_it_when_it_moves(world):
    from meloshelf.infrastructure.repository import LibraryRepository
    original = make(world, 'a.mp3', 1)
    old, st = str(original), original.stat()
    with world['db'].write() as db:
        db.execute("INSERT INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status) VALUES(?,?,?,?,?,?,?,?,?)",
                   (old, st.st_size, '.mp3', st.st_mtime_ns, 'T', 'A', 'B', '', 'tagged'))
        db.execute("""INSERT INTO match_results(path,file_size,file_mtime_ns,status,data,created_at,decision,recording_status,
                      selected_recording_id,instance_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (old, st.st_size, st.st_mtime_ns, 'review', '{}', 1, 'accepted', 'USER_APPROVED', 'rec-1', 'inst-1'))
        db.execute("INSERT INTO file_instances(instance_id,path,size,mtime_ns,fingerprint,state,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)",
                   ('inst-1', old, st.st_size, st.st_mtime_ns, 'fp', 'present', 1, 1))
        db.execute("INSERT INTO manual_identities(path,size,mtime_ns,title,artist,album,created_at) VALUES(?,?,?,?,?,?,?)",
                   (old, st.st_size, st.st_mtime_ns, 'T', 'A', 'B', 1))
    organizer, run_id = start(world, items(world, 'a.mp3', tags={'title': 'New Title'}), same, tagger=write_tags)
    assert organizer.execute(run_id) == 'completed'
    final = str(world['dest'] / 'Artist' / 'Album' / '01 - a.mp3')
    with world['db'].read() as db:
        track = db.execute('SELECT * FROM tracks').fetchall()
        match = db.execute('SELECT * FROM match_results').fetchall()
        inst = db.execute("SELECT * FROM file_instances WHERE state='present'").fetchall()
        manual = db.execute('SELECT * FROM manual_identities').fetchall()
    stat = os.stat(final)
    assert [t['path'] for t in track] == [final] and track[0]['size'] == stat.st_size and track[0]['title'] == 'New Title'
    assert [m['path'] for m in match] == [final] and match[0]['decision'] == 'accepted' and match[0]['instance_id'] == 'inst-1'
    assert match[0]['file_size'] == stat.st_size and match[0]['file_mtime_ns'] == stat.st_mtime_ns
    assert [i['path'] for i in inst] == [final] and inst[0]['instance_id'] == 'inst-1', 'the instance id is stable'
    assert [m['path'] for m in manual] == [final] and manual[0]['size'] == stat.st_size
    assert LibraryRepository(world['db']).counts() is not None


def test_folders_a_move_leaves_empty_are_removed_but_never_the_root_or_a_folder_with_other_files(world):
    nested = world['source'] / 'Rock' / 'Live'
    write_mp3(nested / 'a.mp3', seed=1, tags={'title': 'a'})
    write_mp3(world['source'] / 'Rock' / 'keep.txt'.replace('.txt', '.mp3'), seed=2, tags={'title': 'k'})
    (world['source'] / 'Notes').mkdir()
    organizer = mover(world)
    run_id = organizer.create_run(str(world['source']), str(world['dest']),
                                  [WorkItem(source=str(nested / 'a.mp3'), relative='Artist/Album/01 - a.mp3')])
    assert organizer.execute(run_id) == 'completed'
    assert not nested.exists(), 'the folder the move emptied is gone'
    assert (world['source'] / 'Rock' / 'keep.mp3').exists() and (world['source'] / 'Rock').exists()
    assert world['source'].exists() and (world['source'] / 'Notes').exists(), 'unrelated empty folders are left alone'
