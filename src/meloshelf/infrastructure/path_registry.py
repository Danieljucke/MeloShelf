"""Keeps everything MeloShelf knows about a file attached to it when the file moves.

A decision, a recognition result, a human label or a manual identity belongs to a physical file, not to a path. When the
organizer moves a file, this re-keys each path-keyed record in ONE transaction so nothing is lost and the file keeps its
instance id. It only ever writes MeloShelf's own tables, never a music file.
"""
import json
import time

_KEYED = ('match_results', 'fallback_results', 'human_labels', 'human_version_labels', 'manual_identities')


def _swap_in_json(connection, table: str, column: str, old: str, new: str) -> None:
    rows = connection.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} LIKE ?", (f'%{old}%',)).fetchall()
    for row in rows:
        try:
            values = json.loads(row[column] or '[]')
        except ValueError:
            continue
        if isinstance(values, list) and old in values:
            connection.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                               (json.dumps([new if v == old else v for v in values], ensure_ascii=False), row['rowid']))


def rekey_path(database, old: str, new: str, *, size: int, mtime_ns: int, tags: dict | None = None) -> None:
    """Idempotent: running it twice, or after the file already moved, changes nothing further."""
    with database.write() as db:
        if old != new:
            db.execute('DELETE FROM tracks WHERE path=?', (new,))                  # a stale row for the place it now fills
        db.execute('UPDATE tracks SET path=?, size=?, mtime_ns=? WHERE path=?', (new, size, mtime_ns, old))
        for field in ('title', 'artist', 'album'):
            if tags and tags.get(field):
                db.execute(f'UPDATE tracks SET {field}=? WHERE path=?', (tags[field], new))
        for table in _KEYED:
            if old != new:
                db.execute(f'DELETE FROM {table} WHERE path=?', (new,))
            db.execute(f'UPDATE {table} SET path=? WHERE path=?', (new, old))
        db.execute('UPDATE match_results SET file_size=?, file_mtime_ns=? WHERE path=?', (size, mtime_ns, new))
        db.execute('UPDATE manual_identities SET size=?, mtime_ns=? WHERE path=?', (size, mtime_ns, new))
        instance = db.execute("SELECT instance_id FROM file_instances WHERE path=? AND state='present'", (old,)).fetchone()
        if old != new:
            if db.execute("SELECT 1 FROM file_instances WHERE path=? AND state='present'", (new,)).fetchone():
                db.execute("DELETE FROM file_instances WHERE path=? AND state='present'", (new,))
            db.execute("""UPDATE file_instances SET path=?, size=?, mtime_ns=?, fingerprint=NULL, last_seen=?
                          WHERE path=? AND state='present'""", (new, size, mtime_ns, time.time(), old))
            if instance:
                db.execute('INSERT INTO file_instance_history(instance_id,path,at,reason) VALUES(?,?,?,?)',
                           (instance['instance_id'], new, time.time(), 'moved by the organizer'))
        else:
            db.execute("UPDATE file_instances SET size=?, mtime_ns=?, fingerprint=NULL WHERE path=? AND state='present'",
                       (size, mtime_ns, new))
        if old != new:
            _swap_in_json(db, 'metadata_decisions', 'paths', old, new)
            _swap_in_json(db, 'artist_link_decisions', 'tracks', old, new)
