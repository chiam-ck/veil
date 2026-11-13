import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from conftest import WorkspaceClient as TestClient
from veil import config,store
from veil.api import app

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DATA',tmp_path/'service')
    with TestClient(app) as c:yield c

def complete(engine,jid):
    row=store.claim();assert row['id']==jid
    folder=store.path(jid)
    r=engine.process(folder/'original'/('source'+row['suffix']),folder/'output',private_preview=True)
    store.update(jid,r['status'],'finished');return r

def test_api_lifecycle_and_privacy(client,engine,dataset,caplog):
    p=dataset/'development'/'synthetic-00.docx'
    r=client.post('/api/jobs',files={'file':('Amelia-Quill.docx',p.read_bytes())});assert r.status_code==202
    jid=r.json()['id'];assert len(jid)==32
    assert client.get(f'/api/jobs/{jid}').json()['status']=='queued'
    assert client.get(f'/api/jobs/{jid}/report').status_code==409
    complete(engine,jid)
    r=client.get(f'/api/jobs/{jid}/report');assert r.json()['status']=='completed'
    assert r.json()['notices'] and r.json()['review_summary']['review_items']==0
    assert client.get(f'/api/jobs/{jid}').json()['review_reasons']==[]
    assert 'Amelia' not in r.text and 'person00@example.test' not in r.text
    assert client.get(f'/api/jobs/{jid}/download/sanitised.docx').status_code==200
    assert client.get(f'/api/jobs/{jid}/download/source.docx').status_code==404
    assert client.get(f'/api/jobs/{jid}/original-text').json()['warning'].startswith('ORIGINAL')
    assert 'Amelia' not in caplog.text and 'person00@example.test' not in caplog.text
    assert client.delete(f'/api/jobs/{jid}').status_code==204
    assert not store.path(jid).exists()
    assert client.get(f'/api/jobs/{jid}').status_code==404

def test_rejections(client,monkeypatch):
    assert client.post('/api/jobs',files={'file':('old.doc',b'x')}).status_code==415
    assert client.post('/api/jobs',files={'file':('a.pdf',b'')}).status_code==400
    monkeypatch.setattr(config,'MAX_BYTES',4)
    assert client.post('/api/jobs',files={'file':('a.pdf',b'12345')}).status_code==413
    assert client.post('/api/jobs',files={'file':('a.pdf',b'a')},headers={'origin':'https://evil.test'}).status_code==403
    assert client.get('/api/health',headers={'host':'evil.test'}).status_code==400

def test_restart_recovery_and_retention(client,monkeypatch):
    jid=store.reserve('.pdf');store.update(jid,'processing','test')
    with store.db() as c:c.execute('UPDATE jobs SET updated=0 WHERE id=?',(jid,))
    (store.path(jid)/'output'/'partial.pdf').write_bytes(b'secret')
    store.recover_and_retain();assert store.get(jid)['status']=='failed'
    assert not (store.path(jid)/'output'/'partial.pdf').exists()
    monkeypatch.setattr(config,'RETENTION',-1);store.recover_and_retain()
    assert not store.path(jid).exists()

def test_queue_and_active_deletion(client,monkeypatch):
    monkeypatch.setattr(config,'MAX_QUEUE',1)
    j=store.reserve('.pdf',owner_id=client.workspace_owner);store.update(j,'queued','waiting')
    assert client.post('/api/jobs',files={'file':('a.pdf',b'a')}).status_code==429
    store.update(j,'processing','test')
    assert client.delete('/api/jobs/'+j).status_code==409

@pytest.mark.parametrize('language',['en','mixed-Hant'])
@pytest.mark.parametrize('fmt',['docx','pptx','xlsx','pdf'])
def test_cli_live_end_to_end(tmp_path,dataset,fmt,language,multilingual_dataset):
    import socket
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    env=os.environ|{'VEIL_DATA':str(tmp_path/'data'),'VEIL_PORT':str(port),'PYTHONUNBUFFERED':'1'}
    api=subprocess.Popen([sys.executable,'-m','veil.api'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    worker=subprocess.Popen([sys.executable,'-m','veil.worker'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        import httpx
        server=f'http://127.0.0.1:{port}'
        for _ in range(100):
            try:
                if httpx.get(server+'/api/health',trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.1)
        source=dataset/'development'/('synthetic-00.'+fmt) if language=='en' else multilingual_dataset/language/'development/native'/('synthetic.'+fmt)
        result=subprocess.run([sys.executable,'-m','veil.cli','redact',str(source),'--server',server,'--output',str(tmp_path/'downloads'),'--json'],env=env,capture_output=True,text=True,timeout=30)
        expected='completed'
        assert result.returncode==(2 if expected=='review_required' else 0),result.stdout+result.stderr
        data=json.loads(result.stdout);j=data['jobs'][0];assert j['status']==expected
        assert any(Path(p).suffix=='.'+fmt for p in j['paths'])
        for command in ['status','report']:
            r=subprocess.run([sys.executable,'-m','veil.cli',command,j['id'],'--server',server,'--json'],env=env,capture_output=True,text=True,timeout=5)
            assert r.returncode==0 and json.loads(r.stdout)
        r=subprocess.run([sys.executable,'-m','veil.cli','download',j['id'],'--server',server,'--output',str(Path(j['paths'][0]).parent),'--json'],env=env,capture_output=True,text=True,timeout=5)
        assert r.returncode==1 and 'overwrite' in r.stdout
    finally:
        api.terminate();worker.terminate();api.wait(5);worker.wait(5)
        out,err=worker.communicate()
        assert b'Amelia Quill' not in out+err and b'person00@example.test' not in out+err

def test_chunked_upload_bound(client,monkeypatch):
    monkeypatch.setattr(config,'MAX_BYTES',4)
    chunks=iter([b'--x\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\n\r\n', b'x'*40000,b'x'*40000,b'x'*40000,b'\r\n--x--\r\n'])
    r=client.post('/api/jobs',content=chunks,headers={'content-type':'multipart/form-data; boundary=x'})
    assert r.status_code==413

def test_cli_async_recursive_batch_and_exit_codes(tmp_path,dataset):
    import socket,shutil,httpx
    from veil.cli import Client
    s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close()
    server=f'http://127.0.0.1:{port}';env=os.environ|{'VEIL_DATA':str(tmp_path/'data'),'VEIL_PORT':str(port)}
    api=subprocess.Popen([sys.executable,'-m','veil.api'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    worker=subprocess.Popen([sys.executable,'-m','veil.worker'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def cli(*args):return subprocess.run([sys.executable,'-m','veil.cli',*args,'--server',server,'--json'],env=env,capture_output=True,text=True,timeout=40)
    try:
        for _ in range(100):
            try:
                if httpx.get(server+'/api/health',trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.1)
        r=cli('redact',str(dataset/'development'/'synthetic-00.pdf'),'--async');assert r.returncode==0
        jid=json.loads(r.stdout)['jobs'][0]['id'];assert Client(server).wait(jid,quiet=True)['status']=='completed'
        inputs=tmp_path/'inputs';(inputs/'native').mkdir(parents=True);(inputs/'scan').mkdir()
        shutil.copyfile(dataset/'development'/'synthetic-00.pdf',inputs/'native'/'same.pdf')
        from docx import Document
        doc=Document();doc.add_paragraph('SYNTHETIC TEST MATERIAL. Имя: Александр Иванов');doc.save(inputs/'scan'/'same.docx')
        r=cli('redact',str(inputs),'--recursive','--output',str(tmp_path/'downloads'));assert r.returncode==2,r.stdout+r.stderr
        jobs=json.loads(r.stdout)['jobs'];assert {j['status'] for j in jobs}=={'completed','review_required'}
        for j in jobs:assert Path(j['paths'][0]).relative_to(tmp_path/'downloads').parts[0] in {'native','scan'}
        (inputs/'old.doc').write_bytes(b'legacy')
        r=cli('redact',str(inputs),'--recursive','--output',str(tmp_path/'other'));assert r.returncode==1
        assert any(j['status']=='failed' for j in json.loads(r.stdout)['jobs'])
    finally:
        api.terminate();worker.terminate();api.wait(5);worker.wait(5)

def test_sanitised_pdf_image_preview(client,engine,dataset):
    import io
    from PIL import Image
    source=dataset/'development'/'synthetic-02.pdf'
    result=client.post('/api/jobs',files={'file':('a.pdf',source.read_bytes())});jid=result.json()['id'];complete(engine,jid)
    r=client.get(f'/api/jobs/{jid}/preview/1');assert r.status_code==200 and r.headers['content-type']=='image/png'
    with Image.open(io.BytesIO(r.content)) as image:assert image.width>500 and image.height>500
    assert client.get(f'/api/jobs/{jid}/preview/2').status_code==404

def test_live_worker_deadline_discards_partial_outputs(tmp_path,dataset):
    import socket,httpx
    from veil.cli import Client
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    server=f'http://127.0.0.1:{port}';env=os.environ|{'VEIL_DATA':str(tmp_path/'data'),'VEIL_PORT':str(port),'VEIL_TIMEOUT':'1'}
    api=subprocess.Popen([sys.executable,'-m','veil.api'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    worker=subprocess.Popen([sys.executable,'-m','veil.worker'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if httpx.get(server+'/api/health',trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.1)
        c=Client(server);jid=c.submit(dataset/'development'/'synthetic-08.pdf');j=c.wait(jid,quiet=True,timeout=20)
        assert j['status']=='failed'
        r=c.request('GET',f'/api/jobs/{jid}/report').json();assert r['artifacts']==['report.json']
        assert c.http.get(f'/api/jobs/{jid}/download/sanitised.pdf').status_code==404
        assert not (tmp_path/'data'/'jobs'/jid/'output'/'sanitised.pdf').exists()
    finally:
        api.terminate();worker.terminate();api.wait(5);worker.wait(5)


@pytest.mark.parametrize('language',['zh-Hans','zh-Hant','mixed-Hans','mixed-Hant'])
def test_multilingual_api_privacy(client,engine,multilingual_dataset,language,caplog):
    source=multilingual_dataset/language/'development/native/synthetic.docx'
    result=client.post('/api/jobs',files={'file':('敏感姓名.docx',source.read_bytes())});assert result.status_code==202
    jid=result.json()['id'];r=complete(engine,jid);assert r['status']=='completed'
    response=client.get(f'/api/jobs/{jid}/report');assert response.status_code==200
    assert response.json()['language_coverage']['offset_unit'].startswith('Unicode')
    for value in ['张伟','陳怡君','synthetic@example.test','臺北市大安區仁愛路88號','敏感姓名']:
        assert value not in response.text+caplog.text
    assert client.get(f'/api/jobs/{jid}/download/sanitised.docx').status_code==200
