"""Album covers from verified releases, with manual correction.

resolve(album, artist)  'verified' only when the approved recordings of the album agree on exactly one release whose title
                        matches (see domain.artwork.verified_release); 'ambiguous' editions and 'unverified' albums get
                        no automatic cover.
cover(album, artist)    the image that applies: a person's choice if one stands, else the verified release's front cover
                        from the Cover Art Archive (through the existing bounded cache), else None.
set_manual / clear_manual / undo   a person's append-only choices (kind 'album'); nothing touches a music file.
The key is the normalised album and artist, so the same title by two artists stays separate.
"""
import hashlib
import json
import time
from pathlib import Path

from meloshelf.application.artist_photos import PhotoError
from meloshelf.domain.artists import canonical_key
from meloshelf.domain.artwork import EXTENSION, IMAGE_TYPES, sniff_image, verified_release

APPROVED = ('AUTO_APPROVED', 'USER_APPROVED')
MAX_IMAGE_BYTES = 1_000_000


def album_key(album: str, artist: str) -> str:
    return f'{canonical_key(album)}|{canonical_key(artist)}'


class AlbumCoverService:
    def __init__(self, database, cache, directory, *, max_image_bytes=MAX_IMAGE_BYTES, clock=time.time):
        self.database = database
        self.cache = cache
        self.directory = Path(directory)
        self.max_image_bytes = max_image_bytes
        self.clock = clock

    # ── which release ────────────────────────────────────────────────────────────

    def states(self, pairs) -> dict:
        """{(album, artist): verified_release(...) result} for many albums in two queries."""
        pairs = list({(a, b) for a, b in pairs})
        if not pairs:
            return {}
        albums = sorted({p[0] for p in pairs})
        artists = sorted({p[1] for p in pairs})
        lists = {p: [] for p in pairs}
        with self.database.read() as db:
            rows = db.execute(f"""SELECT t.album, t.artist, m.data, m.selected_recording_id FROM match_results m
                JOIN tracks t ON t.path = m.path
                WHERE m.recording_status IN ({','.join('?' * len(APPROVED))})
                  AND t.album IN ({','.join('?' * len(albums))}) AND t.artist IN ({','.join('?' * len(artists))})""",
                              [*APPROVED, *albums, *artists]).fetchall()
        for row in rows:
            pair = (row['album'], row['artist'])
            if pair not in lists:
                continue
            candidates = json.loads(row['data']).get('candidates') or []
            chosen = next((c for c in candidates if row['selected_recording_id']
                           and c.get('recording_id') == row['selected_recording_id']), None)
            if chosen is None and candidates:
                chosen = candidates[0]
            lists[pair].append((chosen or {}).get('release_details') or [])
        return {pair: verified_release(pair[0], releases) for pair, releases in lists.items()}

    def resolve(self, album: str, artist: str) -> dict:
        return self.states([(album, artist)])[(album, artist)]

    # ── what applies now ─────────────────────────────────────────────────────────

    def decisions(self, key: str | None = None) -> list[dict]:
        with self.database.read() as db:
            if key is None:
                rows = db.execute("SELECT * FROM artwork_decisions WHERE kind='album' ORDER BY id").fetchall()
            else:
                rows = db.execute("SELECT * FROM artwork_decisions WHERE kind='album' AND key=? ORDER BY id",
                                  (key,)).fetchall()
        return [dict(r) for r in rows]

    def _standing(self, album: str, artist: str):
        log = self.decisions(album_key(album, artist))
        undone = {d['ref'] for d in log if d['action'] == 'undo'}
        standing = [d for d in log if d['action'] in ('set', 'clear') and d['id'] not in undone]
        return standing[-1] if standing else None

    def cover(self, album: str, artist: str):
        standing = self._standing(album, artist)
        if standing is not None:
            if standing['action'] == 'clear':
                return None
            try:
                return standing['content_type'], (self.directory / standing['file_key']).read_bytes()
            except OSError:
                return None
        state = self.resolve(album, artist)
        if state['state'] == 'verified':
            return self.cache.cover(state['release_id'])
        if state['state'] == 'verified_group':
            return self.cache.cover(state['group_id'], group=True)   # the album is verified; its edition is not
        return None

    def info(self, album: str, artist: str) -> dict:
        state = self.resolve(album, artist)
        out = {'state': state['state'], 'reason': state['reason'], 'release_ids': state['release_ids']}
        standing = self._standing(album, artist)
        if standing is not None:
            out.update({'provenance': 'user', 'cleared': standing['action'] == 'clear', 'event_id': standing['id'],
                        'note': standing['note']})
        elif state['state'] == 'verified':
            out.update({'provenance': 'coverartarchive', 'release_id': state['release_id'],
                        'source_url': 'https://musicbrainz.org/release/' + state['release_id']})
        elif state['state'] == 'verified_group':
            out.update({'provenance': 'coverartarchive', 'group_id': state['group_id'], 'edition': 'unknown',
                        'source_url': 'https://musicbrainz.org/release-group/' + state['group_id']})
        return out

    def summaries(self, pairs) -> dict:
        """What the Albums view needs for a page: the state, and a cover URL path only where one applies."""
        pairs = list(pairs)
        states = self.states(pairs)
        keys = sorted({album_key(a, b) for a, b in pairs})
        with self.database.read() as db:
            log = [dict(r) for r in db.execute(
                f"SELECT * FROM artwork_decisions WHERE kind='album' AND key IN ({','.join('?' * len(keys))}) ORDER BY id",
                keys)] if keys else []
        undone = {d['ref'] for d in log if d['action'] == 'undo'}
        standing = {}
        for d in log:
            if d['action'] in ('set', 'clear') and d['id'] not in undone:
                standing[d['key']] = d
        out = {}
        for pair in pairs:
            state = states[pair]
            choice = standing.get(album_key(*pair))
            item = {'state': state['state'], 'reason': state['reason'], 'has_cover': False}
            if choice is not None:
                item.update({'provenance': 'user', 'event_id': choice['id'], 'has_cover': choice['action'] == 'set'})
            elif state['state'] == 'verified':
                item.update({'provenance': 'coverartarchive', 'release_id': state['release_id'], 'has_cover': True,
                             'source_url': 'https://musicbrainz.org/release/' + state['release_id']})
            elif state['state'] == 'verified_group':
                item.update({'provenance': 'coverartarchive', 'group_id': state['group_id'], 'has_cover': True,
                             'edition': 'unknown',
                             'source_url': 'https://musicbrainz.org/release-group/' + state['group_id']})
            out[pair] = item
        return out

    # ── a person's choices ───────────────────────────────────────────────────────

    def set_manual(self, album: str, artist: str, content_type: str, data: bytes, note: str = '') -> int:
        if content_type not in IMAGE_TYPES:
            raise PhotoError('Use a JPEG or PNG image.')
        if sniff_image(data) != content_type:
            raise PhotoError('That file is not a real JPEG or PNG image.')
        if len(data) > self.max_image_bytes:
            raise PhotoError(f'The image is larger than {self.max_image_bytes // 1000} KB.')
        key = hashlib.sha256(data).hexdigest() + '.' + EXTENSION[content_type]
        self.directory.mkdir(parents=True, exist_ok=True)
        if not (self.directory / key).exists():
            (self.directory / key).write_bytes(data)
        return self._decide(album, artist, 'set', file_key=key, content_type=content_type, size=len(data), note=note[:200])

    def clear_manual(self, album: str, artist: str, note: str = '') -> int:
        return self._decide(album, artist, 'clear', note=note[:200])

    def undo(self, event_id: int) -> int:
        with self.database.write() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM artwork_decisions WHERE kind='album' ORDER BY id")]
            target = next((r for r in rows if r['id'] == event_id), None)
            if target is None or target['action'] == 'undo':
                raise PhotoError('That choice cannot be undone.')
            if any(r['action'] == 'undo' and r['ref'] == event_id for r in rows):
                raise PhotoError('That choice has already been undone.')
            cursor = db.execute('''INSERT INTO artwork_decisions(kind,key,action,source,ref,at)
                                   VALUES('album',?,'undo','user',?,?)''', (target['key'], event_id, self.clock()))
            return cursor.lastrowid

    def _decide(self, album, artist, action, **fields) -> int:
        if not isinstance(album, str) or not isinstance(artist, str) or not album.strip():
            raise PhotoError('An album and an artist are required.')
        with self.database.write() as db:
            cursor = db.execute('''INSERT INTO artwork_decisions(kind,key,action,source,file_key,content_type,size,note,at)
                                   VALUES('album',?,?,'user',?,?,?,?,?)''',
                                (album_key(album, artist), action, fields.get('file_key', ''),
                                 fields.get('content_type', ''), fields.get('size', 0), fields.get('note', ''),
                                 self.clock()))
            return cursor.lastrowid
