"""Recognition evidence cache and review queue persisted separately from local tags."""
import json
import time
from pathlib import Path

from meloshelf.domain.decisions import DEFAULT_THRESHOLD, THRESHOLD_RANGE, status_for
from meloshelf.domain.matching import Evidence
from meloshelf.infrastructure.database import Database, as_database

THRESHOLD_KEY = 'auto_approve_threshold'
# Earlier results carry the instruction in their reason; it is evidence, not a request, so it is shown without it.
_LEGACY_REASONS = {'Fingerprint, duration and tags agree; confirm to accept': 'Fingerprint, duration and tags agree.'}


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

    @staticmethod
    def _present_instance(db,path) -> str:
        """The file instance currently at this path, or '' when the file has not been reconciled yet."""
        row=db.execute("SELECT instance_id FROM file_instances WHERE path=? AND state='present' LIMIT 1",(str(path),)).fetchone()
        return row['instance_id'] if row else ''

    def put_result(self,path,size,mtime_ns,status,data,*,recording_status=''):
        from meloshelf.infrastructure.decision_binding import archive_decision
        with self.database.write() as db:
            # A human decision is never overwritten silently: the earlier one is archived before the new result replaces it.
            previous=db.execute('SELECT * FROM match_results WHERE path=?',(str(path),)).fetchone()
            if previous is not None and previous['decision'] in ('accepted','rejected'):
                archive_decision(db,previous,'replaced by a new identification')
            instance=self._present_instance(db,path)
            db.execute("""INSERT INTO match_results(path,file_size,file_mtime_ns,status,data,created_at,decision,recording_status,instance_id)
                VALUES(?,?,?,?,?,?, 'pending', ?, ?) ON CONFLICT(path) DO UPDATE SET file_size=excluded.file_size,
                file_mtime_ns=excluded.file_mtime_ns,status=excluded.status,data=excluded.data,
                created_at=excluded.created_at,decision='pending',recording_status=excluded.recording_status,
                selected_recording_id='',selected_release_id='',instance_id=excluded.instance_id""",
                (str(path),size,mtime_ns,status,json.dumps(data,ensure_ascii=False),int(time.time()),recording_status,instance))

    @staticmethod
    def _get_result(db,path):
        row=db.execute('SELECT * FROM match_results WHERE path=?',(str(path),)).fetchone()
        if not row:return None
        d=dict(row);d['data']=json.loads(d['data']);return d

    def get_result(self,path):
        with self.database.read() as db:
            return self._get_result(db,path)

    def recording_status(self,path) -> str:
        with self.database.read() as db:
            row=db.execute('SELECT recording_status FROM match_results WHERE path=?',(str(path),)).fetchone()
        return row['recording_status'] if row else ''

    def list_results(self,limit=100,offset=0):
        with self.database.read() as db:
            rows=db.execute('SELECT * FROM match_results ORDER BY created_at DESC,path LIMIT ? OFFSET ?',
                            (min(500,max(1,int(limit))),max(0,int(offset)))).fetchall()
        policy=self.automation_policy()
        records=[]
        for r in rows:
            data=json.loads(r['data'])
            data['reason']=_LEGACY_REASONS.get(data.get('reason',''),data.get('reason',''))
            # Results stored before the status column existed are evaluated now, without being written.
            stored=r['recording_status'] or status_for(
                [Evidence.from_stored(c['evidence']) for c in data.get('candidates',[]) if c.get('evidence')],
                user_decision=r['decision'],**policy)
            records.append(dict(path=r['path'],status=r['status'],decision=r['decision'],selected_recording_id=r['selected_recording_id'],
                                recording_status=stored,created_at=r['created_at'],**data))
        return records

    def count_results(self):
        with self.database.read() as db:
            return db.execute('SELECT COUNT(*) FROM match_results').fetchone()[0]

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
            # The displayed status follows the user's decision; 'pending' returns to the automatic policy.
            evidence=[Evidence.from_stored(c['evidence']) for c in existing['data'].get('candidates',[]) if c.get('evidence')]
            status=status_for(evidence,user_decision=decision,**self.automation_policy())
            # The decision is bound to the file instance it was made for; an unknown instance leaves the binding empty.
            instance=self._present_instance(db,path)
            row=db.execute("""UPDATE match_results SET decision=?,selected_recording_id=?,selected_release_id=?,recording_status=?,
                              instance_id=CASE WHEN ?<>'' THEN ? ELSE instance_id END WHERE path=?""",
                           (decision,recording_id,'',status,instance,instance,str(path)))
            return row.rowcount>0

    # ── settings ─────────────────────────────────────────────────────────────

    def get_setting(self,key,default=None):
        with self.database.read() as db:
            row=db.execute('SELECT value FROM app_settings WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_setting(self,key,value):
        if key==THRESHOLD_KEY:
            low,high=THRESHOLD_RANGE
            if type(value) is not int or not low<=value<=high:
                raise ValueError(f'Auto-approval threshold must be a whole number from {low} to {high}')
        with self.database.write() as db:
            db.execute('INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)',(key,json.dumps(value)))

    # Settings exposed to the interface, with the storage key behind each one.
    AUTOMATION_KEYS={'auto_approve_enabled':'auto_approve_enabled','threshold':THRESHOLD_KEY,
                     'strict_checks':'strict_checks','review_ambiguous':'review_ambiguous'}
    AUTOMATION_DEFAULTS={'auto_approve_enabled':True,'threshold':DEFAULT_THRESHOLD,'strict_checks':True,'review_ambiguous':True}

    def automation_settings(self) -> dict:
        return {name:self.get_setting(key,self.AUTOMATION_DEFAULTS[name]) for name,key in self.AUTOMATION_KEYS.items()}

    def automation_policy(self) -> dict:
        """Keyword arguments for ``domain.decisions.status_for``."""
        settings=self.automation_settings()
        return {'threshold':settings['threshold'],'auto_enabled':settings['auto_approve_enabled'],
                'strict':settings['strict_checks'],'review_ambiguous':settings['review_ambiguous']}

    def update_automation_settings(self,changes:dict) -> int:
        """Validate every change before writing any, persist them, then reevaluate.

        Returns the number of stored results whose status was recomputed.
        """
        unknown=set(changes)-set(self.AUTOMATION_KEYS)
        if unknown:
            raise ValueError(f'Unknown setting: {", ".join(sorted(unknown))}')
        for name,value in changes.items():
            if name=='threshold':
                low,high=THRESHOLD_RANGE
                if type(value) is not int or not low<=value<=high:
                    raise ValueError(f'Auto-approval threshold must be a whole number from {low} to {high}')
            elif type(value) is not bool:
                raise ValueError(f'{name} must be true or false')
        for name,value in changes.items():
            self.set_setting(self.AUTOMATION_KEYS[name],value)
        return self.reevaluate()

    def reevaluate(self) -> int:
        """Recompute automatic statuses from stored evidence after a settings change.

        No provider is contacted and no user decision is altered: accepted and rejected
        records keep their decision and only their displayed status is recomputed.
        """
        policy=self.automation_policy()
        with self.database.write() as db:
            rows=db.execute('SELECT path,decision,data FROM match_results').fetchall()
            for row in rows:
                candidates=json.loads(row['data']).get('candidates',[])
                evidence=[Evidence.from_stored(c['evidence']) for c in candidates if c.get('evidence')]
                status=status_for(evidence,user_decision=row['decision'],**policy)
                db.execute('UPDATE match_results SET recording_status=? WHERE path=?',(status,row['path']))
        return len(rows)

    def close(self):
        """Kept for callers of the previous API; connections are closed per operation."""
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
