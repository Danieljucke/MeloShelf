"""The organizer engine: puts music into its classified folders without ever endangering a file.

Two modes. ``move`` is what MeloShelf does: the file ends up at its destination and its original location is emptied,
but only after the destination is published and verified; a crash at any step leaves the file recoverable. ``copy`` keeps
the source (used by tests and for dry experiments). Guarantees (each one is a test):
- In copy mode the source is only ever opened for reading. Its content and modification times are unchanged.
- In move mode the original is removed only after the destination is verified and durably published, never merely
  because a destination path exists; if anything cannot be verified the original is kept and the journal says why.
- A file is staged beside its final name, verified byte for byte against the source, tagged (the copy only), and
  only then published with a rename. Nothing is overwritten: an existing name gets a numbered sibling.
- Every step is recorded in a durable journal (organize_runs / organize_items). A crash at any point resumes without
  duplicates and without losing a file; pause and cancel stop between files and leave nothing half written.
- Running the same organization again skips what is already in place and changes nothing.
"""
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

TEMP_MARK = '.meloshelf-partial'
CHUNK = 1024 * 1024
SPACE_MARGIN = 64 * 1024 * 1024
DISK_FULL = 28


class DestinationError(ValueError):
    """The destination cannot be used. ``code`` is stable; the page shows a translated sentence for it."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class WorkItem:
    source: str
    relative: str                       # '/'-separated path under the destination
    kind: str = 'organize'              # 'organize' | 'review'
    tags: dict = field(default_factory=dict)
    cover: str = ''
    reason: str = ''


def copy_stream(source, target) -> None:
    shutil.copyfileobj(source, target, CHUNK)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def stray_temp_files(destination) -> list[str]:
    root = Path(destination)
    return [str(p) for p in root.rglob('*') if TEMP_MARK in p.name] if root.exists() else []


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _is_inside(child: str, parent: str) -> bool:
    return child != parent and child.startswith(parent.rstrip(os.sep) + os.sep)


def check_destination(source_root: str, dest_root: str) -> None:
    """Refuse a destination that could re-index or overwrite the source."""
    source, dest = _norm(source_root), _norm(dest_root)
    if source == dest:
        raise DestinationError('destination_is_source')
    if _is_inside(dest, source):
        raise DestinationError('destination_inside_source')
    if _is_inside(source, dest):
        raise DestinationError('source_inside_destination')
    if os.path.exists(dest_root) and not os.path.isdir(dest_root):
        raise DestinationError('destination_unusable')


def _nearest_existing(path: str) -> str:
    current = os.path.abspath(path)
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def _default_same_volume(source: str, destination_folder: str) -> bool:
    try:
        return os.stat(source).st_dev == os.stat(_nearest_existing(destination_folder)).st_dev
    except OSError:
        return False


class Organizer:
    def __init__(self, database, *, tagger=None, free_space=None, hooks=None, clock=time.time, mode='copy', same_volume=None):
        self.database = database
        self.mode = mode                             # 'move' | 'copy'
        self.same_volume = same_volume or _default_same_volume
        self.tagger = tagger                         # tagger(copy_path, tags, cover) -> bool; sees the copy only
        self.free_space = free_space or (lambda path: shutil.disk_usage(path).free)
        self.hooks = hooks or {}                     # named points where tests simulate a crash
        self.clock = clock

    def _hook(self, name: str, **info) -> None:
        if name in self.hooks:
            self.hooks[name](**info)

    # ── creating a run ───────────────────────────────────────────────────────────

    def embed_cover(self, dest_root: str, source_path: str, cover_path: str) -> bool:
        """Add a cover to the already-organized COPY of ``source_path`` when it has none. The change is made on a temporary
        copy and swapped in atomically, then the journal is updated so the copy still counts as organized. The source is
        never opened for writing. False when nothing was changed."""
        from meloshelf.infrastructure.tags import has_picture_frame
        if self.tagger is None or not cover_path or not os.path.isfile(cover_path):
            return False
        with self.database.read() as db:
            row = db.execute("""SELECT i.run_id, i.seq, i.relative FROM organize_items i JOIN organize_runs r ON r.id=i.run_id
                                WHERE r.dest_root=? AND i.source_path=? AND i.state IN ('done','skipped')
                                ORDER BY r.created_at DESC, i.seq DESC LIMIT 1""", (str(dest_root), source_path)).fetchone()
        if row is None:
            return False
        dest = Path(dest_root)
        final, temp = self._final_for(dest, row['relative']), self._temp_for(dest, row['relative'])
        if not final.is_file() or has_picture_frame(final):
            return False
        self._discard_temp(temp)
        try:
            shutil.copyfile(final, temp)
            if not self.tagger(str(temp), {}, cover_path) or not has_picture_frame(temp):
                return False
            with open(temp, 'r+b') as handle:                      # fsync needs a writable handle on Windows
                os.fsync(handle.fileno())
            digest, size = file_sha256(temp), temp.stat().st_size
            os.replace(temp, final)
            self._set_item(row['run_id'], row['seq'], final_sha=digest, final_size=size)
            return True
        except OSError:
            return False
        finally:
            self._discard_temp(temp)

    def embed_cover_at(self, path: str, cover_path: str) -> bool:
        """Add a cover to the organized file at ``path`` when it has none. Done on a temporary copy that is swapped in
        atomically, and everything MeloShelf knows about the file is updated to its new size and time."""
        from meloshelf.infrastructure.path_registry import rekey_path
        from meloshelf.infrastructure.tags import has_picture_frame
        if self.tagger is None or not cover_path or not os.path.isfile(cover_path):
            return False
        final = Path(path)
        temp = final.with_name(f'{final.stem}{TEMP_MARK}{final.suffix}')
        if not final.is_file() or has_picture_frame(final):
            return False
        self._discard_temp(temp)
        try:
            shutil.copyfile(final, temp)
            if not self.tagger(str(temp), {}, cover_path) or not has_picture_frame(temp):
                return False
            with open(temp, 'r+b') as handle:                      # fsync needs a writable handle on Windows
                os.fsync(handle.fileno())
            os.replace(temp, final)
            stat = final.stat()
            rekey_path(self.database, str(final), str(final), size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            return True
        except OSError:
            return False
        finally:
            self._discard_temp(temp)

    def known_outputs(self, dest_root: str) -> dict:
        """{source path: {relative, size, mtime_ns, final_size, final_sha, kind}} for files already organized into
        this destination whose copy is still in place (the latest copy of each source)."""
        dest = str(dest_root)
        with self.database.read() as db:
            rows = db.execute("""SELECT i.source_path, i.relative, i.size, i.mtime_ns, i.final_size, i.final_sha, i.kind
                                 FROM organize_items i JOIN organize_runs r ON r.id=i.run_id
                                 WHERE r.dest_root=? AND i.state IN ('done','skipped') ORDER BY r.created_at, i.seq""",
                              (dest,)).fetchall()
        found = {}
        for row in rows:
            final = Path(dest, *row['relative'].split('/'))
            if final.is_file() and (not row['final_size'] or final.stat().st_size == row['final_size']):
                found[row['source_path']] = dict(row)
        return found

    def adopt_unfinished(self, dest_root: str) -> None:
        """Settle runs a crash left half-way for this destination, so a file they had already published is counted as
        organized (and a stray temporary file is removed) before a new run plans names."""
        with self.database.read() as db:
            runs = db.execute("""SELECT DISTINCT r.id FROM organize_runs r JOIN organize_items i ON i.run_id=r.id
                                  WHERE r.dest_root=? AND (i.state IN ('planned','staged','finalizing')
                                       OR (i.state='failed' AND i.error='source_not_removed'))""",
                                (str(dest_root),)).fetchall()
        for row in runs:
            self._recover(row['id'], Path(dest_root))

    def create_run(self, source_root: str, dest_root: str, work: list[WorkItem], *, check: bool = True) -> str:
        """``check=False`` is for rearranging files that already live INSIDE the destination (folder consolidation), where the
        source root is the destination itself; emptied folders are then pruned up to, never including, that root."""
        if check:
            check_destination(source_root, dest_root)
        self.adopt_unfinished(dest_root)
        known = {} if self.mode == 'move' else self.known_outputs(dest_root)
        pending_bytes, rows = 0, []
        for seq, item in enumerate(work):
            try:
                stat = os.stat(item.source)
                size, mtime = stat.st_size, stat.st_mtime_ns
            except OSError:
                size, mtime = 0, 0
            if self.mode == 'move' and _norm(item.source) == _norm(self._final_for(Path(dest_root), item.relative)):
                rows.append((seq, item, item.relative, 'skipped', size, mtime, size, 'already in place', ''))
                continue
            prior = known.get(item.source)
            unchanged = prior and prior['size'] == size and prior['mtime_ns'] == mtime
            if unchanged and (prior['relative'] == item.relative or prior['kind'] == item.kind == 'review'):
                rows.append((seq, item, prior['relative'], 'skipped', size, mtime, prior['final_size'], 'already organized', ''))
            else:
                if self.mode != 'move' or not self.same_volume(item.source, str(Path(dest_root, *item.relative.split('/')).parent)):
                    pending_bytes += size             # a rename needs no extra space; a transfer does, for a moment
                replaced = prior['relative'] if unchanged else ''     # same file, new classification: replaces its copy
                rows.append((seq, item, item.relative, 'planned', size, mtime, 0, item.reason, replaced))
        probe = _nearest_existing(dest_root)
        if pending_bytes and self.free_space(probe) < pending_bytes + SPACE_MARGIN:
            raise DestinationError('not_enough_space', f'{pending_bytes} bytes needed')
        run_id, now = uuid.uuid4().hex[:12], self.clock()
        with self.database.write() as db:
            db.execute("INSERT INTO organize_runs(id,source_root,dest_root,mode,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (run_id, str(source_root), str(dest_root), self.mode, 'planned', now, now))
            db.executemany("""INSERT INTO organize_items(run_id,seq,source_path,relative,kind,state,size,mtime_ns,final_size,
                              tags,cover,reason,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           [(run_id, seq, item.source, relative, item.kind, state, size, mtime, final_size,
                             json.dumps(item.tags, ensure_ascii=False), item.cover, reason, replaced)
                            for seq, item, relative, state, size, mtime, final_size, reason, replaced in rows])
        return run_id

    # ── reading ─────────────────────────────────────────────────────────────────

    def run(self, run_id: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute('SELECT * FROM organize_runs WHERE id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def items(self, run_id: str) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM organize_items WHERE run_id=? ORDER BY seq', (run_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['source'] = item['source_path']
            item['tags'] = json.loads(item['tags'] or '{}')
            result.append(item)
        return result

    def counters(self, run_id: str) -> dict:
        counts = {'planned': 0, 'staged': 0, 'finalizing': 0, 'done': 0, 'skipped': 0, 'failed': 0}
        with self.database.read() as db:
            for row in db.execute('SELECT state, COUNT(*) AS n FROM organize_items WHERE run_id=? GROUP BY state', (run_id,)):
                counts[row['state']] = row['n']
        counts['total'] = sum(counts.values())
        return counts

    def _set_run(self, run_id: str, state: str, message: str = '') -> None:
        with self.database.write() as db:
            db.execute('UPDATE organize_runs SET state=?, message=?, updated_at=? WHERE id=?',
                       (state, message, self.clock(), run_id))

    def _set_item(self, run_id: str, seq: int, **fields) -> None:
        assignments = ','.join(f'{name}=?' for name in fields)
        with self.database.write() as db:
            db.execute(f'UPDATE organize_items SET {assignments} WHERE run_id=? AND seq=?', (*fields.values(), run_id, seq))

    # ── running ──────────────────────────────────────────────────────────────────

    def execute(self, run_id: str, *, control=None) -> str:
        """Move (or copy) every pending item. Returns completed | completed_with_errors | paused | cancelled | failed."""
        run = self.run(run_id)
        if run is None:
            raise KeyError(run_id)
        dest = Path(run['dest_root'])
        self._recover(run_id, dest)
        self._set_run(run_id, 'running')
        while True:
            signal = control() if control else None
            if signal in ('pause', 'cancel'):
                state = 'paused' if signal == 'pause' else 'cancelled'
                self._set_run(run_id, state)
                return state
            item = self._next(run_id)
            if item is None:
                break
            try:
                self._process(run_id, dest, item)
            except OSError as exc:
                source, temp = Path(item['source_path']), self._temp_for(dest, item['relative'])
                if self.mode == 'move' and not source.exists() and temp.is_file():
                    try:
                        os.rename(temp, source)                  # put the file back exactly where it was
                    except OSError:
                        log.warning('a staged file could not be put back; recovery will finish its move')
                        self._set_item(run_id, item['seq'], error='copy_failed')       # stays 'staged': recovery completes it
                        continue
                if self.mode != 'move' or source.exists():
                    self._discard_temp(temp)                     # never discard the only copy of a moved file
                if getattr(exc, 'errno', None) == DISK_FULL:
                    self._set_item(run_id, item['seq'], state='failed', error='disk_full')
                    self._set_run(run_id, 'failed', 'disk_full')
                    return 'failed'
                log.info('organize item failed (%s, errno=%s)', type(exc).__name__, getattr(exc, 'errno', None))
                self._set_item(run_id, item['seq'], state='failed', error='copy_failed')
        failed = self.counters(run_id)['failed']
        state = 'completed_with_errors' if failed else 'completed'
        self._set_run(run_id, state)
        return state

    def _next(self, run_id: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute("SELECT * FROM organize_items WHERE run_id=? AND state='planned' ORDER BY seq LIMIT 1",
                             (run_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item['tags'] = json.loads(item['tags'] or '{}')
        return item

    @staticmethod
    def _final_for(dest: Path, relative: str) -> Path:
        return dest.joinpath(*relative.split('/'))

    @staticmethod
    def _temp_for(dest: Path, relative: str) -> Path:
        final = dest.joinpath(*relative.split('/'))
        return final.with_name(f'{final.stem}{TEMP_MARK}{final.suffix}')

    @staticmethod
    def _discard_temp(temp: Path) -> None:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass

    def recover(self, run_id: str) -> int:
        """Settle whatever a crash, a lock or a pause left half-way. Returns how many items reached 'done'."""
        run = self.run(run_id)
        return self._recover(run_id, Path(run['dest_root'])) if run else 0

    def _row(self, run_id: str, seq: int) -> dict:
        with self.database.read() as db:
            row = dict(db.execute('SELECT * FROM organize_items WHERE run_id=? AND seq=?', (run_id, seq)).fetchone())
        row['tags'] = json.loads(row['tags'] or '{}')
        return row

    def _recover(self, run_id: str, dest: Path) -> int:
        """Bring the journal and the disk back in agreement. A move is judged from the journal and the disk together:
        the original is only ever removed after the destination has been verified against the recorded hash."""
        run = self.run(run_id)
        moving = bool(run and run['mode'] == 'move')
        with self.database.read() as db:
            rows = db.execute("""SELECT seq FROM organize_items WHERE run_id=? AND (state IN ('planned','staged','finalizing')
                                 OR (state='failed' AND error='source_not_removed')) ORDER BY seq""", (run_id,)).fetchall()
        finished = 0
        for entry in rows:
            row = self._row(run_id, entry['seq'])
            source = Path(row['source_path'])
            final, temp = self._final_for(dest, row['relative']), self._temp_for(dest, row['relative'])
            state = row['state']
            if state == 'planned':
                self._discard_temp(temp)
            elif state == 'staged':                                  # a move was in flight; source_sha is recorded
                if source.exists():
                    self._discard_temp(temp)                          # the original is intact: start this file again
                    self._set_item(run_id, row['seq'], state='planned')
                elif temp.is_file() and file_sha256(temp) == row['source_sha']:
                    finished += self._finish_move(run_id, dest, row)
                else:
                    self._set_item(run_id, row['seq'], state='failed', error='recovery_required')
            else:                                                     # finalizing, or a published file whose source stayed
                if final.is_file() and file_sha256(final) == row['final_sha']:
                    self._discard_temp(temp)
                    finished += self._complete(run_id, dest, row)
                elif temp.is_file() and file_sha256(temp) == row['final_sha']:
                    os.rename(temp, final)
                    finished += self._complete(run_id, dest, row)
                elif moving and not source.exists():
                    self._set_item(run_id, row['seq'], state='failed', error='recovery_required')
                else:
                    self._discard_temp(temp)
                    self._set_item(run_id, row['seq'], state='planned', error='')
        return finished

    def _complete(self, run_id: str, dest: Path, row: dict) -> int:
        """The destination is published and verified. Copy mode is finished; move mode now empties the original place."""
        run = self.run(run_id)
        if run and run['mode'] == 'move':
            return self._complete_move(run_id, dest, row)
        self._set_item(run_id, row['seq'], state='done')
        if row['supersedes']:
            self._supersede(run_id, dest, row['supersedes'])
        return 1

    def _complete_move(self, run_id: str, dest: Path, row: dict) -> int:
        seq, source = row['seq'], Path(row['source_path'])
        final = self._final_for(dest, row['relative'])
        if source.exists():
            # The original goes only when the destination matches the journal AND the original is still exactly the bytes
            # that were moved. A path that merely exists at the destination proves nothing.
            if not final.is_file() or file_sha256(final) != row['final_sha']:
                self._set_item(run_id, seq, state='failed', error='destination_unverified')
                return 0
            if row['source_sha'] and file_sha256(source) != row['source_sha']:
                self._set_item(run_id, seq, state='failed', error='source_not_removed')
                return 0
            try:
                os.remove(source)
            except OSError:
                log.info('original could not be removed; the verified destination is kept')
                self._set_item(run_id, seq, state='failed', error='source_not_removed')
                return 0
        self._hook('after_source_removed', source=str(source), final=str(final))
        stat = final.stat()
        from meloshelf.infrastructure.path_registry import rekey_path
        rekey_path(self.database, str(source), str(final), size=stat.st_size, mtime_ns=stat.st_mtime_ns, tags=row['tags'])
        self._set_item(run_id, seq, state='done', error='', source_removed=1)
        run = self.run(run_id)
        if run:
            self._prune_empty(source.parent, run['source_root'])
        if row['supersedes']:
            self._supersede(run_id, dest, row['supersedes'])
        return 1

    @staticmethod
    def _prune_empty(folder: Path, root: str) -> None:
        """Remove folders the move emptied, walking up to (never including) the source root. rmdir only ever removes an
        empty folder, so anything else a person keeps there stays."""
        stop = _norm(root)
        current = folder
        while _norm(current) != stop and _is_inside(_norm(current), stop):
            try:
                os.rmdir(current)
            except OSError:
                return
            current = current.parent

    def _process(self, run_id: str, dest: Path, item: dict) -> None:
        if self.mode == 'move':
            return self._process_move(run_id, dest, item)
        seq, source = item['seq'], Path(item['source_path'])
        if not source.is_file():
            self._set_item(run_id, seq, state='failed', error='source_missing')
            return
        before = source.stat()
        relative = item['relative']
        final = self._final_for(dest, relative)
        number = 1
        while final.exists():                                         # never overwrite: take a numbered sibling
            number += 1
            stem, extension = os.path.splitext(item['relative'])
            relative = f'{stem} ({number}){extension}'
            final = self._final_for(dest, relative)
        if relative != item['relative']:
            self._set_item(run_id, seq, relative=relative)
        temp = self._temp_for(dest, relative)
        final.parent.mkdir(parents=True, exist_ok=True)
        self._discard_temp(temp)
        expected = file_sha256(source)
        with open(source, 'rb') as reader, open(temp, 'wb') as writer:
            copy_stream(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        self._hook('after_stage', source=str(source), temp=str(temp))
        if temp.stat().st_size != before.st_size or file_sha256(temp) != expected:
            self._discard_temp(temp)
            self._set_item(run_id, seq, state='failed', error='verification_failed')
            return
        after = source.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            self._discard_temp(temp)
            self._set_item(run_id, seq, state='failed', error='source_changed')
            return
        self._hook('after_verify', source=str(source), temp=str(temp))
        warning = ''
        if self.tagger and (item['tags'] or item['cover']):
            if not self.tagger(str(temp), item['tags'], item['cover']):
                warning = 'tags_not_written'
        self._hook('after_tag', source=str(source), temp=str(temp))
        digest = file_sha256(temp)
        self._set_item(run_id, seq, state='finalizing', final_sha=digest, final_size=temp.stat().st_size, warning=warning)
        os.rename(temp, final)
        self._hook('after_publish', source=str(source), final=str(final))
        self._set_item(run_id, seq, state='done')
        if item['supersedes']:
            self._supersede(run_id, dest, item['supersedes'])

    def _process_move(self, run_id: str, dest: Path, item: dict) -> None:
        seq, source = item['seq'], Path(item['source_path'])
        if not source.is_file():
            self._set_item(run_id, seq, state='failed', error='source_missing')
            return
        before = source.stat()
        relative = item['relative']
        final = self._final_for(dest, relative)
        number = 1
        while final.exists():                                         # never overwrite: take a numbered sibling
            number += 1
            stem, extension = os.path.splitext(item['relative'])
            relative = f'{stem} ({number}){extension}'
            final = self._final_for(dest, relative)
        if relative != item['relative']:
            self._set_item(run_id, seq, relative=relative)
        temp = self._temp_for(dest, relative)
        final.parent.mkdir(parents=True, exist_ok=True)
        self._discard_temp(temp)
        expected = file_sha256(source)
        same = self.same_volume(str(source), str(final.parent))
        self._set_item(run_id, seq, state='staged', source_sha=expected)     # journalled BEFORE anything moves
        if same:
            os.rename(source, temp)                                   # atomic on one volume: the bytes simply travel
        else:
            with open(source, 'rb') as reader, open(temp, 'wb') as writer:
                copy_stream(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
        self._hook('after_stage', source=str(source), temp=str(temp))
        if not same:
            if temp.stat().st_size != before.st_size or file_sha256(temp) != expected:
                self._discard_temp(temp)
                self._set_item(run_id, seq, state='failed', error='verification_failed')
                return
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                self._discard_temp(temp)
                self._set_item(run_id, seq, state='failed', error='source_changed')
                return
        elif temp.stat().st_size != before.st_size:
            self._set_item(run_id, seq, state='failed', error='verification_failed')
            return
        self._finish_move(run_id, dest, self._row(run_id, seq))

    def _finish_move(self, run_id: str, dest: Path, row: dict) -> int:
        """The staged file is complete and verified: tag it, publish it, then (and only then) empty the original place."""
        seq = row['seq']
        source = Path(row['source_path'])
        final, temp = self._final_for(dest, row['relative']), self._temp_for(dest, row['relative'])
        self._hook('after_verify', source=str(source), temp=str(temp))
        warning = ''
        if self.tagger and (row['tags'] or row['cover']):
            if not self.tagger(str(temp), row['tags'], row['cover']):
                warning = 'tags_not_written'
        self._hook('after_tag', source=str(source), temp=str(temp))
        digest = file_sha256(temp)
        self._set_item(run_id, seq, state='finalizing', final_sha=digest, final_size=temp.stat().st_size, warning=warning)
        os.rename(temp, final)
        self._hook('after_publish', source=str(source), final=str(final))
        return self._complete_move(run_id, dest, self._row(run_id, seq))

    def _supersede(self, run_id: str, dest: Path, relative: str) -> None:
        """Remove the earlier copy this one replaces, but only if it is still exactly what MeloShelf wrote."""
        with self.database.read() as db:
            row = db.execute("""SELECT i.run_id, i.seq, i.final_sha FROM organize_items i JOIN organize_runs r ON r.id=i.run_id
                                 WHERE r.dest_root=? AND i.relative=? AND i.state='done' AND i.run_id<>?
                                 ORDER BY r.created_at DESC, i.seq DESC LIMIT 1""", (str(dest), relative, run_id)).fetchone()
        stale = self._final_for(dest, relative)
        if row and stale.is_file() and file_sha256(stale) == row['final_sha']:
            stale.unlink()
            self._set_item(row['run_id'], row['seq'], state='superseded')

