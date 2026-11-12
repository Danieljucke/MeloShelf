"""Bulk human-reference judgement.

The person picks a bounded set of unlabelled proposals and gives them one label. Safeguards, because a bulk label
feeds the accuracy figures: it never replaces an existing label; "correct" is refused for any proposal with a
possible or confirmed version conflict, and for files with no proposal; the selection is capped; the write is one
transaction; the whole batch can be undone, keeping any label the person changed afterwards."""
import json
import time
from dataclasses import replace

from meloshelf.infrastructure.qa_filters import QaFilter, row_category
from meloshelf.infrastructure.qa_store import LABELS, PRIORITY, QaError, QaStore, _top, write_label

MAX_SELECTION = 200
MAX_PAGE = 100
CONFLICT_VERDICTS = ('possible_conflict', 'confirmed_conflict')


def _like(text: str) -> str:
    return '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


class QaBulk:
    def __init__(self, database):
        self.database = database
        self.qa = QaStore(database)

    # ── what can be chosen ─────────────────────────────────────────────────────

    def candidates(self, *, status: str = '', query: str = '', limit: int = 50, offset: int = 0, filters=None) -> dict:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be at least 1')
        limit, offset = min(limit, MAX_PAGE), max(0, int(offset))
        flt = filters or QaFilter()
        if status and not flt.status:
            flt = replace(flt, status=status)
        clauses = ["m.data LIKE '%\"recording_id\"%'", 'l.path IS NULL']
        values: list = []
        more, more_values = flt.sql()
        clauses += more
        values += more_values
        if query.strip():
            clauses.append("(t.title LIKE ? ESCAPE '\\' OR t.artist LIKE ? ESCAPE '\\' OR t.album LIKE ? ESCAPE '\\' "
                           "OR m.path LIKE ? ESCAPE '\\')")
            values.extend([_like(query.strip()[:200])] * 4)
        where = ' AND '.join(clauses)
        source = '''FROM match_results m LEFT JOIN tracks t ON t.path = m.path
                    LEFT JOIN human_labels l ON l.path = m.path'''
        columns = 'm.path, m.recording_status, m.data, t.title, t.artist, t.album'
        with self.database.read() as db:
            if flt.category:        # the category is derived in Python, so the filtered set is walked once
                matching = [r for r in db.execute(f'SELECT {columns} {source} WHERE {where} ORDER BY {PRIORITY}, m.path', values)
                            if flt.accepts(r)]
                total, rows = len(matching), matching[offset:offset + limit]
            else:
                total = db.execute(f'SELECT COUNT(*) {source} WHERE {where}', values).fetchone()[0]
                rows = db.execute(f'SELECT {columns} {source} WHERE {where} ORDER BY {PRIORITY}, m.path LIMIT ? OFFSET ?',
                                  values + [limit, offset]).fetchall()
        items = []
        for row in rows:
            top = _top(json.loads(row['data']))
            evidence = top.get('evidence') or {}
            items.append({'path': row['path'], 'filename': row['path'].replace('\\', '/').rsplit('/', 1)[-1],
                          'status': row['recording_status'] or '',
                          'tags': {'title': row['title'], 'artist': row['artist']},
                          'proposed': {'title': top.get('title', ''), 'artist': top.get('artist', '')},
                          'provider_score': top.get('acoustid_score'), 'meloshelf_score': evidence.get('score'),
                          'version_conflict': bool(evidence.get('version_conflict')), 'category': row_category(row)})
        return {'total': total, 'items': items, 'limit': limit, 'offset': offset}

    # ── preview / apply ────────────────────────────────────────────────────────

    @staticmethod
    def _clean(paths) -> list[str]:
        if not isinstance(paths, (list, tuple)) or not paths or not all(isinstance(p, str) and p for p in paths):
            raise ValueError('Choose at least one file.')
        unique = list(dict.fromkeys(paths))
        if len(unique) > MAX_SELECTION:
            raise ValueError(f'Choose at most {MAX_SELECTION} files at a time.')
        return unique

    def _judge(self, paths: list[str], label: str):
        if not isinstance(label, str) or label not in LABELS:
            raise QaError('Choose correct, incorrect or unsure.')
        eligible, ineligible = [], []
        for path in paths:
            item = self.qa.item(path)
            if item is None:
                ineligible.append({'path': path, 'reason': 'This file has not been analysed, so there is no proposal to judge.'})
            elif item['label'] is not None:
                ineligible.append({'path': path, 'reason': 'This file already has your label; bulk never replaces one.'})
            elif not item['candidates']:
                ineligible.append({'path': path, 'reason': 'There is no proposal to judge.'})
            elif label == 'correct' and (item['candidates'][0]['version_conflict']
                                         or item['candidates'][0]['version']['verdict'] in CONFLICT_VERDICTS):
                ineligible.append({'path': path, 'reason': 'The version may differ from the file: judge it on its own.'})
            else:
                eligible.append(path)
        return eligible, ineligible

    def preview(self, paths, label) -> dict:
        eligible, ineligible = self._judge(self._clean(paths), label)
        return {'label': label, 'eligible': eligible, 'ineligible': ineligible,
                'counts': {'eligible': len(eligible), 'ineligible': len(ineligible)}}

    def apply(self, paths, label) -> dict:
        eligible, ineligible = self._judge(self._clean(paths), label)
        if not eligible:
            return {'written': 0, 'skipped': len(ineligible), 'batch_id': None, 'ineligible': ineligible}
        with self.database.write() as db:
            batch = db.execute('INSERT INTO human_label_batches(label,created_at) VALUES(?,?)', (label, time.time())).lastrowid
            for path in eligible:
                row = db.execute('SELECT data, recording_status FROM match_results WHERE path=?', (path,)).fetchone()
                at = write_label(db, path, label, top=_top(json.loads(row['data'])), status=row['recording_status'] or '',
                                 source='bulk')
                db.execute('INSERT INTO human_label_batch_items(batch_id,path,labeled_at) VALUES(?,?,?)', (batch, path, at))
        return {'written': len(eligible), 'skipped': len(ineligible), 'batch_id': batch, 'ineligible': ineligible}

    # ── batches ────────────────────────────────────────────────────────────────

    def batches(self, limit: int = 20) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('''SELECT b.id, b.label, b.created_at, b.undone_at,
                                 (SELECT COUNT(*) FROM human_label_batch_items i WHERE i.batch_id = b.id) AS n
                                 FROM human_label_batches b ORDER BY b.id DESC LIMIT ?''', (limit,)).fetchall()
        return [{'id': r['id'], 'label': r['label'], 'count': r['n'], 'created_at': r['created_at'],
                 'undone': r['undone_at'] is not None} for r in rows]

    def undo(self, batch_id) -> dict:
        if type(batch_id) is not int:
            raise QaError('Unknown batch.')
        with self.database.write() as db:
            batch = db.execute('SELECT undone_at FROM human_label_batches WHERE id=?', (batch_id,)).fetchone()
            if batch is None:
                raise QaError('Unknown batch.')
            if batch['undone_at'] is not None:
                raise QaError('This batch was already undone.')
            removed = kept = 0
            for item in db.execute('SELECT path, labeled_at FROM human_label_batch_items WHERE batch_id=?', (batch_id,)).fetchall():
                cursor = db.execute('DELETE FROM human_labels WHERE path=? AND labeled_at=?', (item['path'], item['labeled_at']))
                removed, kept = removed + cursor.rowcount, kept + (1 - cursor.rowcount)
            db.execute('UPDATE human_label_batches SET undone_at=? WHERE id=?', (time.time(), batch_id))
        return {'removed': removed, 'kept': kept}
