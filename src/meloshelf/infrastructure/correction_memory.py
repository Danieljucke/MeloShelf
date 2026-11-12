"""Correction memory: an append-only, scope-aware log of corrections.

* confirm and revert are human actions only. An analysis run cannot confirm anything.
* conflict is raised by analysis when the current proposal no longer matches a confirmed correction. It does
  not apply the old value and does not change the human's history.
* A confirmed correction is offered as a suggestion only inside its own folder, field and original value.
  Suggestions are annotated for individual review; they never become evidence for automatic decisions.
"""
import time

EVENTS = ('confirm', 'revert', 'conflict')
SOURCES = ('human', 'analysis')


class MemoryError(ValueError):
    """An event was refused; the message is safe to show to the user."""


class CorrectionMemory:
    def __init__(self, database):
        self.database = database

    def append(self, group_key: str, event: str, *, source: str, folder: str, field: str, original: str,
               corrected: str = '', reason: str = '') -> None:
        if source not in SOURCES:
            raise MemoryError('Only a person or the analysis can record a correction event.')
        if event not in EVENTS:
            raise MemoryError('Unknown correction event.')
        if event in ('confirm', 'revert') and source != 'human':
            raise MemoryError('Only a person can confirm or reverse a correction.')
        if event == 'conflict' and source != 'analysis':
            raise MemoryError('Conflicts are raised by the analysis.')
        with self.database.write() as db:
            db.execute('''INSERT INTO correction_events(group_key,folder,field,original,corrected,event,source,reason,at)
                          VALUES(?,?,?,?,?,?,?,?,?)''',
                       (group_key, folder.replace('\\', '/'), field, original, corrected, event, source, reason, time.time()))

    def history(self, group_key: str) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM correction_events WHERE group_key=? ORDER BY id', (group_key,)).fetchall()
        return [dict(r) for r in rows]

    def _latest_by_group(self, where: str, values) -> dict:
        with self.database.read() as db:
            rows = db.execute(f'SELECT * FROM correction_events WHERE {where} ORDER BY id', values).fetchall()
        latest = {}
        for row in rows:
            latest[row['group_key']] = dict(row)
        return latest

    def active_for(self, folder: str, field: str, original: str) -> dict | None:
        """The confirmed correction in this exact scope, if no later reversal or conflict supersedes it."""
        latest = self._latest_by_group('folder=? AND field=? AND original=?',
                                       (folder.replace('\\', '/'), field, original))
        confirmed = [e for e in latest.values() if e['event'] == 'confirm']
        return confirmed[-1] if confirmed else None

    def reconcile(self, groups) -> int:
        """Raise a conflict where a confirmed correction no longer matches the current proposal."""
        raised = 0
        for group in groups:
            latest = self._latest_by_group('group_key=?', (group['key'],)).get(group['key'])
            if not latest or latest['event'] != 'confirm':
                continue
            if group['proposed'] == latest['corrected']:
                continue
            self.append(group['key'], 'conflict', source='analysis', folder=group['folder'], field=group['field'],
                        original=group['original'], corrected=group['proposed'],
                        reason=f'confirmed as "{latest["corrected"]}"; the analysis now proposes "{group["proposed"]}"')
            raised += 1
        return raised

    def annotate(self, groups) -> list[dict]:
        """Attach the confirmed correction for this scope, if any. Such groups stay for individual review."""
        out = []
        for group in groups:
            item = dict(group)
            confirmed = self.active_for(group['folder'], group['field'], group['original'])
            if confirmed:
                item['previously_confirmed'] = confirmed['corrected']
                item['individual_review'] = True  # a remembered suggestion is never batch-approved
            out.append(item)
        return out
