"""Conservative acoustic recognition. Proposals only; no tag writing or file moves."""
from dataclasses import dataclass, replace, asdict
from enum import Enum
import hashlib
import json
from pathlib import Path

from meloshelf.infrastructure.music_api import RecordingCandidate, APIError
from meloshelf.infrastructure.fingerprint import FingerprintError


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

    def identify(self, path: Path) -> RecognitionReport:
        path=Path(path)
        try:
            before=self._file_identity(path)
            file_key='fingerprint:v1:'+hashlib.sha256(
                f'{path.resolve()}:{before[0]}:{before[1]}'.encode('utf-8')).hexdigest()
            fp_cached=self.store.get_cache(file_key)
            if fp_cached:
                from meloshelf.infrastructure.fingerprint import Fingerprint
                fingerprint=Fingerprint(value=fp_cached['fingerprint'],duration=fp_cached['duration'])
            else:
                fingerprint=self.fingerprinter.compute(path)
            if before!=self._file_identity(path):
                return RecognitionReport(str(path),RecognitionStatus.CHANGED,reason='Source changed during fingerprinting')
            if fp_cached is None:
                self.store.put_cache(file_key,{'fingerprint':fingerprint.value,'duration':fingerprint.duration})
            key='acoustid:v1:'+hashlib.sha256(f'{fingerprint.duration}:{fingerprint.value}'.encode()).hexdigest()
            data=self.store.get_cache(key)
            if data is None:
                if self.offline:
                    return RecognitionReport(str(path),RecognitionStatus.OFFLINE,reason='No cached fingerprint match')
                if self.acoustid is None:
                    return RecognitionReport(str(path),RecognitionStatus.ERROR,reason='AcoustID key is not configured')
                matches=self.acoustid.lookup(fingerprint.value, fingerprint.duration)
                data=[asdict(m) for m in matches]
                self.store.put_cache(key,data)
            candidates=[RecordingCandidate(recording_id=d['recording_id'],title=d['title'],artist=d['artist'],
                         acoustid_score=float(d['acoustid_score']), releases=tuple(d.get('releases') or ())) for d in data]
            enriched=[]
            for candidate in candidates[:5]:
                if not self.musicbrainz or self.offline:
                    enriched.append(candidate)
                    continue
                mbkey='musicbrainz:v1:'+candidate.recording_id
                mb=self.store.get_cache(mbkey)
                if mb is None:
                    try:
                        mb=self.musicbrainz.lookup_recording(candidate.recording_id)
                        self.store.put_cache(mbkey,mb)
                    except (APIError,ValueError):
                        enriched.append(candidate)
                        continue
                releases=tuple(dict.fromkeys(r.get('title','') for r in mb.get('releases',[]) if isinstance(r,dict) and r.get('title')))
                title=mb.get('title') or candidate.title
                credit=mb.get('artist-credit',[])
                artist=''.join(c.get('name',c.get('artist',{}).get('name','')) if isinstance(c,dict) else str(c) for c in credit).strip()
                enriched.append(replace(candidate,title=title,artist=artist or candidate.artist,releases=releases))
            enriched.extend(candidates[5:])
            if before!=self._file_identity(path):
                return RecognitionReport(str(path),RecognitionStatus.CHANGED,reason='Source changed during remote lookup')
            status=RecognitionStatus.REVIEW if enriched else RecognitionStatus.UNMATCHED
            report=RecognitionReport(str(path),status,tuple(enriched),reason=('Review the identity and album edition' if enriched else 'No reference match'))
            self.store.put_result(path,*before,status.value,{'candidates':[asdict(c) for c in report.candidates],'reason':report.reason})
            return report
        except (OSError,FingerprintError,APIError,ValueError,TypeError,KeyError) as exc:
            return RecognitionReport(str(path),RecognitionStatus.ERROR,reason=f'{type(exc).__name__}: {exc}')
