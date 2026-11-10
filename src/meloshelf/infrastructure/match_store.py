"""Recognition evidence cache and review queue persisted separately from local tags."""
import json
import time
from pathlib import Path

from meloshelf.infrastructure.database import Database, as_database


class MatchStore:
    """Stateless facade: holds no connection, so one instance is safe across threads."""

    def __init__(self, database: Database | Path | str):
        self.database = as_database(database)

    def get_cache(self,key):
        with self.database.read() as db:
            row=db.execute('SELECT data FROM remote_cache WHERE cache_key=?',(key,)).fetchone()
        return json.loads(row['data']) if row else None

    def put_cache(self,key,data):
        with self.database.write() as db:
            db.execute('INSERT OR REPLACE INTO remote_cache VALUES(?,?,?)',(key,json.dumps(data,ensure_ascii=False),int(time.time())))

    def put_result(self,path,size,mtime_ns,status,data):
        with self.database.write() as db:
            db.execute('''INSERT INTO match_results(path,file_size,file_mtime_ns,status,data,created_at,decision)
                VALUES(?,?,?,?,?,?, 'pending') ON CONFLICT(path) DO UPDATE SET file_size=excluded.file_size,
                file_mtime_ns=excluded.file_mtime_ns,status=excluded.status,data=excluded.data,
                created_at=excluded.created_at,decision='pending',
                selected_recording_id='',selected_release_id='' ''',
                (str(path),size,mtime_ns,status,json.dumps(data,ensure_ascii=False),int(time.time())))

    @staticmethod
    def _get_result(db,path):
        row=db.execute('SELECT * FROM match_results WHERE path=?',(str(path),)).fetchone()
        if not row:return None
        d=dict(row);d['data']=json.loads(d['data']);return d

    def get_result(self,path):
        with self.database.read() as db:
            return self._get_result(db,path)

    def list_results(self,limit=100):
        with self.database.read() as db:
            rows=db.execute('SELECT * FROM match_results ORDER BY created_at DESC,path LIMIT ?', (min(500,max(1,int(limit))),)).fetchall()
        return [dict(path=r['path'],status=r['status'],decision=r['decision'],selected_recording_id=r['selected_recording_id'],created_at=r['created_at'],**json.loads(r['data'])) for r in rows]

    def set_decision(self,path,decision,*,recording_id=''):
        if decision not in ('pending','accepted','rejected'):
            raise ValueError('Invalid review decision')
        # Validation and update share one transaction so the candidate list cannot change in between.
        with self.database.write() as db:
            existing=self._get_result(db,path)
            if not existing:return False
            if decision=='accepted':
                if not recording_id or recording_id not in {c.get('recording_id') for c in existing['data'].get('candidates',[])}:
                    raise ValueError('Select an available recording candidate explicitly')
            else:recording_id=''
            row=db.execute('UPDATE match_results SET decision=?,selected_recording_id=?,selected_release_id=? WHERE path=?',
                           (decision,recording_id,'',str(path)))
            return row.rowcount>0

    def close(self):
        """Kept for callers of the previous API; connections are closed per operation."""
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
