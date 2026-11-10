from pathlib import Path
import pytest
from meloshelf.infrastructure.match_store import MatchStore
from meloshelf.infrastructure.repository import LibraryRepository
from meloshelf.domain.models import Track

CANDS=[{'recording_id':'11111111-1111-4111-8111-111111111111','title':'One','artist':'Band','acoustid_score':.98,'releases':['Edition A','Edition B']},{'recording_id':'22222222-2222-4222-8222-222222222222','title':'Two','artist':'Band','acoustid_score':.91,'releases':[]}]

def test_accept_requires_explicit_valid_recording_and_does_not_pick_album(tmp_path):
    with MatchStore(tmp_path/'db.sqlite3') as store:
        store.put_result('track.mp3',5,1,'review',{'candidates':CANDS,'reason':'ambiguous'})
        with pytest.raises(ValueError):store.set_decision('track.mp3','accepted')
        with pytest.raises(ValueError):store.set_decision('track.mp3','accepted',recording_id='does-not-exist')
        assert store.set_decision('track.mp3','accepted',recording_id=CANDS[1]['recording_id'])
        item=store.get_result('track.mp3')
    assert item['decision']=='accepted'
    assert item['selected_recording_id']==CANDS[1]['recording_id']
    assert not item['selected_release_id']


def test_reject_clears_prior_selection(tmp_path):
    with MatchStore(tmp_path/'db.sqlite3') as store:
        store.put_result('track.mp3',5,1,'review',{'candidates':CANDS,'reason':'ambiguous'})
        store.set_decision('track.mp3','accepted',recording_id=CANDS[0]['recording_id'])
        store.set_decision('track.mp3','rejected')
        item=store.get_result('track.mp3')
        assert item['decision']=='rejected'
        assert not item['selected_recording_id']


def test_identification_queue_prioritizes_unknowns_over_other_tagged_files(tmp_path):
    repo=LibraryRepository(tmp_path/'db.sqlite3')
    repo.save_many([
        Track('/a.mp3',1,'.mp3',1,'Name','Artist','Album','','tagged'),
        Track('/b.mp3',1,'.mp3',1,'Good','Artist','','','review'),
        Track('/z.mp3',1,'.mp3',1,'','','','','unidentified')])
    paths=[x['path'] for x in repo.for_recognition(2)]
    repo.close()
    assert paths==['/z.mp3','/b.mp3']
