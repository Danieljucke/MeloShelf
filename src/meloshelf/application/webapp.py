"""Loopback-only GUI service. All operations are read-only with respect to audio."""
import json
import logging
import os
import re
import secrets
import sqlite3
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from meloshelf import __version__
from meloshelf.application import error_codes as errors
from meloshelf.application import error_codes as ec
from meloshelf.application.jobs import JobService
from meloshelf.application.recognition import RecognitionService
from meloshelf.domain.outcomes import describe
from meloshelf.infrastructure import log_redaction
from meloshelf.infrastructure.database import Database, DatabaseBusyError
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.qa_store import QaStore
from meloshelf.application.metadata_service import MetadataService
from meloshelf.infrastructure.metadata_store import MetadataStore
from meloshelf.infrastructure.fingerprint import Fingerprinter
from meloshelf.application.config import AppConfig, ConfigError, check_fpcalc
from meloshelf.infrastructure.secrets import KeyringSecretStore, SecretStoreError
from meloshelf.infrastructure.artwork import ArtworkCache, ArtworkError
from meloshelf.infrastructure.music_api import AcoustIDClient, MusicBrainzClient, APIError, RetryPolicy, _request_json

log=logging.getLogger(__name__)

MAX_IDENTIFY_BATCH=100_000
TRACK_STATUSES=('tagged','review','unidentified')
# Sample data lives in one marked block of the page and is removed before serving,
# so the running application can only ever show data from the library database.
DEMO_BLOCK=re.compile(r'<!--DEMO-DATA-->.*?<!--/DEMO-DATA-->',re.S)




def default_recognition(database: Database, fingerprinter: Fingerprinter, config: AppConfig):
    """Build the recognition factory and preflight check used by identification jobs."""
    store=MatchStore(database)

    def refresh():
        """Chromaprint may have been installed after MeloShelf started: look again before each identification."""
        if isinstance(fingerprinter,Fingerprinter):
            fingerprinter.executable=config.fpcalc_location().path or 'fpcalc'

    def factory(offline, should_cancel):
        refresh()
        key=config.acoustid_key()[0]
        agent=config.musicbrainz_user_agent()[0]
        acoustid=AcoustIDClient(key,should_cancel=should_cancel) if key and not offline else None
        musicbrainz=MusicBrainzClient(agent,should_cancel=should_cancel) if agent and not offline else None
        return RecognitionService(fingerprinter,acoustid,musicbrainz,store,offline=offline)

    def preflight(offline):
        refresh()
        if not fingerprinter.available():return 'fpcalc_unavailable'
        if not offline and not config.acoustid_key()[0]:return 'acoustid_key_missing'
        return ''

    return factory,preflight


class MeloShelfServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    # The page fires several API calls at once; the default backlog of 5 makes
    # the surplus connections wait for a TCP retry.
    request_queue_size=128

    def _photo_provider_or_none(self):
        """The photograph provider: an injected one, else one built from the saved public contact, else None."""
        if self.photo_provider is not None:
            return self.photo_provider
        agent=self.config.musicbrainz_user_agent()[0]
        if not agent:return None
        from meloshelf.infrastructure.wikimedia import WikimediaPhotoProvider
        self.photo_provider=WikimediaPhotoProvider(agent)
        return self.photo_provider

    def __init__(self, address, db_path, html_path, *, fingerprinter=None, recognition_factory=None, preflight=None,
                 secret_store=None, environ=None, connection_tester=None, artwork_fetch=None, fallback_client=None,
                 artist_client=None, photo_provider=None, explorer_opener=None, folder_picker=None, release_group_search=None, art_clients=None):
        super().__init__(address,ApiHandler)
        self.db_path=Path(db_path)
        self.html_path=Path(html_path)
        self.token=secrets.token_urlsafe(32)
        log_redaction.install();log_redaction.register(self.token)
        # Schema and WAL are settled here, before the first request can arrive.
        self.database=Database(self.db_path)
        self.database.initialize()
        self.repo=LibraryRepository(self.database)
        self.store=MatchStore(self.database)
        self.qa=QaStore(self.database)
        self.config=AppConfig(self.store,secret_store or KeyringSecretStore(),env=environ)
        if fingerprinter is None:
            location=self.config.fpcalc_location()
            self.fpcalc_location=location
            fingerprinter=Fingerprinter(executable=location.path or 'fpcalc')
        self.fingerprinter=fingerprinter
        self.connection_tester=connection_tester or run_connection_test
        self.artwork=ArtworkCache(self.database,self.db_path.parent/'artwork',fetch=artwork_fetch)
        self.fallback_client=fallback_client
        from meloshelf.application.fallback_job import FallbackJobs
        self.fallback_jobs=FallbackJobs(self.database,self._fallback_client_for)
        self.artist_client=artist_client
        self.photo_provider=photo_provider
        self.art_clients=art_clients                   # injected optional providers (tests); otherwise built from the saved settings
        from meloshelf.application import organize_api
        self.explorer_opener=explorer_opener or organize_api.open_in_explorer
        self.folder_picker=folder_picker or organize_api.pick_folder
        from meloshelf.application.artist_photos import ArtistPhotoService
        self.photos=ArtistPhotoService(self.database,self.db_path.parent/'artist-photos',self._photo_provider_or_none,extras=self._art_extras)
        from meloshelf.application.album_covers import AlbumCoverService
        self.covers=AlbumCoverService(self.database,self.artwork,self.db_path.parent/'album-covers')
        self.enrichment_thread=None
        from meloshelf.application.artist_enrichment import EnrichmentService
        EnrichmentService(self.database,None).recover()  # a job left running by a crash becomes interrupted
        factory,check=default_recognition(self.database,self.fingerprinter,self.config)
        from meloshelf.application import enrichment_api as _enrichment
        self.jobs=JobService(self.database,recognition_factory=recognition_factory or factory,
                             preflight=preflight or check,
                             on_scan_completed=lambda _job_id: _enrichment.after_scan(self))
        from meloshelf.application.organize_service import OrganizeService
        from meloshelf.infrastructure.tag_writer import write_tags
        from meloshelf.application.album_art import AlbumArtService
        self.release_group_search=release_group_search
        self.album_art=AlbumArtService(self.store,self.covers,self.artwork,self.db_path.parent/'organize-covers',search=self._release_group_search,fallbacks=self._art_extras)
        self.organizer=OrganizeService(self.database,self.jobs,tagger=write_tags,covers=self.album_art)
        from meloshelf.application.photo_batch import PhotoBatch
        self.photo_batch=PhotoBatch(self.store,self.repo,self.photos)
        from meloshelf.application.album_enrichment import AlbumEnrichment
        self.album_enrichment=AlbumEnrichment(self.store,self.repo,self.album_art)

    def _art_extras(self):
        """Fanart.tv / TheAudioDB clients that are switched on and keyed; never the first choice, never embedded."""
        return list(self.art_clients) if self.art_clients is not None else self.config.art_clients()

    def _release_group_search(self,album,artist):
        """The album catalogue search: an injected one in tests, otherwise MusicBrainz when a contact is configured."""
        if self.release_group_search is not None:return self.release_group_search(album,artist)
        agent=self.config.musicbrainz_user_agent()[0]
        if not agent:return []
        from meloshelf.infrastructure.music_api import MusicBrainzClient
        return MusicBrainzClient(agent).search_release_groups(album,artist)

    def refresh_fpcalc(self):
        """A changed Chromaprint location applies to the running application; no restart is needed."""
        if isinstance(self.fingerprinter,Fingerprinter):
            location=self.config.fpcalc_location()
            self.fpcalc_location=location
            self.fingerprinter.executable=location.path or 'fpcalc'

    def _fallback_client_for(self,offline):
        """The text-search client: the injected one, else MusicBrainz with the saved contact. None when there is no
        contact (an offline run never needs one)."""
        if self.fallback_client is not None:return self.fallback_client
        if offline:return None
        agent=self.config.musicbrainz_user_agent()[0]
        if not agent:return None
        from meloshelf.infrastructure.music_api import MusicBrainzClient
        return MusicBrainzClient(agent)


def handler_json(handler, status, data):
    raw=json.dumps(data,ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type','application/json; charset=utf-8')
    handler.send_header('Cache-Control','no-store')
    handler.send_header('X-Content-Type-Options','nosniff')
    handler.send_header('Content-Length',str(len(raw)))
    handler.end_headers();handler.wfile.write(raw)


def _job_answer(result, accepted=None):
    """Started or accepted jobs answer with no error; a refused job answers with its translated reason."""
    if result.get('started') or accepted:
        return {**result, 'error': ''}
    return {**result, **errors.from_text(result.get('reason', ''))}


class BadRequest(ValueError):
    pass


def _neutralise(value):
    """Spreadsheets run a cell that starts with = + - @ tab or CR as a formula. Text from audio tags is
    untrusted, so such cells get a leading apostrophe; the text itself is otherwise unchanged."""
    if isinstance(value,str) and value[:1] in ('=','+','-','@','\t','\r'):
        return "'"+value
    return value


def _paging(query, default=60, maximum=300):
    try:
        limit=max(1,min(maximum,int(query.get('limit',[str(default)])[0])))
        offset=max(0,int(query.get('offset',['0'])[0]))
    except ValueError:
        raise BadRequest('Invalid paging') from None
    return limit,offset


def _text(query, name):return query.get(name,[''])[0][:200]


def _qa_filter(query):
    from meloshelf.infrastructure.qa_filters import parse_filter
    try:return parse_filter(query)
    except ValueError as exc:raise BadRequest(str(exc)) from None


class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):pass
    def finish(self):
        """Closing the stream after a player has already gone away is normal, not an error."""
        try:super().finish()
        except (ConnectionAbortedError,ConnectionResetError,BrokenPipeError):pass

    def _authorized_host(self):
        host=self.headers.get('Host','')
        port=self.server.server_address[1]
        return host in (f'127.0.0.1:{port}',f'localhost:{port}')

    def _read_json(self,limit=32768):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<0 or size>limit:raise ValueError('Payload too large')
            return json.loads(self.rfile.read(size))
        except (ValueError,UnicodeDecodeError,json.JSONDecodeError):
            return None

    def _guarded(self,route):
        """Turn failures into a JSON answer instead of a dropped connection."""
        try:return route()
        except BadRequest as exc:return handler_json(self,400,errors.from_text(exc))
        except DatabaseBusyError as exc:
            log.warning('database busy method=%s route=%s detail=%s',self.command,urlparse(self.path).path,type(exc).__name__)
            return handler_json(self,503,errors.body('library_busy'))
        except sqlite3.Error as exc:
            log.exception('database error method=%s route=%s',self.command,urlparse(self.path).path)
            return handler_json(self,500,errors.body('library_database_error'))

    def _csv_export(self):
        import csv, io
        rows=self.server.qa.export_rows()
        columns=[c for c in (rows[0].keys() if rows else ['path']) if c!='candidates']+['candidates']
        buffer=io.StringIO()
        writer=csv.DictWriter(buffer,fieldnames=columns)
        writer.writeheader()
        for row in rows:
            safe={key:_neutralise(value) for key,value in row.items() if key!='candidates'}
            writer.writerow({**safe,'candidates':_neutralise(json.dumps(row['candidates'],ensure_ascii=False))})
        raw=buffer.getvalue().encode('utf-8-sig')
        self.send_response(200);self.send_header('Content-Type','text/csv; charset=utf-8')
        self.send_header('Content-Disposition','attachment; filename="meloshelf-qa.csv"')
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)

    def _audio(self,requested):
        """Read-only playback of an indexed library file. Anything outside the index is refused."""
        AUDIO_TYPES={'.mp3':'audio/mpeg','.flac':'audio/flac','.m4a':'audio/mp4','.aac':'audio/aac',
                     '.ogg':'audio/ogg','.opus':'audio/ogg','.wav':'audio/wav','.aiff':'audio/aiff','.aif':'audio/aiff'}
        wanted=requested.replace('\\','/')
        with self.server.database.read() as db:
            row=db.execute("SELECT path FROM tracks WHERE replace(path,'\\','/')=?",(wanted,)).fetchone()
        if row is None:return handler_json(self,404,errors.body('file_not_indexed'))
        path=Path(row['path'])
        if not path.is_file():return handler_json(self,404,errors.body('file_missing'))
        size=path.stat().st_size
        start,end,status=0,size-1,200
        spec=self.headers.get('Range','')
        if spec.startswith('bytes='):
            first,_,last=spec[6:].partition('-')
            try:
                if first=='' :start=max(0,size-int(last));
                else:start=int(first)
                if last and first!='':end=min(size-1,int(last))
            except ValueError:return handler_json(self,416,errors.body('invalid_range'))
            if start>end or start>=size:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
            status=206
        length=end-start+1
        self.send_response(status)
        self.send_header('Content-Type',AUDIO_TYPES.get(path.suffix.lower(),'application/octet-stream'))
        self.send_header('Accept-Ranges','bytes');self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(length))
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        try:
            self.end_headers()
            with path.open('rb') as handle:
                handle.seek(start);remaining=length
                while remaining>0:
                    chunk=handle.read(min(1<<16,remaining))
                    if not chunk:break
                    self.wfile.write(chunk);remaining-=len(chunk)
        except (ConnectionAbortedError,ConnectionResetError,BrokenPipeError):
            # The player closed the connection (a seek, a skip, a closed tab). That is normal playback, not a failure.
            self.close_connection=True

    def do_GET(self):return self._guarded(self._get)
    def do_POST(self):return self._guarded(self._post)

    # ── GET ──────────────────────────────────────────────────────────────────

    def _page(self):
        html=self.server.html_path.read_text(encoding='utf-8')
        raw=DEMO_BLOCK.sub('',html).replace('__MELOSHELF_TOKEN__',self.server.token).encode('utf-8')
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)

    def _get(self):
        if not self._authorized_host():return handler_json(self,403,errors.body('host_not_allowed'))
        parsed=urlparse(self.path);q=parse_qs(parsed.query);server=self.server
        route=parsed.path
        if route in ('/','/index.html'):return self._page()
        if route.startswith('/api/organize/') and route!='/api/organize/art/image':
            from meloshelf.application import organize_api
            return handler_json(self,*organize_api.get(server,route,q))
        if route=='/api/status':return handler_json(self,200,server.jobs.status())
        if route=='/api/activity':return handler_json(self,200,server.jobs.activity())
        if route=='/api/config':
            summary=self.server.config.summary()
            return handler_json(self,200,{'mode':'local','read_only':True,'version':__version__,
              'database':str(server.db_path),
              'fpcalc_available':summary['fpcalc']['configured'],
              'acoustid_configured':summary['acoustid']['configured'],
              'musicbrainz_configured':summary['musicbrainz']['configured'],
              'configuration':summary,
              'instance':{'process':os.getpid(),'port':server.server_address[1]},
              'max_identify_batch':MAX_IDENTIFY_BATCH})
        if route=='/api/stats':return handler_json(self,200,server.repo.counts())
        if route=='/api/qa/progress':return handler_json(self,200,server.qa.progress())
        if route=='/api/qa/next':
            path=server.qa.next_item(skip=[x[:4096] for x in q.get('skip',[])[:500]],filters=_qa_filter(q))
            return handler_json(self,200,server.qa.item(path) if path else {'path':None})
        if route=='/api/qa/item':
            item=server.qa.item(q.get('path',[''])[0])
            return handler_json(self,200,item) if item else handler_json(self,404,errors.body('no_proposal'))
        if route in ('/api/qa/version/next','/api/qa/version/progress','/api/qa/version/report'):
            from meloshelf.infrastructure.version_qa import VersionQa
            version_qa=VersionQa(server.database)
            if route.endswith('/progress'):return handler_json(self,200,version_qa.progress())
            if route.endswith('/report'):return handler_json(self,200,version_qa.report())
            try:item=version_qa.next_item(skip=[x[:4096] for x in q.get('skip',[])[:500]],verdict=_text(q,'verdict'))
            except ValueError as exc:raise BadRequest(str(exc)) from None
            return handler_json(self,200,item or {'path':None})
        if route=='/api/qa/bulk/candidates':
            from meloshelf.infrastructure.qa_bulk import QaBulk
            limit,offset=_paging(q,50,100)
            return handler_json(self,200,QaBulk(server.database).candidates(query=_text(q,'q'),limit=limit,offset=offset,filters=_qa_filter(q)))
        if route=='/api/qa/categories':
            from meloshelf.domain.sample_category import CATEGORIES
            return handler_json(self,200,{'categories':[{'key':c.key,'label':c.label} for c in CATEGORIES]})
        if route=='/api/qa/bulk/batches':
            from meloshelf.infrastructure.qa_bulk import QaBulk
            return handler_json(self,200,{'batches':QaBulk(server.database).batches()})
        if route=='/api/qa/export':return self._csv_export()
        if route=='/api/qa/report':return handler_json(self,200,server.qa.report())
        if route=='/api/fallback/status':return handler_json(self,200,server.fallback_jobs.status())
        if route=='/api/fallback/results':
            from meloshelf.infrastructure.fallback_store import FallbackStore
            with server.database.read() as db:
                paths=[r[0] for r in db.execute('SELECT path FROM fallback_results ORDER BY created_at DESC LIMIT 200')]
            store=FallbackStore(server.database)
            return handler_json(self,200,{'results':[store.get(p) for p in paths]})
        if route=='/api/metadata/groups':
            from meloshelf.infrastructure.correction_memory import CorrectionMemory
            groups=MetadataService(server.database).groups(folder=_text(q,'folder') or None)
            memory=CorrectionMemory(server.database)
            memory.reconcile(groups)  # a confirmed correction whose proposal changed is recorded as a conflict
            return handler_json(self,200,{'groups':memory.annotate(groups)})
        if route=='/api/artist-recordings':
            credit=_text(q,'credit')
            if not credit:return handler_json(self,400,errors.body('credit_required'))
            try:
                offset=int(_text(q,'offset') or 0);limit=int(_text(q,'limit') or 50)
            except ValueError:return handler_json(self,400,errors.body('paging_not_integer'))
            if not 1<=limit<=200 or offset<0:return handler_json(self,400,errors.body('paging_out_of_range',max=200))
            rows,total=server.repo.credit_recordings(credit,query=_text(q,'q'),offset=offset,limit=limit)
            return handler_json(self,200,{'credit':credit,'total':total,'offset':offset,'limit':limit,'recordings':rows})
        if route=='/api/artist-recordings/paths':
            credit=_text(q,'credit')
            if not credit:return handler_json(self,400,errors.body('credit_required'))
            paths,total=server.repo.credit_recording_paths(credit,query=_text(q,'q'),cap=5000)
            return handler_json(self,200,{'credit':credit,'total':total,'paths':paths,'capped':total>len(paths)})
        if route=='/api/artist-suggestions':
            from meloshelf.application import suggestion_api
            return handler_json(self,200,suggestion_api.list_open(server.database))
        if route=='/api/ui-preferences':
            from meloshelf.application import ui_preferences
            return handler_json(self,*ui_preferences.get_preferences(server))
        if route=='/api/artist-enrichment/settings':
            from meloshelf.application import enrichment_api
            return handler_json(self,*enrichment_api.get_settings(server))
        if route=='/api/artist-enrichment/status':
            from meloshelf.application import enrichment_api
            return handler_json(self,*enrichment_api.status(server,_text(q,'id')))
        if route=='/api/artist-corrections':
            from meloshelf.application import artist_corrections_api as artist_api
            return handler_json(self,200,artist_api.list_events(server.database))
        if route=='/api/artist-corrections/preview':
            from meloshelf.application import artist_corrections_api as artist_api
            action=_text(q,'action')
            if action=='merge':body=artist_api.preview_merge(server.database,_text(q,'source'),_text(q,'target'))
            elif action=='keep-separate':body=artist_api.preview_keep_separate(server.database,_text(q,'a'),_text(q,'b'))
            elif action=='undo':
                try:event_id=int(_text(q,'event_id'))
                except ValueError:return handler_json(self,400,errors.body('correction_event_id_invalid'))
                body=artist_api.preview_undo(server.database,event_id)
            else:return handler_json(self,400,errors.body('correction_action_invalid'))
            return handler_json(self,200,body)
        if route=='/api/metadata/decisions':
            current=MetadataService(server.database).groups()
            return handler_json(self,200,{'decisions':MetadataStore(server.database).list_all(current)})
        if route=='/api/audio':return self._audio(q.get('path',[''])[0])
        if route=='/api/tracks':
            limit,offset=_paging(q,default=50,maximum=200)
            status=_text(q,'status')
            if status and status not in TRACK_STATUSES:raise BadRequest('Invalid status')
            search=_text(q,'q')
            from meloshelf.application import album_cover_api
            album,artist=_text(q,'album'),_text(q,'artist')
            tracks=album_cover_api.annotate_tracks(server,server.repo.list_tracks(limit=limit,offset=offset,query=search,status=status,album=album,artist=artist))
            return handler_json(self,200,{'tracks':tracks,
                                          'total':server.repo.count_tracks(query=search,status=status,album=album,artist=artist),'offset':offset,'limit':limit})
        if route=='/api/artists':
            limit,offset=_paging(q)
            artists,total=server.repo.artists(limit=limit,offset=offset,query=_text(q,'q'))
            return handler_json(self,200,{'artists':artists,'total':total,'offset':offset,'limit':limit})
        if route=='/api/track-artwork':
            # The picture inside an indexed file's own tags (read-only). Never served for a path that is not in the index.
            wanted=_text(q,'path')
            if not wanted:return handler_json(self,400,errors.body('path_required'))
            with server.database.read() as db:
                indexed=db.execute('SELECT 1 FROM tracks WHERE path=?',(wanted,)).fetchone()
            if not indexed:return handler_json(self,404,errors.body('file_not_in_library'))
            from meloshelf.infrastructure.embedded_art import read_embedded_art
            found=read_embedded_art(wanted)
            if found is None:
                server.repo.record_embedded_art(wanted,False)      # remember the answer: the listing stops asking
                return handler_json(self,404,errors.body('no_embedded_picture'))
            server.repo.record_embedded_art(wanted,True)
            content_type,data=found
            self.send_response(200);self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control','private, max-age=300');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if route=='/api/organize/art/image':
            from meloshelf.application import organize_api
            found=organize_api.art_image(server,_text(q,'album'),_text(q,'artist'))
            if found is None:return handler_json(self,404,errors.body('no_album_cover'))
            self.send_response(200);self.send_header('Content-Type',found[0])
            self.send_header('Cache-Control','private, max-age=60');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(found[1])));self.end_headers();self.wfile.write(found[1]);return
        if route in ('/api/album-cover','/api/album-cover/info'):
            album,artist=_text(q,'album'),_text(q,'artist')
            if not album or not artist:return handler_json(self,400,errors.body('album_and_artist_required'))
            if route.endswith('/info'):return handler_json(self,200,server.covers.info(album,artist))
            found=server.covers.cover(album,artist)
            if found is None:return handler_json(self,404,errors.body('no_album_cover'))
            content_type,data=found
            self.send_response(200);self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control','private, max-age=300');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if route in ('/api/artist-image','/api/artist-image/info'):
            from meloshelf.domain.artwork import UUID
            mb=_text(q,'mb')
            if not UUID.match(mb or ''):return handler_json(self,400,errors.body('artist_id_invalid'))
            if route.endswith('/info'):
                info=server.photos.info(mb)
                return handler_json(self,200 if info else 404,info or errors.body('no_artist_photo'))
            found=server.photos.image(mb)
            if found is None:return handler_json(self,404,errors.body('no_artist_photo'))
            content_type,data=found
            self.send_response(200);self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control','private, max-age=300');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if route=='/api/artwork':
            try:cover=server.artwork.cover(_text(q,'release'))
            except ArtworkError:return handler_json(self,400,errors.body('release_id_invalid'))
            if cover is None:return handler_json(self,404,errors.body('no_release_cover'))
            content_type,data=cover
            self.send_response(200);self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control','max-age=86400');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if route=='/api/settings':
            return handler_json(self,200,server.store.automation_settings())
        if route=='/api/artist-identities':
            limit,offset=_paging(q)
            artists,total=server.repo.artist_identities(limit=limit,offset=offset,query=_text(q,'q'))
            from meloshelf.application import photo_api
            artists=photo_api.annotate(server,artists)   # attribution and image URL only where a verified image applies
            return handler_json(self,200,{'artists':artists,'total':total,'offset':offset,'limit':limit})
        if route=='/api/artist-images/batch/status':
            from meloshelf.application import photo_api
            return handler_json(self,*photo_api.batch_status(server))
        if route=='/api/artist-profile':
            from meloshelf.application import photo_api
            return handler_json(self,*photo_api.profile(server,_text(q,'name')))
        if route=='/api/albums/enrich/status':
            from meloshelf.application import album_art_api
            return handler_json(self,*album_art_api.status(server))
        if route=='/api/album-art/image':
            from meloshelf.application import album_art_api
            found=album_art_api.image(server,_text(q,'album'),_text(q,'artist'))
            if found is None:return handler_json(self,404,errors.body('no_album_cover'))
            self.send_response(200);self.send_header('Content-Type',found[0])
            self.send_header('Cache-Control','private, max-age=60');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(found[1])));self.end_headers();self.wfile.write(found[1]);return
        if route=='/api/albums':
            limit,offset=_paging(q)
            albums,total=server.repo.albums(limit=limit,offset=offset,query=_text(q,'q'),artist=_text(q,'artist'),sort=_text(q,'sort'))
            from meloshelf.application import album_cover_api
            albums=album_cover_api.annotate(server,albums)   # cover state per album; a URL only where one applies
            return handler_json(self,200,{'albums':albums,'total':total,'offset':offset,'limit':limit})
        if route=='/api/matches':
            limit,offset=_paging(q,default=50,maximum=200)
            return handler_json(self,200,{'matches':server.store.list_results(limit,offset),
                                          'total':server.store.count_results(),'offset':offset,'limit':limit})
        if route=='/api/jobs/errors':
            job_id=_text(q,'job_id');code=_text(q,'code')
            if not job_id:raise BadRequest('job_id is required')
            limit,offset=_paging(q,default=100,maximum=500)
            outcome=describe(code)
            return handler_json(self,200,{'items':server.jobs.error_items(job_id,code or None,limit=limit,offset=offset),
                                          'outcome':{'label':outcome.label,'hint':outcome.hint,'retryable':outcome.retryable}})
        return handler_json(self,404,errors.body('not_found'))

    # ── POST ─────────────────────────────────────────────────────────────────

    def _refuse(self,message):
        """Answer 403 without having used the body. A small unread body is read and dropped first: closing a
        socket with unread data makes some clients see a connection reset instead of the 403."""
        try:
            size=int(self.headers.get('Content-Length','0'))
            if 0<size<=65536:self.rfile.read(size)
            else:self.close_connection=True
        except ValueError:self.close_connection=True
        return handler_json(self,403,errors.from_text(message))

    def _post(self):
        if not self._authorized_host():return self._refuse('Host not allowed')
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_address[1]}',f'http://localhost:{self.server.server_address[1]}'):
            return self._refuse('Origin not allowed')
        if self.headers.get('X-MeloShelf-Token')!=self.server.token:
            return self._refuse('Invalid session token')
        # Only the manual image upload may be large (a base64 image of at most 1 MB); everything else stays at 32 KB.
        route_path=urlparse(self.path).path
        payload=self._read_json(9_000_000 if route_path=='/api/qa/import' else
                                2_000_000 if route_path in ('/api/artist-images/manual','/api/album-covers/manual') else 32768)
        if not isinstance(payload,dict):return handler_json(self,400,errors.body('invalid_json'))
        path=urlparse(self.path).path;jobs=self.server.jobs
        if path.startswith('/api/organize/'):
            from meloshelf.application import organize_api
            return handler_json(self,*organize_api.post(self.server,path,payload))
        if path in ('/api/artist-images/batch','/api/artist-images/batch/control'):
            from meloshelf.application import photo_api
            return handler_json(self,*(photo_api.batch_start(self.server) if path=='/api/artist-images/batch' else photo_api.batch_control(self.server,payload)))
        if path in ('/api/albums/enrich','/api/albums/enrich/control'):
            from meloshelf.application import album_art_api
            return handler_json(self,*(album_art_api.start(self.server) if path=='/api/albums/enrich' else album_art_api.control(self.server,payload)))
        if path=='/api/scan':
            folder=payload.get('folder')
            if not isinstance(folder,str) or not folder.strip() or not Path(folder).expanduser().is_dir():
                return handler_json(self,400,errors.body('folder_not_found'))
            result=jobs.start_scan(folder)
            return handler_json(self,202 if result['started'] else 409,_job_answer(result))
        if path=='/api/identify':
            limit=payload.get('limit',5)
            if not type(limit) is int or not 1<=limit<=MAX_IDENTIFY_BATCH:
                return handler_json(self,400,errors.body('identify_limit',max=MAX_IDENTIFY_BATCH))
            offline=payload.get('offline',False)
            if not isinstance(offline,bool):return handler_json(self,400,errors.body('offline_not_boolean'))
            if not offline and not self.server.config.acoustid_key()[0]:
                return handler_json(self,409,errors.body('acoustid_key_required'))
            result=jobs.start_identify(limit,offline)
            accepted=result['started'] or result.get('nothing_to_do',False)  # an empty queue is not a conflict
            return handler_json(self,202 if accepted else 409,_job_answer(result,accepted))
        if path=='/api/settings':
            try:reevaluated=self.server.store.update_automation_settings(payload)
            except ValueError as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200,{'settings':self.server.store.automation_settings(),'reevaluated':reevaluated})
        if path=='/api/config':
            try:
                for name,value in payload.items():
                    if name=='acoustid_key':
                        if value in (None,''):self.server.config.clear_acoustid_key()
                        else:self.server.config.set_acoustid_key(str(value))
                    elif name=='musicbrainz_contact':self.server.config.set_musicbrainz_contact(str(value or ''))
                    elif name in ('fanart_project_key','fanart_client_key','theaudiodb_key'):
                        if value in (None,''):self.server.config.clear_art_key(name)
                        else:self.server.config.set_art_key(name,str(value))
                    elif name=='art_sources':self.server.config.set_art_sources(value)
                    elif name=='fpcalc_path':
                        self.server.config.set_fpcalc_path(str(value or ''))
                        self.server.refresh_fpcalc()
                    else:return handler_json(self,400,errors.body('setting_unknown',name=name))
            except ConfigError as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200,{'configuration':self.server.config.summary()})
        if path=='/api/config/test':
            target=payload.get('target')
            if target not in ('acoustid','musicbrainz','fpcalc'):return handler_json(self,400,errors.body('config_target_invalid'))
            return handler_json(self,200,self.server.connection_tester(target,self.server.config))
        if path=='/api/artist-suggestions/decide':
            from meloshelf.application import suggestion_api
            return handler_json(self,*suggestion_api.decide(self.server.database,payload))
        if path=='/api/ui-preferences':
            from meloshelf.application import ui_preferences
            return handler_json(self,*ui_preferences.set_preferences(self.server,payload))
        if path=='/api/artist-enrichment/settings':
            from meloshelf.application import enrichment_api
            return handler_json(self,*enrichment_api.set_settings(self.server,payload))
        if path=='/api/artist-enrichment/start':
            from meloshelf.application import enrichment_api
            return handler_json(self,*enrichment_api.start(self.server,payload))
        if path=='/api/artist-enrichment/control':
            from meloshelf.application import enrichment_api
            return handler_json(self,*enrichment_api.control(self.server,payload))
        if path in ('/api/album-covers/manual','/api/album-covers/clear','/api/album-covers/undo'):
            from meloshelf.application import album_cover_api
            handler={'manual':album_cover_api.manual,'clear':album_cover_api.clear,'undo':album_cover_api.undo}[path.rsplit('/',1)[1]]
            return handler_json(self,*handler(self.server,payload))
        if path in ('/api/artist-images/fetch','/api/artist-images/manual','/api/artist-images/clear','/api/artist-images/undo'):
            from meloshelf.application import photo_api
            handler={'fetch':photo_api.fetch,'manual':photo_api.manual,'clear':photo_api.clear,'undo':photo_api.undo}[path.rsplit('/',1)[1]]
            return handler_json(self,*handler(self.server,payload))
        if path=='/api/artist-links/lookup':
            from meloshelf.application.artist_lookup import ArtistLookup
            from meloshelf.infrastructure.music_api import MusicBrainzClient
            limit=payload.get('limit',25)
            if not type(limit) is int or not 1<=limit<=100:return handler_json(self,400,errors.body('limit_out_of_range',max=100))
            client=self.server.artist_client
            if client is None:
                agent=self.server.config.musicbrainz_user_agent()[0]
                if not agent:return handler_json(self,409,errors.body('lookup_contact_required'))
                client=MusicBrainzClient(agent)
            return handler_json(self,202,ArtistLookup(self.server.database,client).run(limit=limit))
        if path in ('/api/artist-corrections/merge','/api/artist-corrections/keep-separate','/api/artist-corrections/undo'):
            from meloshelf.application import artist_corrections_api as artist_api
            status,body=artist_api.write(self.server.database,path,payload)
            return handler_json(self,status,body)
        if path=='/api/metadata/decision':
            from meloshelf.infrastructure.metadata_store import MetadataError
            key=payload.get('key');action=payload.get('action')
            if not isinstance(key,str) or not key:return handler_json(self,400,errors.body('key_required'))
            # The proposal comes from the server's own analysis; anything the browser sends besides key and action is ignored.
            group=next((g for g in MetadataService(self.server.database).groups() if g['key']==key),None)
            if group is None:return handler_json(self,404,errors.body('group_not_found'))
            try:
                MetadataStore(self.server.database).decide(key,action,folder=group['folder'],field=group['field'],
                    original=group['original'],proposed=group['proposed'],classification=group['classification'],
                    paths=group['paths'])
            except MetadataError as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200,{'saved':True,'decision':MetadataStore(self.server.database).get(key)})
        if path=='/api/fallback/start':
            return handler_json(self,*self.server.fallback_jobs.start(limit=payload.get('limit',25),offline=payload.get('offline',False)))
        if path=='/api/fallback/cancel':return handler_json(self,200,self.server.fallback_jobs.cancel())
        if path=='/api/fallback/run':
            from meloshelf.application.fallback import FallbackService
            from meloshelf.application.fallback_runner import FallbackRunner
            from meloshelf.infrastructure.music_api import MusicBrainzClient
            limit=payload.get('limit',25)
            if not type(limit) is int or not 1<=limit<=100:return handler_json(self,400,errors.body('limit_out_of_range',max=100))
            offline=payload.get('offline',False)
            if not isinstance(offline,bool):return handler_json(self,400,errors.body('offline_not_boolean'))
            client=self.server.fallback_client
            if client is None and not offline:
                agent=self.server.config.musicbrainz_user_agent()[0]
                if not agent:return handler_json(self,409,errors.body('search_contact_required'))
                client=MusicBrainzClient(agent)
            service=FallbackService(self.server.database,client,offline=offline)
            summary=FallbackRunner(self.server.database,service).run(limit=limit)
            return handler_json(self,202,summary)
        if path=='/api/qa/label':
            from meloshelf.infrastructure.qa_store import QaError
            if not isinstance(payload.get('path'),str):return handler_json(self,400,errors.body('path_required'))
            try:
                server_qa=self.server.qa
                server_qa.set_label(payload['path'],payload.get('label'),
                                    corrected_recording_id=str(payload.get('corrected_recording_id') or ''),
                                    corrected_title=str(payload.get('corrected_title') or ''),
                                    corrected_artist=str(payload.get('corrected_artist') or ''))
            except QaError as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200,{'saved':True,'progress':self.server.qa.progress()})
        if path=='/api/qa/version/label':
            from meloshelf.infrastructure.qa_store import QaError
            from meloshelf.infrastructure.version_qa import VersionQa
            if not isinstance(payload.get('path'),str):return handler_json(self,400,errors.body('path_required'))
            version_qa=VersionQa(self.server.database)
            try:version_qa.set_label(payload['path'],payload.get('verdict'))
            except QaError as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200,{'saved':True,'progress':version_qa.progress()})
        if path in ('/api/qa/bulk','/api/qa/bulk/undo'):
            from meloshelf.infrastructure.qa_bulk import QaBulk
            from meloshelf.infrastructure.qa_store import QaError
            bulk=QaBulk(self.server.database)
            try:
                if path.endswith('/undo'):
                    return handler_json(self,200,{**bulk.undo(payload.get('batch_id')),'progress':self.server.qa.progress()})
                mode=payload.get('mode')
                if mode not in ('preview','apply'):return handler_json(self,400,errors.body('qa_mode_invalid'))
                if mode=='preview':return handler_json(self,200,bulk.preview(payload.get('paths'),payload.get('label')))
                result=bulk.apply(payload.get('paths'),payload.get('label'))
            except (ValueError,QaError) as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200,{**result,'progress':self.server.qa.progress()})
        if path=='/api/qa/import':
            from meloshelf.infrastructure.qa_import import QaImporter
            mode,text,overwrite=payload.get('mode'),payload.get('csv'),payload.get('overwrite',[])
            if mode not in ('preview','apply') or not isinstance(text,str):
                return handler_json(self,400,errors.body('qa_import_request_invalid'))
            if not isinstance(overwrite,list) or not all(isinstance(p,str) for p in overwrite) or len(overwrite)>20000:
                return handler_json(self,400,errors.body('qa_overwrite_invalid'))
            importer=QaImporter(self.server.database)
            try:
                if mode=='preview':return handler_json(self,200,importer.plan(text))
                result=importer.apply(text,overwrite)
            except ValueError as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200,{**result,'progress':self.server.qa.progress()})
        if path=='/api/stop':
            jobs.pause_all();return handler_json(self,200,{'stopping':True})
        if path.startswith('/api/jobs/'):
            action={'pause':jobs.pause,'resume':jobs.resume,'cancel':jobs.cancel,'retry':jobs.retry_failed}.get(path.rsplit('/',1)[1])
            if not action:return handler_json(self,404,errors.body('not_found'))
            job_id=payload.get('job_id')
            if not isinstance(job_id,str) or not job_id:return handler_json(self,400,errors.body('job_id_required'))
            result=action(job_id)
            accepted=result.get('started',result.get('ok',False))
            return handler_json(self,202 if accepted else 409,_job_answer(result,accepted))
        if path=='/api/decision':
            decision=payload.get('decision');f=payload.get('path');recording_id=payload.get('recording_id','')
            if not isinstance(f,str) or decision not in ('accepted','rejected','pending'):
                return handler_json(self,400,errors.body('invalid_decision'))
            try:ok=self.server.store.set_decision(f,decision,recording_id=recording_id)
            except ValueError as exc:return handler_json(self,400,errors.from_text(exc))
            return handler_json(self,200 if ok else 404,{'saved':ok,'note':'Recording decision only: album edition unresolved, audio unchanged'})
        return handler_json(self,404,errors.body('not_found'))


def _result(ok: bool, state: str, code: str, *, params=None, http_status=None, provider_code=None, retryable=False) -> dict:
    """One shape for every check. The text is the registered sentence for ``code``; keys, provider text and
    local paths never appear in it."""
    answer = ec.body(code, **(params or {}))
    return {'ok': ok, 'state': state, 'code': code, 'params': answer['params'], 'message': answer['error'],
            'http_status': http_status, 'provider_code': ec.provider_number(provider_code), 'retryable': retryable}


def _probe_fingerprint(config: AppConfig) -> tuple[str, int]:
    """A real Chromaprint fingerprint of a generated tone. Never reads the user's library."""
    import math
    import struct
    import tempfile
    import wave
    location = config.fpcalc_location()
    if not location.path:
        raise RuntimeError(location.problem or 'fpcalc was not found')
    rate, seconds = 44100, 12
    frames = b''.join(struct.pack('<h', int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
                      for i in range(rate * seconds))
    with tempfile.TemporaryDirectory(prefix='meloshelf-probe-') as folder:
        path = Path(folder) / 'probe.wav'
        with wave.open(str(path), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(frames)
        fingerprint = Fingerprinter(executable=location.path).compute(path)
    return fingerprint.value, fingerprint.duration


_ACOUSTID_FAILURES = {
    'invalid_key': ('invalid_key', 'acoustid_invalid_key'),
    'request_rejected': ('request_rejected', 'acoustid_request_rejected'),
    'rate_limited': ('rate_limited', 'acoustid_rate_limited'),
    'dns': ('unreachable', 'acoustid_dns'),
    'tls': ('unreachable', 'acoustid_tls'),
    'timeout': ('unreachable', 'acoustid_timeout'),
    'connection': ('unreachable', 'acoustid_connection'),
    'provider_error': ('provider_error', 'acoustid_provider_error'),
    'malformed': ('provider_error', 'acoustid_malformed'),
}


def _failure_params(code: str, exc: APIError) -> dict:
    if code in ('acoustid_invalid_key', 'acoustid_request_rejected'):
        number = ec.provider_number(exc.provider_code)
        return {'code': str(number) if number is not None else 'no code'}
    if code in ('acoustid_provider_error', 'acoustid_rejected'):
        return {'status': ec.http_status(exc.status)}
    return {}


def _acoustid_failure(exc: APIError) -> dict:
    state, code = _ACOUSTID_FAILURES.get(exc.kind, ('provider_rejected', 'acoustid_rejected'))
    log.warning('acoustid check refused kind=%s status=%s', exc.kind, exc.status)
    return _result(False, state, code, params=_failure_params(code, exc), http_status=exc.status,
                   provider_code=exc.provider_code, retryable=exc.retryable)


def run_connection_test(target: str, config: AppConfig, *, transport=None, fingerprint=None) -> dict:
    """Check one requirement with the real provider, using the saved settings as they are now.

    ``transport`` and ``fingerprint`` exist so tests exercise this same code path offline.
    Returned messages never contain a secret.
    """
    if target == 'fpcalc':
        location = config.fpcalc_location()
        if not location.path:
            log.info('fpcalc unavailable: %s', location.problem)
            return _result(False, 'missing', 'fpcalc_missing')
        ok = check_fpcalc(location.path)
        return _result(ok, 'available' if ok else 'failed', 'fpcalc_available' if ok else 'fpcalc_failed')
    if target == 'acoustid':
        return _test_acoustid(config, transport=transport, fingerprint=fingerprint)
    return _test_musicbrainz(config, transport=transport)


def _test_acoustid(config: AppConfig, *, transport=None, fingerprint=None) -> dict:
    key, _source, store_error = config.acoustid_key_detail()
    if not key:
        if store_error:
            return _result(False, 'store_unavailable', 'acoustid_key_store_unavailable')
        return _result(False, 'missing', 'acoustid_key_missing')
    try:
        fingerprint_value, duration = (fingerprint or (lambda: _probe_fingerprint(config)))()
    except Exception:  # the key is not sent when no real fingerprint exists
        log.info('chromaprint could not create a test fingerprint')
        return _result(False, 'fingerprint_unavailable', 'fingerprint_unavailable')
    client = AcoustIDClient(key, transport=transport or _request_json,
                            retry=RetryPolicy(max_attempts=2, base_delay=0.5), timeout=15)
    try:
        client.lookup(fingerprint_value, duration)
    except APIError as exc:
        return _acoustid_failure(exc)
    except Exception as exc:  # never let a provider problem crash the page; the text may hold request data
        log.error('acoustid check failed unexpectedly (%s)', type(exc).__name__)
        return _result(False, 'unreachable', 'acoustid_unexpected', params={'exception': type(exc).__name__})
    return _result(True, 'valid', 'acoustid_valid', http_status=200)


def _test_musicbrainz(config: AppConfig, *, transport=None) -> dict:
    agent = config.musicbrainz_user_agent()[0]
    if not agent:
        return _result(False, 'missing', 'musicbrainz_contact_missing')
    url = 'https://musicbrainz.org/ws/2/recording/?query=recording:meloshelf&limit=1&fmt=json'
    try:
        payload = (transport or _request_json)(url, headers={'User-Agent': agent, 'Accept': 'application/json'}, timeout=20)
    except APIError as exc:
        log.warning('musicbrainz check refused kind=%s status=%s', exc.kind, exc.status)
        state = 'provider_error' if exc.kind in ('provider_error', 'malformed') else 'unreachable'
        return _result(False, state, 'musicbrainz_unreachable', params={'status': ec.http_status(exc.status)},
                       http_status=exc.status, retryable=exc.retryable)
    ok = isinstance(payload, dict) and 'recordings' in payload
    return _result(ok, 'valid' if ok else 'unexpected',
                   'musicbrainz_answered' if ok else 'musicbrainz_unexpected_answer',
                   http_status=200 if ok else None)


def create_server(*, db_path: Path | None=None, html_path: Path | None=None, port=0, **options):
    db_path=Path(db_path) if db_path else Path.home()/'.meloshelf'/'library.sqlite3'
    html_path=Path(html_path) if html_path else Path(__file__).parents[1]/'presentation'/'web'/'index.html'
    return MeloShelfServer(('127.0.0.1',port),db_path,html_path,**options)
