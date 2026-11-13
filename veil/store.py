import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from . import config

ID=re.compile(r"^[0-9a-f]{32}$")

def init():
    os.umask(0o077)
    config.DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    (config.DATA/"jobs").mkdir(exist_ok=True,mode=0o700)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, suffix TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)")
        columns={row[1] for row in c.execute('PRAGMA table_info(jobs)')}
        for name,kind in [('batch_id','TEXT'),('batch_index','INTEGER'),
                          ('provider_attempts','INTEGER NOT NULL DEFAULT 0'),
                          ('provider_wait_started','REAL'),('retry_at','REAL NOT NULL DEFAULT 0'),
                          ('provider_issue','TEXT'),('owner_id','TEXT'),('parent_id','TEXT'),('superseded_by','TEXT'),
                          ('processing_mode',"TEXT NOT NULL DEFAULT 'rerun'")]:
            if name not in columns:
                try:c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
                except sqlite3.OperationalError:
                    if name not in {row[1] for row in c.execute('PRAGMA table_info(jobs)')}:raise
        c.execute('CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, expected_count INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')
        if 'owner_id' not in {r[1] for r in c.execute('PRAGMA table_info(batches)')}:
            try:c.execute('ALTER TABLE batches ADD COLUMN owner_id TEXT')
            except sqlite3.OperationalError:
                if 'owner_id' not in {r[1] for r in c.execute('PRAGMA table_info(batches)')}:raise
        c.execute('CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, touched REAL NOT NULL, expires REAL NOT NULL, absolute_expires REAL NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS workspace_limits (peer_hash TEXT PRIMARY KEY, started REAL NOT NULL, attempts INTEGER NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner_id)')
        c.execute('CREATE TABLE IF NOT EXISTS batch_items (batch_id TEXT NOT NULL, position INTEGER NOT NULL, job_id TEXT NOT NULL UNIQUE, PRIMARY KEY(batch_id,position))')
        c.execute('''CREATE TABLE IF NOT EXISTS provider_state (
            provider_key TEXT PRIMARY KEY, status TEXT NOT NULL, checked REAL NOT NULL DEFAULT 0,
            expires REAL NOT NULL DEFAULT 0, next_probe REAL NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0, owner TEXT, failures INTEGER NOT NULL DEFAULT 0,
            code TEXT, message TEXT, capabilities TEXT)''')


def db():
    c=sqlite3.connect(config.DATA/"jobs.sqlite3",timeout=15)
    c.row_factory=sqlite3.Row
    return c

def path(jid):
    if not ID.fullmatch(jid): raise KeyError("Invalid job ID")
    return config.DATA/"jobs"/jid

def get(jid,owner_id=None):
    path(jid)
    with db() as c: row=c.execute("SELECT * FROM jobs WHERE id=?"+(" AND owner_id=?" if owner_id is not None else ""),(jid,owner_id) if owner_id is not None else (jid,)).fetchone()
    if not row: raise KeyError("Job not found")
    result=dict(row);result.pop('owner_id',None)
    p=path(jid)/"output"/"report.json"
    if result["status"] in config.TERMINAL and p.exists():
        r=json.loads(p.read_text())
        result.update(entity_counts=r.get("entity_counts",{}),timings_seconds=r.get("timings_seconds",{}),total_seconds=r.get("total_seconds"),warnings=r.get("warnings",[]),reason=r.get("reason"))
        result.update(review_summary=r.get('review_summary'),review_reasons=[{'code':i['code'],'message':i['message'],'location':i['location'],'location_label':i.get('location_label',i['location'])} for i in r.get('issues',[]) if i['severity']=='review'][:3])
    return result

def reserve(suffix,batch_id=None,batch_index=None,owner_id=None):
    jid=uuid.uuid4().hex
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if batch_id is not None:
            path(batch_id)
            batch=c.execute('SELECT expected_count,owner_id FROM batches WHERE id=?'+(' AND owner_id=?' if owner_id is not None else ''),(batch_id,owner_id) if owner_id is not None else (batch_id,)).fetchone()
            if not batch:raise KeyError('Batch not found')
            owner_id=batch['owner_id']
            if batch_index is None or not 0<=batch_index<batch[0]:raise ValueError('Batch index is out of range')
            if c.execute('SELECT 1 FROM batch_items WHERE batch_id=? AND position=?',(batch_id,batch_index)).fetchone():raise BatchConflict('Batch position already has a job')
        elif batch_index is not None:raise ValueError('A batch ID is required with a batch index')
        if c.execute("SELECT count(*) FROM jobs WHERE status IN ('receiving','queued','processing','waiting_provider')").fetchone()[0]>=config.MAX_QUEUE: raise OverflowError("Queue is full")
        now=time.time()
        c.execute("INSERT INTO jobs (id,suffix,status,stage,created,updated,batch_id,batch_index,owner_id) VALUES (?,?,?,?,?,?,?,?,?)",(jid,suffix,"receiving","uploading",now,now,batch_id,batch_index,owner_id))
        if batch_id is not None:
            c.execute('INSERT INTO batch_items VALUES (?,?,?)',(batch_id,batch_index,jid))
            c.execute('UPDATE batches SET updated=? WHERE id=?',(now,batch_id))
    folder=path(jid)
    for name in ("original","working","output"): (folder/name).mkdir(parents=True,mode=0o700)
    return jid

def update(jid,status,stage):
    with db() as c:
        now=time.time()
        c.execute("UPDATE jobs SET status=?,stage=?,updated=? WHERE id=?",(status,stage,now,jid))
        c.execute('UPDATE batches SET updated=? WHERE id=(SELECT batch_id FROM jobs WHERE id=?)',(now,jid))

def reprocess(jid, mode, redactions, owner_id=None):
    """Publish a new revision atomically; keep the earlier result downloadable."""
    from .revisions import previous_rules
    path(jid)
    new_id=uuid.uuid4().hex;folder=path(new_id)
    try:
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM jobs WHERE id=?'+(' AND owner_id=?' if owner_id is not None else ''),(jid,owner_id) if owner_id is not None else (jid,)).fetchone()
            if not row:raise KeyError('Job not found')
            if row['status'] not in config.TERMINAL:raise ValueError('Wait for processing to finish before creating a revision.')
            if row['superseded_by']:raise ValueError('A newer revision already exists. Open it before making more changes.')
            if mode=='manual' and (not redactions or row['status'] not in {'completed','review_required'}):
                raise ValueError('Manual corrections need a finished preview and at least one exact phrase.')
            if c.execute("SELECT count(*) FROM jobs WHERE status IN ('receiving','queued','processing','waiting_provider')").fetchone()[0]>=config.MAX_QUEUE:
                raise OverflowError('Queue is full')
            source=path(jid)/'original'/('source'+row['suffix'])
            if not source.is_file():raise ValueError('The retained original is unavailable. Upload it again.')
            rules=[]
            prior=path(jid)/'original'/'revision.json'
            if prior.exists():rules=json.loads(prior.read_text())['rules']
            if row['status'] in {'completed','review_required'}:
                rules+=previous_rules(path(jid))
            if redactions:
                extracted=path(jid)/'original'/'extracted.json'
                if not extracted.is_file():raise ValueError('Original extracted text is unavailable. Re-run detection first.')
                segments=json.loads(extracted.read_text())
                for text in redactions:
                    if not text.strip() or re.search(r'\[[A-Z_]+_\d{3,}\]',text):
                        raise ValueError('Select original text, without existing redaction placeholders.')
                    if not any(re.search(re.escape(text),s['text'],re.I) for s in segments):
                        raise ValueError('A phrase does not match retained source text. Check spelling and spacing.')
                    rules.append({'kind':'MANUAL','text':text})
            rules=list({(r['kind'],r['text']):r for r in rules}.values())
            if len(rules)>10000:raise ValueError('This document exceeds the retained redaction rule limit.')
            for name in ('original','working','output'):(folder/name).mkdir(parents=True,mode=0o700)
            shutil.copyfile(source,folder/'original'/source.name)
            (folder/'original'/'revision.json').write_text(json.dumps({'rules':rules,'mode':mode},ensure_ascii=False))
            now=time.time();bid=row['batch_id'];index=row['batch_index']
            member=c.execute('SELECT 1 FROM batch_items WHERE batch_id=? AND job_id=?',(bid,jid)).fetchone() if bid else None
            if member:
                if not c.execute('SELECT 1 FROM batches WHERE id=? AND owner_id IS ?',(bid,row['owner_id'])).fetchone():
                    raise KeyError('Batch not found')
                c.execute('UPDATE batch_items SET job_id=? WHERE batch_id=? AND job_id=?',(new_id,bid,jid))
                c.execute('UPDATE batches SET updated=? WHERE id=?',(now,bid))
            else:
                bid=uuid.uuid4().hex;index=0
                c.execute('INSERT INTO batches(id,expected_count,created,updated,owner_id) VALUES (?,?,?,?,?)',(bid,1,now,now,row['owner_id']))
                c.execute('INSERT INTO batch_items VALUES (?,?,?)',(bid,index,new_id))
            c.execute('''INSERT INTO jobs(id,suffix,status,stage,created,updated,batch_id,batch_index,parent_id,processing_mode,owner_id)
                         VALUES (?,?,'queued','waiting for worker',?,?,?,?,?,?,?)''',
                      (new_id,row['suffix'],now,now,bid,index,jid,mode,row['owner_id']))
            c.execute('UPDATE jobs SET superseded_by=?,batch_id=NULL,batch_index=NULL WHERE id=?',(new_id,jid))
        return {'id':new_id,'batch_id':bid,'parent_id':jid,'status':'queued','processing_mode':mode}
    except BaseException:
        shutil.rmtree(folder,ignore_errors=True)
        raise

def claim(provider_key=None, manual_only=False):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if provider_key is not None:
            healthy=c.execute("SELECT 1 FROM provider_state WHERE provider_key=? AND status='ready' AND expires>?",(provider_key,time.time())).fetchone()
            if not healthy:return None
            c.execute("""UPDATE jobs SET status='queued',stage='waiting for worker',retry_at=0,provider_issue=NULL,
                         provider_wait_started=CASE WHEN provider_attempts=0 THEN NULL ELSE provider_wait_started END
                         WHERE status='waiting_provider'""")
        row=c.execute("SELECT * FROM jobs WHERE status='queued'"+ (" AND processing_mode='manual'" if manual_only else '')+" ORDER BY created LIMIT 1").fetchone()
        if row: c.execute("UPDATE jobs SET status='processing',stage='extracting / OCR / detecting / writing',updated=? WHERE id=?",(time.time(),row["id"]))
    return dict(row) if row else None


def has_pending():
    with db() as c:
        return bool(c.execute("SELECT 1 FROM jobs WHERE status IN ('queued','waiting_provider') LIMIT 1").fetchone())


def wait_for_provider(message, code, retry_at):
    """Pause pending work without spending a document attempt or exposing output."""
    now=time.time()
    with db() as c:
        c.execute("""UPDATE jobs SET status='waiting_provider',stage=?,provider_issue=?,retry_at=?,
                     provider_wait_started=COALESCE(provider_wait_started,?),updated=?
                     WHERE processing_mode!='manual' AND status IN ('queued','waiting_provider') AND
                     (status='queued' OR retry_at!=? OR COALESCE(provider_issue,'')!=?)""",
                  (message,code,retry_at,now,now,retry_at,code))


def defer_provider(jid, message, code, retry_at, report):
    """Bound per-document retries; originals and batch membership never change."""
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM jobs WHERE id=? AND status='processing'",(jid,)).fetchone()
        if row is None:return None
        attempts=row['provider_attempts']+1
        report['provider_attempts']=attempts
        expired=row['provider_wait_started'] is not None and now-row['provider_wait_started']>=config.PROVIDER_WAIT_TIMEOUT
        status='failed' if attempts>=3 or expired else 'waiting_provider'
        if status=='failed':
            report.update(status='failed',reason='The model could not process this document after automatic retries. The original is unchanged; retry when the model is ready.',artifacts=['report.json'])
            (path(jid)/'output'/'report.json').write_text(json.dumps(report,indent=2))
            message='model retries exhausted'
        c.execute('''UPDATE jobs SET status=?,stage=?,provider_issue=?,retry_at=?,provider_attempts=?,
                     provider_wait_started=COALESCE(provider_wait_started,?),updated=? WHERE id=?''',
                  (status,message,code,retry_at,attempts,now,now,jid))
        c.execute('UPDATE batches SET updated=? WHERE id=?',(now,row['batch_id']))
    return status

def delete(jid,release_batch_slot=False,owner_id=None):
    job=get(jid,owner_id)
    if job["status"] in {"processing","receiving"}: raise ValueError("Active job cannot be deleted; retry after completion or timeout")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT status FROM jobs WHERE id=?"+(" AND owner_id=?" if owner_id is not None else ""),(jid,owner_id) if owner_id is not None else (jid,)).fetchone()
        if not row:raise KeyError("Job not found")
        if row and row[0] in {"processing","receiving"}: raise ValueError("Job is active")
        c.execute("DELETE FROM jobs WHERE id=?",(jid,))
        if release_batch_slot:c.execute('DELETE FROM batch_items WHERE job_id=?',(jid,))
    shutil.rmtree(path(jid),ignore_errors=True)

def recover_and_retain():
    now=time.time()
    with db() as c:
        c.execute("DELETE FROM workspaces WHERE expires<=? OR absolute_expires<=?",(now,now))
        c.execute('DELETE FROM workspace_limits WHERE started<=?',(now-3600,))
        rows=c.execute("SELECT * FROM jobs").fetchall()
        for row in rows:
            if row["status"] in {"processing","receiving"} and now-row["updated"]>config.TIMEOUT+60:
                # A worker died or a receiving upload was interrupted. Never expose partial artifacts.
                folder=path(row["id"])/"output"
                shutil.rmtree(folder,ignore_errors=True); folder.mkdir(exist_ok=True)
                report={"status":"failed","reason":"Interrupted job or processing deadline exceeded; submit original again","artifacts":["report.json"],"policy":config.POLICY}
                (folder/"report.json").write_text(json.dumps(report))
                c.execute("UPDATE jobs SET status='failed',stage='interrupted',updated=? WHERE id=?",(now,row["id"]))
                if row["batch_id"]:c.execute("UPDATE batches SET updated=? WHERE id=?",(now,row["batch_id"]))
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        waiting=c.execute("SELECT * FROM jobs WHERE status='waiting_provider' AND provider_wait_started<=?",(now-config.PROVIDER_WAIT_TIMEOUT,)).fetchall()
        for row in waiting:
            report={'status':'failed','reason':'The model remained unavailable beyond the automatic retry window. The original is unchanged; submit again when the model is ready.',
                    'provider_error':row['provider_issue'],'artifacts':['report.json'],'policy':config.POLICY}
            (path(row['id'])/'output'/'report.json').write_text(json.dumps(report,indent=2))
            c.execute("UPDATE jobs SET status='failed',stage='model unavailable',updated=? WHERE id=?",(now,row['id']))
            c.execute('UPDATE batches SET updated=? WHERE id=?',(now,row['batch_id']))
    with db() as c:
        expired=c.execute("SELECT id FROM batches WHERE updated<? AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.batch_id=batches.id AND jobs.status IN ('receiving','queued','processing','waiting_provider'))",(now-config.RETENTION,)).fetchall()
        for batch in expired:
            c.execute('DELETE FROM batch_items WHERE batch_id=?',(batch[0],));c.execute('DELETE FROM batches WHERE id=?',(batch[0],))
    for row in rows:
        if row["status"] in config.TERMINAL and now-row["updated"]>config.RETENTION:
            try: delete(row["id"])
            except (KeyError,ValueError): pass


def reserve_batch(count,owner_id=None):
    if not 1<=count<=config.MAX_BATCH:raise ValueError('Batch must contain 1–50 documents')
    bid=uuid.uuid4().hex;now=time.time()
    with db() as c:c.execute('INSERT INTO batches(id,expected_count,created,updated,owner_id) VALUES (?,?,?,?,?)',(bid,count,now,now,owner_id))
    return bid


def get_batch(bid,owner_id=None):
    path(bid)  # Same opaque ID validation; no filesystem access.
    with db() as c:
        batch=c.execute('SELECT * FROM batches WHERE id=?'+(' AND owner_id=?' if owner_id is not None else ''),(bid,owner_id) if owner_id is not None else (bid,)).fetchone()
        members=c.execute('SELECT * FROM batch_items WHERE batch_id=? ORDER BY position',(bid,)).fetchall()
        if owner_id is not None and c.execute('''SELECT 1 FROM batch_items bi JOIN jobs j ON j.id=bi.job_id
            WHERE bi.batch_id=? AND j.owner_id IS NOT ? LIMIT 1''',(bid,owner_id)).fetchone():
            raise KeyError('Batch not found')
    if not batch:raise KeyError('Batch not found')
    items=[];counts={status:0 for status in ['not_submitted','receiving','queued','processing','waiting_provider','completed','review_required','blocked','failed','deleted']}
    for member in members:
        try:job=get(member['job_id'],owner_id)
        except KeyError:job={'id':member['job_id'],'status':'deleted','stage':'Deleted or expired'}
        job.pop('batch_id',None);job.pop('batch_index',None)
        job['index']=member['position'];items.append(job);counts[job['status']]+=1
    counts['not_submitted']=batch['expected_count']-len(items)
    ready=sum(counts[s] for s in config.TERMINAL);finished=ready+counts['deleted']
    return {'id':bid,'total':batch['expected_count'],'created':batch['created'],'updated':batch['updated'],'items':items,'counts':counts,'finished':finished,'ready':ready,'export_ready':finished==batch['expected_count']}

class BatchConflict(Exception):pass
