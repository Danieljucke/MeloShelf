"""Stable local file-instance identity.

A file instance is one physical file, named by a local random id. It is not the path (paths change) and it is not a
MusicBrainz ID (that names a recording, not a file). Evidence is cheap: the size and mtime already recorded by a scan,
plus a sampled fingerprint (size + the first and last SAMPLE bytes) read at most once per new or changed file, with a
per-run cap so a first scan of a large library is spread over several runs.

Reconciliation is conservative:
* same path, same size and mtime -> the same instance, nothing is read;
* same path, new mtime, same size and same fingerprint -> the same instance;
* same path, different content -> a new instance; the old one becomes 'superseded' and keeps its history;
* a new path that matches exactly one missing instance (same size and fingerprint, and no other new path claims it)
  -> that instance has moved or been renamed;
* any other match, including duplicates, is ambiguous -> a new instance, and nothing is transferred.
Duplicate copies are always separate instances. The registry only reads files and writes its own tables.
"""
import hashlib
import time
import uuid

SAMPLE = 65536


class FileRegistry:
    def __init__(self, database, *, opener=open, clock=time.time, sample=SAMPLE, max_fingerprints_per_run=2000):
        self.database = database
        self.opener = opener
        self.clock = clock
        self.sample = sample
        self.max_fp = max_fingerprints_per_run

    # ── reading ──────────────────────────────────────────────────────────────────

    def instance_ids_by_path(self) -> dict:
        with self.database.read() as db:
            rows = db.execute("SELECT path, instance_id FROM file_instances WHERE state='present'").fetchall()
        return {r['path']: r['instance_id'] for r in rows}

    def get(self, instance_id: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute('SELECT * FROM file_instances WHERE instance_id=?', (instance_id,)).fetchone()
        return dict(row) if row else None

    def history(self, instance_id: str) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM file_instance_history WHERE instance_id=? ORDER BY id',
                              (instance_id,)).fetchall()
        return [dict(r) for r in rows]

    def duplicate_groups(self) -> list[list[str]]:
        """Present instances that share size and fingerprint. They stay separate instances."""
        with self.database.read() as db:
            rows = db.execute("""SELECT size, fingerprint, instance_id FROM file_instances
                                 WHERE state='present' AND fingerprint IS NOT NULL ORDER BY size, fingerprint""").fetchall()
        groups = {}
        for row in rows:
            groups.setdefault((row['size'], row['fingerprint']), []).append(row['instance_id'])
        return [ids for ids in groups.values() if len(ids) > 1]

    def pending_fingerprints(self) -> int:
        with self.database.read() as db:
            return db.execute("SELECT COUNT(*) FROM file_instances WHERE state='present' AND fingerprint IS NULL").fetchone()[0]

    # ── evidence ─────────────────────────────────────────────────────────────────

    def _fingerprint(self, path: str, size: int) -> str | None:
        """Size plus the first and last sample bytes. Read-only; None if the file cannot be read."""
        try:
            with self.opener(path, 'rb') as handle:
                head = handle.read(self.sample)
                tail = b''
                if size > 2 * self.sample:
                    handle.seek(size - self.sample)
                    tail = handle.read(self.sample)
        except OSError:
            return None
        digest = hashlib.sha256()
        digest.update(str(size).encode())
        digest.update(head)
        digest.update(tail)
        return digest.hexdigest()

    # ── reconciliation ───────────────────────────────────────────────────────────

    def reconcile(self, only: list[str] | None = None) -> dict:
        """Bring the instances in line with the tracks table. `only` limits the work to those paths (no missing-file
        marking, no fingerprint backfill): used when a decision needs instances for the files it names."""
        now = self.clock()
        with self.database.read() as db:
            if only is None:
                tracks = {r['path']: (r['size'], r['mtime_ns']) for r in db.execute('SELECT path,size,mtime_ns FROM tracks')}
            else:
                tracks = {}
                for path in only:
                    row = db.execute('SELECT path,size,mtime_ns FROM tracks WHERE path=?', (path,)).fetchone()
                    if row:
                        tracks[row['path']] = (row['size'], row['mtime_ns'])
            present = {r['path']: dict(r) for r in db.execute("SELECT * FROM file_instances WHERE state='present'")}
            missing = [dict(r) for r in db.execute("SELECT * FROM file_instances WHERE state='missing'")]
        summary = {'new': 0, 'moved': 0, 'changed': 0, 'missing': 0, 'ambiguous': 0, 'unchanged': 0}
        budget = [self.max_fp]
        updates, inserts, history = [], [], []   # collected, then applied in one transaction

        def fingerprint(path, size, *, forced=False):
            if not forced and budget[0] <= 0:
                return None
            if not forced:
                budget[0] -= 1
            return self._fingerprint(path, size)

        new_paths = []
        for path, (size, mtime) in tracks.items():
            inst = present.get(path)
            if inst is None:
                new_paths.append(path)
                continue
            if inst['size'] == size and inst['mtime_ns'] == mtime:
                summary['unchanged'] += 1
                continue
            if inst['size'] == size and inst['fingerprint'] is not None:
                fp = fingerprint(path, size, forced=True)
                if fp is not None and fp == inst['fingerprint']:
                    updates.append(('touch', inst['instance_id'], path, size, mtime, fp))
                    continue
            # different content at a path we already knew: the old file is superseded, this is a new physical file
            updates.append(('supersede', inst['instance_id']))
            history.append((inst['instance_id'], path, 'content changed at this path; superseded'))
            summary['changed'] += 1
            new_paths.append(path)

        if only is None:
            for path, inst in present.items():
                if path not in tracks:
                    updates.append(('missing', inst['instance_id']))
                    missing.append(dict(inst, state='missing'))
                    summary['missing'] += 1

        # evidence for the new paths: a fingerprint, needed for relocation matching and for the new instance itself
        fps = {}
        missing_sizes = {m['size'] for m in missing}
        for path in new_paths:
            size = tracks[path][0]
            fps[path] = fingerprint(path, size, forced=size in missing_sizes)
        new_by_key, cand_by_key = {}, {}
        for path in new_paths:
            if fps[path] is not None:
                new_by_key.setdefault((tracks[path][0], fps[path]), []).append(path)
        for m in missing:
            if m['fingerprint'] is not None:
                cand_by_key.setdefault((m['size'], m['fingerprint']), []).append(m)
        weak = {}
        for m in missing:
            if m['fingerprint'] is None:
                weak.setdefault((m['size'], m['mtime_ns']), []).append(m)
        claimed = set()
        for path in new_paths:
            size, mtime = tracks[path]
            fp = fps[path]
            target, reason = None, ''
            if fp is not None and (size, fp) in cand_by_key:
                cands, sames = cand_by_key[(size, fp)], new_by_key[(size, fp)]
                if len(cands) == 1 and len(sames) == 1 and cands[0]['instance_id'] not in claimed:
                    target, reason = cands[0], 'moved or renamed: same size and fingerprint'
                else:
                    summary['ambiguous'] += 1
            elif len(weak.get((size, mtime), [])) == 1 and weak[(size, mtime)][0]['instance_id'] not in claimed:
                target, reason = weak[(size, mtime)][0], 'moved or renamed: same size and modification time'
            if target is not None:
                claimed.add(target['instance_id'])
                updates.append(('move', target['instance_id'], path, size, mtime, fp or target['fingerprint']))
                history.append((target['instance_id'], path, reason))
                summary['moved'] += 1
            else:
                new_id = uuid.uuid4().hex
                inserts.append((new_id, path, size, mtime, fp))
                history.append((new_id, path, 'first seen'))
                summary['new'] += 1

        backfill = []
        if only is None:
            for path, inst in present.items():
                if inst['fingerprint'] is None and path in tracks and tracks[path] == (inst['size'], inst['mtime_ns']):
                    fp = fingerprint(path, inst['size'])
                    if fp is not None:
                        backfill.append((fp, inst['instance_id']))

        with self.database.write() as db:
            for item in updates:
                kind = item[0]
                if kind == 'touch':
                    _, instance_id, path, size, mtime, fp = item
                    db.execute('UPDATE file_instances SET mtime_ns=?, fingerprint=?, last_seen=? WHERE instance_id=?',
                               (mtime, fp, now, instance_id))
                elif kind == 'supersede':
                    db.execute("UPDATE file_instances SET state='superseded', last_seen=? WHERE instance_id=?",
                               (now, item[1]))
                elif kind == 'missing':
                    db.execute("UPDATE file_instances SET state='missing', last_seen=? WHERE instance_id=?", (now, item[1]))
            for item in updates:
                if item[0] == 'move':
                    _, instance_id, path, size, mtime, fp = item
                    db.execute("""UPDATE file_instances SET path=?, size=?, mtime_ns=?, fingerprint=?, state='present',
                                  last_seen=? WHERE instance_id=?""", (path, size, mtime, fp, now, instance_id))
            for instance_id, path, size, mtime, fp in inserts:
                db.execute('''INSERT INTO file_instances(instance_id,path,size,mtime_ns,fingerprint,state,first_seen,last_seen)
                              VALUES(?,?,?,?,?,'present',?,?)''', (instance_id, path, size, mtime, fp, now, now))
            for fp, instance_id in backfill:
                db.execute('UPDATE file_instances SET fingerprint=?, last_seen=? WHERE instance_id=?', (fp, now, instance_id))
            db.executemany('INSERT INTO file_instance_history(instance_id,path,at,reason) VALUES(?,?,?,?)',
                           [(i, p, now, r) for i, p, r in history])
        return summary
