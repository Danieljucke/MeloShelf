"""Conservative acoustic recognition. Proposals only; no tag writing or file moves."""
import hashlib
import logging
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path

from meloshelf.domain.decisions import DEFAULT_THRESHOLD, status_for
from meloshelf.domain.matching import Evidence, assess_candidate, assess_match
from meloshelf.infrastructure.match_store import THRESHOLD_KEY
from meloshelf.infrastructure.fingerprint import Fingerprint, FingerprintError, FingerprintUnavailable
from meloshelf.infrastructure.music_api import APIError, OperationCancelled, RecordingCandidate

log = logging.getLogger(__name__)
ENRICH_LIMIT = 5


class RecognitionStatus(str, Enum):
    REVIEW='review'
    UNMATCHED='unmatched'
    OFFLINE='offline'
    CHANGED='changed'
    ERROR='error'


@dataclass(frozen=True)
class RecognitionReport:
    path: str
    status: RecognitionStatus
    candidates: tuple[RecordingCandidate, ...] = ()
    reason: str = ''
    code: str = ''
    assessment: str = ''
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()
    recording_status: str = ''


def classify_api_error(exc: APIError) -> str:
    if exc.kind in ('dns', 'tls', 'timeout', 'connection'):
        return 'network_error'
    if exc.status in (401, 403) or exc.kind == 'invalid_key':
        return 'provider_auth'
    if exc.status == 429:
        return 'rate_limited'
    if exc.status is not None and exc.status >= 500:
        return 'provider_unavailable'
    return 'network_error' if exc.retryable else 'provider_error'


def _release_details(payload: dict) -> tuple[dict, ...]:
    details = []
    for release in payload.get('releases', []):
        if not isinstance(release, dict) or not release.get('title'):
            continue
        group = release.get('release-group') if isinstance(release.get('release-group'), dict) else {}
        details.append({'id': release.get('id', ''), 'title': release['title'], 'date': release.get('date', ''),
                        'country': release.get('country', ''), 'primary_type': group.get('primary-type') or '',
                        'group_id': group.get('id') or '',
                        'secondary_types': list(group.get('secondary-types') or [])})
    return tuple(details)


def _artist_credit(payload: dict) -> str:
    parts = []
    for credit in payload.get('artist-credit', []):
        if isinstance(credit, dict):
            parts.append(credit.get('name', credit.get('artist', {}).get('name', '')) + credit.get('joinphrase', ''))
        else:
            parts.append(str(credit))
    return ''.join(parts).strip()


class RecognitionService:
    def __init__(self, fingerprinter, acoustid, musicbrainz, store, *, offline=False):
        self.fingerprinter=fingerprinter
        self.acoustid=acoustid
        self.musicbrainz=musicbrainz
        self.store=store
        self.offline=offline

    @staticmethod
    def _file_identity(path: Path):
        st=path.stat()
        return (st.st_size,st.st_mtime_ns)

    def identify(self, path: Path, *, tags: dict | None = None, on_stage=None) -> RecognitionReport:
        """Never raises for a per-file failure: the report explains it. Cancellation propagates."""
        path=Path(path)
        stage=on_stage or (lambda _name: None)
        def failed(code, reason, status=RecognitionStatus.ERROR):
            return RecognitionReport(str(path), status, reason=reason, code=code)
        try:
            return self._identify(path, tags or {}, stage, failed)
        except OperationCancelled:
            raise
        except FileNotFoundError as exc:
            return failed('file_missing', f'{type(exc).__name__}: {exc}')
        except OSError as exc:
            return failed('file_unreadable', f'{type(exc).__name__}: {exc}')
        except FingerprintUnavailable as exc:
            return failed('fpcalc_unavailable', str(exc))
        except FingerprintError as exc:
            return failed('invalid_audio', str(exc))
        except APIError as exc:
            return failed(classify_api_error(exc), str(exc))
        except Exception as exc:  # reported per file so one bad track cannot end a whole job
            log.exception('identification failed path=%s', path)
            return failed('internal_error', f'{type(exc).__name__}: {exc}')

    def _fingerprint(self, path: Path, identity) -> tuple[Fingerprint, bool]:
        key='fingerprint:v1:'+hashlib.sha256(f'{path.resolve()}:{identity[0]}:{identity[1]}'.encode('utf-8')).hexdigest()
        cached=self.store.get_cache(key)
        if cached:
            return Fingerprint(value=cached['fingerprint'],duration=cached['duration']), True
        fingerprint=self.fingerprinter.compute(path)
        if identity==self._file_identity(path):
            self.store.put_cache(key,{'fingerprint':fingerprint.value,'duration':fingerprint.duration})
        return fingerprint, False

    def _lookup(self, fingerprint) -> list[RecordingCandidate] | None:
        """Cached AcoustID candidates; None when a lookup is needed but not possible offline."""
        key='acoustid:v1:'+hashlib.sha256(f'{fingerprint.duration}:{fingerprint.value}'.encode()).hexdigest()
        data=self.store.get_cache(key)
        if data is None:
            if self.offline or self.acoustid is None:
                return None
            data=[asdict(m) for m in self.acoustid.lookup(fingerprint.value, fingerprint.duration)]
            self.store.put_cache(key,data)
        return [RecordingCandidate(recording_id=d['recording_id'],title=d['title'],artist=d['artist'],
                                   acoustid_score=float(d['acoustid_score']),releases=tuple(d.get('releases') or ()),
                                   duration=d.get('duration')) for d in data]

    def _enrich(self, candidate: RecordingCandidate, warnings: list[str]) -> RecordingCandidate:
        key='musicbrainz:v1:'+candidate.recording_id
        payload=self.store.get_cache(key)
        if payload is None:
            try:
                payload=self.musicbrainz.lookup_recording(candidate.recording_id)
            except (APIError,ValueError) as exc:
                log.warning('metadata lookup failed recording=%s detail=%s',candidate.recording_id,exc)
                if 'metadata_lookup_failed' not in warnings:
                    warnings.append('metadata_lookup_failed')
                return candidate
            self.store.put_cache(key,payload)
        details=_release_details(payload)
        length=payload.get('length')
        return replace(candidate,title=payload.get('title') or candidate.title,
                       artist=_artist_credit(payload) or candidate.artist,
                       releases=tuple(dict.fromkeys(d['title'] for d in details)),release_details=details,
                       duration=length/1000 if isinstance(length,(int,float)) and length>0 else candidate.duration)

    def _identify(self, path, tags, stage, failed) -> RecognitionReport:
        before=self._file_identity(path)
        stage('FINGERPRINTING')
        fingerprint,_=self._fingerprint(path,before)
        if before!=self._file_identity(path):
            return failed('file_changed','Source changed during fingerprinting',RecognitionStatus.CHANGED)

        stage('IDENTIFYING')
        candidates=self._lookup(fingerprint)
        if candidates is None:
            if self.offline:
                return failed('offline_no_cache','No cached fingerprint match',RecognitionStatus.OFFLINE)
            return failed('acoustid_key_missing','AcoustID key is not configured')

        warnings=[]
        if candidates and self.musicbrainz and not self.offline:
            stage('ENRICHING')
            candidates=[self._enrich(c,warnings) for c in candidates[:ENRICH_LIMIT]]+candidates[ENRICH_LIMIT:]
        if before!=self._file_identity(path):
            return failed('file_changed','Source changed during remote lookup',RecognitionStatus.CHANGED)

        local={'title':str(tags.get('title') or ''),'artist':str(tags.get('artist') or ''),'album':str(tags.get('album') or '')}
        def _score(c):
            release_titles=tuple(r.get('title','') for r in (c.release_details or ()))
            return assess_candidate(provider_score=c.acoustid_score,title=c.title,artist=c.artist,
                                    duration=c.duration,local_title=local['title'],local_artist=local['artist'],
                                    local_duration=fingerprint.duration,releases=release_titles)
        scored=sorted(((c,_score(c)) for c in candidates),
                      key=lambda pair:(pair[1].score,pair[1].provider_score),reverse=True)
        verdict=assess_match([e for _,e in scored])
        recording_status=status_for([e for _,e in scored],**self.store.automation_policy())
        proposable=verdict.verdict not in ('none','insufficient')
        status=RecognitionStatus.REVIEW if proposable else RecognitionStatus.UNMATCHED
        code='' if proposable else ('no_match' if verdict.verdict=='none' else 'insufficient_evidence')
        report=RecognitionReport(str(path),status,tuple(c for c,_ in scored),reason=verdict.reason,code=code,
                                 assessment=verdict.verdict,evidence=tuple(e for _,e in scored),warnings=tuple(warnings),
                                 recording_status=recording_status)
        self.store.put_result(path,*before,status.value,{
            'candidates':[{**asdict(c),'evidence':asdict(e)} for c,e in scored],
            'reason':verdict.reason,'assessment':verdict.verdict,'code':code,
            'fingerprint_duration':fingerprint.duration,'local_tags':local,'warnings':warnings,
            # An edition is never chosen automatically; this only says a choice exists.
            'release_ambiguous':any(len(c.release_details)>1 for c,_ in scored[:1])},
            recording_status=recording_status)
        return report
