"""One warm model per bounded slot; supervisor kills and replaces timed-out slots."""
import json
import multiprocessing as mp
import os
import shutil
import time
from . import config, store
from .provider_health import ProviderMonitor, MESSAGES


def clear_attempt(folder):
    """Remove generated attempt files only; the retained original stays intact."""
    for name in ('output','working'):
        target=folder/name
        shutil.rmtree(target,ignore_errors=True)
        target.mkdir(mode=0o700,exist_ok=True)


def process_next(engine, monitor):
    if not store.has_pending():return False
    # Local corrections remain available during a model outage.
    row=store.claim(manual_only=True)
    if not row:
        if not monitor.ensure_ready(engine.detector):
            store.wait_for_provider(*monitor.waiting())
            return False
        row=store.claim(monitor.key)
    if not row:return False
    jid=row['id'];folder=store.path(jid)
    clear_attempt(folder)
    base=engine.detector
    revision=folder/'original'/'revision.json'
    try:
        if revision.exists():
            from .revisions import RevisionDetector
            data=json.loads(revision.read_text())
            engine.detector=RevisionDetector(base,data['rules'],row['processing_mode']=='rerun')
        report=engine.process(folder/'original'/('source'+row['suffix']),folder/'output',private_preview=True)
    finally:engine.detector=base
    if row['parent_id']:
        report['revision']={'parent_id':row['parent_id'],'mode':row['processing_mode'],'previous_redactions_retained':True}
        if row['processing_mode']=='manual':
            report['models']['detector']='Local literal corrections and retained earlier detections; no new model inspection'
    import psutil
    report['worker_rss_mib']=round(psutil.Process().memory_info().rss/2**20,2)
    report['provider_attempts']=row['provider_attempts']
    if report['status']=='waiting_provider':
        monitor.fail(report.get('provider_error','unavailable'))
        clear_attempt(folder)
        status=store.defer_provider(jid,*monitor.waiting(),report)
    else:
        if row['processing_mode']!='manual':monitor.save_capabilities(engine.detector)
        (folder/'output'/'report.json').write_text(json.dumps(report,indent=2))
        store.update(jid,report['status'],'finished')
        shutil.rmtree(folder/'working',ignore_errors=True)
        status=report['status']
    print(json.dumps({'job_id':jid,'status':status}),flush=True)
    return True

def slot():
    from .engine import Engine
    from .llm import LLMSettings, LLMDetector
    engine=None;monitor=None
    while True:
        try:
            if engine is None:
                engine=Engine()
                monitor=ProviderMonitor(engine.detector.settings)
            elif os.getenv('VEIL_LLM_API_KEY_FILE') and store.has_pending():
                settings=LLMSettings.from_env()
                if settings!=engine.detector.settings:
                    engine.detector.close()
                    engine.detector=LLMDetector(settings)
                    monitor=ProviderMonitor(settings)
        except (ValueError,OSError):
            store.wait_for_provider(MESSAGES['configuration'],'configuration',time.time()+config.PROVIDER_RETRY_SECONDS)
            time.sleep(config.PROVIDER_RETRY_SECONDS)
            continue
        if not process_next(engine,monitor):time.sleep(0.3)

def main():
    import signal
    store.init()
    count=max(1,min(int(os.getenv("VEIL_WORKERS","1")),4))
    children=[]
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            children=[p for p in children if p.is_alive()]
            while len(children)<count:
                p=mp.Process(target=slot); p.start(); children.append(p)
            # With no heartbeat during native calls, a deadline forces all slots to restart.
            with store.db() as c:
                expired=c.execute("SELECT count(*) FROM jobs WHERE status='processing' AND updated<?",(time.time()-config.TIMEOUT,)).fetchone()[0]
            if expired:
                for p in children: p.kill(); p.join()
                children=[]
                with store.db() as c:
                    c.execute("UPDATE jobs SET updated=0 WHERE status='processing'")
                store.recover_and_retain()
            store.recover_and_retain()
            time.sleep(1)
    finally:
        for p in children: p.terminate()
        for p in children:
            p.join(3)
            if p.is_alive(): p.kill(); p.join()
if __name__=="__main__": main()
