"""Human decisions on MusicBrainz identity suggestions, kept apart from the provider's evidence.

* accept: the credit is linked, for the albums named, to one of the candidates the provider returned.
* reject: that artist is not this credit, for the albums named. A rejection is not offered again unless the
  provider's candidate set changes.
* review: keep it open and flag it for later.
* undo: reverses an earlier decision. Undo itself is never undone.

Scope: an accept or reject must name the albums it applies to, and only albums the credit really has. Two different
artists can share one credit string; deciding one album never decides the others. Decisions made before scopes existed
(scope = []) keep applying to every recording with the credit.

Every decision is appended to artist_link_decisions. Provider rows are never edited here.
"""
import json
import time

from meloshelf.domain.artist_identity import AUTO_SCORE, active_decisions, effective_link, is_historical
from meloshelf.domain.artists import canonical_key
from meloshelf.infrastructure.artist_identity_store import ArtistLinks
from meloshelf.infrastructure.file_identity import FileRegistry
from meloshelf.infrastructure.repository import LibraryRepository

OPEN_STATUSES = ('ambiguous', 'suggested')


class SuggestionError(ValueError):
    """A decision was refused; the message is safe to show to the user."""


def fingerprint(candidates) -> str:
    return ','.join(sorted({c['mb_artist_id'] for c in candidates}))


class SuggestionDecisions:
    def __init__(self, database):
        self.database = database

    def events(self) -> list[dict]:
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM artist_link_decisions ORDER BY id').fetchall()
        out = []
        for row in rows:
            event = dict(row)
            event['scope'] = json.loads(event.get('scope') or '[]')
            event['tracks'] = json.loads(event.get('tracks') or '[]')
            event['instances'] = json.loads(event.get('instances') or '[]')
            out.append(event)
        return out

    def by_key(self) -> dict:
        """Every decision, grouped by normalised credit, in log order."""
        grouped = {}
        for event in self.events():
            grouped.setdefault(event['credit_key'], []).append(event)
        return grouped

    _by_key = by_key

    def effective(self, credit: str, album: str | None = None, track_path: str | None = None,
                  instance_id: str | None = None):
        row = ArtistLinks(self.database).get(credit)
        album_key = canonical_key(album) if album is not None else None
        return effective_link(row, self.by_key().get(canonical_key(credit), []), album_key=album_key,
                              track_path=track_path, instance_id=instance_id)

    def backfill_instances(self) -> dict:
        """Attach file-instance ids to decisions that were written when recordings were keyed by path.
        Conservative: a path qualifies only if a present instance sits there and the file's modification time is not
        later than the decision, i.e. nothing suggests it is a different or changed file. Nothing else is applied.
        The decision itself (action, ID, paths, time) is never altered; only the derived `instances` list is filled."""
        registry = FileRegistry(self.database)
        instance_of = registry.instance_ids_by_path()
        summary = {'backfilled': 0, 'unresolved': 0}
        with self.database.write() as db:
            rows = db.execute("""SELECT id, at, tracks FROM artist_link_decisions
                                 WHERE action IN ('accept','reject') AND instances='[]' AND tracks<>'[]'""").fetchall()
            for row in rows:
                qualified = []
                for path in json.loads(row['tracks']):
                    instance = instance_of.get(path)
                    track = db.execute('SELECT mtime_ns FROM tracks WHERE path=?', (path,)).fetchone()
                    if instance and track and track['mtime_ns'] / 1e9 <= row['at']:
                        qualified.append(instance)
                if qualified:
                    db.execute('UPDATE artist_link_decisions SET instances=? WHERE id=?',
                               (json.dumps(sorted(set(qualified))), row['id']))
                    summary['backfilled'] += 1
                else:
                    summary['unresolved'] += 1
        return summary

    def decide(self, credit: str, action: str, mb_artist_id: str | None = None, albums=None, tracks=None) -> int:
        """Record a decision. An accept or reject names EITHER the albums it covers OR the exact recordings (file
        paths) it covers, never both and never neither. A recording decision is narrower and wins over an album one."""
        if action not in ('accept', 'reject', 'review'):
            raise SuggestionError('The action must be accept, reject or review.')
        row = ArtistLinks(self.database).get(credit)
        if row is None:
            raise SuggestionError('The provider has not answered for this name yet.')
        scope, recordings, instances = [], [], []
        if action in ('accept', 'reject'):
            offered = {c['mb_artist_id'] for c in row['candidates']}
            if mb_artist_id not in offered:
                raise SuggestionError("That artist was not one of the provider's candidates for this name.")
            if tracks and albums:
                raise SuggestionError('Choose albums or individual recordings for one decision, not both.')
            if tracks:
                recordings = _recordings_for(self.database, credit, tracks)
                # The decision follows the physical file: resolve each path to its instance id (created if needed).
                registry = FileRegistry(self.database)
                registry.reconcile(only=recordings)
                by_path = registry.instance_ids_by_path()
                if any(p not in by_path for p in recordings):
                    raise SuggestionError('A selected recording could not be identified as a file yet.')
                instances = sorted({by_path[p] for p in recordings})
            else:
                scope = _scope_for(self.database, credit, albums)
        else:
            mb_artist_id = None
        with self.database.write() as db:
            cursor = db.execute('''INSERT INTO artist_link_decisions(credit_key,action,mb_artist_id,ref,fingerprint,at,scope,tracks,instances)
                                   VALUES(?,?,?,NULL,?,?,?,?,?)''',
                                (canonical_key(credit), action, mb_artist_id, fingerprint(row['candidates']),
                                 time.time(), json.dumps(scope), json.dumps(recordings), json.dumps(instances)))
            return cursor.lastrowid

    def undo(self, event_id: int) -> int:
        with self.database.write() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM artist_link_decisions ORDER BY id').fetchall()]
            target = next((e for e in rows if e['id'] == event_id), None)
            if target is None or target['action'] == 'undo':
                raise SuggestionError('That decision cannot be undone.')
            if any(e['action'] == 'undo' and e['ref'] == event_id for e in rows):
                raise SuggestionError('That decision has already been undone.')
            cursor = db.execute('''INSERT INTO artist_link_decisions(credit_key,action,mb_artist_id,ref,fingerprint,at,scope,tracks)
                                   VALUES(?,?,?,?,?,?,'[]','[]')''',
                                (target['credit_key'], 'undo', None, event_id, '', time.time()))
            return cursor.lastrowid

    def open_suggestions(self, *, limit: int = 200) -> list[dict]:
        by_key = self.by_key()
        repo = LibraryRepository(self.database)
        items = []
        for row in ArtistLinks(self.database).all().values():
            if row['status'] not in OPEN_STATUSES:
                continue
            albums = repo.artist_albums(row['credit'])  # normalised key -> display name
            if not albums:
                continue
            candidates = json.loads(row['candidates'] or '[]')
            current = fingerprint(candidates)
            standing = active_decisions(by_key.get(row['credit_key'], []))
            covered = set()
            everywhere = False
            historical_accept = False
            for decision in standing:
                if decision['tracks'] or decision['instances']:
                    # recording-level decisions cover only those files, never a whole album; one that was written
                    # by path and has no file instance yet is kept and flagged, not applied
                    if decision['action'] == 'accept' and not decision['instances']:
                        historical_accept = True
                    continue
                if decision['action'] == 'accept' and is_historical(decision):
                    historical_accept = True  # kept and flagged; it does not decide anything any more
                    continue
                if decision['action'] == 'accept' or (decision['action'] == 'reject' and decision['fingerprint'] == current):
                    if decision['scope']:
                        covered |= set(decision['scope'])
                    elif decision['action'] == 'reject':
                        everywhere = True  # a historical reject blocks the link everywhere (the safe direction)
            undecided = sorted(name for key, name in albums.items() if not everywhere and key not in covered)
            if not undecided:
                continue  # every album of this string has a decision that still stands
            decided_last = [d for d in standing if d['action'] in ('accept', 'reject')]
            reviewed_after = any(d['action'] == 'review' and (not decided_last or d['id'] > decided_last[-1]['id'])
                                 for d in standing)
            recordings, total = repo.artist_recordings(row['credit'], limit=5)
            items.append({
                'credit': row['credit'], 'key': row['credit_key'], 'status': row['status'],
                'reason': row['reason'], 'candidates': candidates, 'tracks': total,
                'albums': sorted(albums.values()), 'undecided_albums': undecided,
                'recordings': [{'title': r['title'], 'album': r['album'], 'path': r['path']} for r in recordings],
                'evidence': _evidence(row['status'], candidates),
                'review_later': reviewed_after, 'fingerprint': current, 'legacy_accept': historical_accept,
            })
        items.sort(key=lambda s: (-s['tracks'], s['credit'].casefold()))
        return items[:limit]


def _scope_for(database, credit: str, albums) -> list[str]:
    """Normalised keys for the albums a decision names. Each must be an album this credit really has."""
    if not isinstance(albums, list) or not albums or not all(isinstance(a, str) and a.strip() for a in albums):
        raise SuggestionError('Choose the albums this decision applies to. A decision cannot cover every recording at once.')
    real = LibraryRepository(database).artist_albums(credit)
    keys = set()
    for album in albums:
        key = canonical_key(album)
        if key not in real:
            raise SuggestionError(f'"{album}" is not an album this name appears on in your library.')
        keys.add(key)
    return sorted(keys)


def _recordings_for(database, credit: str, tracks) -> list[str]:
    """File paths for a recording-level decision. Each must be a recording that really carries this credit."""
    from meloshelf.domain.artists import canonical_key as key_of, is_standalone, parse_credit
    if not isinstance(tracks, list) or not tracks or not all(isinstance(t, str) and t for t in tracks):
        raise SuggestionError('Choose at least one recording (file) for this decision.')
    wanted = key_of(credit)
    with database.read() as connection:
        marks = ','.join('?' * len(tracks))
        found = connection.execute(f'SELECT path, artist FROM tracks WHERE path IN ({marks})', tracks).fetchall()
        known = {key_of(r[0]) for r in connection.execute("SELECT DISTINCT artist FROM tracks WHERE artist<>''")
                 if is_standalone(r[0])}
    by_path = {r[0]: r[1] for r in found}
    for path in tracks:
        artist = by_path.get(path)
        if artist is None:
            raise SuggestionError('That recording is not in your library.')
        parsed = parse_credit(artist, known)
        if wanted not in {key_of(x) for x in parsed.primary + parsed.featured}:
            raise SuggestionError('That recording does not carry this artist name.')
    return sorted(set(tracks))


def _evidence(status: str, candidates: list[dict]) -> list[str]:
    lines = []
    exact = [c for c in candidates if canonical_key(c['name']) and c['score'] >= 80]
    if status == 'ambiguous':
        ids = {c['mb_artist_id'] for c in exact}
        lines.append(f'{len(ids)} different artist IDs match this name; they cannot be told apart by name alone')
    top = max(candidates, key=lambda c: c['score'], default=None)
    if top is not None:
        lines.append(f"strongest candidate: {top['name']} (score {top['score']})")
        if top['score'] < AUTO_SCORE:
            lines.append(f'score is below the automatic threshold of {AUTO_SCORE}')
    lines.append('a name match does not prove identity for common names: check the recordings and collaborators')
    return lines
