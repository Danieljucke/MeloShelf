"""Dedicated recording-version QA.

A person judges whether the proposed recording is the same *version* as the file (live, remix, remaster, ...),
which is a different question from whether it is the right recording. The judgement is stored apart from every
automatic decision and from the recording labels, changes nothing about any decision, and is used only to measure
how trustworthy MeloShelf's version verdicts are."""
import json
import time

from meloshelf.domain.accuracy import MIN_SAMPLE, wilson_interval
from meloshelf.domain.versions import (CONFIRMED_CONFLICT, INSUFFICIENT, POSSIBLE_CONFLICT, PROBABLE_AGREEMENT,
                                       VERDICTS, VERIFIED_AGREEMENT, assess_version)
from meloshelf.infrastructure.qa_store import QaError, _top

LABELS = ('same', 'different', 'unsure')
RANK = {CONFIRMED_CONFLICT: 0, POSSIBLE_CONFLICT: 1, INSUFFICIENT: 2, PROBABLE_AGREEMENT: 3, VERIFIED_AGREEMENT: 4}
CONFLICTS = (CONFIRMED_CONFLICT, POSSIBLE_CONFLICT)
AGREEMENTS = (VERIFIED_AGREEMENT, PROBABLE_AGREEMENT)
ROWS = '''SELECT m.path, m.recording_status, m.data, t.title, t.artist FROM match_results m
          LEFT JOIN tracks t ON t.path = m.path WHERE m.data LIKE '%"recording_id"%' ORDER BY m.path'''


def _assessment(row):
    data = json.loads(row['data'])
    top = _top(data)
    evidence = top.get('evidence') or {}
    duration = evidence.get('duration') or 'unknown'
    assessed = assess_version(row['title'] or '', top.get('title', ''), duration_state=duration,
                              release_titles=[x.get('title', '') for x in (top.get('release_details') or [])])
    return top, duration, assessed


class VersionQa:
    def __init__(self, database):
        self.database = database

    # ── queue ──────────────────────────────────────────────────────────────────

    def next_item(self, *, skip=(), verdict: str = '') -> dict | None:
        """The proposal whose version is most in doubt that this person has not judged (or whose judgement was
        for an earlier proposal). With a verdict, the first of that verdict."""
        if verdict and verdict not in VERDICTS:
            raise ValueError('Unknown version verdict')
        skipped = {str(p).replace('\\', '/') for p in skip}
        best = None
        with self.database.read() as db:
            judged = {r['path']: r['proposal_recording_id'] for r in db.execute(
                'SELECT path, proposal_recording_id FROM human_version_labels')}
            for row in db.execute(ROWS):
                if row['path'].replace('\\', '/') in skipped:
                    continue
                top, duration, assessed = _assessment(row)
                if judged.get(row['path']) == top.get('recording_id', ''):
                    continue
                if verdict and assessed.verdict != verdict:
                    continue
                rank = RANK[assessed.verdict]
                if best is None or rank < best[0]:
                    best = (rank, row, top, duration, assessed)
                    if verdict or rank == 0:
                        break
        if best is None:
            return None
        _, row, top, duration, assessed = best
        return {'path': row['path'], 'filename': row['path'].replace('\\', '/').rsplit('/', 1)[-1],
                'status': row['recording_status'] or '', 'local_title': row['title'] or '', 'local_artist': row['artist'] or '',
                'candidate': {'recording_id': top.get('recording_id', ''), 'title': top.get('title', ''),
                              'artist': top.get('artist', '')},
                'duration': duration, 'version': assessed.as_dict(), 'label': self.get_label(row['path'])}

    # ── labels ─────────────────────────────────────────────────────────────────

    def set_label(self, path: str, verdict) -> None:
        if not isinstance(verdict, str) or verdict not in LABELS:
            raise QaError('Choose same version, different version or not sure.')
        with self.database.write() as db:
            row = db.execute('''SELECT m.path, m.recording_status, m.data, t.title, t.artist FROM match_results m
                                LEFT JOIN tracks t ON t.path = m.path WHERE m.path=?''', (path,)).fetchone()
            if row is None:
                raise QaError('This file has not been analysed, so there is no proposal to judge.')
            top, _, assessed = _assessment(row)
            if not top:
                raise QaError('There is no proposal to judge for this file.')
            now = time.time()
            db.execute('''INSERT INTO human_version_label_history(path,verdict,assessed_verdict,proposal_recording_id,labeled_at,replaced_at)
                          SELECT path,verdict,assessed_verdict,proposal_recording_id,labeled_at,? FROM human_version_labels WHERE path=?''',
                       (now, path))
            db.execute('INSERT OR REPLACE INTO human_version_labels(path,verdict,assessed_verdict,proposal_recording_id,labeled_at) '
                       'VALUES(?,?,?,?,?)', (path, verdict, assessed.verdict, top.get('recording_id', ''), now))

    def get_label(self, path: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute('''SELECT l.*, m.data AS data FROM human_version_labels l
                                JOIN match_results m ON m.path = l.path WHERE l.path=?''', (path,)).fetchone()
        if row is None:
            return None
        current = _top(json.loads(row['data'])).get('recording_id', '')
        return {'verdict': row['verdict'], 'assessed_verdict': row['assessed_verdict'],
                'proposal_recording_id': row['proposal_recording_id'], 'labeled_at': row['labeled_at'],
                'stale': current != row['proposal_recording_id']}

    def _labels(self) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('''SELECT l.verdict, l.assessed_verdict, l.proposal_recording_id, m.data FROM human_version_labels l
                                 JOIN match_results m ON m.path = l.path''').fetchall()
        return [{'verdict': r['verdict'], 'assessed': r['assessed_verdict'],
                 'stale': _top(json.loads(r['data'])).get('recording_id', '') != r['proposal_recording_id']} for r in rows]

    # ── progress and report ────────────────────────────────────────────────────

    def progress(self) -> dict:
        with self.database.read() as db:
            proposals = db.execute("SELECT COUNT(*) FROM match_results WHERE data LIKE '%\"recording_id\"%'").fetchone()[0]
        live = [r for r in self._labels() if not r['stale']]
        counts = {k: sum(1 for r in live if r['verdict'] == k) for k in LABELS}
        return {'proposals': proposals, 'labelled': len(live), **counts, 'remaining': max(0, proposals - len(live))}

    def report(self) -> dict:
        """MeloShelf's version verdicts measured against the judgements. A judgement made for an earlier proposal
        (stale) is left out. Unsure answers are neither right nor wrong."""
        every = self._labels()
        live = [r for r in every if not r['stale']]
        matrix = {v: {k: 0 for k in LABELS} for v in VERDICTS}
        for r in live:
            matrix[r['assessed']][r['verdict']] += 1

        def claim(verdicts, agrees_with):
            judged = sum(matrix[v][k] for v in verdicts for k in ('same', 'different'))
            right = sum(matrix[v][agrees_with] for v in verdicts)
            return judged, right

        flagged_judged, flagged_right = claim(CONFLICTS, 'different')
        claimed_judged, claimed_right = claim(AGREEMENTS, 'same')
        return {'labelled': len(live), 'stale': len(every) - len(live), 'matrix': matrix,
                'unsure': sum(1 for r in live if r['verdict'] == 'unsure'),
                'conflicts_flagged': {'judged': flagged_judged, 'confirmed_by_person': flagged_right,
                                      'precision': flagged_right / flagged_judged if flagged_judged else None,
                                      'interval95': wilson_interval(flagged_right, flagged_judged),
                                      'sufficient_sample': flagged_judged >= MIN_SAMPLE},
                'agreements_claimed': {'judged': claimed_judged, 'confirmed_by_person': claimed_right,
                                       'precision': claimed_right / claimed_judged if claimed_judged else None,
                                       'interval95': wilson_interval(claimed_right, claimed_judged),
                                       'sufficient_sample': claimed_judged >= MIN_SAMPLE},
                'missed_conflicts': sum(matrix[v]['different'] for v in AGREEMENTS + (INSUFFICIENT,))}
