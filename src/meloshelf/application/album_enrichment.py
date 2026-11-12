"""Bulk album artwork enrichment: one cancellable, restartable background pass over the albums of the library.

Albums are looked up by CANONICAL primary artist and title (never by a collaboration string) through the album art
service, one catalogue request at a time. Every answer is cached as it arrives, so a cancel or a restart loses nothing and
a second run only asks about what is still unknown. Progress and the result are persisted for the page to show.
"""
import threading
import time

SETTING = 'album_enrichment'
IDLE = {'state': 'idle', 'done': 0, 'total': 0, 'counts': {}, 'updated_at': None}


class AlbumEnrichment:
    def __init__(self, store, repo, art, *, clock=time.time):
        self.store, self.repo, self.art, self.clock = store, repo, art, clock
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        saved = self.store.get_setting(SETTING, None)
        if isinstance(saved, dict) and saved.get('state') in ('running', 'cancelling'):      # the process that ran it is gone
            self._save({**saved, 'state': 'interrupted'})

    def status(self) -> dict:
        saved = self.store.get_setting(SETTING, None)
        return {**IDLE, **saved} if isinstance(saved, dict) else dict(IDLE)

    def _save(self, status: dict) -> None:
        self.store.set_setting(SETTING, {**status, 'updated_at': self.clock()})

    def pairs(self) -> list[tuple]:
        found, offset = [], 0
        while True:
            albums, total = self.repo.albums(limit=300, offset=offset, sort='title')
            found += [(a['title'], a.get('artist') or '', str(a.get('year') or '')[:4]) for a in albums if a.get('artist')]
            offset += len(albums)
            if not albums or offset >= total:
                return found

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {'started': False, 'code': 'album_enrichment_running'}
            pairs = self.pairs()
            self._cancel.clear()
            self._save({'state': 'running', 'done': 0, 'total': len(pairs), 'counts': {}, 'started_at': self.clock()})
            self._thread = threading.Thread(target=self._run, args=(pairs,), name='album-enrichment', daemon=True)
            self._thread.start()
        return {'started': True, 'total': len(pairs)}

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout=None) -> bool:
        thread = self._thread
        if thread:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _run(self, pairs) -> None:
        started = self.status().get('started_at')

        def progress(done, total, counts):
            self._save({'state': 'cancelling' if self._cancel.is_set() else 'running', 'done': done, 'total': total, 'counts': counts,
                        'started_at': started})
        try:
            summary = self.art.enrich(pairs, should_cancel=self._cancel.is_set, on_progress=progress)
            self._save({'state': 'cancelled' if summary['cancelled'] else 'done', 'done': summary['done'], 'total': summary['total'],
                        'counts': summary['counts'], 'started_at': started})
        except Exception:                      # the worker must always end in a recorded state
            current = self.status()
            self._save({**current, 'state': 'failed'})
            raise
