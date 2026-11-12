"""The whole organizing workflow over real temporary audio: scan, recognise what needs it, plan, copy, report.

Recognition is scripted (no network); everything else is the real application code and the real filesystem.
"""
import hashlib
import os
import threading

import pytest
from mutagen.easyid3 import EasyID3

from audio_fixtures import write_mp3
from meloshelf.application.jobs import JobService
from meloshelf.application.organize_service import OrganizeService
from meloshelf.application.recognition import RecognitionReport, RecognitionStatus
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.tag_writer import write_tags


class ScriptedRecognition:
    """Stands in for AcoustID + MusicBrainz: the outcome for each file name is decided by the test."""

    def __init__(self, store, script):
        self.store, self.script, self.calls = store, script, []

    def identify(self, path, *, tags=None, on_stage=None):
        self.calls.append(path.name)
        outcome = self.script.get(path.name)
        stat = path.stat()
        if outcome is None:
            self.store.put_result(path, stat.st_size, stat.st_mtime_ns, 'unmatched', {'candidates': [], 'reason': 'none'},
                                  recording_status='UNIDENTIFIED')
            return RecognitionReport(str(path), RecognitionStatus.UNMATCHED, code='no_match')
        title, artist, releases, verdict = outcome
        candidate = {'recording_id': 'rec-' + path.stem, 'title': title, 'artist': artist, 'acoustid_score': 0.99,
                     'release_details': [{'id': f'{i:08d}-0000-0000-0000-000000000000', 'title': r} for i, r in enumerate(releases)],
                     'releases': list(releases), 'evidence': {'score': 95, 'version_verdict': verdict}}
        self.store.put_result(path, stat.st_size, stat.st_mtime_ns, 'review', {'candidates': [candidate], 'reason': 'ok'},
                              recording_status='AUTO_APPROVED')
        return RecognitionReport(str(path), RecognitionStatus.REVIEW)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / 'Music'
    for sub in ('Queen', 'Lecrae', 'Downloads', 'More'):
        (root / sub).mkdir(parents=True)
    make = lambda rel, seed, **tags: write_mp3(root / rel, seed=seed, tags=tags)  # noqa: E731
    make('Queen/bohemian.mp3', 1, title='Bohemian Rhapsody', artist='Queen', album='A Night at the Opera', tracknumber='11')
    for i, name in enumerate(('Give In', 'Fear', 'Broken'), start=1):
        make(f'Lecrae/{i:02d}.mp3', 10 + i, title=name, artist='Lecrae', album='Anomaly', tracknumber=str(i))
    make('Downloads/0042.mp3', 20)                                    # recognisable song with no tags at all
    make('Downloads/0043.mp3', 21)                                    # nobody can identify this one
    make('Downloads/0044.mp3', 22)                                    # recognised, but live or studio is unclear
    make('Downloads/song.mp3', 30, title='Same Name', artist='Duo', album='Pair')
    make('More/song.mp3', 31, title='Same Name', artist='Duo', album='Pair')   # different audio, same destination
    return root


SCRIPT = {'0042.mp3': ('Restored Song', 'Queen', ('Greatest Hits',), 'probable_agreement'),
          '0044.mp3': ('Maybe Live', 'Queen', ('Live Album',), 'possible_conflict')}


@pytest.fixture
def world(tmp_path, library):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    store = MatchStore(db)
    recognition = ScriptedRecognition(store, SCRIPT)
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition)
    service = OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12)
    return {'db': db, 'store': store, 'jobs': jobs, 'service': service, 'recognition': recognition,
            'source': library, 'dest': tmp_path / 'Organized'}


def tags_of(path):
    from mutagen import File
    audio = File(path, easy=True)
    return dict(audio.tags or {}) if audio is not None else {}


def hashes(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}


def listing(folder):
    return sorted(str(p.relative_to(folder)).replace(os.sep, '/') for p in folder.rglob('*') if p.is_file())


def organize(world, **options):
    return world['service'].run(str(world['source']), str(world['dest']), **options)




def test_the_destination_inside_the_source_is_refused_before_anything_happens(world):
    result = world['service'].preview(str(world['source']), str(world['source'] / 'Organized'))
    assert result['ok'] is False and result['code'] == 'destination_inside_source'








def test_only_files_that_need_it_are_sent_to_recognition(world):
    organize(world)
    assert sorted(world['recognition'].calls) == ['0042.mp3', '0043.mp3', '0044.mp3']










class CountingCovers:
    def __init__(self, tmp_path):
        self.calls, self.path = [], tmp_path / 'cover.jpg'
        self.path.write_bytes(bytes.fromhex('ffd8ffe000104a46494600') + b'\x01' * 40 + bytes.fromhex('ffd9'))

    def cover_for(self, decision, item):
        self.calls.append((decision.artist, decision.album))
        return str(self.path)


def audio_files(folder):
    return [f for f in listing(folder) if f.endswith('.mp3')]


def test_the_preview_changes_nothing_and_lists_the_proposed_moves(world):
    before = hashes(world['source'])
    jobs = world['jobs']
    jobs.wait(jobs.start_scan(str(world['source']))['job_id'], 30)
    preview = world['service'].preview(str(world['source']), str(world['dest']))
    assert preview['ok'] is True and preview['total'] == 9 and preview['ready'] == 6
    assert preview['will_recognise'] == 3, 'only the three files without usable tags are looked up'
    targets = {m['to'].replace(os.sep, '/').split('Organized/')[1] for m in preview['moves']}
    assert 'Lecrae/Anomaly/01 - Give In.mp3' in targets and 'Queen/A Night at the Opera/11 - Bohemian Rhapsody.mp3' in targets
    assert all(m['from'].startswith(str(world['source'])) for m in preview['moves'])
    assert not world['dest'].exists(), 'a preview never creates the destination'
    assert hashes(world['source']) == before, 'a preview moves nothing'


def test_a_preview_can_scan_the_folder_first_without_changing_it(world):
    before = hashes(world['source'])
    preview = world['service'].preview(str(world['source']), str(world['dest']), scan=True)
    assert preview['ok'] and preview['total'] == 9 and hashes(world['source']) == before


def test_mixed_music_is_moved_by_artist_and_album_and_the_originals_are_gone(world):
    before = hashes(world['source'])
    summary = organize(world)
    assert summary['state'] == 'completed'
    files = listing(world['dest'])
    assert 'Queen/A Night at the Opera/11 - Bohemian Rhapsody.mp3' in files
    assert [f for f in files if f.startswith('Lecrae/Anomaly/')] == [
        'Lecrae/Anomaly/01 - Give In.mp3', 'Lecrae/Anomaly/02 - Fear.mp3', 'Lecrae/Anomaly/03 - Broken.mp3']
    assert 'Queen/Greatest Hits/Restored Song.mp3' in files, 'the recognised song is named and filed from its recognition'
    assert 'Duo/Pair/Same Name.mp3' in files and 'Duo/Pair/Same Name (2).mp3' in files, 'no collision overwrites'
    assert sorted(f for f in files if f.startswith('À vérifier/')) == ['À vérifier/0043.mp3', 'À vérifier/0044.mp3']
    assert audio_files(world['source']) == [], 'every original location is empty after a successful move'
    untouched = {h for name, h in before.items() if not name.endswith('0042.mp3')}
    assert untouched <= set(hashes(world['dest']).values()) | {h for h in before.values()}, 'audio travelled intact'


def test_recognised_files_get_corrected_tags_on_the_moved_file(world):
    organize(world)
    restored = EasyID3(world['dest'] / 'Queen' / 'Greatest Hits' / 'Restored Song.mp3')
    assert restored['title'] == ['Restored Song'] and restored['artist'] == ['Queen'] and restored['album'] == ['Greatest Hits']


def test_unresolved_files_keep_their_original_names_and_carry_no_speculative_metadata(world):
    before = {n: (world['source'] / 'Downloads' / n).read_bytes() for n in ('0043.mp3', '0044.mp3')}
    organize(world)
    for name, data in before.items():
        assert (world['dest'] / 'À vérifier' / name).read_bytes() == data, 'untouched, tags and all'


def test_only_files_that_need_it_are_sent_to_recognition(world):
    organize(world)
    assert sorted(world['recognition'].calls) == ['0042.mp3', '0043.mp3', '0044.mp3']


def test_the_final_statistics_count_real_placements_and_real_source_removals(world):
    summary = organize(world)
    assert summary['total'] == 9 and summary['moved'] == 9 and summary['sources_removed'] == 9
    assert summary['organized'] == 7 and summary['review'] == 2 and summary['recognized'] == 1
    assert summary['failed'] == 0 and summary['skipped'] == 0 and summary['needs_recovery'] == 0


def test_running_it_again_moves_nothing_and_duplicates_nothing(world):
    organize(world)
    first = hashes(world['dest'])
    again = organize(world)
    assert again['state'] == 'completed' and again['moved'] == 0 and again['sources_removed'] == 0
    assert again['skipped'] == 2, 'the two review files are recognised as already in place, not reported as new'
    assert hashes(world['dest']) == first and audio_files(world['source']) == []


def test_an_interrupted_move_resumes_and_completes_without_duplicates_or_loss(world):
    class Crash(BaseException):
        pass

    calls = {'n': 0}
    real = write_tags

    def crashing(path, tags, cover):
        calls['n'] += 1
        if calls['n'] == 1:
            raise Crash()
        return real(path, tags, cover)

    world['service'].tagger = crashing
    with pytest.raises(Crash):
        organize(world)
    world['service'].tagger = real
    resumed = organize(world)
    assert resumed['state'] == 'completed'
    files = audio_files(world['dest'])
    assert len(files) == len(set(files)) == 9 and not [f for f in listing(world['dest']) if 'partial' in f]
    assert audio_files(world['source']) == []


def test_a_crash_right_after_a_file_was_published_does_not_duplicate_it_on_the_next_run(world):
    calls = {'n': 0}

    class Crash(BaseException):
        pass

    def boom(**_):
        calls['n'] += 1
        if calls['n'] == 4:
            raise Crash()

    world['service'].hooks = {'after_publish': boom}
    with pytest.raises(Crash):
        organize(world)
    world['service'].hooks = None
    resumed = organize(world)
    files = listing(world['dest'])
    assert resumed['state'] == 'completed'
    assert files == ['Duo/Pair/Same Name (2).mp3', 'Duo/Pair/Same Name.mp3', 'Lecrae/Anomaly/01 - Give In.mp3',
                     'Lecrae/Anomaly/02 - Fear.mp3', 'Lecrae/Anomaly/03 - Broken.mp3',
                     'Queen/A Night at the Opera/11 - Bohemian Rhapsody.mp3', 'Queen/Greatest Hits/Restored Song.mp3',
                     'À vérifier/0043.mp3', 'À vérifier/0044.mp3'], files
    assert audio_files(world['source']) == []


def test_a_person_can_resolve_a_review_file_and_it_moves_into_the_library(world):
    organize(world)
    review_copy = world['dest'] / 'À vérifier' / '0043.mp3'
    world['service'].resolve(str(review_copy), title='Known Now', artist='Someone', album='Their Album')
    again = organize(world)
    files = listing(world['dest'])
    assert 'Someone/Their Album/Known Now.mp3' in files and 'À vérifier/0043.mp3' not in files
    assert again['moved'] == 1 and again['review'] == 1 and again['state'] == 'completed'
    assert EasyID3(world['dest'] / 'Someone' / 'Their Album' / 'Known Now.mp3')['title'] == ['Known Now']
    with world['db'].read() as db:
        paths = [r['path'] for r in db.execute('SELECT path FROM manual_identities')]
    assert paths == [str(world['dest'] / 'Someone' / 'Their Album' / 'Known Now.mp3')], 'the decision followed the file'


def test_the_workflow_runs_in_the_background_and_reports_its_phases(world):
    started = world['service'].start(str(world['source']), str(world['dest']))
    assert started['started'] is True
    deadline = threading.Event()
    status = {'phase': ''}
    for _ in range(300):
        status = world['service'].status()
        if status['phase'] in ('completed', 'completed_with_errors', 'failed'):
            break
        deadline.wait(0.1)
    assert status['phase'] == 'completed' and status['summary']['moved'] == 9
    assert status['phases_seen'][:3] == ['scanning', 'recognizing', 'planning'] and 'moving' in status['phases_seen']


def test_a_missing_recogniser_is_reported_not_hidden(tmp_path, library):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    recognition = ScriptedRecognition(MatchStore(db), SCRIPT)
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition,
                      preflight=lambda offline: 'fpcalc_unavailable')
    service = OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12)
    summary = service.run(str(library), str(tmp_path / 'Organized'))
    status = service.status()
    assert status['recognition_blocker'] == 'fpcalc_unavailable', 'the person is told why files were not identified'
    assert 'fpcalc_unavailable' not in status['warnings']
    assert summary['total'] == 9 and summary['review'] == 3, 'unrecognised files wait in review, none guessed'
    assert recognition.calls == []


def test_a_cross_volume_move_through_the_whole_workflow_leaves_no_original_and_no_temporary_file(tmp_path, library):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    recognition = ScriptedRecognition(MatchStore(db), SCRIPT)
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition)
    service = OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12, same_volume=lambda a, b: False)
    summary = service.run(str(library), str(tmp_path / 'Organized'))
    assert summary['moved'] == 9 and summary['sources_removed'] == 9
    assert audio_files(library) == [] and not [f for f in listing(tmp_path / 'Organized') if 'partial' in f]


class CountingCovers:
    def __init__(self, tmp_path):
        self.calls, self.path = [], tmp_path / 'cover.jpg'
        self.path.write_bytes(bytes.fromhex('ffd8ffe000104a46494600') + b'\x01' * 40 + bytes.fromhex('ffd9'))

    def cover_for(self, decision, item):
        self.calls.append((decision.artist, decision.album))
        return str(self.path)


def test_covers_are_looked_up_once_per_album_in_their_own_phase(tmp_path, library):
    from mutagen.id3 import ID3
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    recognition = ScriptedRecognition(MatchStore(db), SCRIPT)
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition)
    covers = CountingCovers(tmp_path)
    service = OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12, covers=covers)
    service.run(str(library), str(tmp_path / 'Organized'))
    assert sorted(covers.calls) == sorted(set(covers.calls)), 'one lookup per album, not one per track'
    assert ('Lecrae', 'Anomaly') in covers.calls
    assert service.status()['phases_seen'] == ['scanning', 'recognizing', 'planning', 'artwork', 'moving']
    moved = tmp_path / 'Organized' / 'Lecrae' / 'Anomaly' / '01 - Give In.mp3'
    assert ID3(moved).getall('APIC'), 'the confirmed cover reaches the moved file'


def test_a_cover_confirmed_after_the_run_is_added_to_the_moved_files_in_place(tmp_path, library):
    from mutagen.id3 import ID3
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    recognition = ScriptedRecognition(MatchStore(db), SCRIPT)
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition)
    service = OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12)
    dest = tmp_path / 'Organized'
    service.run(str(library), str(dest))
    moved = dest / 'Lecrae' / 'Anomaly' / '01 - Give In.mp3'
    assert not ID3(moved).getall('APIC')
    service.covers = CountingCovers(tmp_path)
    assert service.apply_covers() == 7, 'every organized file had no cover'
    assert ID3(moved).getall('APIC'), 'the confirmed cover is now in the file'
    snapshot = hashes(dest)
    again = service.run(str(library), str(dest))
    assert again['moved'] == 0 and hashes(dest) == snapshot, 'a later run does not disturb what is in place'


def test_a_file_the_person_identified_is_counted_as_their_correction_not_as_automatic_recognition(world):
    organize(world)
    world['service'].resolve(str(world['dest'] / 'À vérifier' / '0043.mp3'), title='Known Now', artist='Someone', album='Their Album')
    again = organize(world)
    assert again['moved'] == 1 and again['by_you'] == 1 and again['recognized'] == 0 and again['organized_from_tags'] == 0
