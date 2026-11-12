"""Persistent, indexed local library. No file modification operations exist here."""
from collections.abc import Iterable
from pathlib import Path

from meloshelf.domain.models import Track
from meloshelf.infrastructure.database import Database, as_database


def _candidate_aliases(link, mb_id, limit=8):
    """Aliases the provider listed for exactly this artist ID (never for another candidate), bounded."""
    import json
    for candidate in json.loads((link or {}).get('candidates') or '[]'):
        if candidate.get('mb_artist_id') == mb_id:
            return list(candidate.get('aliases') or [])[:limit]
    return []


def _candidate_name(link, mb_id):
    """The provider's name for this artist ID, from the stored candidates; the stored name if none matches."""
    import json
    if not link:
        return ''
    for candidate in json.loads(link.get('candidates') or '[]'):
        if candidate.get('mb_artist_id') == mb_id:
            return candidate.get('name') or link.get('mb_name') or ''
    return link.get('mb_name') or ''


class LibraryRepository:
    """Stateless facade: holds no connection, so one instance is safe across threads."""

    def __init__(self, database: Database | Path | str):
        self.database = as_database(database)

    def save_many(self, tracks: Iterable[Track], *, connection=None) -> None:
        """Upsert tracks; pass ``connection`` to make it part of a larger transaction."""
        rows = [(t.path, t.size, t.extension, t.mtime_ns, t.title, t.artist, t.album, t.year, t.status,
                 t.track_no, t.track_total, t.disc_no, t.disc_total, int(t.numbers_indexed),
                 None if t.has_picture is None else int(t.has_picture)) for t in tracks]
        if connection is not None:
            return self._save(connection, rows)
        with self.database.write() as own:
            self._save(own, rows)

    @staticmethod
    def _save(connection, rows) -> None:
        connection.executemany('''
          INSERT INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status,
                             track_no,track_total,disc_no,disc_total,numbers_indexed,embedded_art)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(path) DO UPDATE SET
             size=excluded.size, extension=excluded.extension, mtime_ns=excluded.mtime_ns,
             title=excluded.title, artist=excluded.artist, album=excluded.album,
             year=excluded.year, status=excluded.status, track_no=excluded.track_no,
             track_total=excluded.track_total, disc_no=excluded.disc_no, disc_total=excluded.disc_total,
             numbers_indexed=excluded.numbers_indexed, embedded_art=excluded.embedded_art
        ''', rows)

    def counts(self) -> dict[str,int]:
        result = {'total':0,'tagged':0,'review':0,'unidentified':0}
        with self.database.read() as connection:
            for row in connection.execute('SELECT status, COUNT(*) AS n FROM tracks GROUP BY status'):
                result[row['status']] = row['n']
                result['total'] += row['n']
        return result

    @classmethod
    def _track_filter(cls, query: str, status: str, album: str = '', artist: str = '') -> tuple[str, list]:
        clauses, values = [], []
        if album:
            clauses.append('t.album=?')
            values.append(album)
        if artist:
            clauses.append('t.artist=?')
            values.append(artist)
        if query:
            clauses.append("(t.title LIKE ? ESCAPE '\\' OR t.artist LIKE ? ESCAPE '\\' OR t.path LIKE ? ESCAPE '\\')")
            values += [cls._like(query)] * 3
        if status:
            clauses.append('t.status=?')
            values.append(status)
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), values

    def list_tracks(self, *, limit:int=60, offset:int=0, query:str='', status:str='', album:str='', artist:str='') -> list[dict]:
        """One page of tracks, each with its identification state for the current file version. Within one album the
        order is disc, then track number (files without numbers last, by path); otherwise by path."""
        limit = min(300, max(1, limit))
        where, values = self._track_filter(query, status, album, artist)
        order = ('(t.track_no IS NULL AND t.disc_no IS NULL), COALESCE(t.disc_no,0), COALESCE(t.track_no,0), t.path'
                 if album else 't.path')
        with self.database.read() as connection:
            rows = connection.execute(f'''SELECT t.*, m.status AS match_status, m.decision AS decision
              FROM tracks t {self._CURRENT_MATCH}{where} ORDER BY {order} LIMIT ? OFFSET ?''', (*values, limit, offset))
            tracks = []
            for row in rows:
                track = dict(row)
                track.pop('numbers_indexed', None)
                match = track.pop('match_status')
                track['identification'] = {'review': 'proposal', 'unmatched': 'unknown'}.get(match, 'awaiting')
                track['decision'] = track['decision'] or ''
                tracks.append(track)
            return tracks

    def count_tracks(self, *, query:str='', status:str='', album:str='', artist:str='') -> int:
        where, values = self._track_filter(query, status, album, artist)
        with self.database.read() as connection:
            return connection.execute(f'SELECT COUNT(*) FROM tracks t{where}', values).fetchone()[0]

    def artists(self, *, limit:int=60, offset:int=0, query:str='') -> tuple[list[dict], int]:
        """Artists across the whole index (not one page of tracks), most tracks first."""
        where, values = "WHERE artist<>''", []
        if query:
            where += " AND artist LIKE ? ESCAPE '\\'"
            values.append(self._like(query))
        with self.database.read() as connection:
            total = connection.execute(f'SELECT COUNT(DISTINCT artist) FROM tracks {where}', values).fetchone()[0]
            rows = connection.execute(f'''SELECT artist AS name, COUNT(*) AS tracks,
                COUNT(DISTINCT NULLIF(album,'')) AS albums FROM tracks {where}
                GROUP BY artist ORDER BY tracks DESC, artist COLLATE NOCASE LIMIT ? OFFSET ?''',
                (*values, min(300, max(1, limit)), offset))
            return [dict(row) for row in rows], total

    def _artist_state(self):
        """Current artist corrections. An empty log means no correction applies."""
        from meloshelf.infrastructure.artist_corrections import ArtistCorrections
        return ArtistCorrections(self.database).state()

    def credit_analyser(self, credits=None):
        """A function credit text -> CreditAnalysis using what the library knows: artists that stand alone, names credited
        first in several credits, and names MusicBrainz reports as one artist. Computed once per call."""
        from meloshelf.domain.artists import canonical_key
        from meloshelf.domain.primary_artist import analyse_credit, known_artists
        from meloshelf.infrastructure.artist_identity_store import ArtistLinks
        if credits is None:
            with self.database.read() as connection:
                credits=[r[0] for r in connection.execute("SELECT DISTINCT artist FROM tracks WHERE artist<>''").fetchall()]
        known=known_artists(credits)
        groups={key for key,link in ArtistLinks(self.database).all().items()
                if link and link.get('status')=='linked' and canonical_key(link.get('mb_name') or '')==key}
        cache={}
        def analyse(text):
            if text not in cache:
                cache[text]=analyse_credit(text,known=known,groups=groups)
            return cache[text]
        return analyse

    def _credit_strings_for(self, connection, name:str) -> list:
        """Every stored credit string in which this name is the primary or a collaborating artist."""
        from meloshelf.domain.artists import canonical_key
        credits=[r[0] for r in connection.execute("SELECT DISTINCT artist FROM tracks WHERE artist<>''").fetchall()]
        analyse=self.credit_analyser(credits)
        wanted=canonical_key(name)
        return [c for c in credits if wanted in {canonical_key(x) for x in (analyse(c).primary,*analyse(c).collaborators)}]

    @staticmethod
    def _search_clause(query:str):
        """A case-insensitive match on title, album or file path. Empty query matches everything."""
        text=' '.join((query or '').split()).casefold()
        if not text:
            return '',[]
        like=f"%{text}%"
        return ("AND (lower(title) LIKE ? ESCAPE '\\' OR lower(album) LIKE ? ESCAPE '\\' OR lower(path) LIKE ? ESCAPE '\\')",
                [like,like,like])

    def credit_recordings(self, name:str, *, query:str='', offset:int=0, limit:int=50) -> tuple[list[dict], int]:
        """One page of the recordings a name appears on, with the filtered total. Paths are the current file key;
        the stable identity layer will sit beside them."""
        limit=max(1,min(int(limit),200)); offset=max(0,int(offset))
        with self.database.read() as connection:
            matching=self._credit_strings_for(connection,name)
            if not matching:
                return [],0
            marks=','.join('?'*len(matching))
            clause,extra=self._search_clause(query)
            total=connection.execute(f'SELECT COUNT(*) FROM tracks WHERE artist IN ({marks}) {clause}',
                                     [*matching,*extra]).fetchone()[0]
            rows=connection.execute(f"""SELECT path,title,artist,album FROM tracks WHERE artist IN ({marks}) {clause}
                ORDER BY album COLLATE NOCASE, path LIMIT ? OFFSET ?""",
                [*matching,*extra,limit,offset]).fetchall()
        return [dict(r) for r in rows],total

    def credit_recording_paths(self, name:str, *, query:str='', cap:int=5000) -> tuple[list[str], int]:
        """Every matching path, up to cap, for an explicit 'select all matching' action. Reports the true total."""
        cap=max(1,min(int(cap),5000))
        with self.database.read() as connection:
            matching=self._credit_strings_for(connection,name)
            if not matching:
                return [],0
            marks=','.join('?'*len(matching))
            clause,extra=self._search_clause(query)
            total=connection.execute(f'SELECT COUNT(*) FROM tracks WHERE artist IN ({marks}) {clause}',
                                     [*matching,*extra]).fetchone()[0]
            rows=connection.execute(f"""SELECT path FROM tracks WHERE artist IN ({marks}) {clause}
                ORDER BY album COLLATE NOCASE, path LIMIT ?""",[*matching,*extra,cap]).fetchall()
        return [r[0] for r in rows],total

    def artist_albums(self, name:str) -> dict:
        """Album names this name appears on (primary or featured), keyed by normalised album name."""
        from meloshelf.domain.artists import canonical_key
        with self.database.read() as connection:
            matching=self._credit_strings_for(connection,name)
            if not matching:
                return {}
            marks=','.join('?'*len(matching))
            rows=connection.execute(f"SELECT DISTINCT album FROM tracks WHERE artist IN ({marks})",matching).fetchall()
        return {canonical_key(r[0]):r[0] for r in rows}

    def artist_recordings(self, name:str, *, limit:int=200) -> tuple[list[dict], int]:
        """Recordings where this name is a primary or featured artist, and the total count."""
        with self.database.read() as connection:
            matching=self._credit_strings_for(connection,name)
            if not matching:
                return [],0
            marks=','.join('?'*len(matching))
            total=connection.execute(f'SELECT COUNT(*) FROM tracks WHERE artist IN ({marks})',matching).fetchone()[0]
            rows=connection.execute(f"""SELECT path,title,artist,album FROM tracks WHERE artist IN ({marks})
                ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, path LIMIT ?""",
                (*matching,min(500,max(1,limit)))).fetchall()
        return [dict(r) for r in rows],total

    def artist_identities(self, *, limit:int=60, offset:int=0, query:str='') -> tuple[list[dict], int]:
        """Canonical artists: variants merged, collaborations split, featured appearances counted.

        Raw tags are never rewritten; grouping is computed from the distinct credit strings,
        so the cost follows the number of distinct credits rather than the number of tracks.
        """
        from collections import Counter, defaultdict
        from meloshelf.domain.artists import canonical_key
        from meloshelf.infrastructure.artist_identity_store import ArtistLinks
        with self.database.read() as connection:
            rows=connection.execute("""SELECT artist, album, COUNT(*) AS n FROM tracks
                WHERE artist<>'' GROUP BY artist, album""").fetchall()
        analyse=self.credit_analyser(sorted({artist for artist,_,_ in rows}))
        state=self._artist_state()
        links=ArtistLinks(self.database).all()
        def fix(name):
            return (state.resolve(name) if state else None) or name
        separated=state.separated if state else set()
        claims=defaultdict(set)   # artist ID -> credit keys that hold it
        resolved={}
        from meloshelf.domain.artist_identity import effective_link
        from meloshelf.infrastructure.artist_suggestions import SuggestionDecisions
        decisions=SuggestionDecisions(self.database).by_key()
        # Credits with recording-level decisions are decided one file at a time; every other credit per album.
        track_keys={k for k,ds in decisions.items() if any(d['tracks'] or d.get('instances') for d in ds)}
        instance_of={}
        if track_keys:
            from meloshelf.infrastructure.file_identity import FileRegistry
            instance_of=FileRegistry(self.database).instance_ids_by_path()   # path -> the physical file's local id
            with self.database.read() as connection:
                units=[(a,al,1,p) for p,a,al in connection.execute(
                    "SELECT path, artist, album FROM tracks WHERE artist<>''").fetchall()]
        else:
            units=[(a,al,n,None) for a,al,n in rows]
        def identity(name,album,path=None):
            # Groups by artist ID only where the effective link says so, for this album (or this recording, where a
            # recording-level decision exists). Narrower decisions win. Anything else groups by normalised spelling.
            # A human keep-separate decision always wins.
            key=canonical_key(name)
            album_key=canonical_key(album)
            track_path=path if key in track_keys else None
            cache_key=(key,album_key,track_path)
            if cache_key in resolved:
                return resolved[cache_key]
            link=links.get(key)
            # A recording-level decision follows the physical file: a path with no file instance matches nothing.
            instance_id=instance_of.get(track_path,'') if track_path is not None else None
            state_,mb_id=effective_link(link,decisions.get(key,[]),album_key=album_key,track_path=track_path,
                                        instance_id=instance_id)
            if state_ in ('linked','accepted') and mb_id:
                if any(frozenset({key,holder}) in separated for holder in claims[mb_id]):
                    result=('key:'+key,None)
                else:
                    claims[mb_id].add(key)
                    shown=dict(link,status=state_,mb_artist_id=mb_id,mb_name=_candidate_name(link,mb_id))
                    result=('mb:'+mb_id,shown)
            elif state_=='rejected' and link:
                result=('key:'+key,dict(link,status='rejected',mb_artist_id=None))
            else:
                result=('key:'+key,link)
            resolved[cache_key]=result
            return result
        tracks,albums,featured=Counter(),defaultdict(set),Counter()
        displays=defaultdict(Counter)
        credits=defaultdict(Counter)
        collaborators=defaultdict(Counter)
        link_of={}
        for artist,album,n,path in units:
            credit=analyse(artist)
            primary_name=fix(credit.primary)
            primary,plink=identity(primary_name,album,path)
            link_of.setdefault(primary,plink)
            tracks[primary]+=n
            albums[primary].add(canonical_key(album))
            displays[primary][primary_name]+=n
            credits[primary][credit.display]+=n
            for name in credit.collaborators:
                name=fix(name)
                key,flink=identity(name,album,path)
                link_of.setdefault(key,flink)
                featured[key]+=n
                displays[key].setdefault(name,0)
                collaborators[primary][key]+=n
        keys=set(tracks)|set(featured)
        display_of={}
        for key in keys:
            link=link_of.get(key)
            if link and link['status'] in ('linked','accepted') and link['mb_name']:
                display_of[key]=link['mb_name']
            elif displays[key]:
                display_of[key]=max(displays[key].items(),key=lambda item:(item[1],item[0]))[0]
            else:
                display_of[key]=key.split(':',1)[1]
        def info(k):
            link=link_of.get(k)
            linked=bool(link and link['status'] in ('linked','accepted') and link['mb_artist_id'])
            return dict(mb_artist_id=link['mb_artist_id'] if linked else None,
                        link_status=link['status'] if link else None,
                        aliases=_candidate_aliases(link,link['mb_artist_id']) if linked else [])
        records=[dict(name=display_of[k],tracks=tracks[k],albums=len(albums[k]),featured_tracks=featured[k],
                      variants=[dict(credit=c,tracks=n) for c,n in credits[k].most_common()],
                      collaborators=[display_of[f] for f,_ in collaborators[k].most_common(10) if f in display_of and f!=k],
                      **info(k)) for k in keys
                 if not query or query.casefold() in display_of[k].casefold()]
        records.sort(key=lambda r:(-r['tracks'],-r['featured_tracks'],r['name'].casefold()))
        return records[offset:offset+min(300,max(1,limit))], len(records)

    def artist_profile(self, name: str) -> dict | None:
        """Everything about ONE canonical artist: albums, singles, tracks as primary artist, collaborations, counts and
        the complete original credit of every song. A collaboration never becomes its own artist."""
        from collections import defaultdict
        from meloshelf.domain.artists import canonical_key
        state = self._artist_state()
        analyse = self.credit_analyser()
        fix = lambda n: (state.resolve(n) if state else None) or n      # noqa: E731
        wanted = canonical_key(fix(name))
        with self.database.read() as connection:
            rows = [dict(r) for r in connection.execute("""SELECT path,title,artist,album,year,track_no FROM tracks
                WHERE artist<>'' ORDER BY album COLLATE NOCASE, track_no, title COLLATE NOCASE, path""")]
        primary, collaborations, display = [], [], name
        for row in rows:
            credit = analyse(row['artist'])
            entry = {'title': row['title'] or '', 'credit': credit.display, 'album': row['album'] or '', 'path': row['path'],
                     'year': row['year'] or '', 'track_no': row['track_no']}
            if canonical_key(fix(credit.primary)) == wanted:
                primary.append(entry)
                display = fix(credit.primary) if display == name and canonical_key(display) == wanted else display
            elif wanted in {canonical_key(fix(c)) for c in credit.collaborators}:
                collaborations.append({**entry, 'primary': fix(credit.primary)})
        if not primary and not collaborations:
            return None
        by_album = defaultdict(list)
        singles = []
        for entry in primary:
            album_key = canonical_key(entry['album'])
            if not album_key or (album_key == canonical_key(entry['title']) and not entry['album'].strip()):
                singles.append(entry)
            else:
                by_album[album_key].append(entry)
        albums = []
        for key, tracks in by_album.items():
            if len(tracks) == 1 and key == canonical_key(tracks[0]['title']):    # a one-track "album" named like its song
                singles.append(tracks[0])
                continue
            years = [t['year'] for t in tracks if t['year']]
            albums.append({'name': tracks[0]['album'], 'year': min(years) if years else '', 'tracks': len(tracks), 'track_list': tracks})
        albums.sort(key=lambda a: (a['year'] or '9999', a['name'].casefold()))
        credits = defaultdict(int)
        for entry in primary:
            credits[entry['credit']] += 1
        return {'name': display, 'counts': {'tracks': len(primary), 'albums': len(albums), 'singles': len(singles),
                                            'collaborations': len(collaborations)},
                'albums': albums, 'singles': singles, 'collaborations': collaborations,
                'credits': [{'credit': c, 'tracks': n} for c, n in sorted(credits.items(), key=lambda kv: (-kv[1], kv[0]))]}

    _ALBUM_ORDER = {'tracks': 'tracks DESC, title COLLATE NOCASE', 'title': 'title COLLATE NOCASE, artist COLLATE NOCASE',
                    'artist': 'artist COLLATE NOCASE, title COLLATE NOCASE', 'year': 'year DESC, title COLLATE NOCASE'}

    def albums(self, *, limit:int=60, offset:int=0, query:str='', artist:str='', sort:str='tracks') -> tuple[list[dict], int]:
        """Albums by CANONICAL primary artist: "Golden Hour" credited to "Kygo, Selena Gomez" and "Kygo, John Legend" is one
        album of Kygo. The complete credits stay on the tracks."""
        from collections import defaultdict
        from meloshelf.domain.artists import canonical_key
        state = self._artist_state()
        fix = lambda n: (state.resolve(n) if state else None) or n      # noqa: E731
        with self.database.read() as connection:
            rows = connection.execute("SELECT album AS title, artist, MAX(year) AS year, COUNT(*) AS tracks FROM tracks WHERE album<>'' GROUP BY album, artist").fetchall()
        analyse = self.credit_analyser(sorted({r['artist'] for r in rows if r['artist']}))
        groups = defaultdict(lambda: {'tracks': 0, 'year': '', 'names': defaultdict(int), 'credits': set()})
        for row in rows:
            primary = fix(analyse(row['artist']).primary) if row['artist'] else ''
            group = groups[(canonical_key(row['title']), canonical_key(primary), row['title'])]
            group['tracks'] += row['tracks']
            group['year'] = max(group['year'] or '', row['year'] or '')
            group['names'][primary] += row['tracks']
            group['credits'].add(row['artist'] or '')
        merged = {}
        for (album_key, artist_key, _spelling), group in groups.items():        # titles that differ only in case share one entry
            slot = merged.setdefault((album_key, artist_key), {'year': '', 'tracks': 0, 'credits': set(), 'names': defaultdict(int)})
            slot['tracks'] += group['tracks']
            slot['year'] = max(slot['year'], group['year'] or '')
            slot['credits'] |= group['credits']
            for name, n in group['names'].items():
                slot['names'][name] += n
        results = []
        for (album_key, artist_key), slot in merged.items():
            title = next(r['title'] for r in rows if canonical_key(r['title']) == album_key)
            display = max(slot['names'].items(), key=lambda kv: (kv[1], kv[0]))[0] if slot['names'] else ''
            results.append({'title': title, 'artist': display, 'year': slot['year'], 'tracks': slot['tracks'], '_credits': slot['credits']})
        if query:
            q = query.casefold()
            results = [r for r in results if q in r['title'].casefold() or q in r['artist'].casefold() or any(q in c.casefold() for c in r['_credits'])]
        if artist:
            wanted = canonical_key(fix(artist))
            results = [r for r in results if canonical_key(r['artist']) == wanted or artist in r['_credits']]
        sorts = {'tracks': lambda r: (-r['tracks'], r['title'].casefold()), 'title': lambda r: (r['title'].casefold(), r['artist'].casefold()),
                 'artist': lambda r: (r['artist'].casefold(), r['title'].casefold()), 'year': lambda r: (-(int(str(r['year'])[:4]) if str(r['year'])[:4].isdigit() else 0), r['title'].casefold())}
        results.sort(key=sorts.get(sort, sorts['tracks']))
        total = len(results)
        window = results[offset:offset + min(300, max(1, limit))]
        for r in window:
            r.pop('_credits', None)
        return window, total

    def for_recognition(self, limit: int = 50) -> list[dict]:
        """Prioritize unidentified tags across the entire indexed collection."""
        limit=min(200,max(1,int(limit)))
        with self.database.read() as connection:
            rows=connection.execute("""SELECT * FROM tracks
              ORDER BY CASE status WHEN 'unidentified' THEN 0 WHEN 'review' THEN 1 ELSE 2 END, path
              LIMIT ?""",(limit,))
            return [dict(row) for row in rows]

    _CURRENT_MATCH = '''LEFT JOIN match_results m
          ON m.path=t.path AND m.file_size=t.size AND m.file_mtime_ns=t.mtime_ns'''

    def record_embedded_art(self, path: str, present: bool) -> None:
        """The thumbnail endpoint learned whether a usable picture exists; keep the answer so it is not asked again."""
        with self.database.write() as connection:
            connection.execute('UPDATE tracks SET embedded_art=? WHERE path=? AND COALESCE(embedded_art,-1)<>?',
                               (int(present), str(path), int(present)))

    def get_many(self, paths: list[str]) -> dict[str, dict]:
        with self.database.read() as connection:
            marks = ','.join('?' * len(paths))
            rows = connection.execute(f'SELECT * FROM tracks WHERE path IN ({marks})', paths)
            return {row['path']: dict(row) for row in rows}

    def identification_summary(self) -> dict[str, int]:
        """Counts over the whole index; a result only counts for the file version it was made for."""
        with self.database.read() as connection:
            row = connection.execute(f'''SELECT
                COALESCE(SUM(m.path IS NULL),0) AS awaiting,
                COALESCE(SUM(m.status='review'),0) AS identified,
                COALESCE(SUM(m.status='unmatched'),0) AS unknown
              FROM tracks t {self._CURRENT_MATCH}''').fetchone()
        return dict(row)

    def awaiting_identification(self, limit: int, *, skip_codes: Iterable[str] = ()) -> list[str]:
        """Paths without a current result, unknown tags first. Files whose last attempt ended
        with one of ``skip_codes`` (permanent failures) are left out."""
        codes = list(skip_codes)
        marks = ','.join('?' * len(codes)) or "''"
        with self.database.read() as connection:
            rows = connection.execute(f'''SELECT t.path FROM tracks t {self._CURRENT_MATCH}
              WHERE m.path IS NULL AND NOT EXISTS (
                SELECT 1 FROM job_items i JOIN jobs j ON j.id=i.job_id
                WHERE i.path=t.path AND i.state='error' AND j.kind='identify' AND i.code IN ({marks}))
              ORDER BY CASE t.status WHEN 'unidentified' THEN 0 WHEN 'review' THEN 1 ELSE 2 END, t.path
              LIMIT ?''', (*codes, max(1, int(limit))))
            return [row['path'] for row in rows]

    def tracks_under(self, root: str) -> list[dict]:
        """Every indexed track under ``root`` with the result that applies to its current content, if any."""
        prefix = __import__('os').path.normpath(root).rstrip('\\/') if root else root   # the index stores native separators
        with self.database.read() as connection:
            rows = connection.execute(f'''SELECT t.path, t.size, t.mtime_ns, t.title, t.artist, t.album, t.track_no, t.status,
                       t.embedded_art, m.recording_status, m.decision, m.selected_recording_id, m.data AS match_data
              FROM tracks t {self._CURRENT_MATCH}
              WHERE (t.path LIKE ? ESCAPE '\\' OR t.path LIKE ? ESCAPE '\\') ORDER BY t.path''',
              (self._like(prefix + '\\')[1:], self._like(prefix + '/')[1:]))
            return [dict(row) for row in rows]

    def without_current_result(self, paths: list[str]) -> list[str]:
        """The given paths that have no result for their current content, in the given order."""
        wanted, found = list(paths), set()
        with self.database.read() as connection:
            for start in range(0, len(wanted), 400):
                chunk = wanted[start:start + 400]
                rows = connection.execute(f'''SELECT t.path FROM tracks t {self._CURRENT_MATCH}
                    WHERE m.path IS NULL AND t.path IN ({','.join('?' * len(chunk))})''', chunk)
                found.update(row['path'] for row in rows)
        return [p for p in wanted if p in found]

    def paths_not_in_job(self, root: str, job_id: str) -> list[str]:
        """Indexed paths under ``root`` that a completed discovery did not find."""
        prefix = __import__('os').path.normpath(root).rstrip('\\/') if root else root   # the index stores native separators
        with self.database.read() as connection:
            rows = connection.execute('''SELECT path FROM tracks t
              WHERE (path LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')
                AND NOT EXISTS (SELECT 1 FROM job_items i WHERE i.job_id=? AND i.path=t.path)''',
              (self._like(prefix + '\\')[1:], self._like(prefix + '/')[1:], job_id))
            return [row['path'] for row in rows]

    def remove(self, paths: list[str]) -> None:
        """Forget index rows. Only the index is touched; files are never deleted."""
        with self.database.write() as connection:
            connection.executemany('DELETE FROM tracks WHERE path=?', [(p,) for p in paths])

    @staticmethod
    def _like(query:str) -> str:
        return '%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'

    def close(self):
        """Kept for callers of the previous API; connections are closed per operation."""
