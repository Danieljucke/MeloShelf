"""Library-level metadata analysis: reads indexed tracks and their stored candidates, groups them by folder,
and returns correction groups with their scoped decision keys. Read-only with respect to the library.

Results are cached per library file. The cache key is a cheap fingerprint of the indexed library, so a scan
or an identification run changes it and forces a fresh analysis; ordinary interface polls reuse the result.
"""
import json
import posixpath
import threading

from meloshelf.domain.metadata import MetaRecord, analyse_folder
from meloshelf.infrastructure.metadata_store import MetadataStore

ACOUSTIC = 'ACOUSTICALLY_MATCHED'
_cache: dict = {}          # (library path, revision, folder or '') -> groups
_cache_lock = threading.Lock()
_MAX_ENTRIES = 32


def _folder(path: str) -> str:
    return posixpath.dirname(path.replace('\\', '/'))


class MetadataService:
    def __init__(self, database):
        self.database = database

    @classmethod
    def clear_cache(cls) -> None:
        with _cache_lock:
            _cache.clear()

    def revision(self) -> tuple:
        """A fingerprint of everything the analysis reads. Any scan or identification changes it."""
        with self.database.read() as db:
            tracks = db.execute('SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(MAX(mtime_ns),0), '
                                'COALESCE(SUM(LENGTH(title)+LENGTH(artist)+LENGTH(album)),0) FROM tracks').fetchone()
            matches = db.execute('SELECT COUNT(*), COALESCE(MAX(created_at),0) FROM match_results').fetchone()
        return tuple(tracks) + tuple(matches)

    def records(self, folder: str | None = None) -> list[MetaRecord]:
        with self.database.read() as db:
            rows = db.execute('''SELECT t.path, t.title, t.artist, t.album, m.data
                                 FROM tracks t LEFT JOIN match_results m ON m.path = t.path''').fetchall()
        records = []
        for row in rows:
            if folder and _folder(row['path']) != folder.replace('\\', '/'):
                continue
            top = {}
            if row['data']:
                candidates = json.loads(row['data']).get('candidates') or []
                top = candidates[0] if candidates else {}
            records.append(MetaRecord(
                path=row['path'], title=row['title'] or '', artist=row['artist'] or '', album=row['album'] or '',
                candidate_artist=top.get('artist', ''), candidate_title=top.get('title', ''),
                candidate_album='', candidate_status=ACOUSTIC if top else '',
                filename=row['path'].replace('\\', '/').rsplit('/', 1)[-1]))
        return records

    def groups(self, folder: str | None = None) -> list[dict]:
        scope = (str(self.database.path), self.revision(), (folder or '').replace('\\', '/'))
        with _cache_lock:
            cached = _cache.get(scope)
        if cached is not None:
            return [dict(g) for g in cached]
        computed = self._compute(folder)
        with _cache_lock:
            # Entries for older revisions of this library are dropped; the cache holds only the current library.
            for key in [k for k in _cache if k[0] == scope[0] and k[1] != scope[1]]:
                del _cache[key]
            if len(_cache) >= _MAX_ENTRIES:
                _cache.clear()
            _cache[scope] = computed
        return [dict(g) for g in computed]

    def _compute(self, folder: str | None) -> list[dict]:
        by_folder: dict[str, list[MetaRecord]] = {}
        for record in self.records(folder):
            by_folder.setdefault(_folder(record.path), []).append(record)
        out = []
        for name, records in sorted(by_folder.items()):
            report = analyse_folder(records)
            for group in report.groups:
                out.append({
                    'key': MetadataStore.key_for(name, group.field, group.original, group.classification),
                    'folder': name, 'field': group.field, 'original': group.original, 'proposed': group.proposed,
                    'classification': group.classification, 'paths': list(group.paths),
                    'confidence': group.confidence, 'individual_review': group.individual_review,
                    'reasons': list(group.reasons),
                })
        return out
