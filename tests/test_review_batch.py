"""Needs Review in bulk: edit many songs, save them all at once, and every valid correction is moved into its Artist/Album folder.

Real temporary audio files, an isolated database, and independent filesystem checks.
"""
import hashlib
import os

import pytest

from audio_fixtures import write_mp3
from meloshelf.application.jobs import JobService
from meloshelf.application.organize_service import OrganizeService
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.tag_writer import write_tags
from test_organize_service import ScriptedRecognition

NAMES = [f'r{i}.mp3' for i in range(1, 8)]
REVIEW = 'À vérifier'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listing(folder):
    return sorted(str(p.relative_to(folder)).replace(os.sep, '/') for p in folder.rglob('*') if p.is_file())


def build(tmp_path, hooks=None, db=None):
    db = db or Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    recognition = ScriptedRecognition(MatchStore(db), {})
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition)
    return db, OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12, hooks=hooks)


@pytest.fixture
def world(tmp_path):
    source, dest = tmp_path / 'Music', tmp_path / 'Organized'
    for i, name in enumerate(NAMES):
        write_mp3(source / name, seed=40 + i)                     # untagged and unidentifiable: all land in review
    db, service = build(tmp_path)
    first = service.run(str(source), str(dest))
    assert first['review'] == 7 and first['moved'] == 7
    return {'tmp': tmp_path, 'source': source, 'dest': dest, 'db': db, 'service': service,
            'review': dest / REVIEW, 'bytes': {n: sha(dest / REVIEW / n) for n in NAMES}}


def edit(world, name, title, artist, album='', **extra):
    return {'path': str(world['review'] / name), 'title': title, 'artist': artist, 'album': album, **extra}


FIVE = [('r1.mp3', 'First Song', 'Artist One', 'Album One'), ('r2.mp3', 'Second Song', 'Artist One', 'Album One'),
        ('r3.mp3', 'Third Song', 'Artist Two', 'Album Two'), ('r4.mp3', 'Fourth Song', 'Artist Three', ''),
        ('r5.mp3', 'Fifth Song', 'Artist Four', 'Album Four')]


def five_edits(world):
    return [edit(world, *row) for row in FIVE]


def test_five_edited_cards_are_saved_with_one_action_and_each_file_moves_to_its_destination(world):
    report = world['service'].apply_review(five_edits(world))
    assert report['saved'] == 5 and report['moved'] == 5 and report['failed'] == [] and report['skipped'] == []
    files = listing(world['dest'])
    assert 'Artist One/Album One/First Song.mp3' in files and 'Artist One/Album One/Second Song.mp3' in files
    assert 'Artist Two/Album Two/Third Song.mp3' in files and 'Artist Four/Album Four/Fifth Song.mp3' in files
    unknown = [f for f in files if f.startswith('Artist Three/')]
    assert len(unknown) == 1 and unknown[0].endswith('Fourth Song.mp3'), 'an empty album follows the existing Unknown Album policy'
    assert sorted(f for f in files if f.startswith(REVIEW + '/')) == [f'{REVIEW}/r6.mp3', f'{REVIEW}/r7.mp3'], 'unedited stay'
    assert [r for r in os.listdir(world['source'])] == [], 'nothing came back to the source'
    for name, title in (('r1.mp3', 'First Song'), ('r5.mp3', 'Fifth Song')):
        from mutagen.easyid3 import EasyID3
        found = [p for p in world['dest'].rglob(f'{title}.mp3')][0]
        assert EasyID3(found)['title'] == [title]
        assert found.stat().st_size > 0


def test_the_audio_is_intact_after_the_bulk_move(world):
    world['service'].apply_review(five_edits(world))
    moved = {p.name: p for p in world['dest'].rglob('*.mp3') if p.parent.name != REVIEW}
    for _, title, _, _ in FIVE:
        assert (title + '.mp3') in moved
    untouched = {p.name: sha(p) for p in world['review'].glob('*.mp3')}
    assert untouched == {n: world['bytes'][n] for n in ('r6.mp3', 'r7.mp3')}


def test_save_this_song_moves_only_that_card(world):
    report = world['service'].apply_review([edit(world, *FIVE[2])])
    assert report['saved'] == 1 and report['moved'] == 1
    files = listing(world['dest'])
    assert 'Artist Two/Album Two/Third Song.mp3' in files
    assert len([f for f in files if f.startswith(REVIEW + '/')]) == 6, 'the other six are still waiting'


def test_a_mix_of_valid_incomplete_conflicting_and_foreign_edits(world):
    service = world['service']
    service.resolve(str(world['review'] / 'r3.mp3'), title='Earlier Decision', artist='Someone Else', album='')
    items = [edit(world, *FIVE[0]),                                        # valid
             edit(world, 'r2.mp3', 'Only A Title', ''),                    # incomplete: no artist
             edit(world, 'r3.mp3', 'Different', 'Another Artist'),          # conflicts with the earlier human decision
             {'path': str(world['tmp'] / 'elsewhere.mp3'), 'title': 'x', 'artist': 'y', 'album': ''}]   # not in review
    preview = service.validate_review(items)
    states = {i['name']: i['status'] for i in preview['items']}
    assert states == {'r1.mp3': 'ready', 'r2.mp3': 'incomplete', 'r3.mp3': 'conflict', 'elsewhere.mp3': 'not_in_review'}
    assert preview['ready'] == 1 and preview['incomplete'] == 1 and preview['conflict'] == 1
    errors = {i['name']: i['errors'] for i in preview['items']}
    assert errors['r2.mp3'] == ['artist_required'] and errors['r3.mp3'] == ['conflict_existing_decision']
    ready = {i['name']: i['to'] for i in preview['items']}
    assert ready['r1.mp3'].replace('\\', '/').endswith('Artist One/Album One/First Song.mp3')
    report = service.apply_review(items)
    assert report['saved'] == 1 and report['moved'] == 1
    assert {s['code'] for s in report['skipped']} == {'artist_required', 'conflict_existing_decision', 'not_in_review'}
    with world['db'].read() as db:
        identity = {os.path.basename(r['path']): r['title'] for r in db.execute('SELECT path,title FROM manual_identities')}
    assert identity['r3.mp3'] == 'Earlier Decision', 'the human decision was not overwritten'
    assert (world['review'] / 'r2.mp3').exists() and (world['review'] / 'r3.mp3').exists()


def test_a_conflict_is_replaced_only_when_the_person_explicitly_says_so(world):
    service = world['service']
    service.resolve(str(world['review'] / 'r3.mp3'), title='Earlier Decision', artist='Someone Else', album='')
    item = edit(world, 'r3.mp3', 'Different', 'Another Artist', overwrite=True)
    report = service.apply_review([item])
    assert report['saved'] == 1 and report['moved'] == 1
    assert any(f.startswith('Another Artist/') for f in listing(world['dest']))


def test_one_failed_move_loses_no_other_edit_and_can_be_retried_safely(tmp_path):
    def deny(**info):
        if 'r3' in info.get('source', ''):
            raise OSError(13, 'denied')

    source, dest = tmp_path / 'Music', tmp_path / 'Organized'
    for i, name in enumerate(NAMES):
        write_mp3(source / name, seed=40 + i)
    db, service = build(tmp_path)
    service.run(str(source), str(dest))
    review = dest / REVIEW
    original = {n: sha(review / n) for n in NAMES}
    items = [{'path': str(review / n), 'title': t, 'artist': a, 'album': b} for n, t, a, b in FIVE]
    service.hooks = {'after_stage': deny}
    report = service.apply_review(items)
    assert report['saved'] == 5 and report['moved'] == 4
    assert [f['name'] for f in report['failed']] == ['r3.mp3'] and report['failed'][0]['code']
    assert sha(review / 'r3.mp3') == original['r3.mp3'], 'the failed file is back where it was, byte for byte'
    with db.read() as conn:
        saved = [os.path.basename(r['path']) for r in conn.execute('SELECT path FROM manual_identities')]
    assert sorted(saved) == ['Fifth Song.mp3', 'First Song.mp3', 'Fourth Song.mp3', 'Second Song.mp3', 'r3.mp3'], (
        'every decision is kept (re-keyed with the four moved files), including the failed one still in review')
    service.hooks = None
    retry = service.apply_review(items)
    assert retry['moved'] == 1 and retry['failed'] == [], 'only the failed one is moved again'
    assert sorted(s['code'] for s in retry['skipped']) == ['not_in_review'] * 4, 'nothing already moved is moved twice'
    files = listing(dest)
    assert len(files) == 7 and len(set(files)) == 7 and not [f for f in files if 'partial' in f]
    assert 'Artist Two/Album Two/Third Song.mp3' in files


def test_running_the_same_save_again_after_success_moves_nothing_twice(world):
    first = world['service'].apply_review(five_edits(world))
    before = listing(world['dest'])
    again = world['service'].apply_review(five_edits(world))
    assert first['moved'] == 5 and again['moved'] == 0 and again['saved'] == 0
    assert {s['code'] for s in again['skipped']} == {'not_in_review'}
    assert listing(world['dest']) == before


class Crash(BaseException):
    pass


def test_a_crash_in_the_middle_of_a_bulk_save_recovers_after_a_restart_without_loss_or_duplicates(tmp_path):
    source, dest = tmp_path / 'Music', tmp_path / 'Organized'
    for i, name in enumerate(NAMES):
        write_mp3(source / name, seed=40 + i)
    db, service = build(tmp_path)
    service.run(str(source), str(dest))
    review = dest / REVIEW
    items = [{'path': str(review / n), 'title': t, 'artist': a, 'album': b} for n, t, a, b in FIVE]
    calls = {'n': 0}

    def boom(**_):
        calls['n'] += 1
        if calls['n'] == 3:
            raise Crash()

    service.hooks = {'after_publish': boom}
    with pytest.raises(Crash):
        service.apply_review(items)
    db2, restarted = build(tmp_path, db=Database(tmp_path / 'lib.sqlite3'))              # a new process over the same database
    report = restarted.apply_review(items)
    files = listing(dest)
    assert len(files) == 7 and len(set(files)) == 7 and not [f for f in files if 'partial' in f]
    for _, title, artist, album in FIVE:
        assert f'{artist}/{album or "Unknown Album"}/{title}.mp3' in files or any(f.endswith(f'/{title}.mp3') for f in files)
    assert report['failed'] == []
    assert not os.listdir(source)


def test_validation_alone_changes_nothing(world):
    before = listing(world['dest'])
    world['service'].validate_review(five_edits(world))
    assert listing(world['dest']) == before
    with world['db'].read() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_identities').fetchone()[0] == 0


def test_no_whole_library_scan_or_recognition_happens_for_a_review_save(world):
    calls_before = list(world['service'].jobs.recognition_factory(False, lambda: False).calls)
    scans_before = len([j for j in world['service'].jobs.store.list_recent(50) if j['kind'] == 'scan'])
    world['service'].apply_review(five_edits(world))
    scans_after = len([j for j in world['service'].jobs.store.list_recent(50) if j['kind'] == 'scan'])
    assert scans_after == scans_before, 'saving corrections does not rescan the library'
    assert list(world['service'].jobs.recognition_factory(False, lambda: False).calls) == calls_before


def test_the_review_queue_drops_organized_files_and_keeps_the_rest(world):
    from meloshelf.application import organize_api

    class Server:
        organizer = world['service']
    world['service'].apply_review(five_edits(world))
    names = sorted(i['name'] for i in organize_api.review_items(Server))
    assert names == ['r6.mp3', 'r7.mp3']


def test_the_review_list_offers_the_title_first_reading_for_the_tito_prince_files(tmp_path):
    source, dest = tmp_path / 'Music', tmp_path / 'Organized'
    names = ['Un_Fils_de_Roi_à_Paris_-_Tito_Prince.mp3', 'Une_Étoile_parmi_Nous_-_Tito_Prince_.mp3', 'Alpha_Song_-_Beta_Name.mp3']
    for i, name in enumerate(names):
        write_mp3(source / name, seed=90 + i)
    db, service = build(tmp_path)
    service.run(str(source), str(dest))
    from meloshelf.application import organize_api

    class Server:
        organizer = service
    items = {i['name']: i for i in organize_api.review_items(Server)}
    for name, title in ((names[0], 'Un Fils de Roi à Paris'), (names[1], 'Une Étoile parmi Nous')):
        filename_candidates = [c for c in items[name]['candidates'] if c['source'] == 'filename']
        assert [(c['title'], c['artist']) for c in filename_candidates] == [(title, 'Tito Prince')], name
        assert filename_candidates[0]['evidence'] == 'recurring' and not filename_candidates[0]['ambiguous']
    alone = [c for c in items[names[2]]['candidates'] if c['source'] == 'filename']
    assert len(alone) == 2 and all(c['ambiguous'] for c in alone), 'no evidence: both readings, none chosen for the person'
