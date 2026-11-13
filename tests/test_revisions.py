"""Manual redactions change actual artifacts, preserve earlier masks and stay private."""
import hashlib
import io
import json
import re
import zipfile

import pymupdf
import pytest
from docx import Document
from conftest import WorkspaceClient as TestClient
from openpyxl import Workbook, load_workbook
from pptx import Presentation
from pptx.util import Inches

from veil import config, store
from veil.api import app
from veil.detection import Hit
from veil.provider_health import ProviderMonitor
from veil.revisions import RevisionDetector
from veil.worker import process_next

MISSED='OSPREY-SECRET-9182'
NEW='OTHER-SECRET-4920'
EMAIL='private@example.test'

@pytest.fixture
def service(tmp_path, monkeypatch, engine):
    monkeypatch.setattr(config,'DATA',tmp_path/'service')
    with TestClient(app) as client:
        yield client,engine,ProviderMonitor(engine.detector.settings),tmp_path


def create_source(folder,fmt):
    p=folder/('input.'+fmt);text=f'Email: {EMAIL}\nCode: {MISSED}\nAgain: {MISSED}\nNote: {NEW}'
    if fmt=='docx':
        d=Document();d.core_properties.author='';d.add_paragraph(text);d.save(p)
    elif fmt=='pptx':
        d=Presentation();d.core_properties.author='';slide=d.slides.add_slide(d.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1),Inches(1),Inches(8),Inches(4)).text_frame.text=text;d.save(p)
    elif fmt=='xlsx':
        d=Workbook();d.properties.creator=''
        for line in text.splitlines():d.active.append([line])
        d.save(p)
    else:
        d=pymupdf.open();d.new_page().insert_text((50,70),text);d.save(p);d.close()
    return p


def submit(service,fmt='docx',batch=True):
    client,engine,monitor,folder=service
    source=create_source(folder,fmt)
    kwargs={}
    if batch:
        bid=client.post('/api/batches',json={'count':1}).json()['id']
        kwargs={'batch_id':bid,'batch_index':'0'}
    job=client.post('/api/jobs',data=kwargs,files={'file':('upload.'+fmt,source.read_bytes())}).json()['id']
    assert process_next(engine,monitor)
    assert store.get(job)['status']=='completed'
    return job,source.read_bytes()


@pytest.mark.parametrize('fmt',['docx','pptx','xlsx','pdf'])
def test_manual_revision_changes_real_artifact_without_provider_and_preserves_previous(service,monkeypatch,fmt):
    client,engine,monitor,_=service
    parent,source=submit(service,fmt)
    before=client.get('/api/jobs/'+parent+'/download/sanitised.'+fmt).content
    parent_batch=store.get(parent)['batch_id']
    # Exercise reconstruction of findings for jobs from earlier releases.
    (store.path(parent)/'original'/'redaction-values.json').unlink()
    def no_model(*a,**k):pytest.fail('Manual correction must not call the model')
    monkeypatch.setattr(engine.detector,'find',no_model)
    monkeypatch.setattr(engine.detector,'prefetch',no_model)
    monkeypatch.setattr(monitor,'ensure_ready',no_model)
    result=client.post('/api/jobs/'+parent+'/reprocess',json={'mode':'manual','redactions':[MISSED]} )
    assert result.status_code==202,result.text
    new=result.json();jid=new['id'];assert jid!=parent and new['batch_id']==parent_batch
    assert client.get('/api/jobs/'+jid+'/download/sanitised.'+fmt).status_code==409
    assert client.get('/api/batches/'+parent_batch).json()['items'][0]['id']==jid
    assert process_next(engine,monitor)
    assert store.get(jid)['status']=='completed'
    assert (store.path(jid)/'original'/('source.'+fmt)).read_bytes()==source
    assert client.get('/api/jobs/'+parent+'/download/sanitised.'+fmt).content==before
    text=client.get('/api/jobs/'+jid+'/download/sanitised.txt').text
    assert MISSED not in text and EMAIL not in text and NEW in text
    report=client.get('/api/jobs/'+jid+'/report')
    assert all(value not in report.text for value in [MISSED,EMAIL,NEW])
    assert report.json()['detector']['calls']==0
    assert report.json()['entity_counts']['MANUAL']>=2
    artifact=client.get('/api/jobs/'+jid+'/download/sanitised.'+fmt).content
    assert artifact!=before
    if fmt!='pdf':
        with zipfile.ZipFile(io.BytesIO(artifact)) as z:
            assert z.testzip() is None
            xml=b' '.join(z.read(n) for n in z.namelist() if n.endswith('.xml'))
            assert MISSED.encode() not in xml and EMAIL.encode() not in xml
        if fmt=='docx':assert '[MANUAL_' in '\n'.join(p.text for p in Document(io.BytesIO(artifact)).paragraphs)
        elif fmt=='pptx':assert len(Presentation(io.BytesIO(artifact)).slides)==1
        else:assert load_workbook(io.BytesIO(artifact)).active.max_row==4
    else:
        with pymupdf.open(stream=artifact,filetype='pdf') as doc:
            assert not doc[0].get_text().strip()
            # Real pixels change when the formerly visible code is masked.
            with pymupdf.open(stream=before,filetype='pdf') as old:
                assert doc[0].get_pixmap().samples!=old[0].get_pixmap().samples
    batch_zip=client.get('/api/batches/'+parent_batch+'/download').content
    with zipfile.ZipFile(io.BytesIO(batch_zip)) as z:
        assert not any('original' in n or 'revision.json' in n or 'redaction-values' in n for n in z.namelist())
        assert z.read(f'001-{jid}/sanitised.{fmt}')==artifact
        assert MISSED not in z.read('batch-report.json').decode()
    assert store.get(parent)['superseded_by']==jid
    assert client.post('/api/jobs/'+parent+'/reprocess',json={'mode':'rerun'}).status_code==409


def test_rerun_keeps_old_auto_and_manual_detections_when_model_changes(service,monkeypatch):
    client,engine,monitor,_=service
    parent,_=submit(service)
    first=client.post('/api/jobs/'+parent+'/reprocess',json={'mode':'manual','redactions':[MISSED]}).json()['id']
    assert process_next(engine,monitor)
    # New model misses the old email, but finds another identifier.
    def changed(text,**kwargs):return [Hit(m.start(),m.end(),'ACCOUNT_ID',.9) for m in re.finditer(NEW,text)]
    monkeypatch.setattr(engine.detector,'find',changed)
    monkeypatch.setattr(engine.detector,'prefetch',lambda *a,**k:None)
    next_job=client.post('/api/jobs/'+first+'/reprocess',json={'mode':'rerun'}).json()['id']
    assert process_next(engine,monitor)
    assert store.get(next_job)['status']=='completed'
    text=client.get('/api/jobs/'+next_job+'/download/sanitised.txt').text
    assert all(v not in text for v in [EMAIL,MISSED,NEW])
    assert store.get(next_job)['parent_id']==first
    assert store.get(first)['superseded_by']==next_job


def test_manual_phrases_are_literal_case_insensitive_and_overlaps_are_unioned():
    class NoInference:
        def find(self,*a,**k):raise AssertionError('No inference allowed')
    detector=RevisionDetector(NoInference(),[{'kind':'MANUAL','text':'Token.*(sample)'},{'kind':'PERSON','text':'sample) suffix'}],False)
    text='token.*(SAMPLE) suffix; tokenXYZsample'
    hits=detector.find(text)
    assert len(hits)==1 and text[hits[0].start:hits[0].end]=='token.*(SAMPLE) suffix'


def test_revision_validation_queue_privacy_and_deletion(service,monkeypatch):
    client,engine,monitor,_=service
    parent,_=submit(service,batch=False)
    for body,status in [({'mode':'manual','redactions':[]},409),({'mode':'manual','redactions':['MISSING-PRIVATE-VALUE']},409),({'mode':'manual','redactions':['[PERSON_001]']},409),({'mode':'other'},422),({'redactions':[True]},422),({'redactions':['x'*513]},422)]:
        result=client.post('/api/jobs/'+parent+'/reprocess',json=body)
        assert result.status_code==status
        assert 'MISSING-PRIVATE-VALUE' not in result.text
    monkeypatch.setattr(config,'MAX_QUEUE',0)
    assert client.post('/api/jobs/'+parent+'/reprocess',json={'mode':'rerun'}).status_code==429
    monkeypatch.setattr(config,'MAX_QUEUE',32)
    result=client.post('/api/jobs/'+parent+'/reprocess',json={'mode':'manual','redactions':[MISSED]})
    assert result.status_code==202
    jid=result.json()['id'];bid=result.json()['batch_id']
    assert client.get('/api/batches/'+bid).json()['total']==1
    assert client.post('/api/jobs/'+jid+'/reprocess',json={'mode':'rerun'}).status_code==409
    with store.db() as db:
        records=json.dumps([dict(r) for r in db.execute('SELECT * FROM jobs')])
        assert MISSED not in records and EMAIL not in records
    # A revision owns its own source and survives deletion of its parent.
    assert client.delete('/api/jobs/'+parent).status_code==204
    assert process_next(engine,monitor)
    assert store.get(jid)['status']=='completed'
    assert client.delete('/api/jobs/'+jid).status_code==204
    assert not store.path(jid).exists()


def test_failed_original_can_be_rerun_but_not_manually_approved(service):
    client,engine,monitor,_=service
    jid=client.post('/api/jobs',files={'file':('bad.docx',b'not-a-package')}).json()['id']
    assert process_next(engine,monitor)
    assert store.get(jid)['status']=='blocked'
    assert client.post('/api/jobs/'+jid+'/reprocess',json={'mode':'manual','redactions':[MISSED]}).status_code==409
    next_job=client.post('/api/jobs/'+jid+'/reprocess',json={'mode':'rerun'}).json()['id']
    assert process_next(engine,monitor)
    assert store.get(next_job)['status']=='blocked'
    assert sorted(p.name for p in (store.path(next_job)/'output').iterdir())==['report.json']


def test_published_developer_guides_use_example_addresses(service):
    client,*_=service
    for url in ['/developer-guide.html','/developer-guide.md']:
        body=client.get(url).text
        assert '/home/' not in body and '192.168.' not in body
        assert 'veil.example.internal' in body and '/path/to/veil' in body
        assert 'Re-run detection' in body and 'Apply manual redactions' in body
