import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from meloshelf.application.recognition import RecognitionService, RecognitionStatus
from meloshelf.infrastructure.fingerprint import Fingerprinter, FingerprintError
from meloshelf.infrastructure.music_api import AcoustIDClient, MusicBrainzClient, RateLimiter, APIError
from meloshelf.infrastructure.match_store import MatchStore


FIXTURE_ACOUSTID = {
    "status": "ok", "results": [
        {"id":"sound-a", "score":0.97, "recordings":[{"id":"11111111-1111-4111-8111-111111111111", "title":"Good Song", "artists":[{"name":"Artist A"}]}]},
        {"id":"sound-b", "score":0.93, "recordings":[{"id":"22222222-2222-4222-8222-222222222222", "title":"Different Song", "artists":[{"name":"Artist B"}]}]},
    ]
}
FIXTURE_MB = {"id":"11111111-1111-4111-8111-111111111111","title":"Good Song","artist-credit":[{"name":"Artist A"}],"releases":[{"id":"release-a","title":"First Album","date":"2020-03-01"},{"id":"release-b","title":"Deluxe Album","date":"2021-05-01"}]}


def test_fpcalc_valid_json_without_touching_source(tmp_path):
    path=tmp_path/'00001.flac'; path.write_bytes(b'music fixture')
    run=Mock(return_value=subprocess.CompletedProcess(['fpcalc'],0,stdout=json.dumps({'duration':182,'fingerprint':'ABCDEF'}),stderr=''))
    f=Fingerprinter(runner=run, executable='fpcalc')
    fp=f.compute(path)
    assert (fp.duration, fp.value)==(182,'ABCDEF')
    assert path.read_bytes()==b'music fixture'
    assert run.call_args.args[0][-1]==str(path)
    assert run.call_args.kwargs['shell'] if 'shell' in run.call_args.kwargs else False is False


def test_fpcalc_bad_output_is_a_reported_error(tmp_path):
    path=tmp_path/'bad.mp3'; path.write_bytes(b'bad')
    run=Mock(return_value=subprocess.CompletedProcess(['fpcalc'],0,stdout='not json',stderr=''))
    with pytest.raises(FingerprintError): Fingerprinter(runner=run).compute(path)


def test_acoustid_never_uploads_raw_audio_and_parses_candidates():
    seen=[]
    def transport(url, *, data=None, headers=None, timeout=None):
        seen.append((url,data,headers))
        return FIXTURE_ACOUSTID
    ac=AcoustIDClient(client_key='test-key', transport=transport, limiter=RateLimiter(interval=0))
    matches=ac.lookup('compressed-fingerprint', 182)
    assert len(matches)==2
    assert matches[0].recording_id=='11111111-1111-4111-8111-111111111111'
    assert matches[0].artist=='Artist A'
    assert b'fingerprint=compressed-fingerprint' in seen[0][1]
    assert b'audio' not in seen[0][1]
    assert b'file=' not in seen[0][1]
    assert seen[0][0]=='https://api.acoustid.org/v2/lookup'


def test_musicbrainz_requires_user_agent_and_safe_uuid():
    with pytest.raises(ValueError): MusicBrainzClient(user_agent='')
    mb=MusicBrainzClient(user_agent='MeloShelf/0.2 (contact: dev@example.org)', transport=lambda *_a,**_kw:FIXTURE_MB, limiter=RateLimiter(interval=0))
    assert mb.lookup_recording('11111111-1111-4111-8111-111111111111')['releases'][0]['title']=='First Album'
    with pytest.raises(ValueError): mb.lookup_recording('../../path')


def test_ambiguous_audio_match_must_never_be_called_verified(tmp_path):
    song=tmp_path/'numbers.mp3'; song.write_bytes(b'audio')
    fp=Mock(compute=Mock(return_value=Mock(value='fp', duration=182)))
    api=Mock(lookup=Mock(return_value=AcoustIDClient(client_key='x',transport=lambda *_a,**_kw:FIXTURE_ACOUSTID,limiter=RateLimiter(0)).lookup('fp',182)))
    with MatchStore(tmp_path/'matches.db') as store:
        report=RecognitionService(fp,api, None, store).identify(song)
    assert report.status==RecognitionStatus.REVIEW
    assert len(report.candidates)==2
    assert song.read_bytes()==b'audio'


def test_high_acoustid_score_is_still_a_proposal_not_album_verification(tmp_path):
    song=tmp_path/'audio001.mp3';song.write_bytes(b'audio')
    fp=Mock(compute=Mock(return_value=Mock(value='fp',duration=182)))
    matches=AcoustIDClient(client_key='x',transport=lambda *_a,**_kw:{'status':'ok','results':FIXTURE_ACOUSTID['results'][:1]},limiter=RateLimiter(0)).lookup('fp',182)
    api=Mock(lookup=Mock(return_value=matches))
    mb=Mock(lookup_recording=Mock(return_value=FIXTURE_MB))
    with MatchStore(tmp_path/'matches.db') as store:
        report=RecognitionService(fp,api,mb,store).identify(song)
        persisted=store.get_result(str(song))
    assert report.status==RecognitionStatus.REVIEW
    assert report.candidates[0].releases==('First Album','Deluxe Album')
    assert persisted['status']=='review'


def test_cache_prevents_repeat_external_calls(tmp_path):
    path=tmp_path/'f.flac';path.write_bytes(b'file')
    fp=Mock(compute=Mock(return_value=Mock(value='fp',duration=11)))
    api=Mock(lookup=Mock(return_value=[]))
    with MatchStore(tmp_path/'a.db') as store:
        service=RecognitionService(fp,api,None,store)
        first=service.identify(path)
        second=service.identify(path)
    assert first.status==RecognitionStatus.UNMATCHED
    assert second.status==RecognitionStatus.UNMATCHED
    assert api.lookup.call_count==1


def test_offline_mode_never_calls_network(tmp_path):
    path=tmp_path/'f.mp3';path.write_bytes(b'x')
    fp=Mock(compute=Mock(return_value=Mock(value='fp',duration=11)))
    api=Mock()
    with MatchStore(tmp_path/'a.db') as store:
        report=RecognitionService(fp,api,None,store,offline=True).identify(path)
    assert report.status==RecognitionStatus.OFFLINE
    api.lookup.assert_not_called()


def test_invalid_api_response_cannot_be_treated_as_identified():
    ac=AcoustIDClient(client_key='x',transport=lambda *_a,**_kw:{'status':'error'},limiter=RateLimiter(0))
    with pytest.raises(APIError):ac.lookup('fp',12)


def test_rate_limiter_waits_between_calls():
    now=[0.0]; delays=[]
    def sleep(s):delays.append(s);now[0]+=s
    limit=RateLimiter(1.0, clock=lambda:now[0], sleeper=sleep)
    limit.wait();limit.wait();limit.wait()
    assert delays==[1.0,1.0]


def test_track_changed_since_fingerprint_is_not_persisted_as_match(tmp_path):
    path=tmp_path/'song.mp3';path.write_bytes(b'a')
    def change(_path):
        _path.write_bytes(b'another file')
        return Mock(value='fp',duration=11)
    fp=Mock(compute=change)
    api=Mock()
    with MatchStore(tmp_path/'a.db') as store:
        report=RecognitionService(fp,api,None,store).identify(path)
        assert store.get_result(str(path)) is None
    assert report.status==RecognitionStatus.CHANGED
    api.lookup.assert_not_called()


def test_fingerprint_is_cached_by_file_identity(tmp_path):
    path=tmp_path/'f.flac';path.write_bytes(b'file')
    fp=Mock(compute=Mock(return_value=Mock(value='fp',duration=14)))
    api=Mock(lookup=Mock(return_value=[]))
    with MatchStore(tmp_path/'f.db') as store:
        service=RecognitionService(fp,api,None,store)
        service.identify(path)
        service.identify(path)
    assert fp.compute.call_count==1
