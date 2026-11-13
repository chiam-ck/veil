import io
import json
import sqlite3
import zipfile
from pathlib import Path
import pytest
import pymupdf
from conftest import WorkspaceClient as TestClient
from veil import config,store
from veil.api import app
from test_service import complete

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DATA',tmp_path/'service')
    with TestClient(app) as c:yield c

@pytest.fixture
def sample():
    with pymupdf.open() as d:
        p=d.new_page();p.insert_text((40,70),'SYNTHETIC TEST MATERIAL');p.insert_text((40,110),'Email: batch@example.test')
        return d.tobytes()

def create(client,count):
    r=client.post('/api/batches',json={'count':count});assert r.status_code==201,r.text
    return r.json()['id']

def upload(client,bid,index,data):
    return client.post('/api/jobs',data={'batch_id':bid,'batch_index':str(index)},files={'file':('Private-Name.pdf',data)})

@pytest.mark.parametrize('count',[0,51,-1,1.5,'2',True])
def test_batch_count_enforced_by_server(client,count):
    assert client.post('/api/batches',json={'count':count}).status_code==422
    with store.db() as c:assert c.execute('SELECT count(*) FROM batches').fetchone()[0]==0


def test_batch_50_real_processing_backpressure_and_zip(client,engine,sample,monkeypatch):
    monkeypatch.setattr(config,'MAX_QUEUE',3)
    bid=create(client,50);pending=[];jobs=[];backpressure=0
    for index in range(50):
        r=upload(client,bid,index,sample)
        if r.status_code==429:
            backpressure+=1;assert r.headers['Retry-After']=='2'
            complete(engine,pending.pop(0));r=upload(client,bid,index,sample)
        assert r.status_code==202,r.text
        jid=r.json()['id'];pending.append(jid);jobs.append(jid)
    for jid in pending:complete(engine,jid)
    result=client.get('/api/batches/'+bid);assert result.status_code==200
    batch=result.json();assert batch['total']==50 and batch['finished']==50 and batch['counts']['completed']==50 and batch['export_ready']
    assert backpressure>0 and len(set(jobs))==50
    assert 'Private-Name' not in result.text and 'batch@example.test' not in result.text
    r=client.get('/api/batches/'+bid+'/download');assert r.status_code==200
    assert r.headers['content-type']=='application/zip'
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names=z.namelist();assert len(names)==151
        summary=json.loads(z.read('batch-report.json'));assert summary['complete'] and summary['counts']['completed']==50
        assert not any('original' in n or 'source' in n or 'extracted' in n or 'Private-Name' in n for n in names)
        for jid in jobs:
            prefix=next(n.rsplit('/',1)[0] for n in names if jid in n)
            report=json.loads(z.read(prefix+'/report.json'));assert report['entity_counts']['EMAIL_ADDRESS']==1
            assert b'batch@example.test' not in z.read(prefix+'/sanitised.txt')
            with pymupdf.open(stream=z.read(prefix+'/sanitised.pdf'),filetype='pdf') as document:assert len(document)==1 and not document[0].get_text().strip()
    assert list((config.DATA/'downloads').iterdir())==[]


def test_batch_slots_duplicate_and_invalid_do_not_overwrite(client,sample):
    bid=create(client,2);r=upload(client,bid,0,sample);jid=r.json()['id'];original=(store.path(jid)/'original/source.pdf').read_bytes()
    assert upload(client,bid,0,b'different').status_code==409
    assert upload(client,bid,2,sample).status_code==400
    assert upload(client,bid,-1,sample).status_code==400
    assert upload(client,'f'*32,0,sample).status_code==404
    assert (store.path(jid)/'original/source.pdf').read_bytes()==original
    assert client.get('/api/batches/'+bid).json()['counts']['not_submitted']==1


def test_invalid_upload_releases_slot_and_partial_zip_is_explicit(client,engine,sample):
    bid=create(client,2)
    assert upload(client,bid,0,b'').status_code==400
    assert not client.get('/api/batches/'+bid).json()['items']
    r=upload(client,bid,0,sample);complete(engine,r.json()['id'])
    assert client.get('/api/batches/'+bid+'/download').status_code==409
    partial=client.get('/api/batches/'+bid+'/download?partial=true');assert partial.status_code==200
    with zipfile.ZipFile(io.BytesIO(partial.content)) as z:
        report=json.loads(z.read('batch-report.json'));assert report['complete'] is False and report['counts']['not_submitted']==1


def test_bundle_limit_preserves_individual_downloads(client,engine,sample,monkeypatch):
    bid=create(client,1);r=upload(client,bid,0,sample);jid=r.json()['id'];complete(engine,jid)
    monkeypatch.setattr(config,'MAX_BUNDLE_BYTES',1)
    assert client.get('/api/batches/'+bid+'/download').status_code==413
    assert client.get(f'/api/jobs/{jid}/download/sanitised.pdf').status_code==200


def test_mixed_outcomes_and_deleted_member_are_honest(client,engine,sample):
    bid=create(client,3)
    ids=[upload(client,bid,i,data).json()['id'] for i,data in enumerate([sample,b'bad PDF',sample])]
    for jid in ids:complete(engine,jid)
    client.delete('/api/jobs/'+ids[2])
    data=client.get('/api/batches/'+bid).json();assert data['counts']['completed']==1 and data['counts']['failed']==1 and data['counts']['deleted']==1 and data['export_ready']
    r=client.get('/api/batches/'+bid+'/download');assert r.status_code==200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert len([n for n in z.namelist() if n.endswith('sanitised.pdf')])==1
        assert len([n for n in z.namelist() if n.endswith('/report.json')])==2


def test_private_hostnames_configurable_and_origin_checked(client,monkeypatch):
    monkeypatch.setattr(config,'ALLOWED_HOSTS',config.ALLOWED_HOSTS|{'veil.internal'})
    assert client.get('/api/config',headers={'Host':'veil.internal'}).status_code==200
    assert client.get('/api/config',headers={'Host':'evil.invalid'}).status_code==400
    assert client.post('/api/batches',json={'count':1},headers={'Host':'veil.internal','Origin':'https://elsewhere.invalid'}).status_code==403
    assert client.post('/api/batches',json={'count':1},headers={'Host':'veil.internal','Origin':'https://veil.internal'}).status_code==201


def test_old_database_migration_keeps_jobs(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DATA',tmp_path);jid='a'*32
    c=sqlite3.connect(tmp_path/'jobs.sqlite3');c.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY,suffix TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)');c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',(jid,'.pdf','queued','old',1,1));c.commit();c.close()
    store.init();store.init()
    assert store.get(jid)['stage']=='old'
    with store.db() as c:assert {'batch_id','batch_index'}<={r[1] for r in c.execute('PRAGMA table_info(jobs)')}


def test_batch_retention_does_not_remove_active_groups(client,sample,monkeypatch):
    bid=create(client,1);jid=upload(client,bid,0,sample).json()['id']
    with store.db() as c:c.execute('UPDATE batches SET updated=0 WHERE id=?',(bid,))
    store.recover_and_retain();assert store.get_batch(bid)['items'][0]['id']==jid
    client.delete('/api/jobs/'+jid);store.recover_and_retain()
    assert client.get('/api/batches/'+bid).status_code==404
