"""Fallback runs: persisted, resumable, offline-safe, and separate from acoustic results."""
import json

import pytest

from meloshelf.application.fallback import FallbackService
from meloshelf.application.fallback_runner import FallbackRunner
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.fallback_store import FallbackStore
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.application.fallback_runner import read_duration as REAL_READ_DURATION  # captured before any patching
from test_metadata_store import seed_track


@pytest.fixture(autouse=True)
def fixed_length(monkeypatch):
    """The fixture files are not on disk; the runner's length reader is given the length a real file would have."""
    import meloshelf.application.fallback_runner as runner_module
    monkeypatch.setattr(runner_module, 'read_duration', lambda path: 188.0)


class FakeClient:
    def __init__(self, by_title=None):
        self.by_title = by_title or {}
        self.calls = []

    def search_recordings(self, title, artist='', *, limit=5):
        self.calls.append((title, artist))
        return self.by_title.get(title, [])


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    seed_track(db, 'D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3', title='Making History', artist='', album='',
               cand_artist=None)
    seed_track(db, 'D:/Library/Aaron/Unknown_Thing.mp3', title='', artist='', album='', cand_artist=None)
    seed_track(db, 'D:/Library/Acoustic/Matched.mp3', title='Matched', artist='X', album='', cand_artist='X')
    return db


CATALOG = {'Making History': [{'recording_id': 'rec-1', 'title': 'Making History', 'artist': 'Aaron Cole',
                               'duration': 188.0, 'releases': [], 'search_score': 100}]}


def test_a_no_match_file_with_a_clear_text_agreement_is_stored_as_a_metadata_suggestion(library):
    client = FakeClient(CATALOG)
    runner = FallbackRunner(library, FallbackService(library, client))
    summary = runner.run(limit=10)
    stored = FallbackStore(library).get('D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3')
    assert stored['level'] == 'METADATA_SUGGESTED'
    assert summary['suggested'] == 1 and summary['unidentified'] >= 1


def test_files_that_already_have_an_acoustic_match_are_never_searched(library):
    client = FakeClient(CATALOG)
    FallbackRunner(library, FallbackService(library, client)).run(limit=10)
    assert all(title != 'Matched' for title, _ in client.calls)


def test_a_run_never_writes_to_the_acoustic_results(library):
    before = MatchStore(library).get_result('D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3')
    FallbackRunner(library, FallbackService(library, FakeClient(CATALOG))).run(limit=10)
    assert MatchStore(library).get_result('D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3') == before


def test_a_resumed_run_does_not_search_files_already_answered(library):
    FallbackRunner(library, FallbackService(library, FakeClient(CATALOG))).run(limit=10)
    second = FakeClient(CATALOG)
    FallbackRunner(library, FallbackService(library, second)).run(limit=10)
    assert second.calls == []


def test_results_survive_a_restart(library, tmp_path):
    FallbackRunner(library, FallbackService(library, FakeClient(CATALOG))).run(limit=10)
    reopened = Database(tmp_path / 'lib.sqlite3')
    reopened.initialize()
    assert FallbackStore(reopened).get('D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3')['level'] == 'METADATA_SUGGESTED'


def test_an_offline_run_sends_nothing_and_stores_no_answer(library):
    client = FakeClient(CATALOG)
    summary = FallbackRunner(library, FallbackService(library, client, offline=True)).run(limit=10)
    assert client.calls == [] and summary['offline_skipped'] >= 1
    assert FallbackStore(library).get('D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3') is None


def test_a_provider_failure_is_counted_and_left_for_a_later_attempt(library):
    from meloshelf.infrastructure.music_api import APIError

    class Failing(FakeClient):
        def search_recordings(self, title, artist='', *, limit=5):
            raise APIError('down', retryable=True, status=503, kind='provider_error')

    summary = FallbackRunner(library, FallbackService(library, Failing())).run(limit=10)
    assert summary['provider_failed'] >= 1
    assert FallbackStore(library).get('D:/Library/Aaron/Aaron_Cole_-_Making_History.mp3') is None


def test_a_cancelled_run_stops_and_keeps_what_it_finished(library):
    calls = {'n': 0}

    def cancel():
        calls['n'] += 1
        return calls['n'] > 1
    summary = FallbackRunner(library, FallbackService(library, FakeClient(CATALOG)), should_cancel=cancel).run(limit=10)
    assert summary['cancelled'] is True


def test_the_runner_reports_its_totals_as_json_serialisable_data(library):
    summary = FallbackRunner(library, FallbackService(library, FakeClient(CATALOG))).run(limit=10)
    json.dumps(summary)
    assert {'examined', 'suggested', 'unidentified', 'provider_failed', 'offline_skipped', 'cancelled'} <= set(summary)


def test_the_search_uses_the_base_title_so_a_bracketed_descriptor_does_not_hide_the_recording(library):
    # Real case: "Hold Me Back (Latin)" returned no hits. Descriptors are compared at scoring, not used to search.
    seed_track(library, 'D:/Library/KB/KB_-_Hold_Me_Back_(Latin).mp3', title='Hold Me Back (Latin)', artist='', album='',
               cand_artist=None)
    client = FakeClient({'Hold Me Back': []})
    FallbackRunner(library, FallbackService(library, client)).run(limit=50)
    assert any(title == 'Hold Me Back' for title, _ in client.calls)


def test_a_bare_audio_marker_in_brackets_is_removed_from_the_title():
    from meloshelf.domain.fallback import parse_filename
    assert parse_filename('Aaron_Cole_&_CASS_-_1994_(audio).mp3').title == '1994'


def test_the_runner_reads_the_length_from_the_file_read_only_and_says_when_it_cannot(tmp_path):
    read_duration = REAL_READ_DURATION
    assert read_duration(tmp_path / 'missing.mp3') is None
    garbage = tmp_path / 'garbage.mp3'
    garbage.write_bytes(b'not audio at all')
    assert read_duration(garbage) is None
    assert garbage.read_bytes() == b'not audio at all'  # reading never modifies the file
