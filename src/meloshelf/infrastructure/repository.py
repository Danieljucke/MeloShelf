"""Persistent, indexed local library. No file modification operations exist here."""
import sqlite3
from pathlib import Path
from collections.abc import Iterable
from meloshelf.domain.models import Track


class LibraryRepository:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(db_path), timeout=20)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA busy_timeout=5000')
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS tracks (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            extension TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            artist TEXT NOT NULL DEFAULT '',
            album TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('tagged','review','unidentified'))
          );
          CREATE INDEX IF NOT EXISTS ix_tracks_status ON tracks(status);
          CREATE INDEX IF NOT EXISTS ix_tracks_artist ON tracks(artist);
        ''')

    def save_many(self, tracks: Iterable[Track]) -> None:
        self.connection.executemany('''
          INSERT INTO tracks(path,size,extension,mtime_ns,title,artist,album,year,status)
          VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(path) DO UPDATE SET
             size=excluded.size, extension=excluded.extension, mtime_ns=excluded.mtime_ns,
             title=excluded.title, artist=excluded.artist, album=excluded.album,
             year=excluded.year, status=excluded.status
        ''', ((t.path, t.size, t.extension, t.mtime_ns, t.title, t.artist, t.album, t.year, t.status) for t in tracks))
        self.connection.commit()

    def counts(self) -> dict[str,int]:
        result = {'total':0,'tagged':0,'review':0,'unidentified':0}
        for row in self.connection.execute('SELECT status, COUNT(*) AS n FROM tracks GROUP BY status'):
            result[row['status']] = row['n']
            result['total'] += row['n']
        return result

    def list_tracks(self, *, limit:int=60, offset:int=0, query:str='') -> list[dict]:
        limit = min(300, max(1, limit))
        if query:
            rows = self.connection.execute('''SELECT * FROM tracks
              WHERE title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'
              ORDER BY path LIMIT ? OFFSET ?''', (self._like(query),)*3 + (limit,offset))
        else:
            rows = self.connection.execute('SELECT * FROM tracks ORDER BY path LIMIT ? OFFSET ?', (limit,offset))
        return [dict(row) for row in rows]

    def for_recognition(self, limit: int = 50) -> list[dict]:
        """Prioritize unidentified tags across the entire indexed collection."""
        limit=min(200,max(1,int(limit)))
        rows=self.connection.execute("""SELECT * FROM tracks
          ORDER BY CASE status WHEN 'unidentified' THEN 0 WHEN 'review' THEN 1 ELSE 2 END, path
          LIMIT ?""",(limit,))
        return [dict(row) for row in rows]

    @staticmethod
    def _like(query:str) -> str:
        return '%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'

    def close(self):
        self.connection.close()
