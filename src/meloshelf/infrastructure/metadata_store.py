"""Persisted decisions about metadata proposals.

A decision is scoped to one folder, one field and one original value, so it cannot propagate to unrelated
artists or albums. Each decision keeps its history. A later analysis that changes the proposal marks the
decision stale; it never overwrites the human's status. Storing an approval records the proposal only; no
tag is written and no audio file is touched.
"""
import hashlib
import json
import time

ACTIONS = ('approve', 'reject', 'revert')


class MetadataError(ValueError):
    """A decision was refused; the message is safe to show to the user."""


class MetadataStore:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def key_for(folder: str, field: str, original: str, classification: str) -> str:
        scope = json.dumps([folder.replace('\\', '/'), field, original, classification], ensure_ascii=False)
        return hashlib.sha256(scope.encode('utf-8')).hexdigest()

    def decide(self, key: str, action: str, *, folder: str, field: str, original: str, proposed: str,
               classification: str, paths) -> None:
        if action not in ACTIONS:
            raise MetadataError('Choose approve, reject or revert.')
        if action == 'approve' and not proposed:
            raise MetadataError('There is no single replacement to approve. Review the tracks individually.')
        with self.database.write() as db:
            row = db.execute('SELECT status, history FROM metadata_decisions WHERE key=?', (key,)).fetchone()
            history = json.loads(row['history']) if row else []
            history.append({'action': action, 'at': time.time()})
            status = {'approve': 'approved', 'reject': 'rejected', 'revert': 'pending'}[action]
            db.execute('''INSERT INTO metadata_decisions(key,folder,field,original,proposed,current_proposed,
                          classification,paths,status,history,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(key) DO UPDATE SET proposed=excluded.proposed, current_proposed=excluded.current_proposed,
                          paths=excluded.paths, status=excluded.status, history=excluded.history, decided_at=excluded.decided_at''',
                       (key, folder.replace('\\', '/'), field, original, proposed, proposed, classification,
                        json.dumps(list(paths), ensure_ascii=False), status, json.dumps(history), time.time()))
        # Every human decision is also an event in the correction log, so the full history survives later changes.
        from meloshelf.infrastructure.correction_memory import CorrectionMemory
        memory = CorrectionMemory(self.database)
        if action == 'approve':
            memory.append(key, 'confirm', source='human', folder=folder, field=field, original=original, corrected=proposed)
        else:
            memory.append(key, 'revert', source='human', folder=folder, field=field, original=original,
                          reason='reverted to pending' if action == 'revert' else 'rejected')

    def get(self, key: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute('SELECT * FROM metadata_decisions WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        return {'key': row['key'], 'folder': row['folder'], 'field': row['field'], 'original': row['original'],
                'proposed': row['proposed'], 'current_proposed': row['current_proposed'],
                'classification': row['classification'], 'paths': json.loads(row['paths']),
                'status': row['status'], 'history': json.loads(row['history']),
                'stale': row['current_proposed'] != row['proposed']}

    def list_all(self, current_groups) -> list[dict]:
        """Every stored decision, with staleness judged against the current proposals (nothing is written)."""
        current = {g['key']: g['proposed'] for g in current_groups}
        with self.database.read() as db:
            keys = [r['key'] for r in db.execute('SELECT key FROM metadata_decisions ORDER BY decided_at DESC')]
        out = []
        for key in keys:
            stored = self.get(key)
            if key in current:
                stored['current_proposed'] = current[key]
                stored['stale'] = current[key] != stored['proposed']
            else:
                stored['stale'] = True  # the group no longer appears in the library
            out.append(stored)
        return out

    def reconcile(self, groups) -> int:
        """Record the proposals the latest analysis produced. Statuses are untouched; changes are flagged."""
        flagged = 0
        with self.database.write() as db:
            for group in groups:
                row = db.execute('SELECT proposed FROM metadata_decisions WHERE key=?', (group['key'],)).fetchone()
                if row is None:
                    continue
                db.execute('UPDATE metadata_decisions SET current_proposed=? WHERE key=?',
                           (group['proposed'], group['key']))
                if group['proposed'] != row['proposed']:
                    flagged += 1
        return flagged
