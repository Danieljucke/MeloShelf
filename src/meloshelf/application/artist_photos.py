"""Artist photographs: licensed, attributed, verified identities only, cached within a bound, correctable by a person.

ensure(mb_id)   fetch and cache a photograph, but only for a verified artist ID: a strong provider link or a person's
                accept. Suggested, ambiguous and unlinked identities get nothing (the page shows initials).
image(mb_id)    the image that applies now: a person's choice if one stands, otherwise the cached provider photograph.
info(mb_id)     attribution (author, licence, source page) and provenance for the image that applies.
set_manual / clear_manual / undo   a person's append-only choices; nothing is written into any music file.

A missing photograph or a refused licence is remembered; a provider failure is not, so a later attempt can succeed.
"""
import hashlib
import logging
import time
from pathlib import Path

from meloshelf.domain.artist_identity import active_decisions
from meloshelf.domain.artwork import EXTENSION, IMAGE_TYPES, UUID, license_acceptable, sniff_image
from meloshelf.infrastructure.art_sources import SourceError
from meloshelf.infrastructure.music_api import APIError

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 100_000_000


class PhotoError(ValueError):
    """A request was refused; the message is safe to show to the user."""


class ArtistPhotoService:
    def __init__(self, database, directory, provider, *, max_total_bytes=MAX_TOTAL_BYTES,
                 max_image_bytes=MAX_IMAGE_BYTES, clock=time.time, extras=None):
        self.database = database
        self._extras = extras                         # optional fallback providers (Fanart.tv, TheAudioDB), never the first choice
        self.directory = Path(directory)
        self._provider = provider
        self.max_total_bytes = max_total_bytes
        self.max_image_bytes = max_image_bytes
        self.clock = clock

    def provider(self):
        return self._provider() if callable(self._provider) else self._provider

    # ── verification ─────────────────────────────────────────────────────────────

    def verified(self, mb_id: str) -> bool:
        """A strong provider link, or a person's standing accept, names this artist ID."""
        from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions
        with self.database.read() as db:
            linked = db.execute("SELECT 1 FROM artist_links WHERE mb_artist_id=? AND status='linked' LIMIT 1",
                                (mb_id,)).fetchone()
        if linked:
            return True
        return any(d['action'] == 'accept' and d['mb_artist_id'] == mb_id
                   for d in active_decisions(SuggestionDecisions(self.database).events()))

    # ── fetching ─────────────────────────────────────────────────────────────────

    NEGATIVE = ('none', 'rejected_license', 'rejected_image')

    def extras(self) -> list:
        return list((self._extras() if callable(self._extras) else self._extras) or [])

    def _sources_marker(self) -> str:
        names = sorted(c.name for c in self.extras())
        return f' [sources:{",".join(names)}]' if names else ''

    def _answered_with(self, reason: str) -> str:
        """The optional sources a stored negative answer was given with ('' for the original Commons-only answer)."""
        import re
        found = re.search(r'\[sources:([^\]]*)\]', reason or '')
        return found.group(1) if found else ''

    def ensure(self, mb_id: str) -> dict:
        if not isinstance(mb_id, str) or not UUID.match(mb_id):
            raise PhotoError('That is not a MusicBrainz artist ID.')
        mb_id = mb_id.lower()
        with self.database.read() as db:
            existing = db.execute('SELECT status, reason FROM artist_images WHERE mb_artist_id=?', (mb_id,)).fetchone()
        if existing:
            # A stored 'no photograph' is final unless a new optional source has been switched on since it was given.
            current = ','.join(sorted(c.name for c in self.extras()))
            if not (existing['status'] in self.NEGATIVE and current and self._answered_with(existing['reason']) != current):
                return {'status': existing['status']}
        if not self.verified(mb_id):
            return {'status': 'unverified', 'reason': 'This artist identity is not verified, so no photograph is fetched.'}
        provider = self.provider()
        if provider is None and not self.extras():
            return {'status': 'no_contact', 'reason': 'Save a MusicBrainz contact in Settings to fetch photographs.'}
        wiki = {'status': 'none', 'reason': ''}
        if provider is not None:
            try:
                wiki = provider.find(mb_id)
            except APIError as exc:
                return {'status': 'failed', 'reason': str(exc)}
        if wiki.get('status') == 'found' and license_acceptable(wiki.get('license', '')):
            return self._fetch_wikimedia(mb_id, provider, wiki)
        outcome = self._try_extras(mb_id)
        if outcome is not None:
            return outcome
        marker = self._sources_marker()
        if wiki.get('status') == 'found':
            self._record(mb_id, 'rejected_license', license=wiki.get('license', ''), author=wiki.get('author', ''),
                         source_url=wiki.get('source_url', ''), reason='the licence does not allow reuse' + marker)
            return {'status': 'rejected_license'}
        self._record(mb_id, 'none', reason=(wiki.get('reason', '') or '') + marker)
        return {'status': 'none'}

    def _try_extras(self, mb_id: str):
        """Optional providers, in order. A rate limit or outage stops the attempt without storing anything; an answer that cannot
        be trusted (wrong host, not an image) is skipped. Returns a result dict, or None when no provider had a usable photograph."""
        for client in self.extras():
            try:
                found = client.artist_thumb(mb_id)
                if not found:
                    continue
                declared, data = client.download(found['url'])
            except APIError as exc:
                return {'status': 'failed', 'reason': str(exc)}
            except SourceError:
                continue
            real = sniff_image(data)
            if real is None or len(data) > self.max_image_bytes:
                continue
            self._record(mb_id, 'available', provenance=client.name, file_key=self._store(data, real), content_type=real, size=len(data),
                         license=found.get('license', ''), author=found.get('author', ''), source_url=found.get('source_url', ''))
            self._evict()
            return {'status': 'available', 'provenance': client.name}
        return None

    def _fetch_wikimedia(self, mb_id: str, provider, found: dict) -> dict:
        try:
            declared, data = provider.download(found['thumb_url'])
        except APIError as exc:
            return {'status': 'failed', 'reason': str(exc)}
        real = sniff_image(data)
        if real is None or real != declared or len(data) > self.max_image_bytes:
            self._record(mb_id, 'rejected_image', reason='not a JPEG or PNG within the size limit',
                         source_url=found.get('source_url', ''))
            return {'status': 'rejected_image'}
        file_key = self._store(data, real)
        self._record(mb_id, 'available', file_key=file_key, content_type=real, size=len(data),
                     license=found.get('license', ''), author=found.get('author', ''),
                     source_url=found.get('source_url', ''), source_file=found.get('file', ''))
        self._evict()
        return {'status': 'available'}

    def _store(self, data: bytes, content_type: str) -> str:
        key = hashlib.sha256(data).hexdigest() + '.' + EXTENSION[content_type]
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / key
        if not path.exists():
            path.write_bytes(data)
        return key

    def _record(self, mb_id, status, **fields):
        with self.database.write() as db:
            db.execute('''INSERT OR REPLACE INTO artist_images(mb_artist_id,status,provenance,file_key,content_type,size,
                          license,author,source_url,source_file,reason,fetched_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (mb_id, status, fields.get('provenance', 'wikimedia'), fields.get('file_key', ''), fields.get('content_type', ''), fields.get('size', 0),
                        fields.get('license', ''), fields.get('author', ''), fields.get('source_url', ''),
                        fields.get('source_file', ''), fields.get('reason', ''), self.clock()))

    def total_bytes(self) -> int:
        with self.database.read() as db:
            return db.execute("SELECT COALESCE(SUM(size),0) FROM artist_images WHERE status='available'").fetchone()[0]

    def _evict(self):
        """Keep the provider cache within its bound by removing the oldest images first."""
        while self.total_bytes() > self.max_total_bytes:
            with self.database.write() as db:
                row = db.execute("""SELECT mb_artist_id, file_key FROM artist_images WHERE status='available'
                                    ORDER BY fetched_at LIMIT 1""").fetchone()
                if row is None:
                    return
                db.execute('DELETE FROM artist_images WHERE mb_artist_id=?', (row['mb_artist_id'],))
                shared = db.execute('SELECT 1 FROM artist_images WHERE file_key=? LIMIT 1', (row['file_key'],)).fetchone()
            if not shared:
                try:
                    (self.directory / row['file_key']).unlink()
                except OSError:
                    pass

    # ── what applies now ─────────────────────────────────────────────────────────

    def decisions(self, key: str | None = None) -> list[dict]:
        with self.database.read() as db:
            if key is None:
                rows = db.execute("SELECT * FROM artwork_decisions WHERE kind='artist' ORDER BY id").fetchall()
            else:
                rows = db.execute("SELECT * FROM artwork_decisions WHERE kind='artist' AND key=? ORDER BY id",
                                  (key,)).fetchall()
        return [dict(r) for r in rows]

    def _standing(self, mb_id: str):
        """The person's latest standing set or clear for this artist, or None."""
        log = self.decisions(mb_id.lower())
        undone = {d['ref'] for d in log if d['action'] == 'undo'}
        standing = [d for d in log if d['action'] in ('set', 'clear') and d['id'] not in undone]
        return standing[-1] if standing else None

    def _provider_row(self, mb_id: str):
        with self.database.read() as db:
            row = db.execute("SELECT * FROM artist_images WHERE mb_artist_id=? AND status='available'",
                             (mb_id.lower(),)).fetchone()
        return dict(row) if row else None

    def image(self, mb_id: str):
        """(content_type, bytes) for the image that applies, or None (the page then shows initials)."""
        standing = self._standing(mb_id)
        if standing is not None:
            if standing['action'] == 'clear':
                return None
            return self._read(standing['file_key'], standing['content_type'])
        row = self._provider_row(mb_id)
        return self._read(row['file_key'], row['content_type']) if row else None

    def _read(self, file_key: str, content_type: str):
        try:
            return content_type, (self.directory / file_key).read_bytes()
        except OSError:
            return None

    def info(self, mb_id: str) -> dict | None:
        standing = self._standing(mb_id)
        if standing is not None:
            if standing['action'] == 'clear':
                return None
            return {'provenance': 'user', 'license': '', 'author': 'chosen by you', 'source_url': '',
                    'note': standing['note'], 'event_id': standing['id']}
        row = self._provider_row(mb_id)
        if not row:
            return None
        return {'provenance': row['provenance'], 'license': row['license'], 'author': row['author'],
                'source_url': row['source_url'], 'source_file': row['source_file']}

    def summaries(self, mb_ids) -> dict:
        """info() for many IDs, for the Artists view: {mb_id: info} only where an image applies."""
        keys = sorted({m.lower() for m in mb_ids if m})
        if not keys:
            return {}
        marks = ','.join('?' * len(keys))
        with self.database.read() as db:   # two queries for the whole page; no image bytes are read
            rows = {r['mb_artist_id']: dict(r) for r in db.execute(
                f"SELECT * FROM artist_images WHERE status='available' AND mb_artist_id IN ({marks})", keys)}
            log = [dict(r) for r in db.execute(
                f"SELECT * FROM artwork_decisions WHERE kind='artist' AND key IN ({marks}) ORDER BY id", keys)]
        undone = {d['ref'] for d in log if d['action'] == 'undo'}
        standing = {}
        for d in log:
            if d['action'] in ('set', 'clear') and d['id'] not in undone:
                standing[d['key']] = d
        out = {}
        for mb in keys:
            choice = standing.get(mb)
            if choice is not None:
                if choice['action'] == 'set':
                    out[mb] = {'provenance': 'user', 'license': '', 'author': 'chosen by you',
                               'source_url': '', 'note': choice['note'], 'event_id': choice['id']}
            elif mb in rows:
                row = rows[mb]
                out[mb] = {'provenance': row['provenance'], 'license': row['license'], 'author': row['author'],
                           'source_url': row['source_url'], 'source_file': row['source_file']}
        return out

    # ── a person's choices ───────────────────────────────────────────────────────

    def set_manual(self, mb_id: str, content_type: str, data: bytes, note: str = '') -> int:
        if content_type not in IMAGE_TYPES:
            raise PhotoError('Use a JPEG or PNG image.')
        if sniff_image(data) != content_type:
            raise PhotoError('That file is not a real JPEG or PNG image.')
        if len(data) > self.max_image_bytes:
            raise PhotoError(f'The image is larger than {self.max_image_bytes // 1000} KB.')
        key = self._store(data, content_type)
        return self._decide(mb_id, 'set', file_key=key, content_type=content_type, size=len(data), note=note[:200])

    def clear_manual(self, mb_id: str, note: str = '') -> int:
        return self._decide(mb_id, 'clear', note=note[:200])

    def undo(self, event_id: int) -> int:
        with self.database.write() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM artwork_decisions WHERE kind='artist' ORDER BY id")]
            target = next((r for r in rows if r['id'] == event_id), None)
            if target is None or target['action'] == 'undo':
                raise PhotoError('That choice cannot be undone.')
            if any(r['action'] == 'undo' and r['ref'] == event_id for r in rows):
                raise PhotoError('That choice has already been undone.')
            cursor = db.execute('''INSERT INTO artwork_decisions(kind,key,action,source,ref,at)
                                   VALUES('artist',?,'undo','user',?,?)''', (target['key'], event_id, self.clock()))
            return cursor.lastrowid

    def _decide(self, mb_id, action, **fields) -> int:
        if not isinstance(mb_id, str) or not UUID.match(mb_id):
            raise PhotoError('That is not a MusicBrainz artist ID.')
        with self.database.write() as db:
            cursor = db.execute('''INSERT INTO artwork_decisions(kind,key,action,source,file_key,content_type,size,note,at)
                                   VALUES('artist',?,?,'user',?,?,?,?,?)''',
                                (mb_id.lower(), action, fields.get('file_key', ''), fields.get('content_type', ''),
                                 fields.get('size', 0), fields.get('note', ''), self.clock()))
            return cursor.lastrowid
