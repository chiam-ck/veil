"""Ground-truth evaluation. Values stay only in synthetic dataset/private extraction."""
import concurrent.futures
import json
import multiprocessing as mp
import os
import re
import resource
import threading
import psutil
import subprocess
import sys
import statistics
import time
from collections import Counter,defaultdict
from pathlib import Path
from .engine import Engine, package, parse

_ENGINE=None
_PEAK=0
_MONITOR_STARTED=False

def normal(s):return re.sub(r'\s+',' ',s).strip().casefold()

def entity_normal(kind,value):
    value=normal(value)
    return re.sub(r'[\s().-]+','',value) if kind=='PHONE_NUMBER' else value

def init_worker():
    global _ENGINE, _PEAK, _MONITOR_STARTED
    if not _MONITOR_STARTED:
        _MONITOR_STARTED=True
        def sample():
            global _PEAK
            proc=psutil.Process()
            while True:
                _PEAK=max(_PEAK,proc.memory_info().rss)
                time.sleep(.01)
        threading.Thread(target=sample,daemon=True).start()
    _ENGINE=Engine()

def process_one(args):
    source,folder=args
    folder=Path(folder);(folder/'original').mkdir(parents=True,exist_ok=True)
    report=_ENGINE.process(Path(source),folder/'output',private_preview=True)
    report['process_peak_rss_mib']=round(max(_PEAK,psutil.Process().memory_info().rss)/2**20,2)
    return report

def valid(path,fmt):
    # Cross-library XML ZIP + library parsing. Independent Office rendering is separate.
    try:
        if fmt=='docx':
            from docx import Document
            Document(path)
        elif fmt=='pptx':
            from pptx import Presentation
            Presentation(path)
        elif fmt=='xlsx':
            from openpyxl import load_workbook
            w=load_workbook(path);w.close()
        else:
            import pymupdf
            import shutil
            if shutil.which("pdfinfo"):
                probe=subprocess.run(["pdfinfo",str(path)],capture_output=True,timeout=15)
                if probe.returncode or b"Syntax Error" in probe.stderr: return False
            with pymupdf.open(path) as d:
                for p in d:p.get_pixmap(matrix=pymupdf.Matrix(.1,.1))
        return True
    except Exception:return False

def score(doc,report,folder):
    p=folder/'original'/'extracted.json'
    segments=json.loads(p.read_text()) if p.exists() else []
    lookup={x['location']:x['text'] for x in segments}
    expected=list(doc['entities'])
    # Office images are shared package parts, inspected once even when reused across pages.
    if doc['format'] in {'docx','pptx'}:
        seen=set();dedup=[]
        for e in expected:
            key=(e['type'],e['value'],e['source'])
            if e['source']=='ocr' and key in seen:continue
            dedup.append(e);seen.add(key)
        expected=dedup
    predicted=[]
    for e in report.get('entities',[]):
        raw=lookup.get(e['location'],'')[e['start']:e['end']]
        predicted.append((e['type'],entity_normal(e['type'],raw),e['source']))
    # Imported datasets may annotate only selected PII classes. Unknown classes are not negatives.
    scoped=doc.get('scored_types')
    if scoped is not None:
        predicted=[e for e in predicted if e[0] in scoped]
        expected=[e for e in expected if e['type'] in scoped]
    available=Counter(predicted);rows=[];ocr_text='\n'.join(x['text'] for x in segments if x['source']=='ocr')
    ocr_miss=detect_miss=0
    for e in expected:
        key=(e['type'],entity_normal(e['type'],e['value']),e['source'])
        hit=available[key]>0
        if hit:available[key]-=1
        elif e['source']=='ocr' and entity_normal(e['type'],e['value']) not in entity_normal(e['type'],ocr_text):ocr_miss+=1
        else:detect_miss+=1
        rows.append({'type':e['type'],'tp':int(hit),'fn':int(not hit),'fp':0,'source':e['source']})
    for (kind,value,source),n in available.items():
        if n:rows.append({'type':kind,'tp':0,'fn':0,'fp':n,'source':source})
    residual_values=[]
    output=folder/'output';clean=(output/'sanitised.txt').read_text() if (output/'sanitised.txt').exists() else ''
    if doc['format']!='pdf' and (output/('sanitised.'+doc['format'])).exists():
        for name,data in package(output/('sanitised.'+doc['format'])).items():
            if name.endswith(('.xml','.rels')):
                root=parse(data);clean+=' '+' '.join(root.itertext())+' '+' '.join(v for n in root.iter() if isinstance(n.tag,str) for v in n.attrib.values())
    for e in expected:
        if normal(e['value']) in normal(clean):residual_values.append(e['type'])
    # Optical audit of final pixels is separate from extracted/package residual checks.
    return {'rows':rows,'ocr_value_misses':ocr_miss,'detection_value_misses':detect_miss,'residual_occurrences':len(residual_values),'residual_types':dict(Counter(residual_values)),'expected_occurrences':len(expected)}

def benchmark(dataset,output,concurrency=2):
    dataset=Path(dataset).resolve();output=Path(output).resolve()
    if output.exists() and any(output.iterdir()):raise FileExistsError('Benchmark output must be empty')
    output.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((dataset/'manifest.json').read_text())
    cold_begin=time.perf_counter()
    cold_run=subprocess.run([sys.executable,'-m','veil.cold',str(dataset/manifest['documents'][0]['path']),str(output/'cold')],capture_output=True,text=True,timeout=120,check=True)
    cold_process_wall=time.perf_counter()-cold_begin
    start=time.perf_counter();init_worker();cold=time.perf_counter()-start
    results=[];aggregates=defaultdict(Counter)
    began=time.perf_counter()
    for i,doc in enumerate(manifest['documents']):
        print(f'Benchmark {i+1}/{len(manifest["documents"])} {doc["path"]}',file=sys.stderr,flush=True)
        folder=output/'single'/f'{i:02d}'
        report=process_one((dataset/doc['path'],folder))
        scored=score(doc,report,folder)
        for row in scored['rows']:
            for key in ('all',f'subset:{doc["subset"]}',f'format:{doc["format"]}',f'format:{doc["format"]}:type:{row["type"]}',f'type:{row["type"]}',f'{doc["subset"]}:{doc["format"]}:{row["type"]}',f'source:{row["source"]}',f'language:{doc.get("language","unspecified")}',f'language:{doc.get("language","unspecified")}:source:{row["source"]}',f'language:{doc.get("language","unspecified")}:type:{row["type"]}',f'language:{doc.get("language","unspecified")}:format:{doc["format"]}'):
                aggregates[key].update({k:row[k] for k in ('tp','fn','fp')})
        file=folder/'output'/('sanitised.'+doc['format'])
        result={k:v for k,v in doc.items() if k not in {'entities','removed_under_policy'}}
        result.update(status=report['status'],reason=report.get('reason'),seconds=report['total_seconds'],stage_seconds=report.get('timings_seconds',{}),peak_rss_mib=report['process_peak_rss_mib'],valid=valid(file,doc['format']) if file.exists() else False,**{k:v for k,v in scored.items() if k!='rows'})
        results.append(result)
    sequential=time.perf_counter()-began
    selected=[i for i in (0,3,8,11) if i<len(manifest['documents'])] # native and image documents; identical in warm and concurrent runs.
    started=time.perf_counter()
    warm=[process_one((dataset/manifest['documents'][i]['path'],output/'warm'/str(i))) for i in selected]
    warm_wall=time.perf_counter()-started
    concurrency=max(1,min(concurrency,4));started=time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(max_workers=concurrency,initializer=init_worker,mp_context=mp.get_context("spawn")) as pool:
        concurrent_reports=list(pool.map(process_one,[(dataset/manifest['documents'][i]['path'],output/'concurrent'/str(i)) for i in selected]))
    concurrent_wall=time.perf_counter()-started
    metrics={}
    for key,c in aggregates.items():
        metrics[key]={**dict(c),'precision':round(c['tp']/(c['tp']+c['fp']),4) if c['tp']+c['fp'] else None,'recall':round(c['tp']/(c['tp']+c['fn']),4) if c['tp']+c['fn'] else None}
    stages=Counter()
    for r in results:stages.update(r['stage_seconds'])
    summary={'dataset_seed':manifest['seed'],'documents':len(results),'device':'CPU','cold_process_and_first_doc_seconds':round(cold_process_wall,4),'cold_model_load_seconds':round(cold,4),'sequential_wall_seconds':round(sequential,4),'median_job_seconds':round(statistics.median(r['seconds'] for r in results),4),'max_process_peak_rss_mib':max(r['peak_rss_mib'] for r in results),'status_counts':dict(Counter(r['status'] for r in results)),'valid_outputs':sum(r['valid'] for r in results),'metrics':metrics,'stage_total_seconds':dict(stages),'ocr_value_misses':sum(r['ocr_value_misses'] for r in results),'detection_value_misses':sum(r['detection_value_misses'] for r in results),'residual_extracted_or_package_occurrences':sum(r['residual_occurrences'] for r in results),'small_batch':{'documents':[manifest['documents'][i]['path'] for i in selected],'warm_single_worker_wall_seconds':round(warm_wall,4),'concurrency':concurrency,'concurrent_wall_including_model_start_seconds':round(concurrent_wall,4),'warm_job_seconds':[r['total_seconds'] for r in warm],'concurrent_job_seconds':[r['total_seconds'] for r in concurrent_reports],'concurrent_worker_peak_rss_mib':[r['process_peak_rss_mib'] for r in concurrent_reports]},'matching_rules':'One-to-one exact type + complete value after casefold and whitespace normalization. Phone matching additionally ignores whitespace, dots, parentheses and hyphens; digits and country code must match completely. Partial overlaps count FP+FN. OCR misses mean complete ground-truth value absent from OCR extraction; remaining unmatched expected entities are detection misses. Office shared image parts counted once. No fuzzy credit. Metadata/comments/formula removals excluded from entity accuracy and tested separately. Mixed Chinese entity misses included.','memory_method':'psutil process RSS sampled every 10 ms, process lifetime high water including models; concurrent workers sampled individually; excludes other NUC services. Sampling can miss short allocation spikes.', 'validation':'Automated ZIP/XML and reopen checks, PDF render smoke checks. These do not prove Microsoft Office compatibility. Independent render/visual checks recorded separately. Residual extraction does not inspect final image pixels; optical audit is separate.','documents_detail':results}
    if any(d.get('ocr_expected_text') for d in manifest['documents']):
        from .multilingual_evaluation import evaluate
        summary['multilingual']=evaluate(dataset,output,manifest,results)
    summary['detector']=_ENGINE.detector.describe()
    summary['detector']['statistics_scope']='Last warm document only; per-document reports contain their own calls and usage'
    if any('scored_types' in d for d in manifest['documents']):
        summary['matching_rules'] += ' Imported-corpus scores are conditional on each document scored_types; unannotated categories are unknown, not negative. Upstream annotations are not independently human-adjudicated.'
        summary['ground_truth_status'] = manifest.get('ground_truth_status','Imported synthetic annotations')
    (output/'summary.json').write_text(json.dumps(summary,indent=2))
    lines=['# Veil CPU benchmark','',f'Documents: {len(results)}. Sequential wall: {sequential:.2f}s. Cold model load: {cold:.2f}s.',f'Peak process high-water RSS: {summary["max_process_peak_rss_mib"]:.1f} MiB.',f'Statuses: {summary["status_counts"]}. Valid outputs: {summary["valid_outputs"]}.','', '| Group | TP | FP | FN | Precision | Recall |','|---|---:|---:|---:|---:|---:|']
    for key,m in metrics.items():
        if key=='all' or key.startswith(('subset:','format:','type:','source:','language:')):lines.append(f'| {key} | {m["tp"]} | {m["fp"]} | {m["fn"]} | {m["precision"]} | {m["recall"]} |')
    lines+=['',summary['matching_rules'],'',f'OCR value misses: {summary["ocr_value_misses"]}; detection value misses: {summary["detection_value_misses"]}.',f'Residual extracted/package occurrences: {summary["residual_extracted_or_package_occurrences"]}.','',summary['validation'],'','| File | Status | Seconds | Valid |','|---|---|---:|---|']
    for r in results:lines.append(f'| {r["path"]} | {r["status"]} | {r["seconds"]} | {r["valid"]} |')
    if 'multilingual' in summary:
        from .multilingual_evaluation import markdown
        lines+=markdown(summary['multilingual'])
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    return {k:v for k,v in summary.items() if k not in {'documents_detail','metrics'}}|{'overall':metrics.get('all') ,'report':str(output/'report.md')}
