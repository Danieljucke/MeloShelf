"""Persistent, indexed local library. No file modification operations exist here."""
from collections.abc import Iterable
from pathlib import Path

from meloshelf.domain.models import Track
from meloshelf.infrastructure.database import Database, as_database


class LibraryRepository:
    """Stateless facade: holds no connection, so one instance is safe across threads."""

    def __init__(self, database: Database | Path | str):
        self.database = as_database(database)

    def save_many(self, tracks: Iterable[Track]) -> None:
        rows = [(t.path, t.size, t.extension, t.mtime_ns, t.title, t.artist, t.album, t.year, t.status)
                for t in tracks]
        with self.database.write() as connection:
            connection.executemany('''
              INSERT INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status)
              VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(path) DO UPDATE SET
                 size=excluded.size, extension=excluded.extension, mtime_ns=excluded.mtime_ns,
                 title=excluded.title, artist=excluded.artist, album=excluded.album,
                 year=excluded.year, status=excluded.status
            ''', rows)

    def counts(self) -> dict[str,int]:
        result = {'total':0,'tagged':0,'review':0,'unidentified':0}
        with self.database.read() as connection:
            for row in connection.execute('SELECT status, COUNT(*) AS n FROM tracks GROUP BY status'):
                result[row['status']] = row['n']
                result['total'] += row['n']
        return result

    def list_tracks(self, *, limit:int=60, offset:int=0, query:str='') -> list[dict]:
        limit = min(300, max(1, limit))
        with self.database.read() as connection:
            if query:
                rows = connection.execute('''SELECT * FROM tracks
                  WHERE title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'
                  ORDER BY path LIMIT ? OFFSET ?''', (self._like(query),)*3 + (limit,offset))
            else:
                rows = connection.execute('SELECT * FROM tracks ORDER BY path LIMIT ? OFFSET ?', (limit,offset))
            return [dict(row) for row in rows]

    def for_recognition(self, limit: int = 50) -> list[dict]:
        """Prioritize unidentified tags across the entire indexed collection."""
        limit=min(200,max(1,int(limit)))
        with self.database.read() as connection:
            rows=connection.execute("""SELECT * FROM tracks
              ORDER BY CASE status WHEN 'unidentified' THEN 0 WHEN 'review' THEN 1 ELSE 2 END, path
              LIMIT ?""",(limit,))
            return [dict(row) for row in rows]

    @staticmethod
    def _like(query:str) -> str:
        return '%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'

    def close(self):
        """Kept for callers of the previous API; connections are closed per operation."""
