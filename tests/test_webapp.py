import json
import threading
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import pytest
from meloshelf.application.webapp import create_server

@pytest.fixture
def app(tmp_path):
    html=tmp_path/'index.html';html.write_text('TOKEN: __MELOSHELF_TOKEN__',encoding='utf-8')
    server=create_server(db_path=tmp_path/'s.sqlite3',html_path=html)
    t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    yield server,f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown();server.server_close();t.join(timeout=2)

def post(server,base,path,data,*,token=None):
    req=Request(base+path, data=json.dumps(data).encode(), headers={'Content-Type':'application/json',
              'X-MeloShelf-Token':token or server.token},method='POST')
    return urlopen(req,timeout=5)

def test_html_and_empty_stats(app):
    _,base=app
    with urlopen(base+'/') as response: assert 'TOKEN: ' in response.read().decode()
    with urlopen(base+'/api/stats') as response: assert json.load(response)['total']==0

def test_token_and_missing_folder_rejected(app):
    server,base=app
    with pytest.raises(HTTPError) as exc:post(server,base,'/api/scan',{'folder':'/'},token='wrong')
    assert exc.value.code==403
    with pytest.raises(HTTPError) as exc:post(server,base,'/api/scan',{'folder':'/does-not-exist'})
    assert exc.value.code==400

def test_read_only_scan_indexes_without_changing_audio(app,tmp_path):
    server,base=app
    music=tmp_path/'music';music.mkdir()
    sample=music/'000147.mp3';sample.write_bytes(b'not actual audio')
    with post(server,base,'/api/scan',{'folder':str(music)}) as response:assert response.status==202
    for _ in range(50):
        with urlopen(base+'/api/status') as response:status=json.load(response)
        if not status['running']:break
        time.sleep(.03)
    with urlopen(base+'/api/stats') as response:stats=json.load(response)
    assert stats['total']==1
    assert sample.read_bytes()==b'not actual audio'
    with urlopen(base+'/api/tracks?limit=5') as response:
        assert json.load(response)['tracks'][0]['path']==str(sample)


def test_identify_requires_credentials_but_offline_is_allowed(app):
    server,base=app
    with pytest.raises(HTTPError) as exc:post(server,base,'/api/identify',{'limit':5})
    assert exc.value.code==409
    with post(server,base,'/api/identify',{'limit':5,'offline':True}) as response:assert response.status==202
