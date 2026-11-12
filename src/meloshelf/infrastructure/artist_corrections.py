"""Persistent artist corrections: merge, keep-separate and undo, stored as an append-only log.

Each write is validated against the full log inside the same immediate transaction that appends it, so two
writers cannot both pass validation on the same state. Reads replay the log; the state is never stored.
"""
import time

from meloshelf.domain.artist_corrections import (
    MERGE, SPLIT, UNDO, ArtistCorrectionError, ArtistState, check_merge, replay)
from meloshelf.domain.artists import canonical_key

__all__ = ['ArtistCorrections', 'ArtistCorrectionError']


class ArtistCorrections:
    def __init__(self, database):
        self.database = database

    def events(self) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM artist_corrections ORDER BY id').fetchall()
        return [dict(r) for r in rows]

    def state(self) -> ArtistState:
        return replay(self.events())

    def merge(self, source: str, target: str) -> int:
        """Resolve every credit that matches `source` to the name `target`. Returns the event id."""
        def build(state):
            clean_source, clean_target = check_merge(source, target, state)
            return (MERGE, canonical_key(clean_source), canonical_key(clean_target), clean_target, None, clean_source)
        return self._write(build)

    def keep_separate(self, a: str, b: str) -> int:
        """Record that two names must never be merged. Returns the event id."""
        def build(_state):
            if not canonical_key(a) or not canonical_key(b):
                raise ArtistCorrectionError('Both artist names are needed.')
            if canonical_key(a) == canonical_key(b):
                raise ArtistCorrectionError('These names are already the same artist.')
            return (SPLIT, canonical_key(a), canonical_key(b), ' '.join(b.split()), None, ' '.join(a.split()))
        return self._write(build)

    def undo(self, event_id: int) -> int:
        """Reverse one earlier decision. Returns the id of the undo event."""
        def build(events_state):
            events, _state = events_state
            target = next((e for e in events if e['id'] == event_id), None)
            if target is None or target['action'] == UNDO:
                raise ArtistCorrectionError('That decision cannot be undone.')
            if any(e['action'] == UNDO and e['ref'] == event_id for e in events):
                raise ArtistCorrectionError('That decision has already been undone.')
            return (UNDO, '', '', '', event_id, '')
        return self._write(build, pass_events=True)

    def _write(self, build, pass_events: bool = False) -> int:
        with self.database.write() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM artist_corrections ORDER BY id').fetchall()]
            state = replay(rows)
            action, source_key, target_key, target_name, ref, source_name = (
                build((rows, state)) if pass_events else build(state))
            next_id = (rows[-1]['id'] + 1) if rows else 1
            # Validate the log as it will be after this append, before anything is written.
            candidate = rows + [{'id': next_id, 'action': action, 'source_key': source_key,
                                 'target_key': target_key, 'target_name': target_name, 'ref': ref}]
            replay(candidate)
            db.execute(
                '''INSERT INTO artist_corrections(id,action,source_key,target_key,target_name,ref,at,source_name)
                   VALUES(?,?,?,?,?,?,?,?)''',
                (next_id, action, source_key, target_key, target_name, ref, time.time(), source_name))
        return next_id
