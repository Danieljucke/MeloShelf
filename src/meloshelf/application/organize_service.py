"""The organizing workflow: scan, recognise what needs it, plan, find covers, MOVE, report. One action for the person.

Reuses the existing parts: the scanner and its durable jobs, the recognition pipeline and its stored results, and the
journaled move engine. Each phase is honest about itself: a finished scan is not an organized library.
Phases: scanning -> recognizing -> planning -> artwork -> moving -> completed (or completed_with_errors / paused / cancelled /
failed). Everything here is restartable: running the same workflow again resumes whichever part was interrupted.
"""
import dataclasses
import json
import logging
from dataclasses import replace
import os
import threading
import time
from pathlib import Path

from meloshelf.domain.artists import canonical_key
from meloshelf.domain.classify import Recognized, decide, fields_to_write, needs_recognition
from meloshelf.domain.filename_order import OrderHints, recurring_sides
from meloshelf.domain.models import is_unknown_name
from meloshelf.domain.organize import REVIEW_FOLDER, PlanEntry, canonical_spellings, plan_destinations, safe_component
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.organizer import DestinationError, Organizer, WorkItem, check_destination
from meloshelf.infrastructure.repository import LibraryRepository

log = logging.getLogger(__name__)
SETTING = 'organize_workflow'
TERMINAL = ('completed', 'completed_with_errors', 'failed', 'cancelled', 'paused', 'interrupted', 'idle')
APPROVED = ('AUTO_APPROVED', 'USER_APPROVED')


def normalize_folder(path: str) -> str:
    """The folder spelled the way the scanner stores it (resolved, native separators). A folder picked in a dialog comes
    back with forward slashes; indexed paths do not, so every comparison must use this one spelling."""
    return str(Path(path).expanduser().resolve())


def recognized_from_row(row: dict) -> Recognized | None:
    """What the stored recognition result for a track's current content says; None when there is none."""
    status = row.get('recording_status') or ''
    text = row.get('match_data')
    if not text and not status:
        return None
    data = json.loads(text) if text else {}
    candidates = data.get('candidates') or []
    chosen = row.get('selected_recording_id') or ''
    top = next((c for c in candidates if chosen and c.get('recording_id') == chosen), candidates[0] if candidates else None)
    if status in APPROVED and top:
        releases = tuple(d['title'] for d in (top.get('release_details') or []) if d.get('title')) \
            or tuple(top.get('releases') or ())
        verdict = (top.get('evidence') or {}).get('version_verdict', 'insufficient')
        return Recognized(True, True, top.get('title', ''), top.get('artist', ''), releases, verdict)
    code = 'rejected_by_user' if status == 'REJECTED' else ('weak_evidence' if top else 'not_recognised')
    return Recognized(False, True, (top or {}).get('title', ''), (top or {}).get('artist', ''), reason_code=code)


class OrganizeService:
    def __init__(self, database, jobs, *, tagger=None, free_space=None, clock=time.time, covers=None, hooks=None, mode='move', same_volume=None):
        self.database = database
        self.jobs = jobs
        self.tagger = tagger
        self.free_space = free_space
        self.clock = clock
        self.mode, self.same_volume = mode, same_volume
        self.hooks = hooks                      # test seam: simulated crashes at chosen steps
        self._cover_cache: dict = {}            # (artist, album) -> cover file found during this run
        self.covers = covers                    # optional: cover_for(decision, entries) -> path or ''
        self.store = MatchStore(database)
        self.repo = LibraryRepository(database)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._control: str | None = None
        self._status = self._load()
        if self._status['phase'] not in TERMINAL:                     # the process that was running it is gone
            self._status['phase'] = 'interrupted'
            self._persist()

    # ── status ───────────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        saved = self.store.get_setting(SETTING, None)
        base = {'phase': 'idle', 'source': '', 'dest': '', 'run_id': '', 'scan_job': '', 'identify_job': '',
                'phases_seen': [], 'summary': {}, 'warnings': [], 'recognition_blocker': '', 'artwork': {}, 'error_code': '',
                'scanned': 0, 'identify': {}, 'empty_reason': '', 'batch': {}, 'started_at': None, 'updated_at': None}
        return {**base, **saved} if isinstance(saved, dict) else base

    def _persist(self) -> None:
        self._status['updated_at'] = self.clock()
        self.store.set_setting(SETTING, self._status)

    def _phase(self, phase: str) -> None:
        with self._lock:
            self._status['phase'] = phase
            if phase not in self._status['phases_seen'] and phase not in TERMINAL:
                self._status['phases_seen'].append(phase)
            self._persist()

    def _organizer(self) -> Organizer:
        return Organizer(self.database, tagger=self.tagger, free_space=self.free_space, hooks=self.hooks,
                         mode=self.mode, same_volume=self.same_volume)

    def status(self) -> dict:
        with self._lock:
            status = json.loads(json.dumps(self._status))
        status['running'] = bool(self._thread and self._thread.is_alive())
        status['progress'] = self._progress(status)
        return status

    def _progress(self, status: dict) -> dict:
        phase = status['phase']
        try:
            if phase == 'artwork':
                return dict(status.get('artwork') or {'done': 0, 'total': 0})
            if phase == 'moving' and status['run_id']:
                counts = self._organizer().counters(status['run_id'])
                return {'done': counts['done'] + counts['skipped'] + counts['failed'], 'total': counts['total']}
            job_id = status['scan_job'] if phase == 'scanning' else status['identify_job'] if phase == 'recognizing' else ''
            if job_id:
                job = self.jobs.store.get(job_id)
                counts = self.jobs.store.counters(job)
                return {'done': counts['total'] - counts['pending'], 'total': counts['total']}
        except Exception:  # progress is informative only; never let it break a status request
            log.debug('progress unavailable', exc_info=True)
        return {'done': 0, 'total': 0}

    # ── reading the library into decisions ───────────────────────────────────────

    def _manual(self, paths) -> dict:
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM manual_identities').fetchall()
        return {r['path']: dict(r) for r in rows}

    def decisions(self, source: str) -> list[dict]:
        rows = self.repo.tracks_under(normalize_folder(source))
        manual = self._manual([r['path'] for r in rows])
        known = self._known_artists()
        by_folder: dict = {}
        for row in rows:
            by_folder.setdefault(os.path.dirname(row['path']), []).append(os.path.basename(row['path']))
        recurring = {folder: recurring_sides(names) for folder, names in by_folder.items()}
        result = []
        for row in rows:
            tags = {'title': row['title'] or '', 'artist': row['artist'] or '', 'album': row['album'] or '',
                    'tracknumber': str(row['track_no']) if row['track_no'] else ''}
            name = os.path.basename(row['path'])
            identity = manual.get(row['path'])
            by_you = bool(identity and identity['size'] == row['size'] and identity['mtime_ns'] == row['mtime_ns'])
            if by_you:
                recognized = Recognized(True, True, identity['title'], identity['artist'],
                                        (identity['album'],) if identity['album'] else (), 'verified_agreement')
            else:
                recognized = recognized_from_row(row)
            evidence = dict(known)
            evidence.update(recurring.get(os.path.dirname(row['path']), {}))
            folder = canonical_key(os.path.basename(os.path.dirname(row['path'])))
            if folder and folder != canonical_key(REVIEW_FOLDER):
                evidence[folder] = 'folder'
            if tags['artist'] and not is_unknown_name(tags['artist']):
                evidence[canonical_key(tags['artist'])] = 'tag'
            decision = decide(tags, name, recognized, OrderHints(evidence))
            if by_you and decision.status == 'organize':
                decision = replace(decision, origin='manual')          # the person's own correction is not automatic recognition
            result.append({'row': row, 'tags': tags, 'name': name, 'recognized': recognized, 'decision': decision})
        self._unify_artist_spellings(result)
        return result

    def _known_artists(self) -> dict:
        """Artists the library already knows well enough to trust as an artist: named on two or more tracks."""
        with self.database.read() as db:
            rows = db.execute("SELECT artist FROM tracks WHERE artist<>'' GROUP BY artist HAVING COUNT(*)>=2").fetchall()
        return {canonical_key(r['artist']): 'known_artist' for r in rows if not is_unknown_name(r['artist'])}

    def _folder_names(self, credits) -> dict:
        """{credit string: the canonical PRIMARY artist's folder name}. "Kygo, Selena Gomez" files under Kygo; the full credit
        stays in the tags. A merge or a MusicBrainz name the library already uses names the folder; spellings that differ only
        by case or accents share the most common one."""
        from meloshelf.infrastructure.artist_corrections import ArtistCorrections
        from meloshelf.infrastructure.artist_identity_store import ArtistLinks
        credits = [c for c in dict.fromkeys(credits) if c]
        if not credits:
            return {}
        analyse = self.repo.credit_analyser()
        state = ArtistCorrections(self.database).state()
        links = ArtistLinks(self.database).all()

        def canonical(name: str) -> str:
            named = (state.resolve(name) if state else None) or name
            link = links.get(canonical_key(named))
            if link and link.get('status') in ('linked', 'accepted') and link.get('mb_name'):
                return link['mb_name']
            return named

        primaries = {c: canonical(analyse(c).primary or c) for c in credits}
        spellings = canonical_spellings(list(primaries.values()))
        return {c: spellings.get(canonical_key(p), p) for c, p in primaries.items()}

    def _unify_artist_spellings(self, items: list[dict]) -> None:
        organized = [i for i in items if i['decision'].status == 'organize']
        names = self._folder_names([i['decision'].artist for i in organized])
        for item in organized:
            item['decision'] = dataclasses.replace(item['decision'], folder_artist=names.get(item['decision'].artist, ''))

    def _needing_recognition(self, items: list[dict]) -> list[str]:
        wanted = []
        for item in items:
            if item['row']['recording_status'] or item['recognized'] is not None:
                continue
            if needs_recognition(item['tags'], item['name'])[0]:
                wanted.append(item['row']['path'])
        return wanted

    @staticmethod
    def review_root(dest: str) -> str:
        return os.path.join(normalize_folder(dest), REVIEW_FOLDER)

    def all_decisions(self, source: str, dest: str) -> list[dict]:
        """What is waiting in the source, plus what is already sitting in the review folder (so a corrected file can move)."""
        items = self.decisions(source)
        if dest and os.path.isdir(self.review_root(dest)):
            items += self.decisions(self.review_root(dest))
        return items

    def _existing(self, dest: str) -> set[str]:
        root = Path(dest)
        if not root.is_dir():
            return set()
        return {str(p.relative_to(root)).replace(os.sep, '/').lower() for p in root.rglob('*') if p.is_file()}

    def _work(self, source: str, dest: str, items: list[dict]) -> list[WorkItem]:
        organizer = self._organizer()
        organizer.adopt_unfinished(dest)       # a file a crash left published counts as organized before names are planned
        known = organizer.known_outputs(dest)
        mine = {i['row']['path'] for i in items}
        occupied = self._existing(dest) - {v['relative'].lower() for k, v in known.items() if k in mine}
        for path in mine:                                  # a file already inside the destination does not collide with itself
            try:
                rel = os.path.relpath(path, dest).replace(os.sep, '/')
            except ValueError:
                continue
            if not rel.startswith('..'):
                occupied.discard(rel.lower())
        entries = [PlanEntry(source=i['row']['path'], decision=i['decision'], extension=Path(i['row']['path']).suffix,
                             original_name=i['name']) for i in items]
        planned = plan_destinations(entries, destination_root=dest, occupied=occupied)
        by_source = {i['row']['path']: i for i in items}
        same_album: dict = {}
        for plan in planned:
            if plan.decision.status == 'organize' and plan.decision.album:
                same_album.setdefault((plan.decision.artist.lower(), plan.decision.album.lower()), []).append(plan.source)
        work = []
        for plan in planned:
            item = by_source[plan.source]
            item['paths'] = same_album.get((plan.decision.artist.lower(), plan.decision.album.lower()), [])
            item['decision'] = plan.decision
            decision = plan.decision
            work.append(WorkItem(source=plan.source, relative=plan.relative, kind=decision.status,
                                 tags=fields_to_write(item['tags'], decision), cover='',
                                 reason=decision.origin if decision.status == 'organize' else decision.reason_code))
        return work

    # ── the read-only preview ────────────────────────────────────────────────────

    def preview(self, source: str, dest: str, *, scan: bool = False) -> dict:
        """What would move where. Reads only; with ``scan`` it first indexes the folder (still read-only)."""
        source, dest = normalize_folder(source), normalize_folder(dest)
        try:
            check_destination(source, dest)
        except DestinationError as exc:
            return {'ok': False, 'code': exc.code}
        if scan:
            if self._thread and self._thread.is_alive():
                return {'ok': False, 'code': 'organize_running'}
            if not os.path.isdir(source):
                return {'ok': False, 'code': 'folder_not_found'}
            started = self.jobs.start_scan(source)
            if started.get('job_id'):
                self.jobs.wait(started['job_id'], 600)
        items = self.all_decisions(source, dest)
        work = self._work(source, dest, items)
        needed = set(self._needing_recognition(items))
        size = sum(i['row']['size'] for i in items)
        organizer = self._organizer()
        same_volume = organizer.same_volume
        cross = sum(1 for w in work if not same_volume(w.source, os.path.dirname(os.path.join(dest, *w.relative.split('/')))))
        in_place = sum(1 for w in work if os.path.normcase(os.path.realpath(w.source)) == os.path.normcase(os.path.realpath(os.path.join(dest, *w.relative.split('/')))))
        moves = [{'from': w.source, 'to': os.path.join(dest, *w.relative.split('/')), 'kind': w.kind}
                 for w in work if w.source not in needed and not os.path.normcase(w.source) == os.path.normcase(os.path.join(dest, *w.relative.split('/')))][:200]
        free = None
        try:
            from meloshelf.infrastructure.organizer import _nearest_existing
            import shutil
            free = (self.free_space or (lambda p: shutil.disk_usage(p).free))(_nearest_existing(dest))
        except OSError:
            pass
        return {'ok': True, 'source': source, 'dest': dest, 'total': len(items),
                'ready': sum(1 for i in items if i['decision'].status == 'organize' and i['row']['path'] not in needed),
                'will_recognise': len(needed),
                'review': sum(1 for i in items if i['decision'].status == 'review' and i['row']['path'] not in needed),
                'already_organized': in_place, 'moves': moves, 'cross_volume': cross,
                'bytes': size, 'free_bytes': free, 'enough_space': free is None or cross == 0 or free >= size,
                'destination_exists': os.path.isdir(dest)}

    # ── running ──────────────────────────────────────────────────────────────────

    def start(self, source: str, dest: str, *, offline: bool = False) -> dict:
        source, dest = normalize_folder(source), normalize_folder(dest)
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {'started': False, 'code': 'organize_running'}
            try:
                check_destination(source, dest)
            except DestinationError as exc:
                return {'started': False, 'code': exc.code}
            if not os.path.isdir(source):
                return {'started': False, 'code': 'folder_not_found'}
            self._control = None
            self._status.update({'source': source, 'dest': dest, 'run_id': '', 'scan_job': '', 'identify_job': '',
                                 'phases_seen': [], 'summary': {}, 'warnings': [], 'recognition_blocker': '', 'error_code': '',
                                 'scanned': 0, 'identify': {}, 'empty_reason': '', 'artwork': {},
                                 'started_at': self.clock(), 'phase': 'scanning'})   # never show the previous run's result
            self._thread = threading.Thread(target=self._guarded, args=(source, dest, offline), name='organize', daemon=True)
            self._thread.start()
        return {'started': True}

    def pause(self) -> None:
        self._control = 'pause'

    def cancel(self) -> None:
        self._control = 'cancel'

    def _signal(self):
        return self._control

    def _guarded(self, source: str, dest: str, offline: bool) -> None:
        try:
            self.run(source, dest, offline=offline)
        except DestinationError as exc:
            self._fail(exc.code)
        except Exception as exc:  # the worker must always end in a recorded state; detail goes to the log
            log.exception('organize workflow failed')
            self._fail('organize_failed')
            self._status['warnings'].append(type(exc).__name__)

    def _fail(self, code: str) -> None:
        with self._lock:
            self._status['error_code'] = code
        self._phase('failed')

    def _await(self, job_id: str) -> bool:
        """Wait for a job; False when the person paused or cancelled meanwhile (the job is stopped too)."""
        while not self.jobs.wait(job_id, 0.25):
            signal = self._signal()
            if signal == 'pause':
                self.jobs.pause(job_id)
                self.jobs.wait(job_id, 30)
                return False
            if signal == 'cancel':
                self.jobs.cancel(job_id)
                self.jobs.wait(job_id, 30)
                return False
        return True

    def run(self, source: str, dest: str, *, offline: bool = False) -> dict:
        """The whole workflow, synchronously. Returns the summary."""
        source, dest = normalize_folder(source), normalize_folder(dest)
        with self._lock:
            self._status.update({'source': source, 'dest': dest, 'scanned': 0, 'identify': {}, 'empty_reason': ''})
            if not self._status['started_at']:
                self._status['started_at'] = self.clock()
        check_destination(source, dest)
        self._organizer().adopt_unfinished(dest)      # settle what an interrupted run left, before anything is planned

        self._cover_cache = {}
        self._phase('scanning')
        for root in [source] + ([self.review_root(dest)] if os.path.isdir(self.review_root(dest)) else []):
            started = self.jobs.start_scan(root)
            if not started.get('job_id'):
                self._fail('folder_not_found')
                return {}
            self._status['scan_job'] = started['job_id']
            if not self._await(started['job_id']):
                return self._stopped()
            job = self.jobs.store.get(started['job_id'])
            if job and job['state'] in ('FAILED', 'failed'):
                self._fail('scan_failed')
                return {}

        self._phase('recognizing')
        items = self.all_decisions(source, dest)
        with self._lock:
            self._status['scanned'] = len(self.decisions(source))      # what THIS folder holds, not the whole library
        wanted = self._needing_recognition(items)
        if wanted:
            identify = self.jobs.start_identify(len(wanted), offline, paths=wanted)
            if identify.get('started') and identify.get('job_id'):      # a job started FOR this run, never one found lying around
                self._status['identify_job'] = identify['job_id']
                if not self._await(identify['job_id']):
                    return self._stopped()
                job = self.jobs.store.get(identify['job_id']) or {}
                counts = self.jobs.store.counters(job) if job else {}
                with self._lock:
                    self._status['identify'] = {'job': identify['job_id'], 'analysed': counts.get('done', 0) + counts.get('unmatched', 0) + counts.get('error', 0),
                                                'proposed': counts.get('done', 0), 'unknown': counts.get('unmatched', 0)}
                blocker = job.get('error_code') or ''
                if blocker:
                    self._status['recognition_blocker'] = blocker   # e.g. fpcalc_unavailable: those files wait in review
            elif not identify.get('nothing_to_do'):
                self._status['recognition_blocker'] = 'recognition_not_started'     # e.g. another identification is running

        self._phase('planning')
        items = self.all_decisions(source, dest)
        work = self._work(source, dest, items)
        if not work:                                  # say WHY nothing happened instead of claiming an organized library
            reason = 'already_organized' if self._previously_organized(source, dest) else 'no_audio_files'
            with self._lock:
                self._status['empty_reason'] = reason
                self._status['summary'] = self._empty_summary(dest)
            self._cover_cache = {}
            self._phase('completed')
            return self._status['summary']

        if self.covers is not None:
            self._phase('artwork')
            found = self._artwork(work, items)
            if found is None:
                return self._stopped()
            work = [replace(w, cover=found.get(w.source, '')) for w in work]

        self._phase('moving')
        organizer = self._organizer()
        run_id = organizer.create_run(source, dest, work)
        self._status['run_id'] = run_id
        self._persist()
        result = organizer.execute(run_id, control=self._signal)
        summary = self.summary(run_id)
        with self._lock:
            self._status['summary'] = summary
        phase = {'paused': 'paused', 'cancelled': 'cancelled', 'failed': 'failed'}.get(result, result)
        if result == 'failed':
            self._status['error_code'] = organizer.run(run_id)['message'] or 'organize_failed'
        if summary['total'] and summary['moved'] == 0 and summary['skipped'] == summary['total'] and summary['failed'] == 0:
            self._status['empty_reason'] = 'already_in_place'
        if result in ('completed', 'completed_with_errors'):
            self._embed_covers(source, dest)       # files moved by earlier runs get covers confirmed since
        self._cover_cache = {}                     # what a run found is only valid for that run
        self._phase(phase)
        return summary

    def _artwork(self, work: list[WorkItem], items: list[dict]):
        """One cover lookup per album, between albums so pause and cancel are honoured.
        Returns {source path: cover file}, or None when stopped."""
        by_source = {i['row']['path']: i for i in items}
        albums: dict = {}
        found: dict = {}
        for w in work:
            item = by_source.get(w.source)
            if w.kind == 'organize' and item is not None and item['decision'].album:
                decision = item['decision']
                albums.setdefault((decision.artist.lower(), decision.album.lower()), []).append((w, item))
        with self._lock:
            self._status['artwork'] = {'done': 0, 'total': len(albums)}
        for n, members in enumerate(albums.values(), start=1):
            if self._control in ('pause', 'cancel'):
                return None
            first_work, first_item = members[0]
            try:
                cover = self.covers.cover_for(first_item['decision'], first_item) or ''
            except Exception:               # a cover that cannot be looked up is a missing cover, never a failed run
                log.warning('cover lookup failed', exc_info=True)
                cover = ''
            decision = first_item['decision']
            self._cover_cache[(decision.artist.lower(), decision.album.lower())] = cover
            for w, _ in members:
                found[w.source] = cover
            with self._lock:
                self._status['artwork'] = {'done': n, 'total': len(albums)}
        return found

    def apply_covers(self) -> int:
        """Embed each album's CONFIRMED cover in organized copies that lack one. Only copies are changed, never sources.
        Returns how many copies received a cover."""
        status = self.status()
        if status['phase'] in ('scanning', 'recognizing', 'planning', 'artwork', 'moving'):
            return 0                                # a run is in progress and will add covers itself
        return self._embed_covers(status.get('source') or '', status.get('dest') or '')

    def _embed_covers(self, source: str, dest: str) -> int:
        """Embed each album's CONFIRMED cover in organized files that lack one."""
        if self.covers is None or not dest or not os.path.isdir(dest):
            return 0
        organizer = self._organizer()
        albums: dict = {}
        review = self.review_root(dest)
        for entry in self.decisions(dest):
            decision, path = entry['decision'], entry['row']['path']
            if decision.status == 'organize' and decision.album and not path.startswith(review):
                albums.setdefault((decision.artist, decision.album), []).append((path, decision))
        count = 0
        for members in albums.values():
            decision = members[0][1]
            key = (decision.artist.lower(), decision.album.lower())
            if key in self._cover_cache:
                cover = self._cover_cache[key]
            else:
                try:
                    cover = self.covers.cover_for(decision, {'paths': [p for p, _ in members]}) or ''
                except Exception:
                    log.warning('cover lookup failed', exc_info=True)
                    continue
            for path, _ in members:
                if cover and organizer.embed_cover_at(path, cover):
                    count += 1
        return count

    def _previously_organized(self, source: str, dest: str) -> bool:
        """True only when an earlier run for this very folder and destination completed and placed files."""
        with self.database.read() as db:
            row = db.execute("""SELECT 1 FROM organize_runs r JOIN organize_items i ON i.run_id=r.id
                                 WHERE r.source_root=? AND r.dest_root=? AND r.state IN ('completed','completed_with_errors')
                                   AND i.state='done' LIMIT 1""", (source, dest)).fetchone()
        return row is not None

    @staticmethod
    def _empty_summary(dest: str) -> dict:
        return {'run_id': '', 'state': 'completed', 'total': 0, 'moved': 0, 'sources_removed': 0, 'organized': 0,
                'organized_from_tags': 0, 'review': 0, 'recognized': 0, 'by_you': 0, 'skipped': 0, 'failed': 0, 'needs_recovery': 0,
                'tags_not_written': 0, 'dest': dest}

    def _stopped(self) -> dict:
        self._phase('paused' if self._control == 'pause' else 'cancelled')
        return {}

    # ── results ──────────────────────────────────────────────────────────────────

    def summary(self, run_id: str) -> dict:
        """Counts what really happened: placements that were verified, originals that were removed, files still waiting."""
        organizer = self._organizer()
        items = organizer.items(run_id)
        run = organizer.run(run_id)
        placed = [i for i in items if i['state'] in ('done', 'skipped')]
        return {'run_id': run_id, 'state': run['state'] if run else '', 'total': len(items),
                'moved': sum(1 for i in items if i['state'] == 'done'),
                'sources_removed': sum(1 for i in items if i['state'] == 'done' and i['source_removed']),
                'organized': sum(1 for i in placed if i['kind'] == 'organize'),
                'review': sum(1 for i in placed if i['kind'] == 'review'),
                'organized_from_tags': sum(1 for i in placed if i['kind'] == 'organize' and i['reason'] == 'tags'),
                'recognized': sum(1 for i in placed if i['kind'] == 'organize' and i['reason'] == 'recognition'),
                'by_you': sum(1 for i in placed if i['kind'] == 'organize' and i['reason'] == 'manual'),
                'skipped': sum(1 for i in items if i['state'] == 'skipped'),
                'failed': sum(1 for i in items if i['state'] == 'failed'),
                'needs_recovery': sum(1 for i in items if i['state'] == 'failed' and i['error'] in ('source_not_removed', 'recovery_required', 'destination_unverified')),
                'tags_not_written': sum(1 for i in items if i['warning'] == 'tags_not_written'),
                'dest': run['dest_root'] if run else ''}

    # ── consolidating collaboration folders in an already-organized destination ──

    def consolidation_plan(self, dest: str, *, scan: bool = False) -> dict:
        """A dry run: which files sit under a collaboration or variant artist folder and where they would go. Changes nothing."""
        dest = normalize_folder(dest)
        if scan and os.path.isdir(dest):
            started = self.jobs.start_scan(dest)
            if started.get('job_id'):
                self.jobs.wait(started['job_id'], 600)
        review = self.review_root(dest)
        rows = [r for r in self.repo.tracks_under(dest) if not r['path'].startswith(review) and r['artist']]
        names = self._folder_names([r['artist'] for r in rows])
        taken = {p.lower() for p in self._existing(dest)}
        moves, folders, conflicts = [], {}, 0
        for row in rows:
            parts = os.path.relpath(row['path'], dest).replace(os.sep, '/').split('/')
            if len(parts) < 2 or not os.path.isfile(row['path']):
                continue
            target = names.get(row['artist'], '')
            if not target or canonical_key(parts[0]) == canonical_key(target):
                continue                                              # already filed under the canonical artist
            relative = '/'.join([safe_component(target), *parts[1:]])
            entry = {'from': row['path'], 'to': os.path.join(dest, *relative.split('/')), 'relative': relative,
                     'credit': row['artist'], 'folder': parts[0], 'conflict': ''}
            if relative.lower() in taken:
                same = os.path.isfile(entry['to']) and self._same_bytes(row['path'], entry['to'])
                entry['conflict'] = 'duplicate' if same else 'renamed'
                conflicts += 1
            taken.add(relative.lower())
            moves.append(entry)
            folders[parts[0]] = folders.get(parts[0], 0) + 1
        movable = [m for m in moves if m['conflict'] != 'duplicate']
        return {'ok': True, 'dest': dest, 'moves': moves, 'total': len(moves), 'to_move': len(movable),
                'duplicates': sum(1 for m in moves if m['conflict'] == 'duplicate'),
                'renamed': sum(1 for m in moves if m['conflict'] == 'renamed'), 'conflicts': conflicts,
                'folders': [{'name': k, 'files': v} for k, v in sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))]}

    @staticmethod
    def _same_bytes(a: str, b: str) -> bool:
        from meloshelf.infrastructure.organizer import file_sha256
        try:
            return os.path.getsize(a) == os.path.getsize(b) and file_sha256(a) == file_sha256(b)
        except OSError:
            return False

    def consolidate(self, dest: str, *, confirm: bool = False) -> dict:
        """Move files out of collaboration folders into the canonical artist folder, through the journaled engine. Needs an
        explicit confirmation; never runs by itself. Byte-identical duplicates are left where they are for a person to judge."""
        if not confirm:
            raise PermissionError('consolidation needs explicit confirmation')
        dest = normalize_folder(dest)
        plan = self.consolidation_plan(dest)
        organizer = self._organizer()
        organizer.adopt_unfinished(dest)
        work = [WorkItem(source=m['from'], relative=m['relative'], kind='organize', reason='consolidation')
                for m in plan['moves'] if m['conflict'] != 'duplicate']
        if not work:
            return {'run_id': '', 'moved': 0, 'failed': 0, 'duplicates_left': plan['duplicates'], 'folders_removed': 0}
        before = self._folder_count(dest)
        run_id = organizer.create_run(dest, dest, work, check=False)
        organizer.execute(run_id)
        summary = self.summary(run_id)
        return {'run_id': run_id, 'moved': summary['moved'], 'failed': summary['failed'], 'duplicates_left': plan['duplicates'],
                'folders_removed': max(0, before - self._folder_count(dest)), 'needs_recovery': summary['needs_recovery']}

    @staticmethod
    def _folder_count(dest: str) -> int:
        return sum(1 for _ in Path(dest).rglob('*') if _.is_dir()) if os.path.isdir(dest) else 0

    def undo_consolidation(self, run_id: str) -> dict:
        """Put every file that run moved back where it was, through the same journaled engine."""
        organizer = self._organizer()
        run = organizer.run(run_id)
        if run is None:
            raise KeyError(run_id)
        dest = run['dest_root']
        back = []
        for item in organizer.items(run_id):
            if item['state'] != 'done':
                continue
            final = os.path.join(dest, *item['relative'].split('/'))
            if os.path.isfile(final):
                back.append(WorkItem(source=final, relative=os.path.relpath(item['source_path'], dest).replace(os.sep, '/'),
                                     kind='organize', reason='undo'))
        if not back:
            return {'run_id': '', 'moved': 0, 'failed': 0}
        undo_id = organizer.create_run(dest, dest, back, check=False)
        organizer.execute(undo_id)
        summary = self.summary(undo_id)
        return {'run_id': undo_id, 'moved': summary['moved'], 'failed': summary['failed']}

    # ── a person identifies several files at once ───────────────────────────────

    def _review_entries(self, dest: str) -> dict:
        review = self.review_root(dest)
        if not os.path.isdir(review):
            return {}
        return {os.path.normcase(e['row']['path']): e for e in self.decisions(review) if os.path.isfile(e['row']['path'])}

    def validate_review(self, items: list[dict]) -> dict:
        """What saving these edits would do, per file. Changes nothing."""
        dest = self.status().get('dest') or ''
        if dest:
            self._organizer().adopt_unfinished(dest)       # a crashed move is settled before judging what is still waiting
        entries = self._review_entries(dest) if dest else {}
        manual = self._manual(None)
        results = []
        for item in items:
            path = str(item.get('path') or '')
            title, artist, album = (str(item.get(k) or '').strip() for k in ('title', 'artist', 'album'))
            entry = entries.get(os.path.normcase(path))
            result = {'path': path, 'name': os.path.basename(path), 'title': title, 'artist': artist, 'album': album,
                      'errors': [], 'to': '', 'status': 'ready'}
            if entry is None:
                result.update(status='not_in_review', errors=['not_in_review'])
                results.append(result)
                continue
            real = entry['row']['path']
            if not title:
                result['errors'].append('title_required')
            if not artist:
                result['errors'].append('artist_required')
            if result['errors']:
                result['status'] = 'incomplete'
            else:
                current = manual.get(real)
                same_file = current and current['size'] == entry['row']['size'] and current['mtime_ns'] == entry['row']['mtime_ns']
                if same_file and (current['title'], current['artist'], current['album']) != (title, artist, album) \
                        and not item.get('overwrite'):
                    result.update(status='conflict', errors=['conflict_existing_decision'])
                result['to'] = self._target_for(dest, entry, title, artist, album)
            result['path'] = real
            results.append(result)
        count = lambda s: sum(1 for r in results if r['status'] == s)      # noqa: E731
        return {'items': results, 'ready': count('ready'), 'incomplete': count('incomplete'), 'conflict': count('conflict'),
                'not_in_review': count('not_in_review'), 'modified': len(results)}

    def _target_for(self, dest: str, entry: dict, title: str, artist: str, album: str) -> str:
        recognized = Recognized(True, True, title, artist, (album,) if album else (), 'verified_agreement')
        decision = decide(entry['tags'], entry['name'], recognized)
        planned = plan_destinations([PlanEntry(source=entry['row']['path'], decision=decision, extension=Path(entry['row']['path']).suffix,
                                               original_name=entry['name'])], destination_root=dest)
        return os.path.join(dest, *planned[0].relative.split('/'))

    def apply_review(self, items: list[dict]) -> dict:
        """Save every valid edit, then move exactly those files through the journaled engine. Partial success is normal:
        files that fail keep their saved decision and stay in the review folder for a safe retry."""
        validation = self.validate_review(items)
        usable = [r for r in validation['items'] if r['status'] == 'ready']
        skipped = [{'path': r['path'], 'name': r['name'], 'code': r['errors'][0]} for r in validation['items'] if r['status'] != 'ready']
        moved, failed = 0, []
        if usable:
            self._save_identities(usable)
            outcome = self._run_review({r['path'] for r in usable})
            for entry in outcome:
                if entry['state'] == 'done':
                    moved += 1
                elif entry['state'] == 'skipped':
                    skipped.append({'path': entry['source_path'], 'name': os.path.basename(entry['source_path']), 'code': 'already_in_place'})
                else:
                    failed.append({'path': entry['source_path'], 'name': os.path.basename(entry['source_path']),
                                   'code': entry['error'] or 'move_failed'})
        report = {'saved': len(usable), 'moved': moved, 'skipped': skipped, 'failed': failed,
                  'modified': validation['modified'], 'validation': validation}
        with self._lock:
            self._status['batch'] = {k: report[k] for k in ('saved', 'moved', 'skipped', 'failed', 'modified')}
            self._persist()
        return report

    def _save_identities(self, usable: list[dict]) -> None:
        now = self.clock()
        with self.database.write() as db:
            for r in usable:
                stat = os.stat(r['path'])
                db.execute("""INSERT INTO manual_identities(path,size,mtime_ns,title,artist,album,created_at) VALUES(?,?,?,?,?,?,?)
                              ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, title=excluded.title,
                              artist=excluded.artist, album=excluded.album, created_at=excluded.created_at""",
                           (r['path'], stat.st_size, stat.st_mtime_ns, r['title'], r['artist'], r['album'], now))

    def _run_review(self, only: set) -> list[dict]:
        """Move just these review files. No library scan and no recognition: the corrected identity is the evidence."""
        status = self.status()
        source, dest = status.get('source') or '', status.get('dest') or ''
        if not source or not dest:
            raise ValueError('Nothing has been organized yet.')
        with self._lock:
            self._status.update({'phases_seen': [], 'summary': {}, 'warnings': [], 'error_code': '', 'empty_reason': ''})
        self._cover_cache = {}
        self._phase('planning')
        organizer = self._organizer()
        wanted = {os.path.normcase(p) for p in only}
        items = [e for e in self.decisions(self.review_root(dest)) if os.path.normcase(e['row']['path']) in wanted]
        work = self._work(source, dest, items)
        if self.covers is not None:
            self._phase('artwork')
            found = self._artwork(work, items)
            if found is not None:
                work = [replace(w, cover=found.get(w.source, '')) for w in work]
        self._phase('moving')
        run_id = organizer.create_run(source, dest, work)
        self._status['run_id'] = run_id
        self._persist()
        result = organizer.execute(run_id, control=self._signal)
        with self._lock:
            self._status['summary'] = self.summary(run_id)
        self._cover_cache = {}
        self._phase({'paused': 'paused', 'cancelled': 'cancelled', 'failed': 'failed'}.get(result, result))
        return organizer.items(run_id)

    def start_review(self, items: list[dict]) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {'started': False, 'code': 'organize_running'}
            self._control = None
            self._status['batch'] = {}
            self._thread = threading.Thread(target=self._guarded_review, args=(items,), name='organize-review', daemon=True)
            self._thread.start()
        return {'started': True}

    def _guarded_review(self, items: list[dict]) -> None:
        try:
            self.apply_review(items)
        except Exception as exc:
            log.exception('review batch failed')
            self._fail('organize_failed')
            self._status['warnings'].append(type(exc).__name__)

    # ── a person identifies a file ───────────────────────────────────────────────

    def resolve(self, path: str, *, title: str, artist: str, album: str = '') -> None:
        title, artist, album = title.strip(), artist.strip(), album.strip()
        if not title or not artist:
            raise ValueError('A title and an artist are required.')
        stat = os.stat(path)
        with self.database.write() as db:
            db.execute("""INSERT INTO manual_identities(path,size,mtime_ns,title,artist,album,created_at) VALUES(?,?,?,?,?,?,?)
                          ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, title=excluded.title,
                          artist=excluded.artist, album=excluded.album, created_at=excluded.created_at""",
                       (path, stat.st_size, stat.st_mtime_ns, title, artist, album, self.clock()))
