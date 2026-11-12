"""The organizer works on exactly the folder the person chose, however that folder is spelled, and says plainly what it found.

Regression for a real report: a folder that was already indexed (129 unchanged files) produced an organizer result of
0 files found / 0 moved yet "Your music is organized", while a second folder worked. The folder was spelled with forward
slashes (as the folder chooser returns it); the scanner stores resolved Windows paths, so the organizer's prefix
match found nothing.
"""
import os
from pathlib import Path

import pytest

from audio_fixtures import write_mp3
from meloshelf.application.jobs import JobService
from meloshelf.application.organize_service import OrganizeService
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.tag_writer import write_tags
from test_organize_service import ScriptedRecognition


def audio(folder):
    return sorted(str(p.relative_to(folder)).replace(os.sep, '/') for p in folder.rglob('*.mp3'))


@pytest.fixture
def lab(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    recognition = ScriptedRecognition(MatchStore(db), {})
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition)
    service = OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12)
    return {'db': db, 'jobs': jobs, 'service': service, 'recognition': recognition, 'tmp': tmp_path}


def tagged_library(root, count, prefix='Song'):
    for i in range(count):
        write_mp3(root / f'sub{i % 4}' / f'{prefix}{i:03d}.mp3', seed=i % 200, tags={
            'title': f'Zebra Crossing {prefix} {i:03d}', 'artist': f'Artist {i % 5}', 'album': f'Album {i % 3}', 'tracknumber': str(i + 1)})


def scan(lab, folder):
    started = lab['jobs'].start_scan(str(folder))
    lab['jobs'].wait(started['job_id'], 60)
    return lab['jobs'].store.get(started['job_id'])


def spellings(path):
    """The same folder as a person might give it: native, forward slashes, trailing separator."""
    native = str(path)
    return {'native': native, 'forward-slashes': native.replace('\\', '/'), 'trailing-separator': native + os.sep}


SPELLINGS = ['native', 'forward-slashes', 'trailing-separator']


@pytest.mark.parametrize('spelling', SPELLINGS)
def test_a_already_indexed_folder_of_129_files_is_planned_and_moved_however_it_is_spelled(lab, spelling):
    source, dest = lab['tmp'] / 'Telegram', lab['tmp'] / 'Organized'
    tagged_library(source, 129)
    first = scan(lab, source)
    again = scan(lab, source)
    assert lab['jobs'].store.counters(first)['done'] == 129
    assert lab['jobs'].store.counters(again)['skipped'] == 129 and again['summary']['file_identity']['unchanged'] == 129
    summary = lab['service'].run(spellings(source)[spelling], spellings(dest)['native'])
    assert summary['total'] == 129 and summary['moved'] == 129 and summary['sources_removed'] == 129, summary
    assert len(audio(dest)) == 129
    assert audio(source) == [], 'no successfully moved original remains in the source'


def test_a_preview_of_an_already_indexed_folder_sees_every_file_however_it_is_spelled(lab):
    source = lab['tmp'] / 'Telegram'
    tagged_library(source, 20)
    scan(lab, source)
    for spelling, text in spellings(source).items():
        preview = lab['service'].preview(text, str(lab['tmp'] / 'Organized'))
        assert preview['ok'] and preview['total'] == 20, (spelling, preview)


def test_only_the_chosen_folder_is_identified_and_organized(lab):
    one, two, dest = lab['tmp'] / 'One', lab['tmp'] / 'Two', lab['tmp'] / 'Organized'
    for folder in (one, two):
        for i in range(5):
            write_mp3(folder / f'{i:04d}.mp3', seed=(1 if folder == one else 50) + i)       # untagged: needs recognition
    tagged_library(one, 6, 'One')
    tagged_library(two, 6, 'Two')
    scan(lab, one)
    scan(lab, two)
    before_two = {p: Path(p).read_bytes() for p in map(str, two.rglob('*.mp3'))}
    summary = lab['service'].run(str(one), str(dest))
    assert summary['total'] == 11 and summary['moved'] == 11
    assert audio(one) == [] and len(audio(two)) == 11, 'the other folder is untouched'
    assert {p: Path(p).read_bytes() for p in map(str, two.rglob('*.mp3'))} == before_two
    assert sorted(lab['recognition'].calls) == [f'{i:04d}.mp3' for i in range(5)], 'only the chosen folder was looked up'
    with lab['db'].read() as db:
        outside = db.execute("SELECT COUNT(*) FROM match_results WHERE path LIKE ?", (str(two) + '%',)).fetchone()[0]
    assert outside == 0, 'nothing was recognised outside the selected folder'


def test_repeating_a_successful_organization_says_it_is_already_organized(lab):
    source, dest = lab['tmp'] / 'Music', lab['tmp'] / 'Organized'
    tagged_library(source, 8)
    first = lab['service'].run(str(source), str(dest))
    assert first['moved'] == 8
    again = lab['service'].run(str(source), str(dest))
    status = lab['service'].status()
    assert again['total'] == 0 and again['moved'] == 0
    assert status['empty_reason'] == 'already_organized', 'proved by the earlier completed run for this folder and destination'
    assert len(audio(dest)) == 8


def test_an_empty_folder_reports_that_no_files_were_found(lab):
    empty = lab['tmp'] / 'Empty'
    empty.mkdir()
    summary = lab['service'].run(str(empty), str(lab['tmp'] / 'Organized'))
    status = lab['service'].status()
    assert summary['total'] == 0 and summary['moved'] == 0
    assert status['empty_reason'] == 'no_audio_files' and status['phase'] == 'completed'
    assert status['scanned'] == 0 and status['source'] == str(empty)


def test_a_folder_that_never_contained_music_is_not_called_already_organized(lab):
    empty = lab['tmp'] / 'Empty'
    empty.mkdir()
    lab['service'].run(str(empty), str(lab['tmp'] / 'Organized'))
    lab['service'].run(str(empty), str(lab['tmp'] / 'Organized'))
    assert lab['service'].status()['empty_reason'] == 'no_audio_files'


def test_a_new_organization_does_not_inherit_an_unfinished_identification_of_another_folder(lab):
    """A paused or cancelled library-wide identification (thousands queued) must not be resumed by a scoped organization."""
    other, mine, dest = lab['tmp'] / 'Other', lab['tmp'] / 'Mine', lab['tmp'] / 'Organized'
    for i in range(30):
        write_mp3(other / f'{i:03d}.mp3', seed=100 + i)
    for i in range(4):
        write_mp3(mine / f'{i:04d}.mp3', seed=i)
    scan(lab, other)
    store = lab['jobs'].store
    stale = store.create('identify', params={'offline': False, 'limit': 5040})
    store.add_items(stale, [str(p) for p in other.rglob('*.mp3')])
    store.update(stale, state='paused')
    summary = lab['service'].run(str(mine), str(dest))
    assert summary['total'] == 4 and summary['moved'] == 4
    assert sorted(lab['recognition'].calls) == ['0000.mp3', '0001.mp3', '0002.mp3', '0003.mp3'], \
        'the unrelated queue was not worked through'
    assert store.get(stale)['state'] == 'paused' and store.counters(store.get(stale))['pending'] == 30
    identify = store.get(lab['service'].status()['identify_job'])
    assert identify['id'] != stale and store.counters(identify)['total'] == 4, 'this run has its own job and counts'
    assert audio(other) and len(audio(other)) == 30


def test_the_status_names_the_folder_and_job_each_count_belongs_to(lab):
    source = lab['tmp'] / 'Music'
    tagged_library(source, 6)
    for i in range(3):
        write_mp3(source / f'{i:04d}.mp3', seed=70 + i)
    lab['service'].run(str(source), str(lab['tmp'] / 'Organized'))
    status = lab['service'].status()
    assert status['source'] == str(source) and status['dest'] == str(lab['tmp'] / 'Organized')
    assert status['scanned'] == 9
    assert status['identify'] == {'job': status['identify_job'], 'analysed': 3, 'proposed': 0, 'unknown': 3}


def test_the_breakdown_explains_why_moved_differs_from_completed_automatically(lab):
    """65 moved = 58 organized (53 on their own tags + 5 identified) + 7 awaiting review: different metrics, one total."""
    source, dest = lab['tmp'] / 'Music', lab['tmp'] / 'Organized'
    tagged_library(source, 10)
    for i in range(3):
        write_mp3(source / f'{i:04d}.mp3', seed=70 + i)                  # untagged and unidentifiable: review
    summary = lab['service'].run(str(source), str(dest))
    assert summary['moved'] == 13 and summary['organized'] == 10 and summary['review'] == 3
    assert summary['organized_from_tags'] == 10 and summary['recognized'] == 0
    assert summary['organized'] + summary['review'] == summary['moved'], 'every moved file is either organized or awaiting review'
