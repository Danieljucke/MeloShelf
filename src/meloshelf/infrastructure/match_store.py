"""Recognition evidence cache and review queue persisted separately from local tags."""
from pathlib import Path
import sqlite3
import json
import time


class MatchStore:
    def __init__(self, db_path: Path):
        path=Path(db_path)
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(path),timeout=30,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS remote_cache (cache_key TEXT PRIMARY KEY, data TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS match_results (
                path TEXT PRIMARY KEY, file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
                status TEXT NOT NULL, data TEXT NOT NULL, created_at INTEGER NOT NULL,
                decision TEXT NOT NULL DEFAULT 'pending',
                selected_recording_id TEXT NOT NULL DEFAULT '',
                selected_release_id TEXT NOT NULL DEFAULT ''
            );
        ''')
        columns={row['name'] for row in self.db.execute('PRAGMA table_info(match_results)')}
        for column in ('selected_recording_id','selected_release_id'):
            if column not in columns:
                self.db.execute(f"ALTER TABLE match_results ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def get_cache(self,key):
        row=self.db.execute('SELECT data FROM remote_cache WHERE cache_key=?',(key,)).fetchone()
        return json.loads(row['data']) if row else None

    def put_cache(self,key,data):
        self.db.execute('INSERT OR REPLACE INTO remote_cache VALUES(?,?,?)',(key,json.dumps(data,ensure_ascii=False),int(time.time())))
        self.db.commit()

    def put_result(self,path,size,mtime_ns,status,data):
        self.db.execute('''INSERT INTO match_results(path,file_size,file_mtime_ns,status,data,created_at,decision)
            VALUES(?,?,?,?,?,?, 'pending') ON CONFLICT(path) DO UPDATE SET file_size=excluded.file_size,
            file_mtime_ns=excluded.file_mtime_ns,status=excluded.status,data=excluded.data,
            created_at=excluded.created_at,decision='pending',
            selected_recording_id='',selected_release_id='' ''',
            (str(path),size,mtime_ns,status,json.dumps(data,ensure_ascii=False),int(time.time())))
        self.db.commit()

    def get_result(self,path):
        row=self.db.execute('SELECT * FROM match_results WHERE path=?',(str(path),)).fetchone()
        if not row:return None
        d=dict(row);d['data']=json.loads(d['data']);return d

    def list_results(self,limit=100):
        rows=self.db.execute('SELECT * FROM match_results ORDER BY created_at DESC,path LIMIT ?', (min(500,max(1,int(limit))),)).fetchall()
        return [dict(path=r['path'],status=r['status'],decision=r['decision'],selected_recording_id=r['selected_recording_id'],created_at=r['created_at'],**json.loads(r['data'])) for r in rows]

    def set_decision(self,path,decision,*,recording_id=''):
        if decision not in ('pending','accepted','rejected'):
            raise ValueError('Invalid review decision')
        existing=self.get_result(path)
        if not existing:return False
        if decision=='accepted':
            if not recording_id or recording_id not in {c.get('recording_id') for c in existing['data'].get('candidates',[])}:
                raise ValueError('Select an available recording candidate explicitly')
        else:recording_id=''
        row=self.db.execute('UPDATE match_results SET decision=?,selected_recording_id=?,selected_release_id=? WHERE path=?',
                            (decision,recording_id,'',str(path)))
        self.db.commit()
        return row.rowcount>0

    def close(self):self.db.close()
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
