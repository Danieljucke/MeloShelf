"""One canonical entry per primary artist: in the Artists view, in the artist profile and in the destination folders."""
import os

import pytest
from mutagen.easyid3 import EasyID3

from audio_fixtures import write_mp3
from meloshelf.application.jobs import JobService
from meloshelf.application.organize_service import OrganizeService
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.tag_writer import write_tags
from test_organize_service import ScriptedRecognition

KYGO = ['Kygo', 'Kygo, Selena Gomez', 'Kygo, John Legend', 'Kygo, Conrad Sewell', 'Kygo, Donna Summer', 'Kygo & Tina Turner']
LECRAE = ['Lecrae', 'Lecrae & Andy Mineo', 'Lecrae Feat. E-40']


def listing(folder):
    return sorted(str(p.relative_to(folder)).replace(os.sep, '/') for p in folder.rglob('*') if p.is_file())


@pytest.fixture
def lab(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    recognition = ScriptedRecognition(MatchStore(db), {})
    jobs = JobService(db, recognition_factory=lambda offline, should_cancel: recognition)
    service = OrganizeService(db, jobs, tagger=write_tags, free_space=lambda path: 10**12)
    source = tmp_path / 'Music'
    seed = 0
    for credit in KYGO:
        seed += 1
        write_mp3(source / 'kygo' / f'k{seed}.mp3', seed=seed, tags={'title': f'Wave Rider {seed}', 'artist': credit,
                                                                  'album': 'Cloud Nine' if seed % 2 else 'Golden Hour', 'tracknumber': str(seed)})
    for credit in LECRAE:
        seed += 1
        write_mp3(source / 'lecrae' / f'l{seed}.mp3', seed=seed, tags={'title': f'Anomaly Song {seed}', 'artist': credit,
                                                                     'album': 'Anomaly', 'tracknumber': str(seed)})
    write_mp3(source / 'misc' / 'simon.mp3', seed=90, tags={'title': 'Bridge Over Water', 'artist': 'Simon & Garfunkel', 'album': 'Bookends', 'tracknumber': '1'})
    service.scan_only = None
    return {'db': db, 'service': service, 'jobs': jobs, 'source': source, 'dest': tmp_path / 'Organized', 'tmp': tmp_path}


def scan(lab):
    lab['jobs'].wait(lab['jobs'].start_scan(str(lab['source']))['job_id'], 60)


def test_the_destination_has_one_folder_per_primary_artist_and_credits_are_preserved(lab):
    summary = lab['service'].run(str(lab['source']), str(lab['dest']))
    assert summary['moved'] == 10 and summary['failed'] == 0
    folders = sorted({f.split('/')[0] for f in listing(lab['dest'])})
    assert folders == ['Kygo', 'Lecrae', 'Simon & Garfunkel'], folders
    kygo = [p for p in (lab['dest'] / 'Kygo').rglob('*.mp3')]
    assert len(kygo) == 6
    credits = {EasyID3(p)['artist'][0] for p in kygo}
    assert credits == set(KYGO), 'the complete original credit stays in every file'
    assert not os.listdir(lab['source'] / 'kygo') if (lab['source'] / 'kygo').exists() else True


def test_the_artists_view_shows_one_kygo_and_one_lecrae(lab):
    scan(lab)
    artists, total = LibraryRepository(lab['db']).artist_identities(query='')
    names = {a['name']: a for a in artists}
    assert set(names) >= {'Kygo', 'Lecrae', 'Selena Gomez', 'Andy Mineo'}
    kygo = [a for a in artists if a['name'].casefold().startswith('kygo')]
    assert [a['name'] for a in kygo] == ['Kygo'], 'one profile, not one per collaboration string'
    assert names['Kygo']['tracks'] == 6 and names['Kygo']['albums'] == 2
    assert names['Lecrae']['tracks'] == 3
    found, _ = LibraryRepository(lab['db']).artist_identities(query='Kygo')
    assert [a['name'] for a in found] == ['Kygo']
    assert names['Selena Gomez']['tracks'] == 0 and names['Selena Gomez']['featured_tracks'] == 1, 'a collaboration is not a new primary'


def test_a_group_name_is_one_artist_and_stays_whole(lab):
    scan(lab)
    artists, _ = LibraryRepository(lab['db']).artist_identities(query='Simon')
    assert [a['name'] for a in artists] == ['Simon & Garfunkel']
    assert not any(a['name'] == 'Garfunkel' for a in LibraryRepository(lab['db']).artist_identities()[0])


def test_collaboration_names_that_are_primary_elsewhere_keep_their_own_profile(lab):
    write_mp3(lab['source'] / 'misc' / 'tina.mp3', seed=95, tags={'title': 'Simply Better', 'artist': 'Tina Turner', 'album': 'Private Dancer', 'tracknumber': '1'})
    scan(lab)
    names = {a['name']: a for a in LibraryRepository(lab['db']).artist_identities()[0]}
    assert names['Tina Turner']['tracks'] == 1 and names['Tina Turner']['featured_tracks'] == 1


def test_an_artist_with_only_collaboration_credits_still_gets_one_profile(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    root = tmp_path / 'Music'
    for i, credit in enumerate(['Kygo, Selena Gomez', 'Kygo, John Legend', 'Kygo & Tina Turner', 'Kygo vs. U2']):
        write_mp3(root / f'k{i}.mp3', seed=i, tags={'title': f'Only Collab {i}', 'artist': credit, 'album': 'Cloud Nine', 'tracknumber': str(i + 1)})
    jobs = JobService(db, recognition_factory=lambda o, c: None)
    jobs.wait(jobs.start_scan(str(root))['job_id'], 60)
    artists, _ = LibraryRepository(db).artist_identities()
    primary = [a for a in artists if a['tracks']]
    assert [(a['name'], a['tracks']) for a in primary] == [('Kygo', 4)]


def library_for_profile(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    root = tmp_path / 'Music'
    rows = [('Kygo', 'Firestone', 'Cloud Nine', 1), ('Kygo, Selena Gomez', 'It Ain’t Me', 'Golden Hour', 2), ('Kygo, John Legend', 'Hymn', 'Golden Hour', 3),
            ('Kygo', 'Lone Single', '', 0), ('Selena Gomez', 'Her Own Song', 'Revival', 1), ('Selena Gomez feat. Kygo', 'Shared Track', 'Revival', 2),
            ('Kygo', 'Stargazing', 'Cloud Nine', 2)]
    for i, (credit, title, album, number) in enumerate(rows):
        write_mp3(root / f'p{i}.mp3', seed=i, tags={'title': title, 'artist': credit, 'album': album, 'tracknumber': str(number)} if album else {'title': title, 'artist': credit})
    jobs = JobService(db, recognition_factory=lambda o, c: None)
    jobs.wait(jobs.start_scan(str(root))['job_id'], 60)
    return db


def test_the_profile_lists_albums_singles_collaborations_and_original_credits(tmp_path):
    profile = LibraryRepository(library_for_profile(tmp_path)).artist_profile('Kygo')
    assert profile['name'] == 'Kygo'
    assert profile['counts'] == {'tracks': 5, 'albums': 2, 'singles': 1, 'collaborations': 1}
    assert {a['name']: a['tracks'] for a in profile['albums']} == {'Cloud Nine': 2, 'Golden Hour': 2}
    assert [s['title'] for s in profile['singles']] == ['Lone Single']
    assert [(c['title'], c['credit']) for c in profile['collaborations']] == [('Shared Track', 'Selena Gomez feat. Kygo')]
    credits = {c['credit']: c['tracks'] for c in profile['credits']}
    assert credits == {'Kygo': 3, 'Kygo, Selena Gomez': 1, 'Kygo, John Legend': 1}, 'the original credit of each song is kept'
    golden = [a for a in profile['albums'] if a['name'] == 'Golden Hour'][0]
    assert sorted(t['credit'] for t in golden['track_list']) == ['Kygo, John Legend', 'Kygo, Selena Gomez']


def test_a_collaborator_who_is_also_primary_has_a_separate_profile(tmp_path):
    profile = LibraryRepository(library_for_profile(tmp_path)).artist_profile('Selena Gomez')
    assert profile['counts']['tracks'] == 2 and profile['counts']['albums'] == 1
    assert profile['counts']['collaborations'] == 1, 'her appearance on a Kygo track stays a collaboration, not a primary track'


def test_an_unknown_artist_has_an_empty_profile(tmp_path):
    profile = LibraryRepository(library_for_profile(tmp_path)).artist_profile('Nobody At All')
    assert profile is None
