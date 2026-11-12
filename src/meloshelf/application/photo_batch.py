"""Fetch artist photographs for every verified artist, in one cancellable, restartable background pass.

Why photographs never appeared on their own: fetching only ever happened when a person pressed a button for one artist,
and the lookup keys were whole credit strings ("Kygo, Selena Gomez"), which MusicBrainz cannot link to one artist. Names are
now the canonical primary artists, and this pass fetches a licensed photograph for each artist whose identity is verified.

Permanent answers (no photograph, a licence that does not allow reuse, an unusable image) are stored and never asked again.
A transient failure is not stored; two in a row mean the provider is probably unavailable, so the pass stops instead of
sending more requests.
"""
import threading
import time

SETTING = 'photo_batch'
IDLE = {'state': 'idle', 'done': 0, 'total': 0, 'counts': {}, 'updated_at': None}
CONSECUTIVE_FAILURES = 2


class PhotoBatch:
    def __init__(self, store, repo, photos, *, clock=time.time):
        self.store, self.repo, self.photos, self.clock = store, repo, photos, clock
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        saved = self.store.get_setting(SETTING, None)
        if isinstance(saved, dict) and saved.get('state') in ('running', 'cancelling'):
            self._save({**saved, 'state': 'interrupted'})

    def status(self) -> dict:
        saved = self.store.get_setting(SETTING, None)
        return {**IDLE, **saved} if isinstance(saved, dict) else dict(IDLE)

    def _save(self, status: dict) -> None:
        self.store.set_setting(SETTING, {**status, 'updated_at': self.clock()})

    def pending(self) -> list[str]:
        """Verified artist IDs with no stored answer yet, once each, most-played artists first."""
        current = ','.join(sorted(c.name for c in self.photos.extras()))
        with self.repo.database.read() as db:
            # A stored 'no photograph' stays final, unless an optional source has been switched on since it was given.
            known = {r['mb_artist_id'] for r in db.execute('SELECT mb_artist_id, status, reason FROM artist_images')
                     if r['status'] not in self.photos.NEGATIVE or not current or self.photos._answered_with(r['reason']) == current}
        ids, seen, offset = [], set(), 0
        while True:
            artists, total = self.repo.artist_identities(limit=300, offset=offset)
            for a in artists:
                mb = a.get('mb_artist_id')
                if mb and a.get('link_status') in ('linked', 'accepted') and mb not in known and mb not in seen:
                    seen.add(mb)
                    ids.append(mb)
            offset += len(artists)
            if not artists or offset >= total:
                return ids

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {'started': False, 'code': 'photo_batch_running'}
            ids = self.pending()
            self._cancel.clear()
            self._save({'state': 'running', 'done': 0, 'total': len(ids), 'counts': {}, 'started_at': self.clock()})
            self._thread = threading.Thread(target=self._run, args=(ids,), name='photo-batch', daemon=True)
            self._thread.start()
        return {'started': True, 'total': len(ids)}

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout=None) -> bool:
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _run(self, ids: list[str]) -> None:
        counts: dict = {}
        started = self.status().get('started_at')
        failures = done = 0
        state = 'done'
        for mb in ids:
            if self._cancel.is_set():
                state = 'cancelled'
                break
            try:
                result = self.photos.ensure(mb)['status']
            except Exception:
                result = 'failed'
            counts[result] = counts.get(result, 0) + 1
            done += 1
            failures = failures + 1 if result == 'failed' else 0
            self._save({'state': 'cancelling' if self._cancel.is_set() else 'running', 'done': done, 'total': len(ids),
                        'counts': counts, 'started_at': started})
            if result == 'no_contact':
                state = 'needs_contact'
                break
            if failures >= CONSECUTIVE_FAILURES:
                state = 'provider_unavailable'
                break
        self._save({'state': state, 'done': done, 'total': len(ids), 'counts': counts, 'started_at': started})
