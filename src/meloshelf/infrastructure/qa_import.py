"""Import human reference labels from a CSV sheet (the MeloShelf export, or the filled-in accuracy worksheet).

Nothing is written by a preview. An apply writes new labels and only those existing labels whose path the person
names for overwriting, in one transaction; every replaced label goes to human_label_history. Rows are validated
with the same rules as a label given by hand, and a row that cannot be matched to exactly one analysed file is
rejected rather than guessed."""
import csv
import io
import json

from meloshelf.infrastructure.qa_store import QaError, _top, check_label, write_label

MAX_ROWS = 20_000
MAX_CHARS = 8_000_000
LISTED = 200            # rows of each kind returned for display; counts are always exact
PATH_COLUMNS = ('path', 'file')
EXTRA = ('corrected_recording_id', 'corrected_title', 'corrected_artist')
ALIASES = {'wrong': 'incorrect'}
FORMULA_PREFIX = ('=', '+', '-', '@', '\t', '\r')


def _key(path: str) -> str:
    return path.replace('\\', '/').casefold()


def _unneutralise(value: str) -> str:
    """The export puts an apostrophe before a cell a spreadsheet would run as a formula; take that one back off."""
    return value[1:] if value[:1] == "'" and value[1:2] in FORMULA_PREFIX else value


def _columns(header: list[str]) -> dict:
    names = [h.strip().lstrip('﻿').casefold() for h in header]
    found = {}
    for index, name in enumerate(names):
        if name in PATH_COLUMNS and 'path' not in found:
            found['path'] = index
        elif (name == 'label' or name.startswith('verdict')) and 'label' not in found:
            found['label'] = index
        elif name in EXTRA:
            found[name] = index
    if 'path' not in found:
        raise ValueError('The sheet needs a "path" (or "file") column.')
    if 'label' not in found:
        raise ValueError('The sheet needs a "label" (or "verdict") column.')
    return found


def _rows(text: str) -> tuple[dict, list[list[str]]]:
    if not isinstance(text, str):
        raise ValueError('The sheet must be text.')
    text = text.lstrip('﻿')
    if len(text) > MAX_CHARS:
        raise ValueError(f'The sheet is larger than {MAX_CHARS:,} characters.')
    if not text.strip():
        raise ValueError('The sheet is empty.')
    if '\x00' in text:
        raise ValueError('This looks like a binary file, not a CSV sheet.')
    first = text.splitlines()[0]
    delimiter = ';' if first.count(';') > first.count(',') else ','
    try:
        parsed = list(csv.reader(io.StringIO(text, newline=''), delimiter=delimiter))
    except csv.Error as exc:            # e.g. a field longer than csv's limit: refuse the sheet, never crash
        raise ValueError(f'The sheet could not be read: {exc}.') from None
    header, body = parsed[0], [r for r in parsed[1:] if any(cell.strip() for cell in r)]
    columns = _columns(header)
    if len(body) > MAX_ROWS:
        raise ValueError(f'The sheet has more than {MAX_ROWS:,} rows; split it.')
    return columns, body


class QaImporter:
    def __init__(self, database):
        self.database = database

    # ── planning ───────────────────────────────────────────────────────────────

    def plan(self, text: str) -> dict:
        public, _ = self._plan(text)
        return public

    def _plan(self, text: str):
        columns, body = _rows(text)
        counts = {'new': 0, 'unchanged': 0, 'conflicts': 0, 'rejected': 0, 'skipped': 0}
        rejected: list[dict] = []
        valid: list[dict] = []
        with self.database.read() as db:
            by_name: dict[str, list[str]] = {}
            exact: dict[str, list[str]] = {}
            for row in db.execute('SELECT path FROM match_results'):
                exact.setdefault(_key(row['path']), []).append(row['path'])
                by_name.setdefault(_key(row['path']).rsplit('/', 1)[-1], []).append(row['path'])
            # 1. each row on its own: shape, file, and the same rules as a label given by hand
            for number, cells in enumerate(body, start=2):
                def cell(name):
                    index = columns.get(name)
                    return _unneutralise(cells[index].strip()) if index is not None and index < len(cells) else ''
                given, label = cell('path'), cell('label').casefold()
                if not given:
                    counts['rejected'] += 1
                    rejected.append({'row': number, 'path': '', 'reason': 'No path on this row.'})
                    continue
                if not label:
                    counts['skipped'] += 1
                    continue
                label = ALIASES.get(label, label)
                path, problem = self._resolve(given, exact, by_name)
                if problem:
                    counts['rejected'] += 1
                    rejected.append({'row': number, 'path': given, 'reason': problem})
                    continue
                record = {'row': number, 'path': path, 'label': label,
                          'corrected_recording_id': cell('corrected_recording_id'),
                          'corrected_title': cell('corrected_title'), 'corrected_artist': cell('corrected_artist')}
                row = db.execute('SELECT data, recording_status FROM match_results WHERE path=?', (path,)).fetchone()
                data = json.loads(row['data'])
                try:
                    check_label(label, record['corrected_recording_id'], record['corrected_title'],
                                record['corrected_artist'], {c.get('recording_id') for c in data.get('candidates') or []})
                except QaError as exc:
                    counts['rejected'] += 1
                    rejected.append({'row': number, 'path': path, 'reason': str(exc)})
                    continue
                record['top'] = _top(data)
                record['status'] = row['recording_status'] or ''
                valid.append(record)
            # 2. several rows for one file: identical repeats count once, contradictions use none of them
            grouped: dict[str, list[dict]] = {}
            for record in valid:
                grouped.setdefault(record['path'], []).append(record)
            writes: list[dict] = []
            conflicts: list[dict] = []
            for path, records in grouped.items():
                if any(not self._same(records[0], other) for other in records[1:]):
                    counts['rejected'] += len(records)
                    rejected.extend({'row': r['row'], 'path': path,
                                     'reason': 'Rows for this file contradict each other; none was used.'} for r in records)
                    continue
                record = records[0]
                existing = db.execute('SELECT * FROM human_labels WHERE path=?', (path,)).fetchone()
                # 3. against the judgement already stored
                if existing is None:
                    counts['new'] += 1
                    writes.append(record)
                elif self._same_as_existing(existing, record):
                    counts['unchanged'] += 1
                else:
                    counts['conflicts'] += 1
                    conflicts.append({'row': record['row'], 'path': path, 'existing': self._shape(existing),
                                      'incoming': self._shape(record), '_record': record})
        rejected.sort(key=lambda r: r['row'])
        public = {'counts': counts, 'rows': len(body), 'rejected': rejected[:LISTED],
                  'conflicts': [{k: v for k, v in c.items() if k != '_record'} for c in conflicts[:LISTED]],
                  'new': [{'row': w['row'], 'path': w['path'], 'label': w['label']} for w in writes[:LISTED]],
                  'listed_limit': LISTED}
        return public, {'new': writes, 'conflicts': conflicts}

    @staticmethod
    def _resolve(given: str, exact: dict, by_name: dict):
        key = _key(given)
        if key in exact:
            hits = exact[key]
            return (hits[0], None) if len(hits) == 1 else (None, 'This path matches more than one analysed file.')
        hits = [p for p in by_name.get(key.rsplit('/', 1)[-1], []) if _key(p).endswith('/' + key.lstrip('/'))]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, 'This relative path matches more than one analysed file; use the full path.'
        return None, 'This file has not been analysed, so there is no proposal to judge.'

    @staticmethod
    def _same(a: dict, b: dict) -> bool:
        keys = ('label', 'corrected_recording_id', 'corrected_title', 'corrected_artist')
        return all(a[k] == b[k] for k in keys)

    @staticmethod
    def _same_as_existing(existing, record: dict) -> bool:
        return (existing['label'] == record['label'] and existing['corrected_recording_id'] == record['corrected_recording_id']
                and existing['corrected_title'] == record['corrected_title']
                and existing['corrected_artist'] == record['corrected_artist'])

    @staticmethod
    def _shape(source) -> dict:
        return {'label': source['label'], 'corrected_recording_id': source['corrected_recording_id'],
                'corrected_title': source['corrected_title'], 'corrected_artist': source['corrected_artist']}

    # ── applying ───────────────────────────────────────────────────────────────

    def apply(self, text: str, overwrite=()) -> dict:
        public, work = self._plan(text)
        named = set(overwrite)
        chosen = list(work['new']) + [c['_record'] for c in work['conflicts'] if c['path'] in named]
        kept = len(work['conflicts']) - sum(1 for c in work['conflicts'] if c['path'] in named)
        with self.database.write() as db:
            for record in chosen:
                self._write_row(db, record)
        counts = public['counts']
        return {'written': len(chosen), 'kept_existing': kept, 'unchanged': counts['unchanged'],
                'rejected': counts['rejected'], 'skipped': counts['skipped']}

    @staticmethod
    def _write_row(db, record: dict) -> None:
        write_label(db, record['path'], record['label'], top=record['top'], status=record['status'],
                    corrected_recording_id=record['corrected_recording_id'], corrected_title=record['corrected_title'],
                    corrected_artist=record['corrected_artist'], source='csv-import')
