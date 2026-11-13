"""A known document ID must never substitute for its browser credential."""
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pymupdf
import pytest
from fastapi.testclient import TestClient

from veil import config,store,workspaces
from veil.api import app
from veil.worker import process_next
from veil.provider_health import ProviderMonitor

HEADERS={'X-Veil-Request':'1'}

@pytest.fixture
def clients(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DATA',tmp_path/'data')
    with ExitStack() as stack:
        clients=[stack.enter_context(TestClient(app,base_url='https://testserver',headers=HEADERS)) for _ in range(3)]
        for client in clients[:2]:assert client.get('/api/workspace').status_code==200
        yield clients


def owner(client):
    digest=hashlib.sha256(client.cookies[workspaces.COOKIE].encode()).hexdigest()
    with store.db() as db:return db.execute('SELECT id FROM workspaces WHERE token_hash=?',(digest,)).fetchone()[0]


def pdf_bytes():
    with pymupdf.open() as d:
        d.new_page().insert_text((50,70),'Email: private@example.test\nMissed: SECRET-OSPREY')
        return d.tobytes()


def document(client,engine):
    bid=client.post('/api/batches',json={'count':1}).json()['id']
    result=client.post('/api/jobs',data={'batch_id':bid,'batch_index':'0'},files={'file':('upload.pdf',pdf_bytes())})
    assert result.status_code==202
    jid=result.json()['id'];assert process_next(engine,ProviderMonitor(engine.detector.settings))
    assert client.get('/api/jobs/'+jid).json()['status']=='completed'
    return bid,jid


def endpoints(bid,jid):
    base='/api/jobs/'+jid
    return [('GET',base,{}),('GET',base+'/report',{}),('GET',base+'/download/sanitised.pdf',{}),
            ('GET',base+'/download/sanitised.txt',{}),('GET',base+'/download/report.json',{}),
            ('GET',base+'/original-text',{}),('GET',base+'/preview/1',{}),('DELETE',base,{}),
            ('POST',base+'/reprocess',{'json':{'mode':'rerun'}}),
            ('POST',base+'/reprocess',{'json':{'mode':'manual','redactions':['SECRET-OSPREY']}}),
            ('GET','/api/batches/'+bid,{}),('GET','/api/batches/'+bid+'/download',{}),
            ('GET','/api/batches/'+bid+'/download?partial=true',{}),
            ('POST','/api/jobs',{'data':{'batch_id':bid,'batch_index':'0'},'files':{'file':('upload.pdf',pdf_bytes())}})]


def test_two_workspaces_and_anonymous_cannot_access_known_foreign_ids(clients,engine):
    a,b,anonymous=clients
    bid,jid=document(a,engine)
    for method,path,kwargs in endpoints(bid,jid):
        denied=b.request(method,path,**kwargs)
        assert denied.status_code==404,(method,path,denied.status_code)
        assert 'private@example.test' not in denied.text and 'SECRET-OSPREY' not in denied.text
        assert anonymous.request(method,path,**kwargs).status_code==401
    assert a.get('/api/jobs/'+jid+'/original-text').status_code==200
    assert a.get('/api/batches/'+bid+'/download').status_code==200
    assert 'owner_id' not in a.get('/api/jobs/'+jid).json()
    assert 'owner_id' not in a.get('/api/batches/'+bid).text


def test_revisions_and_new_batches_inherit_owner_and_cannot_be_spoofed(clients,engine):
    a,b,_=clients
    bid,jid=document(a,engine)
    body={'mode':'manual','redactions':['SECRET-OSPREY'],'owner_id':owner(b)}
    result=a.post('/api/jobs/'+jid+'/reprocess',json=body)
    assert result.status_code==202
    child=result.json()['id'];assert process_next(engine,ProviderMonitor(engine.detector.settings))
    assert b.get('/api/jobs/'+child).status_code==404
    assert a.get('/api/jobs/'+child).status_code==200
    with store.db() as db:
        assert db.execute('SELECT owner_id FROM jobs WHERE id=?',(child,)).fetchone()[0]==owner(a)
    # A revision of a standalone result gets a new, equally private batch.
    independent=a.post('/api/jobs',files={'file':('upload.pdf',pdf_bytes())}).json()['id']
    assert process_next(engine,ProviderMonitor(engine.detector.settings))
    next_revision=a.post('/api/jobs/'+independent+'/reprocess',json={'mode':'rerun'}).json()
    assert b.get('/api/batches/'+next_revision['batch_id']).status_code==404
    assert a.get('/api/batches/'+next_revision['batch_id']).status_code==200
    assert b.get('/api/jobs/'+jid+'?owner_id='+owner(a),headers={'X-Workspace-ID':owner(a)}).status_code==404


def test_secure_cookie_hash_storage_unknown_cookie_and_fixation(clients):
    a,b,_=clients
    cookie=a.cookies[workspaces.COOKIE]
    assert len(cookie)==43 and workspaces.TOKEN.fullmatch(cookie)
    with store.db() as db:
        records=[dict(r) for r in db.execute('SELECT * FROM workspaces')]
    assert cookie not in json.dumps(records)
    assert hashlib.sha256(cookie.encode()).hexdigest() in json.dumps(records)
    with TestClient(app,base_url='https://testserver') as fresh:
        fresh.cookies.set(workspaces.COOKIE,'A'*43,domain='testserver.local',path='/')
        response=fresh.get('/api/workspace')
        header=response.headers['set-cookie']
        assert 'HttpOnly' in header and 'Secure' in header and 'SameSite=strict' in header and 'Path=/' in header
        assert 'Domain=' not in header and 'A'*43 not in header
        assert response.headers['cache-control']=='no-store'
        assert 'token' not in response.json() and 'id' not in response.json()
        assert fresh.cookies[workspaces.COOKIE]!='A'*43


def test_session_expiry_logout_revocation_and_cookie_loss_never_reclaim(clients,engine):
    a,b,blank=clients
    bid,jid=document(a,engine)
    stolen=TestClient(app,base_url='https://testserver',headers=HEADERS,cookies=a.cookies)
    assert stolen.get('/api/jobs/'+jid).status_code==200
    assert a.post('/api/workspace/end').status_code==204
    assert stolen.get('/api/jobs/'+jid).status_code==401
    assert store.path(jid).exists()  # Ending access does not change file retention.
    assert a.get('/api/workspace').status_code==200
    assert a.get('/api/jobs/'+jid).status_code==404
    other_batch,other_job=document(b,engine)
    with store.db() as db:db.execute('UPDATE workspaces SET expires=0 WHERE id=?',(owner(b),))
    assert b.get('/api/jobs/'+other_job).status_code==401
    b.get('/api/workspace')
    assert b.get('/api/jobs/'+other_job).status_code==404
    # Losing cookies cannot be undone by knowing the job or a URL-supplied secret.
    blank.get('/api/workspace?workspace='+stolen.cookies[workspaces.COOKIE])
    assert blank.get('/api/jobs/'+jid).status_code==404


def test_absolute_lifetime_is_enforced_even_if_idle_deadline_is_in_future(clients):
    a,*_=clients
    with store.db() as db:db.execute('UPDATE workspaces SET expires=?,absolute_expires=0 WHERE id=?',(time.time()+10000,owner(a)))
    assert a.post('/api/batches',json={'count':1}).status_code==401


def test_legacy_ownerless_jobs_are_not_claimable_by_opening_a_link(clients,engine):
    a,b,_=clients
    bid=store.reserve_batch(1);jid=store.reserve('.pdf',bid,0)
    (store.path(jid)/'original'/'source.pdf').write_bytes(pdf_bytes());store.update(jid,'queued','waiting')
    assert process_next(engine,ProviderMonitor(engine.detector.settings))
    for client in [a,b]:
        for method,path,kwargs in endpoints(bid,jid):assert client.request(method,path,**kwargs).status_code==404
    assert (store.path(jid)/'original'/'source.pdf').is_file()
    with store.db() as db:assert db.execute('SELECT owner_id FROM jobs WHERE id=?',(jid,)).fetchone()[0] is None


def test_csrf_and_plaintext_lan_are_rejected(clients):
    a,*_=clients
    for headers in [{'Origin':'https://evil.example'},{'Sec-Fetch-Site':'cross-site'},{'Sec-Fetch-Site':'same-site'}]:
        assert a.post('/api/batches',json={'count':1},headers=headers).status_code==403
    a.headers.pop('X-Veil-Request')
    assert a.post('/api/batches',json={'count':1}).status_code==403
    assert a.post('/api/workspace/end').status_code==403
    with TestClient(app,base_url='http://testserver',headers={'X-Forwarded-Proto':'https',**HEADERS}) as insecure:
        assert insecure.get('/api/workspace').status_code==426
        assert insecure.post('/api/batches',json={'count':1}).status_code==426


def test_mixed_owner_batch_never_exports_foreign_member(clients,engine):
    a,b,_=clients
    bid,jid=document(a,engine);other_bid,foreign=document(b,engine)
    with store.db() as db:
        db.execute('DELETE FROM batch_items WHERE job_id=?',(foreign,))
        db.execute('UPDATE batch_items SET job_id=? WHERE batch_id=?',(foreign,bid))
    for path in ['/api/batches/'+bid,'/api/batches/'+bid+'/download']:
        assert a.get(path).status_code==404


def test_touch_refreshes_idle_but_not_absolute_deadline(clients):
    a,*_=clients
    with store.db() as db:
        before=dict(db.execute('SELECT * FROM workspaces WHERE id=?',(owner(a),)).fetchone())
        db.execute('UPDATE workspaces SET touched=0,expires=? WHERE id=?',(time.time()+100,owner(a)))
    response=a.get('/api/workspace')
    assert 'set-cookie' in response.headers
    with store.db() as db:after=dict(db.execute('SELECT * FROM workspaces WHERE id=?',(owner(a),)).fetchone())
    assert after['expires']>before['expires']-5 and after['absolute_expires']==before['absolute_expires']


def test_workspace_creation_limits_do_not_block_existing_browser(clients,monkeypatch):
    a,b,blank=clients
    monkeypatch.setattr(config,'WORKSPACE_CREATIONS_PER_HOUR',2)
    assert a.get('/api/workspace').status_code==200
    assert blank.get('/api/workspace').status_code==429
    with store.db() as db:
        db.execute('UPDATE workspace_limits SET started=0')
    assert blank.get('/api/workspace').status_code==200
    with store.db() as db:
        db.execute('UPDATE workspace_limits SET started=0')
    monkeypatch.setattr(config,'MAX_WORKSPACES',3)
    blank.cookies.clear()
    assert blank.get('/api/workspace').status_code==429
    assert b.get('/api/workspace').status_code==200


@pytest.mark.parametrize('host',['testserver/evil','user@testserver','testserver?x=1','testserver#fragment','testserver:bad','testserver extra'])
def test_malformed_host_is_rejected_with_security_headers(clients,host):
    a,*_=clients
    result=a.get('/api/health',headers={'Host':host})
    assert result.status_code==400
    assert result.headers['cache-control']=='no-store'
    assert result.headers['x-frame-options']=='DENY'


def test_multipart_counts_and_field_sizes_are_bounded(clients):
    a,*_=clients
    data=pdf_bytes()
    assert a.post('/api/jobs',files=[('file',('a.pdf',data)),('other',('b.pdf',data))]).status_code==400
    assert a.post('/api/jobs',data={'batch_id':'x'*5000},files={'file':('a.pdf',data)}).status_code==400
    assert a.post('/api/jobs',data={'one':'1','two':'2','three':'3'},files={'file':('a.pdf',data)}).status_code==400


@pytest.mark.parametrize('server',['https://'+'user:password@'+'example.com','https://example.com/path','https://example.com?key=value','http://10.0.0.1:8787'])
def test_cli_rejects_credential_urls_and_network_plaintext(server):
    from veil.cli import Client
    with pytest.raises(ValueError):Client(server)
