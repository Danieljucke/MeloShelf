"""Metadata analysis cache: recomputed only when the indexed library changes, never on an ordinary poll."""
import pytest

import meloshelf.application.metadata_service as service_module
from meloshelf.application.metadata_service import MetadataService
from meloshelf.infrastructure.correction_memory import CorrectionMemory
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.metadata_store import MetadataStore
from test_metadata_store import seed_track


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    for i in range(3):
        seed_track(db, f'D:/Library/A/{i}.mp3', title=f'S{i}', artist='Shared', album='A', cand_artist='Shared')
    seed_track(db, 'D:/Library/B/x.mp3', title='X', artist='Wrong', album='B', cand_artist='Right')
    return db


@pytest.fixture
def counted(monkeypatch):
    calls = {'n': 0}
    real = service_module.analyse_folder

    def counting(records):
        calls['n'] += 1
        return real(records)
    monkeypatch.setattr(service_module, 'analyse_folder', counting)
    MetadataService.clear_cache()
    return calls


def test_a_repeated_request_on_an_unchanged_library_does_not_reanalyse(library, counted):
    service = MetadataService(library)
    first = service.groups()
    cold = counted['n']
    for _ in range(5):  # what the interface does on repeated polls
        assert MetadataService(library).groups() == first
    assert cold == 2 and counted['n'] == cold  # two folders, analysed once


def test_a_scan_or_new_track_changes_the_revision_and_forces_a_fresh_analysis(library, counted):
    MetadataService(library).groups()
    before = counted['n']
    seed_track(library, 'D:/Library/B/new.mp3', title='N', artist='Wrong', album='B', cand_artist='Right')
    MetadataService(library).groups()
    assert counted['n'] > before


def test_a_cached_result_equals_a_fresh_analysis(library):
    MetadataService.clear_cache()
    cached = MetadataService(library).groups()
    MetadataService.clear_cache()
    fresh = MetadataService(library).groups()
    assert cached == fresh


def test_the_cache_is_separate_for_each_library_file(library, tmp_path, counted):
    other = Database(tmp_path / 'other.sqlite3')
    other.initialize()
    MetadataService(library).groups()
    before = counted['n']
    assert MetadataService(other).groups() == []
    assert counted['n'] == before + 0  # an empty library has no folders to analyse


def test_an_explicit_invalidation_forces_recomputation(library, counted):
    MetadataService(library).groups()
    before = counted['n']
    MetadataService.clear_cache()
    MetadataService(library).groups()
    assert counted['n'] > before


def test_raising_the_same_conflict_twice_records_it_once(library):
    group = [g for g in MetadataService(library).groups() if g['original'] == 'Wrong'][0]
    MetadataStore(library).decide(group['key'], 'approve', folder=group['folder'], field=group['field'],
                                  original=group['original'], proposed=group['proposed'],
                                  classification=group['classification'], paths=group['paths'])
    changed = dict(group, proposed='Different')
    memory = CorrectionMemory(library)
    assert memory.reconcile([changed]) == 1
    assert memory.reconcile([changed]) == 0
    conflicts = [e for e in memory.history(group['key']) if e['event'] == 'conflict']
    assert len(conflicts) == 1
