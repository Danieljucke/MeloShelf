"""Loopback-only GUI service. All operations are read-only with respect to audio."""
import json
import logging
import os
import secrets
import sqlite3
from pathlib import Path
from threading import Thread, Event, Lock
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from meloshelf.application.scanner import LibraryScanner
from meloshelf.application.recognition import RecognitionService
from meloshelf.infrastructure.database import Database, DatabaseBusyError
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.fingerprint import Fingerprinter
from meloshelf.infrastructure.music_api import AcoustIDClient, MusicBrainzClient

log=logging.getLogger(__name__)


class JobManager:
    def __init__(self, database: Database):
        self.database=database
        self._lock=Lock()
        self._stop=Event()
        self._state={'running':False,'kind':'','completed':0,'message':'Ready. Your music has not been modified.','errors':0}

    def status(self):
        with self._lock:return dict(self._state)

    def _update(self, **kw):
        with self._lock:self._state.update(kw)

    def start(self,kind,worker):
        with self._lock:
            if self._state['running']:
                return False
            self._stop.clear()
            self._state={'running':True,'kind':kind,'completed':0,'message':f'{kind.title()} started','errors':0}
        def run():
            try:worker()
            except Exception as exc:
                log.exception('job failed kind=%s',kind)
                self._update(message=f'{kind.title()} failed: {type(exc).__name__}: {exc}',errors=1)
            finally:self._update(running=False)
        Thread(target=run,name=f'meloshelf-{kind}',daemon=True).start()
        return True

    def stop(self):self._stop.set();self._update(message='Stop requested; finishing current item safely.')

    def scan(self,folder):
        def run():
            scanner=LibraryScanner()
            count=0
            repo=LibraryRepository(self.database)
            batch=[]
            # Tags are read outside any transaction; each batch is one short write.
            for track in scanner.iter_tracks(Path(folder),should_stop=self._stop.is_set):
                batch.append(track);count+=1
                if len(batch)>=200:
                    repo.save_many(batch);batch.clear();self._update(completed=count,message=f'Indexed {count:,} audio files')
            if batch:repo.save_many(batch)
            self._update(completed=count,errors=len(scanner.errors),message=(
                'Scan stopped' if self._stop.is_set() else f'Indexed {count:,} audio files. {len(scanner.errors)} access errors.'))
        return self.start('scan',run)

    def identify(self,limit,offline):
        def run():
            repo=LibraryRepository(self.database)
            store=MatchStore(self.database)
            key=os.environ.get('MELOSHELF_ACOUSTID_KEY','').strip()
            agent=os.environ.get('MELOSHELF_MUSICBRAINZ_USER_AGENT','').strip()
            acoustid=AcoustIDClient(key) if key and not offline else None
            musicbrainz=MusicBrainzClient(agent) if agent and not offline else None
            service=RecognitionService(Fingerprinter(),acoustid,musicbrainz,store,offline=offline)
            errors=0;processed=0
            tracks=repo.for_recognition(limit)
            for t in tracks[:limit]:
                if self._stop.is_set():break
                path=Path(t['path'])
                stat=path.stat() if path.is_file() else None
                if stat is None or stat.st_size!=t['size'] or stat.st_mtime_ns!=t['mtime_ns']:
                    errors+=1
                    self._update(message=f'File changed since scan: {path.name}',errors=errors)
                    continue
                report=service.identify(path)
                processed+=1
                if report.status.value in ('error','changed'):errors+=1
                self._update(completed=processed,errors=errors,message=f'Analyzed {processed}/{min(limit,len(tracks))} fingerprints · {report.status.value}')
            self._update(message=('Identification paused' if self._stop.is_set() else f'Identification completed: {processed} tracks, {errors} errors.'))
        return self.start('identify',run)


class MeloShelfServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    # The page fires several API calls at once; the default backlog of 5 makes
    # the surplus connections wait for a TCP retry.
    request_queue_size=128

    def __init__(self, address, db_path, html_path):
        super().__init__(address,ApiHandler)
        self.db_path=Path(db_path)
        self.html_path=Path(html_path)
        self.token=secrets.token_urlsafe(32)
        # Schema and WAL are settled here, before the first request can arrive.
        self.database=Database(self.db_path)
        self.database.initialize()
        self.repo=LibraryRepository(self.database)
        self.store=MatchStore(self.database)
        self.jobs=JobManager(self.database)


def handler_json(handler, status, data):
    raw=json.dumps(data,ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type','application/json; charset=utf-8')
    handler.send_header('Cache-Control','no-store')
    handler.send_header('X-Content-Type-Options','nosniff')
    handler.send_header('Content-Length',str(len(raw)))
    handler.end_headers();handler.wfile.write(raw)


class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):pass

    def _authorized_host(self):
        host=self.headers.get('Host','')
        port=self.server.server_address[1]
        return host in (f'127.0.0.1:{port}',f'localhost:{port}')

    def _read_json(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<0 or size>32768:raise ValueError('Payload too large')
            return json.loads(self.rfile.read(size))
        except (ValueError,UnicodeDecodeError,json.JSONDecodeError):
            return None

    def _guarded(self,route):
        """Turn database failures into a JSON answer instead of a dropped connection."""
        try:return route()
        except DatabaseBusyError as exc:
            log.warning('database busy method=%s path=%s detail=%s',self.command,self.path,exc)
            return handler_json(self,503,{'error':str(exc)})
        except sqlite3.Error as exc:
            log.exception('database error method=%s path=%s',self.command,self.path)
            return handler_json(self,500,{'error':f'Library database error: {type(exc).__name__}: {exc}'})

    def do_GET(self):return self._guarded(self._get)
    def do_POST(self):return self._guarded(self._post)

    def _get(self):
        if not self._authorized_host():return handler_json(self,403,{'error':'Host not allowed'})
        parsed=urlparse(self.path)
        if parsed.path in ('/','/index.html'):
            raw=self.server.html_path.read_text(encoding='utf-8').replace('__MELOSHELF_TOKEN__',self.server.token).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw);return
        if parsed.path=='/api/status':return handler_json(self,200,self.server.jobs.status())
        if parsed.path=='/api/config':
            return handler_json(self,200,{'mode':'local','read_only':True,
              'acoustid_configured':bool(os.environ.get('MELOSHELF_ACOUSTID_KEY')),
              'musicbrainz_configured':bool(os.environ.get('MELOSHELF_MUSICBRAINZ_USER_AGENT'))})
        if parsed.path=='/api/tracks':
            q=parse_qs(parsed.query)
            try:limit=max(1,min(200,int(q.get('limit',['50'])[0])));offset=max(0,int(q.get('offset',['0'])[0]))
            except ValueError:return handler_json(self,400,{'error':'Invalid paging'})
            search=q.get('q',[''])[0][:200]
            rows=self.server.repo.list_tracks(limit=limit,offset=offset,query=search)
            return handler_json(self,200,{'tracks':rows,'offset':offset,'limit':limit})
        if parsed.path=='/api/stats':
            return handler_json(self,200,self.server.repo.counts())
        if parsed.path=='/api/matches':
            return handler_json(self,200,{'matches':self.server.store.list_results()})
        return handler_json(self,404,{'error':'Not found'})

    def _post(self):
        if not self._authorized_host():return handler_json(self,403,{'error':'Host not allowed'})
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_address[1]}',f'http://localhost:{self.server.server_address[1]}'):
            return handler_json(self,403,{'error':'Origin not allowed'})
        if self.headers.get('X-MeloShelf-Token')!=self.server.token:
            return handler_json(self,403,{'error':'Invalid session token'})
        payload=self._read_json()
        if not isinstance(payload,dict):return handler_json(self,400,{'error':'Invalid JSON'})
        path=urlparse(self.path).path
        if path=='/api/scan':
            folder=payload.get('folder')
            if not isinstance(folder,str) or not folder.strip() or not Path(folder).expanduser().is_dir():
                return handler_json(self,400,{'error':'Select an existing folder on this computer'})
            ok=self.server.jobs.scan(str(Path(folder).expanduser().resolve()))
            return handler_json(self,202 if ok else 409,{'started':ok})
        if path=='/api/identify':
            limit=payload.get('limit',5)
            if not type(limit) is int or not 1<=limit<=200:
                return handler_json(self,400,{'error':'Limit must be 1–200'})
            offline=payload.get('offline',False)
            if not isinstance(offline,bool):return handler_json(self,400,{'error':'offline must be boolean'})
            if not offline and not os.environ.get('MELOSHELF_ACOUSTID_KEY'):
                return handler_json(self,409,{'error':'Set MELOSHELF_ACOUSTID_KEY first or choose offline mode'})
            ok=self.server.jobs.identify(limit,offline)
            return handler_json(self,202 if ok else 409,{'started':ok})
        if path=='/api/stop':
            self.server.jobs.stop();return handler_json(self,200,{'stopping':True})
        if path=='/api/decision':
            decision=payload.get('decision');f=payload.get('path');recording_id=payload.get('recording_id','')
            if not isinstance(f,str) or decision not in ('accepted','rejected','pending'):
                return handler_json(self,400,{'error':'Invalid decision'})
            try:ok=self.server.store.set_decision(f,decision,recording_id=recording_id)
            except ValueError as exc:return handler_json(self,400,{'error':str(exc)})
            return handler_json(self,200 if ok else 404,{'saved':ok,'note':'Recording decision only: album edition unresolved, audio unchanged'})
        return handler_json(self,404,{'error':'Not found'})


def create_server(*, db_path: Path | None=None, html_path: Path | None=None, port=0):
    db_path=Path(db_path) if db_path else Path.home()/'.meloshelf'/'library.sqlite3'
    html_path=Path(html_path) if html_path else Path(__file__).parents[3]/'design-preview'/'index.html'
    # Installed wheels can provide the HTML within the package.
    if not html_path.exists():html_path=Path(__file__).parents[1]/'presentation'/'web'/'index.html'
    return MeloShelfServer(('127.0.0.1',port),db_path,html_path)
