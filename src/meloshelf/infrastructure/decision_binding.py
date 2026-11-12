"""Keeps human recognition decisions attached to the right physical file after a scan.

A decision moves with its file instance only when that instance is present at exactly one new path, with the same
size and modification time as the version the decision was made for, and no result already exists there. In every
other case the decision is left in place and counted for review: nothing is guessed. Decisions whose file changed in
place are archived to the append-only history, never applied to the new content.
"""
import time

HUMAN = ('accepted', 'rejected')
_COPY = ('path', 'file_size', 'file_mtime_ns', 'status', 'data', 'created_at', 'decision', 'recording_status',
         'selected_recording_id', 'selected_release_id', 'instance_id')


def archive_decision(db, row, reason: str) -> None:
    db.execute("""INSERT INTO match_decision_history(path,instance_id,decision,recording_id,recording_status,file_size,
                  file_mtime_ns,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
               (row['path'], row['instance_id'], row['decision'], row['selected_recording_id'], row['recording_status'],
                row['file_size'], row['file_mtime_ns'], reason, int(time.time())))


def bind_after_scan(database) -> dict:
    """Runs in one write transaction. Returns the counts the scan reports."""
    transferred = archived = 0
    with database.write() as db:
        # 1. A decision whose indexed file changed in place is archived; the new content starts without it.
        changed = db.execute(f"""SELECT m.* FROM match_results m JOIN tracks t ON t.path=m.path
                                 WHERE m.decision IN ({','.join('?' * len(HUMAN))})
                                   AND (m.file_size<>t.size OR m.file_mtime_ns<>t.mtime_ns)""", HUMAN).fetchall()
        for row in changed:
            archive_decision(db, row, 'file changed')
            db.execute('DELETE FROM match_results WHERE path=?', (row['path'],))
            archived += 1
        # 2. A decision whose path is no longer indexed follows its instance to the one new path that matches it.
        orphans = db.execute(f"""SELECT m.* FROM match_results m
                                 WHERE m.decision IN ({','.join('?' * len(HUMAN))})
                                   AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.path=m.path)""", HUMAN).fetchall()
        for row in orphans:
            if not row['instance_id']:
                continue                                        # path-keyed and never bound: kept for review
            homes = db.execute("""SELECT f.path FROM file_instances f JOIN tracks t ON t.path=f.path
                                  WHERE f.instance_id=? AND f.state='present' AND t.size=? AND t.mtime_ns=?
                                    AND NOT EXISTS (SELECT 1 FROM match_results m WHERE m.path=f.path)""",
                               (row['instance_id'], row['file_size'], row['file_mtime_ns'])).fetchall()
            if len(homes) != 1:
                continue                                        # gone or ambiguous: kept for review, never transferred
            new_path = homes[0]['path']
            columns = ','.join(_COPY)
            values = ','.join('?' if column != 'path' else '?' for column in _COPY)
            copied = [new_path if column == 'path' else row[column] for column in _COPY]
            db.execute(f'INSERT INTO match_results({columns}) VALUES({values})', copied)
            db.execute('DELETE FROM match_results WHERE path=?', (row['path'],))
            archive_decision(db, row, f'moved to {new_path}')
            transferred += 1
        remaining = db.execute(f"""SELECT COUNT(*) FROM match_results m
                                   WHERE m.decision IN ({','.join('?' * len(HUMAN))})
                                     AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.path=m.path)""", HUMAN).fetchone()[0]
    return {'transferred': transferred, 'archived': archived, 'awaiting_review': remaining}
