"""Strictly read-only fingerprint and catalog lookups. Never upload audio files."""
from dataclasses import dataclass
import json
from threading import Lock
import time
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingCandidate:
    recording_id: str
    title: str
    artist: str
    acoustid_score: float
    releases: tuple[str, ...] = ()


class RateLimiter:
    """At most one request per interval for one client instance."""
    def __init__(self, interval: float, *, clock=time.monotonic, sleeper=time.sleep):
        self.interval = max(0.0, interval)
        self.clock = clock
        self.sleeper = sleeper
        self._next = None
        self._lock = Lock()

    def wait(self):
        with self._lock:
            now = self.clock()
            if self._next is not None and now < self._next:
                self.sleeper(self._next - now)
            self._next = self.clock() + self.interval


def _request_json(url, *, data=None, headers=None, timeout=15):
    req = Request(url, data=data, headers=headers or {}, method='POST' if data is not None else 'GET')
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(3_000_001)
        if len(raw) > 3_000_000:
            raise APIError('API response too large')
        return json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise APIError(f'Network or response error: {type(exc).__name__}') from exc


class AcoustIDClient:
    ENDPOINT = 'https://api.acoustid.org/v2/lookup'
    def __init__(self, client_key: str, *, transport=_request_json, limiter=None):
        if not client_key or not client_key.strip():
            raise ValueError('AcoustID application client key is required')
        self.client_key = client_key.strip()
        self.transport = transport
        self.limiter = limiter or RateLimiter(1/3)

    def lookup(self, fingerprint: str, duration: int) -> list[RecordingCandidate]:
        self.limiter.wait()
        data = urlencode({'client':self.client_key,'fingerprint':fingerprint,'duration':duration,
                          'meta':'recordings','format':'json'}).encode('utf-8')
        payload = self.transport(self.ENDPOINT, data=data, headers={'Content-Type':'application/x-www-form-urlencoded',
                                          'User-Agent':'MeloShelf/0.2 (local music organizer)'}, timeout=20)
        if not isinstance(payload, dict) or payload.get('status') != 'ok' or not isinstance(payload.get('results'), list):
            raise APIError('Invalid AcoustID response')
        candidates = {}
        for match in payload['results']:
            if not isinstance(match, dict):
                continue
            score = max(0.0,min(1.0,float(match.get('score',0))))
            for rec in match.get('recordings',[]) or []:
                if not isinstance(rec,dict) or not isinstance(rec.get('id'),str):
                    continue
                artists = rec.get('artists') or []
                name = ', '.join(a.get('name','') for a in artists if isinstance(a,dict) and a.get('name'))
                item = RecordingCandidate(rec['id'],str(rec.get('title','')),name,score)
                if item.recording_id not in candidates or score > candidates[item.recording_id].acoustid_score:
                    candidates[item.recording_id] = item
        return sorted(candidates.values(),key=lambda c:c.acoustid_score,reverse=True)


class MusicBrainzClient:
    ROOT = 'https://musicbrainz.org/ws/2/recording/'
    UUID = re.compile(r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$', re.I)
    def __init__(self, user_agent: str, *, transport=_request_json, limiter=None):
        if not user_agent or not user_agent.strip():
            raise ValueError('A meaningful MusicBrainz User-Agent with application contact is required')
        self.user_agent = user_agent.strip()
        self.transport = transport
        self.limiter = limiter or RateLimiter(1.1)

    def lookup_recording(self, recording_id: str) -> dict:
        if not self.UUID.fullmatch(recording_id):
            raise ValueError('Invalid MusicBrainz recording ID')
        self.limiter.wait()
        payload = self.transport(self.ROOT + recording_id + '?' + urlencode({'inc':'artist-credits+releases+release-groups','fmt':'json'}),
                                 headers={'User-Agent':self.user_agent,'Accept':'application/json'},timeout=20)
        if not isinstance(payload,dict) or payload.get('id','').lower()!=recording_id.lower():
            raise APIError('Invalid MusicBrainz recording response')
        return payload
