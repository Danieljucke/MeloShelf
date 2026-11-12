"""Human reference labels for recognition proposals.

Labels are judgements about a proposal. They are stored apart from automatic decisions (match_results)
and are never written by a scan, so a later automated run cannot overwrite them. A label remembers which
proposal it judged; if the proposal later changes, the label is flagged as stale rather than silently kept.
"""
import json
import time

from dataclasses import replace

from meloshelf.domain.accuracy import by_category, summarize
from meloshelf.domain.matching import Evidence
from meloshelf.domain.versions import assess_version
from meloshelf.infrastructure.qa_filters import QaFilter, row_category

LABELS = ('correct', 'incorrect', 'unsure')
MAX_TEXT = 200
PRIORITY = "CASE m.recording_status WHEN 'AUTO_APPROVED' THEN 0 WHEN 'NEEDS_REVIEW' THEN 1 ELSE 2 END"


class QaError(ValueError):
    """A label was rejected; the message is safe to show to the user."""


def _top(data: dict) -> dict:
    candidates = data.get('candidates') or []
    return candidates[0] if candidates else {}


def check_label(label, corrected_recording_id, corrected_title, corrected_artist, candidate_ids) -> None:
    """The rules for one label, shared by a label given by hand and one imported from a sheet."""
    if not isinstance(label, str) or label not in LABELS:
        raise QaError('Choose correct, incorrect or unsure.')
    for name, value in (('corrected_title', corrected_title), ('corrected_artist', corrected_artist)):
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise QaError(f'{name} must be text of at most {MAX_TEXT} characters.')
    if label == 'correct' and (corrected_recording_id or corrected_title or corrected_artist):
        raise QaError('A correct label cannot carry a correction.')
    if candidate_ids is not None and corrected_recording_id and corrected_recording_id not in candidate_ids:
        raise QaError('The chosen alternative is not one of the candidates for this file.')


def write_label(db, path: str, label: str, *, top: dict, status: str, corrected_recording_id: str = '',
                corrected_title: str = '', corrected_artist: str = '', source: str) -> float:
    """Store one label inside the caller's transaction. A label it replaces is first copied to the history, so a
    judgement is never lost by being changed (by hand or by an approved CSV import)."""
    now = time.time()
    db.execute('''INSERT INTO human_label_history(path,label,proposal_recording_id,proposal_status,corrected_recording_id,
                  corrected_title,corrected_artist,labeled_at,replaced_at,source)
                  SELECT path,label,proposal_recording_id,proposal_status,corrected_recording_id,corrected_title,
                         corrected_artist,labeled_at,?,? FROM human_labels WHERE path=?''', (now, source, path))
    db.execute('''INSERT OR REPLACE INTO human_labels(path,label,proposal_recording_id,proposal_status,
                  corrected_recording_id,corrected_title,corrected_artist,labeled_at)
                  VALUES(?,?,?,?,?,?,?,?)''',
               (path, label, top.get('recording_id', ''), status, corrected_recording_id,
                corrected_title.strip(), corrected_artist.strip(), now))
    return now


class QaStore:
    def __init__(self, database):
        self.database = database

    # ── writing ────────────────────────────────────────────────────────────────

    def set_label(self, path: str, label, *, corrected_recording_id: str = '', corrected_title: str = '',
                  corrected_artist: str = '') -> None:
        check_label(label, corrected_recording_id, corrected_title, corrected_artist, None)
        with self.database.write() as db:
            row = db.execute('SELECT data, recording_status FROM match_results WHERE path=?', (path,)).fetchone()
            if row is None:
                raise QaError('This file has not been analysed, so there is no proposal to judge.')
            data = json.loads(row['data'])
            check_label(label, corrected_recording_id, corrected_title, corrected_artist,
                        {c.get('recording_id') for c in data.get('candidates') or []})
            write_label(db, path, label, top=_top(data), status=row['recording_status'] or '',
                        corrected_recording_id=corrected_recording_id, corrected_title=corrected_title,
                        corrected_artist=corrected_artist, source='manual')

    # ── reading ────────────────────────────────────────────────────────────────

    def get_label(self, path: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute('''SELECT l.*, m.data AS data FROM human_labels l
                                JOIN match_results m ON m.path = l.path WHERE l.path=?''', (path,)).fetchone()
        if row is None:
            return None
        current = _top(json.loads(row['data'])).get('recording_id', '')
        return {'label': row['label'], 'proposal_recording_id': row['proposal_recording_id'],
                'corrected_recording_id': row['corrected_recording_id'], 'corrected_title': row['corrected_title'],
                'corrected_artist': row['corrected_artist'], 'labeled_at': row['labeled_at'],
                'stale': current != row['proposal_recording_id']}

    def _proposals(self, db, *, status: str = '', skip=(), filters=None):
        """Unlabelled proposals in review order, as a lazy cursor so the first match ends the work."""
        flt = filters or QaFilter()
        if status and not flt.status:
            flt = replace(flt, status=status)
        clauses, values = ["m.data LIKE '%\"recording_id\"%'", 'l.path IS NULL'], []
        more, more_values = flt.sql()
        clauses += more
        values += more_values
        skipped = sorted({str(p).replace('\\', '/') for p in skip})
        if skipped:
            clauses.append("replace(m.path, '\\', '/') NOT IN (%s)" % ','.join('?' * len(skipped)))
            values.extend(skipped)
        where = ' AND '.join(clauses)
        cursor = db.execute(f'''SELECT m.path, m.recording_status, m.data, t.title, t.artist, t.album
                               FROM match_results m LEFT JOIN human_labels l ON l.path = m.path
                               LEFT JOIN tracks t ON t.path = m.path
                               WHERE {where} ORDER BY {PRIORITY}, m.path''', values)
        return (row for row in cursor if flt.accepts(row))

    def next_item(self, *, status: str = '', skip=(), filters=None) -> str | None:
        """The next proposal without a label, automatic approvals first, then review, then the rest.
        Skipped paths are passed over for this request only; skipping is not judging."""
        with self.database.read() as db:
            row = next(self._proposals(db, status=status, skip=skip, filters=filters), None)
        return row['path'] if row else None

    def progress(self) -> dict:
        with self.database.read() as db:
            proposals = db.execute("SELECT COUNT(*) FROM match_results WHERE data LIKE '%\"recording_id\"%'").fetchone()[0]
            labelled = db.execute('''SELECT l.label, COUNT(*) AS n FROM human_labels l
                                     JOIN match_results m ON m.path = l.path
                                     WHERE m.data LIKE '%\"recording_id\"%' GROUP BY l.label''').fetchall()
        counts = {row['label']: row['n'] for row in labelled}
        labelled_total = sum(counts.values())
        return {'proposals': proposals, 'labelled': labelled_total,
                'correct': counts.get('correct', 0), 'incorrect': counts.get('incorrect', 0),
                'unsure': counts.get('unsure', 0), 'remaining': max(0, proposals - labelled_total),
                'stale': sum(1 for path in self._labelled_paths() if (self.get_label(path) or {}).get('stale'))}

    def item(self, path: str) -> dict | None:
        """Everything needed to judge one proposal: local tags, candidates with evidence, and the current label."""
        with self.database.read() as db:
            row = db.execute("""SELECT t.title, t.artist, t.album, m.recording_status, m.data
                                FROM match_results m JOIN tracks t ON t.path = m.path WHERE m.path=?""", (path,)).fetchone()
        if row is None:
            return None
        data = json.loads(row['data'])
        candidates = []
        for c in data.get('candidates') or []:
            evidence = c.get('evidence') or {}
            candidates.append({'recording_id': c.get('recording_id', ''), 'title': c.get('title', ''),
                               'artist': c.get('artist', ''), 'provider_score': c.get('acoustid_score'),
                               'meloshelf_score': evidence.get('score'), 'duration': c.get('duration'),
                               'releases': [x.get('title', '') for x in (c.get('release_details') or [])][:5],
                               'notes': list(evidence.get('notes') or []),
                               'version_conflict': Evidence.from_stored(evidence).version_conflict,
                               'version': assess_version(row['title'] or '', c.get('title', ''),
                                                         duration_state=evidence.get('duration') or 'unknown',
                                                         release_titles=[x.get('title', '') for x in (c.get('release_details') or [])]).as_dict()})
        filename = path.replace('\\', '/').rsplit('/', 1)[-1]
        return {'path': path, 'filename': filename,
                'tags': {'title': row['title'], 'artist': row['artist'], 'album': row['album']},
                'status': row['recording_status'] or '', 'reason': data.get('reason', ''),
                'candidates': candidates, 'label': self.get_label(path)}

    def _labelled_paths(self) -> list[str]:
        with self.database.read() as db:
            return [r[0] for r in db.execute('SELECT path FROM human_labels')]

    def report(self, technical_failures: int = 0, submitted: int | None = None) -> dict:
        """Accuracy from human labels only. Proposals without a label do not count as correct."""
        rows = []
        with self.database.read() as db:
            joined = db.execute('''SELECT m.path, m.recording_status, m.data, l.label, l.corrected_recording_id,
                                   l.corrected_title, t.title, t.artist, t.album FROM match_results m
                                   LEFT JOIN human_labels l ON l.path = m.path
                                   LEFT JOIN tracks t ON t.path = m.path''').fetchall()
        proposals = 0
        for row in joined:
            data = json.loads(row['data'])
            if data.get('candidates'):
                proposals += 1
            if row['label'] is None:
                continue
            rows.append({'status': row['recording_status'] or '', 'label': row['label'],
                         'has_correction': bool(row['corrected_recording_id'] or row['corrected_title']),
                         'cause': (data.get('reason') or '') if row['label'] == 'incorrect' else '',
                         'category': row_category(row)})
        report = summarize(rows, proposals=proposals, technical_failures=technical_failures, submitted=submitted)
        report['by_category'] = by_category(rows)
        return report

    def export_rows(self) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('''SELECT t.path, t.title, t.artist, t.album, m.recording_status, m.data,
                                 l.label, l.corrected_recording_id, l.corrected_title, l.corrected_artist
                                 FROM match_results m JOIN tracks t ON t.path = m.path
                                 LEFT JOIN human_labels l ON l.path = m.path ORDER BY m.path''').fetchall()
        exported = []
        for r in rows:
            data = json.loads(r['data'])
            top = _top(data)
            evidence = top.get('evidence') or {}
            exported.append({
                'path': r['path'], 'local_title': r['title'], 'local_artist': r['artist'], 'local_album': r['album'],
                'proposed_recording_id': top.get('recording_id', ''), 'proposed_title': top.get('title', ''),
                'proposed_artist': top.get('artist', ''), 'meloshelf_status': r['recording_status'] or '',
                'provider_score': top.get('acoustid_score'), 'meloshelf_score': evidence.get('score'),
                'label': r['label'] or '', 'corrected_recording_id': r['corrected_recording_id'] or '',
                'corrected_title': r['corrected_title'] or '', 'corrected_artist': r['corrected_artist'] or '',
                'candidates': [{'recording_id': c.get('recording_id', ''), 'title': c.get('title', ''),
                                'artist': c.get('artist', ''), 'provider_score': c.get('acoustid_score')}
                               for c in data.get('candidates') or []],
            })
        return exported
