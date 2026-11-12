"""P2: the wording of a recognition decision must match its status, and the Undo preview must name every recording it affects.

No approval decision changes here: only the words that describe it and the list of recordings a preview shows.
"""
import json
from pathlib import Path

from meloshelf.application.artist_corrections_api import preview_undo, write
from meloshelf.domain.matching import Evidence, assess_match
from meloshelf.domain.models import Track
from meloshelf.infrastructure.artist_corrections import ArtistCorrections
from meloshelf.infrastructure.database import Database
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.repository import LibraryRepository

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / 'src' / 'meloshelf' / 'presentation' / 'web' / 'index.html').read_text(encoding='utf-8')
EN = json.loads((ROOT / 'src' / 'meloshelf' / 'presentation' / 'web' / 'i18n' / 'en.json').read_text(encoding='utf-8'))


def test_a_strong_match_states_the_evidence_and_gives_no_instruction():
    evidence = Evidence(provider_score=0.99, score=90, title='match', artist='match', duration='match', duration_delta=0.0)
    verdict = assess_match([evidence])
    assert verdict.verdict == 'strong'
    assert 'confirm' not in verdict.reason.lower(), verdict.reason


def test_a_stored_result_with_the_old_instruction_is_shown_without_it(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    store = MatchStore(db)
    store.put_result('/music/a.mp3', 10, 20, 'matched',
                     {'candidates': [], 'reason': 'Fingerprint, duration and tags agree; confirm to accept'},
                     recording_status='AUTO_APPROVED')
    record = next(r for r in store.list_results() if r['path'] == '/music/a.mp3')
    assert record['recording_status'] == 'AUTO_APPROVED'
    assert 'confirm' not in record['reason'].lower(), record['reason']


def test_the_page_names_each_state_and_its_pending_version_without_an_instruction_for_approved_records():
    script = PAGE.split('<script id="meloshelf-app">', 1)[1]
    for status in ('AUTO_APPROVED', 'USER_APPROVED', 'NEEDS_REVIEW', 'UNIDENTIFIED', 'REJECTED', 'EDITION_PENDING'):
        assert status in script, status
    for key in ('m.awaiting-review-note', 'm.version-pending-note', 'm.version-pending-tag'):
        assert key in EN, key
        assert f"'{key}'" in script, key


def test_an_undo_of_keep_separate_previews_both_names(tmp_path):
    db = Database(tmp_path / 'lib.sqlite3')
    db.initialize()
    LibraryRepository(db).save_many([
        Track('/music/a.mp3', 1, '.mp3', 1, 'A', 'Kirk Franklin', 'Q', '', 'tagged'),
        Track('/music/b.mp3', 1, '.mp3', 1, 'B', 'The Kirk Franklin', 'Q', '', 'tagged'),
    ])
    status, _ = write(db, '/api/artist-corrections/keep-separate', {'a': 'Kirk Franklin', 'b': 'The Kirk Franklin', 'confirm': True})
    assert status == 200
    event_id = max(e['id'] for e in ArtistCorrections(db).events())
    preview = preview_undo(db, event_id)
    assert preview['allowed'] is True
    assert preview['total'] == 2, preview
    assert {r['artist'] for r in preview['recordings']} == {'Kirk Franklin', 'The Kirk Franklin'}
