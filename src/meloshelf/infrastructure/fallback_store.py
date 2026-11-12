"""Persisted fallback results. Kept apart from the acoustic results (match_results) so the two can never be confused."""
import json
import time


class FallbackStore:
    def __init__(self, database):
        self.database = database

    def put(self, path: str, level: str, data: dict) -> None:
        with self.database.write() as db:
            db.execute('INSERT OR REPLACE INTO fallback_results(path,level,data,created_at) VALUES(?,?,?,?)',
                       (path, level, json.dumps(data, ensure_ascii=False), time.time()))

    def get(self, path: str) -> dict | None:
        with self.database.read() as db:
            row = db.execute('SELECT * FROM fallback_results WHERE path=?', (path,)).fetchone()
        if row is None:
            return None
        return {'path': row['path'], 'level': row['level'], **json.loads(row['data']), 'created_at': row['created_at']}

    def answered(self) -> set[str]:
        with self.database.read() as db:
            return {r[0] for r in db.execute('SELECT path FROM fallback_results')}
