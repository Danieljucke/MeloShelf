"""The copy engine: real files, a durable journal, and the guarantees that make organizing safe.

The source library is never opened for writing. A file reaches its destination only after its copy was staged beside
it, verified byte for byte against the source, and tagged (the copy only). Nothing is overwritten, a crash at any
step resumes without duplicates, and running the same job again changes nothing.
"""
import hashlib
import os
import shutil

import pytest

from audio_fixtures import write_mp3
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.organizer import (DestinationError, Organizer, WorkItem, check_destination,
                                                stray_temp_files)


class Crash(BaseException):
    """A simulated power cut: not an Exception, so no engine code can swallow it."""


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    source = tmp_path / 'source'
    dest = tmp_path / 'organized'
    source.mkdir()
    return {'db': db, 'source': source, 'dest': dest, 'tmp': tmp_path}


def make(world, name, seed, **tags):
    return write_mp3(world['source'] / name, seed=seed, tags=tags or {'title': name, 'artist': 'A', 'album': 'B'})


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(folder):
    return {str(p.relative_to(folder)): (sha(p), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}


def items(world, *names, kind='organize'):
    return [WorkItem(source=str(world['source'] / n), relative=f'Artist/Album/{i + 1:02d} - {n}', kind=kind)
            for i, n in enumerate(names)]


def run(world, work, **options):
    organizer = Organizer(world['db'], free_space=lambda path: 10**12, **options)
    run_id = organizer.create_run(str(world['source']), str(world['dest']), work)
    return organizer, run_id


def counts(world, run_id):
    return Organizer(world['db']).counters(run_id)


# ── the destination guard ───────────────────────────────────────────────────────

def test_the_destination_cannot_be_the_source_or_inside_it(world):
    for bad in (world['source'], world['source'] / 'organized'):
        with pytest.raises(DestinationError) as caught:
            check_destination(str(world['source']), str(bad))
        assert caught.value.code in ('destination_is_source', 'destination_inside_source')


def test_the_source_cannot_be_inside_the_destination(world):
    with pytest.raises(DestinationError) as caught:
        check_destination(str(world['source']), str(world['tmp']))
    assert caught.value.code == 'source_inside_destination'


def test_a_separate_destination_is_accepted_even_before_it_exists(world):
    check_destination(str(world['source']), str(world['dest']))


def test_not_enough_free_space_stops_the_run_before_anything_is_created(world):
    make(world, 'a.mp3', 1)
    organizer = Organizer(world['db'], free_space=lambda path: 1000)
    with pytest.raises(DestinationError) as caught:
        organizer.create_run(str(world['source']), str(world['dest']), items(world, 'a.mp3'))
    assert caught.value.code == 'not_enough_space'
    assert not world['dest'].exists()


# ── copying ─────────────────────────────────────────────────────────────────────

def test_files_are_copied_to_their_planned_paths_and_the_source_is_untouched(world):
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        make(world, name, i + 1)
    before = snapshot(world['source'])
    organizer, run_id = run(world, items(world, 'a.mp3', 'b.mp3', 'c.mp3'))
    assert organizer.execute(run_id) == 'completed'
    assert snapshot(world['source']) == before, 'hashes and modification times of the source are unchanged'
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        copy = world['dest'] / 'Artist' / 'Album' / f'{i + 1:02d} - {name}'
        assert copy.is_file() and sha(copy) == before[name][0]
    assert counts(world, run_id)['done'] == 3
    assert not stray_temp_files(world['dest']), 'no staging file is left behind'


def test_a_corrupt_copy_is_never_published(world, monkeypatch):
    make(world, 'a.mp3', 1)
    organizer, run_id = run(world, items(world, 'a.mp3'))
    real = shutil.copyfileobj

    def corrupting(src, dst, *args, **kwargs):
        real(src, dst, *args, **kwargs)
        dst.write(b'\x00')                           # the copy gains a byte: it no longer matches the source

    monkeypatch.setattr('meloshelf.infrastructure.organizer.copy_stream', corrupting)
    assert organizer.execute(run_id) == 'completed_with_errors'
    assert not (world['dest'] / 'Artist' / 'Album' / '01 - a.mp3').exists()
    assert not stray_temp_files(world['dest'])
    state = organizer.items(run_id)[0]
    assert state['state'] == 'failed' and state['error'] == 'verification_failed'


def test_an_existing_different_file_is_never_overwritten(world):
    original = make(world, 'a.mp3', 1)
    occupied = world['dest'] / 'Artist' / 'Album' / '01 - a.mp3'
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b'someone else wrote this')
    keep = sha(occupied)
    organizer, run_id = run(world, items(world, 'a.mp3'))
    assert organizer.execute(run_id) == 'completed'
    assert sha(occupied) == keep, 'the existing file is untouched'
    produced = organizer.items(run_id)[0]
    assert produced['relative'] != '/'.join(('Artist', 'Album', '01 - a.mp3')), 'the copy got another name'
    assert sha(world['dest'] / produced['relative']) == sha(original)


def test_a_source_that_disappeared_fails_that_file_and_the_rest_continue(world):
    make(world, 'a.mp3', 1)
    make(world, 'b.mp3', 2)
    organizer, run_id = run(world, items(world, 'a.mp3', 'b.mp3'))
    (world['source'] / 'a.mp3').unlink()
    assert organizer.execute(run_id) == 'completed_with_errors'
    states = {os.path.basename(i['source']): (i['state'], i['error']) for i in organizer.items(run_id)}
    assert states == {'a.mp3': ('failed', 'source_missing'), 'b.mp3': ('done', '')}


# ── tags go to the copy only ────────────────────────────────────────────────────

def test_tags_are_written_to_the_copy_and_the_source_keeps_its_own(world):
    original = make(world, 'a.mp3', 1, title='0042', artist='Unknown', album='')
    seen = []

    def tagger(copy_path, tags, cover):
        seen.append((os.path.dirname(copy_path), dict(tags)))
        from mutagen.easyid3 import EasyID3
        audio = EasyID3(copy_path)
        for key, value in tags.items():
            audio[key] = value
        audio.save()
        return True

    work = [WorkItem(source=str(original), relative='Queen/Opera/11 - Song.mp3', kind='organize',
                     tags={'title': 'Song', 'artist': 'Queen', 'album': 'Opera', 'tracknumber': '11'})]
    before = sha(original)
    organizer, run_id = run(world, work, tagger=tagger)
    assert organizer.execute(run_id) == 'completed'
    from mutagen.easyid3 import EasyID3
    assert EasyID3(world['dest'] / 'Queen' / 'Opera' / '11 - Song.mp3')['title'] == ['Song']
    assert EasyID3(original)['title'] == ['0042'], 'the source file still carries its original tag'
    assert sha(original) == before
    assert seen and all(str(world['source']) not in folder for folder, _ in seen), 'the tagger never sees the source'


def test_a_tagging_failure_keeps_the_verified_copy_and_records_a_warning(world):
    original = make(world, 'a.mp3', 1)
    work = [WorkItem(source=str(original), relative='A/B/a.mp3', kind='organize', tags={'title': 'x'})]
    organizer, run_id = run(world, work, tagger=lambda path, tags, cover: False)
    assert organizer.execute(run_id) == 'completed'
    item = organizer.items(run_id)[0]
    assert item['state'] == 'done' and item['warning'] == 'tags_not_written'
    assert sha(world['dest'] / 'A' / 'B' / 'a.mp3') == sha(original)


# ── idempotence ─────────────────────────────────────────────────────────────────

def test_running_the_same_organization_again_changes_nothing(world):
    for i, name in enumerate(('a.mp3', 'b.mp3')):
        make(world, name, i + 1)
    organizer, run_id = run(world, items(world, 'a.mp3', 'b.mp3'))
    organizer.execute(run_id)
    first = snapshot(world['dest'])
    again, second_id = run(world, items(world, 'a.mp3', 'b.mp3'))
    assert again.execute(second_id) == 'completed'
    assert snapshot(world['dest']) == first, 'no file was created, replaced or rewritten'
    assert counts(world, second_id)['skipped'] == 2 and counts(world, second_id)['done'] == 0


def test_a_changed_source_is_organized_again_without_touching_the_earlier_copy(world):
    source = make(world, 'a.mp3', 1)
    organizer, run_id = run(world, items(world, 'a.mp3'))
    organizer.execute(run_id)
    first_copy = sha(world['dest'] / 'Artist' / 'Album' / '01 - a.mp3')
    source.write_bytes(source.read_bytes() + b'\x00' * 40)
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 5_000_000_000))
    again, second_id = run(world, items(world, 'a.mp3'))
    assert again.execute(second_id) == 'completed'
    assert sha(world['dest'] / 'Artist' / 'Album' / '01 - a.mp3') == first_copy
    produced = again.items(second_id)[0]
    assert produced['state'] == 'done' and produced['relative'] != 'Artist/Album/01 - a.mp3'


# ── crash recovery ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('crash_at', ['after_stage', 'after_verify', 'after_tag', 'after_publish'])
def test_a_crash_at_any_step_resumes_without_duplicates_or_loss(world, crash_at):
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        make(world, name, i + 1)
    before = snapshot(world['source'])

    def boom(**_):
        raise Crash()

    organizer, run_id = run(world, items(world, 'a.mp3', 'b.mp3', 'c.mp3'), hooks={crash_at: boom})
    with pytest.raises(Crash):
        organizer.execute(run_id)
    # a new process: no hooks, same database
    fresh = Organizer(world['db'], free_space=lambda path: 10**12)
    assert fresh.execute(run_id) == 'completed'
    produced = sorted(p.name for p in (world['dest'] / 'Artist' / 'Album').iterdir())
    assert produced == ['01 - a.mp3', '02 - b.mp3', '03 - c.mp3'], produced
    assert not stray_temp_files(world['dest'])
    assert counts(world, run_id)['done'] == 3
    assert snapshot(world['source']) == before
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        assert sha(world['dest'] / 'Artist' / 'Album' / f'{i + 1:02d} - {name}') == before[name][0]


@pytest.mark.parametrize('crash_at', ['after_stage', 'after_verify', 'after_tag', 'after_publish'])
def test_a_brand_new_run_after_a_crash_adopts_what_the_dead_run_left_instead_of_duplicating_it(world, crash_at):
    """The application starts a NEW run after a restart; the interrupted run's half-finished file must not become '(2)'."""
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        make(world, name, i + 1)
    before = snapshot(world['source'])

    def boom(**_):
        raise Crash()

    organizer, run_id = run(world, items(world, 'a.mp3', 'b.mp3', 'c.mp3'), hooks={crash_at: boom})
    with pytest.raises(Crash):
        organizer.execute(run_id)
    fresh = Organizer(world['db'], free_space=lambda path: 10**12)
    second = fresh.create_run(str(world['source']), str(world['dest']), items(world, 'a.mp3', 'b.mp3', 'c.mp3'))
    assert fresh.execute(second) == 'completed'
    produced = sorted(p.name for p in (world['dest'] / 'Artist' / 'Album').iterdir())
    assert produced == ['01 - a.mp3', '02 - b.mp3', '03 - c.mp3'], produced
    assert not stray_temp_files(world['dest'])
    assert snapshot(world['source']) == before


def test_a_cover_can_be_added_to_an_existing_copy_without_breaking_idempotence_or_touching_the_source(world):
    from mutagen.id3 import ID3
    from meloshelf.infrastructure.tag_writer import write_tags
    make(world, 'a.mp3', 1)
    before = snapshot(world['source'])
    organizer, run_id = run(world, items(world, 'a.mp3'))
    assert organizer.execute(run_id) == 'completed'
    cover = world['tmp'] / 'cover.jpg'
    cover.write_bytes(bytes.fromhex('ffd8ffe000104a46494600') + b'' * 40 + bytes.fromhex('ffd9'))
    final = world['dest'] / 'Artist' / 'Album' / '01 - a.mp3'
    assert not ID3(final).getall('APIC')
    fresh = Organizer(world['db'], tagger=write_tags, free_space=lambda path: 10**12)
    assert fresh.embed_cover(str(world['dest']), str(world['source'] / 'a.mp3'), str(cover)) is True
    assert ID3(final).getall('APIC'), 'the copy now carries the cover'
    assert fresh.embed_cover(str(world['dest']), str(world['source'] / 'a.mp3'), str(cover)) is False, 'already has one'
    again = fresh.create_run(str(world['source']), str(world['dest']), items(world, 'a.mp3'))
    assert fresh.execute(again) == 'completed' and fresh.counters(again)['skipped'] == 1, 'not copied a second time'
    assert [p.name for p in (world['dest'] / 'Artist' / 'Album').iterdir()] == ['01 - a.mp3']
    assert not stray_temp_files(world['dest'])
    assert snapshot(world['source']) == before


def test_a_failed_cover_write_leaves_the_existing_copy_exactly_as_it_was(world):
    make(world, 'a.mp3', 1)
    organizer, run_id = run(world, items(world, 'a.mp3'))
    organizer.execute(run_id)
    final = world['dest'] / 'Artist' / 'Album' / '01 - a.mp3'
    digest = sha(final)

    def failing(path, tags, cover):
        return False

    fresh = Organizer(world['db'], tagger=failing, free_space=lambda path: 10**12)
    assert fresh.embed_cover(str(world['dest']), str(world['source'] / 'a.mp3'), str(world['tmp'] / 'none.jpg')) is False
    assert sha(final) == digest and not stray_temp_files(world['dest'])


# ── pause and cancel ────────────────────────────────────────────────────────────

def test_pause_stops_between_files_and_resume_finishes_the_rest(world):
    for i, name in enumerate(('a.mp3', 'b.mp3', 'c.mp3')):
        make(world, name, i + 1)
    organizer, run_id = run(world, items(world, 'a.mp3', 'b.mp3', 'c.mp3'))
    asked = []

    def control():
        asked.append(1)
        return 'pause' if len([i for i in organizer.items(run_id) if i['state'] == 'done']) >= 1 else None

    assert organizer.execute(run_id, control=control) == 'paused'
    assert counts(world, run_id)['done'] == 1 and counts(world, run_id)['planned'] == 2
    assert not stray_temp_files(world['dest'])
    assert organizer.execute(run_id) == 'completed'
    assert counts(world, run_id)['done'] == 3


def test_cancel_leaves_finished_copies_and_nothing_half_written(world):
    for i, name in enumerate(('a.mp3', 'b.mp3')):
        make(world, name, i + 1)
    organizer, run_id = run(world, items(world, 'a.mp3', 'b.mp3'))
    calls = []

    def control():
        calls.append(1)
        return 'cancel' if len(calls) > 1 else None

    assert organizer.execute(run_id, control=control) == 'cancelled'
    assert counts(world, run_id)['done'] == 1
    assert not stray_temp_files(world['dest'])
    assert not (world['dest'] / 'Artist' / 'Album' / '02 - b.mp3').exists()
    assert organizer.run(run_id)['state'] == 'cancelled'


# ── resolving a file that was in À vérifier ─────────────────────────────────────

def test_a_resolved_review_file_replaces_its_review_copy_and_a_rerun_then_changes_nothing(world):
    source = make(world, 'a.mp3', 1)
    review = [WorkItem(source=str(source), relative='À vérifier/a.mp3', kind='review')]
    organizer, first = run(world, review)
    organizer.execute(first)
    assert (world['dest'] / 'À vérifier' / 'a.mp3').is_file()
    resolved = [WorkItem(source=str(source), relative='Queen/Opera/01 - Song.mp3', kind='organize')]
    again, second = run(world, resolved)
    assert again.execute(second) == 'completed'
    assert (world['dest'] / 'Queen' / 'Opera' / '01 - Song.mp3').is_file()
    assert not (world['dest'] / 'À vérifier' / 'a.mp3').exists(), 'the review copy we made is removed once replaced'
    assert source.is_file(), 'the source is never touched'
    third_organizer, third = run(world, resolved)
    snapshot_before = snapshot(world['dest'])
    assert third_organizer.execute(third) == 'completed'
    assert snapshot(world['dest']) == snapshot_before and counts(world, third)['skipped'] == 1


def test_a_review_copy_the_person_edited_is_kept_when_it_is_replaced(world):
    source = make(world, 'a.mp3', 1)
    organizer, first = run(world, [WorkItem(source=str(source), relative='À vérifier/a.mp3', kind='review')])
    organizer.execute(first)
    stale = world['dest'] / 'À vérifier' / 'a.mp3'
    stale.write_bytes(stale.read_bytes() + b'edited by hand')
    again, second = run(world, [WorkItem(source=str(source), relative='Queen/Opera/01 - Song.mp3', kind='organize')])
    again.execute(second)
    assert stale.is_file(), 'a file that no longer matches what MeloShelf wrote is not MeloShelf’s to delete'
    assert (world['dest'] / 'Queen' / 'Opera' / '01 - Song.mp3').is_file()
