#!/usr/bin/env python3
"""Acceptance test against a deployed Veil API and its REAL configured provider.

Uses synthetic data only. No mock transport, provider substitution, or browser keys.
"""
import argparse
import os
import ssl
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import zipfile

import httpx
from veil import __version__
import pymupdf
from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation
from pptx.util import Inches

TEXT = 'Name: Amelia Quill\nEmail: amelia.quill@example.test\nAccount ID: DEMO-13579'
VALUES = ['Amelia Quill', 'amelia.quill@example.test', 'DEMO-13579']
MIXED = TEXT + '\n姓名：陳怡君\n电话：+65 6123 4567\nNRIC: S1234567D'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fixtures(folder):
    d = Document()
    d.core_properties.author = ''
    d.core_properties.last_modified_by = ''
    for line in MIXED.splitlines():
        d.add_paragraph(line)
    d.save(folder / 'synthetic.docx')
    p = Presentation()
    p.core_properties.author = ''
    slide = p.slides.add_slide(p.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(3))
    box.text_frame.text = TEXT
    p.save(folder / 'synthetic.pptx')
    w = Workbook()
    w.properties.creator = ''
    sheet = w.active
    sheet.title = 'Example'
    for line in TEXT.splitlines():
        label, value = line.split(': ', 1)
        sheet.append([label, value])
    w.save(folder / 'synthetic.xlsx')
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 70), TEXT, fontsize=12)
        pdf.save(folder / 'synthetic.pdf')
    return [folder / ('synthetic.' + ext) for ext in ['docx', 'pptx', 'xlsx', 'pdf']]


def check_clean(text, values=VALUES):
    for index, value in enumerate(values):
        require(value.casefold() not in text.casefold(), f'Synthetic PII field {index + 1} remained in output')


def reopen(data, suffix):
    stream = io.BytesIO(data)
    if suffix == '.docx':
        require(len(Document(stream).paragraphs) >= 3, 'DOCX paragraphs were lost')
    elif suffix == '.pptx':
        require(len(Presentation(stream).slides) == 1, 'PPTX slide count changed')
    elif suffix == '.xlsx':
        require(load_workbook(stream).active.max_row == 3, 'XLSX rows were lost')
    else:
        with pymupdf.open(stream=data, filetype='pdf') as pdf:
            require(len(pdf) == 1, 'PDF page count changed')
            require(not pdf[0].get_text().strip(), 'PDF retained a searchable text layer')
            require(not pdf.embfile_count() and not list(pdf[0].annots() or []), 'PDF retained attachments or annotations')
        return
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(archive.testzip() is None, 'Office output is corrupt')
        xml = '\n'.join(archive.read(n).decode('utf-8') for n in archive.namelist() if n.endswith(('.xml', '.rels')))
        check_clean(xml)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='https://127.0.0.1:8787')
    parser.add_argument('--output', type=Path, default=Path('evidence/llm-live'))
    parser.add_argument('--deadline', type=int, default=1200)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary = {'started_at': datetime.now(timezone.utc).isoformat(), 'server': args.server,
               'provider': 'real configured provider', 'mocked': False, 'checks': [], 'documents': [], 'passed': False}
    try:
        with httpx.Client(base_url=args.server.rstrip('/'), trust_env=False, timeout=240, follow_redirects=False,headers={'X-Veil-Request':'1'},verify=ssl.create_default_context(cafile=os.getenv('VEIL_CA_BUNDLE')) if os.getenv('VEIL_CA_BUNDLE') else True) as client:
            client.get('/api/workspace').raise_for_status()
            def call(method, url, expected=200, **kwargs):
                response = client.request(method, url, **kwargs)
                require(response.status_code == expected, f'{method} {url}: expected HTTP {expected}, received {response.status_code}')
                return response

            health = call('GET', '/api/health').json()
            require(health['version'] == __version__, 'Deployed service version does not match this checkout')
            config = call('GET', '/api/llm/config').json()
            require(config['mode'] == 'llm' and config['configured'], 'Deployed LLM configuration is not enabled')
            require(config['credentials'] == 'server-managed', 'Credentials are not server-managed')
            require('api_key' not in config, 'Configuration exposed a credential field')
            summary['model'] = config['model']
            summary['checks'].append('version and server-managed LLM configuration')
            require('/api/llm/compare' not in call('GET', '/openapi.json').json()['paths'], 'Comparison route is still installed')
            workspace = call('GET', '/').text
            require('/lab/' not in workspace and 'Presidio' not in workspace, 'Comparison UI is still exposed')
            require(client.get('/lab/').status_code == 404, 'Comparison page is still exposed')
            summary['checks'].append('LLM-only workspace; comparison API and UI removed')
            print('Uploading four synthetic documents to the configured LLM…', flush=True)
            batch = call('POST', '/api/batches', expected=201, json={'count': 4}).json()['id']
            summary['batch_id'] = batch
            with tempfile.TemporaryDirectory(prefix='veil-live-') as temp:
                jobs = []
                for index, path in enumerate(fixtures(Path(temp))):
                    result = call('POST', '/api/jobs', expected=202,
                                  data={'batch_id': batch, 'batch_index': str(index)},
                                  files={'file': (path.name, path.read_bytes())}).json()
                    jobs.append((result['id'], path.suffix))
                deadline = time.monotonic() + args.deadline
                for jid, suffix in jobs:
                    while True:
                        job = call('GET', '/api/jobs/' + jid).json()
                        if job['status'] in {'completed', 'review_required', 'blocked', 'failed'}:
                            break
                        require(time.monotonic() < deadline, 'Timed out waiting for document processing')
                        time.sleep(2)
                    report = call('GET', f'/api/jobs/{jid}/report').json()
                    summary['documents'].append({'job_id': jid, 'format': suffix, 'status': job['status'],
                                                 'detector': report.get('detector'), 'seconds': report.get('total_seconds'),
                                                 'reason': report.get('reason')})
                    require(job['status'] in {'completed', 'review_required'}, f'{suffix} processing ended as {job["status"]}; see saved report summary')
                    require(report['detector']['mode'] == 'llm', f'{suffix} used a non-LLM detector')
                    require(report['detector']['model'] == config['model'], f'{suffix} used the wrong model')
                    require(report['detector']['calls'] > 0, f'{suffix} made no real provider calls')
                    clean = call('GET', f'/api/jobs/{jid}/download/sanitised.txt').text
                    check_clean(clean, VALUES + (['陳怡君', '+65 6123 4567', 'S1234567D'] if suffix == '.docx' else []))
                    require('Name' in clean and 'Email' in clean, f'{suffix} lost benign labels')
                    document = call('GET', f'/api/jobs/{jid}/download/sanitised{suffix}').content
                    reopen(document, suffix)
                    (args.output / ('sanitised' + suffix)).write_bytes(document)
                    (args.output / (suffix[1:] + '-report.json')).write_text(json.dumps(report, indent=2), encoding='utf-8')
                    print(f'{suffix}: {job["status"]}; {report["detector"]["calls"]} real model calls; output verified', flush=True)
            batch_result = call('GET', '/api/batches/' + batch).json()
            require(batch_result['export_ready'] and batch_result['finished'] == 4, 'Batch did not finish')
            archive_data = call('GET', f'/api/batches/{batch}/download').content
            with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
                require(archive.testzip() is None, 'Batch ZIP is corrupt')
                names = archive.namelist()
                require(sum(n.endswith(('sanitised.docx', 'sanitised.pptx', 'sanitised.xlsx', 'sanitised.pdf')) for n in names) == 4, 'Batch ZIP is missing documents')
                require(not any('original' in n or 'source.' in n for n in names), 'Batch ZIP contains originals')
            summary['checks'].append('four-format upload, queue, actual LLM reports, redacted downloads, reopening and batch ZIP')
            summary['passed'] = True
    except Exception as exc:
        # Never print provider payloads, request headers, configuration or credentials.
        summary['error'] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print('Live acceptance failed: ' + summary['error'], file=sys.stderr, flush=True)
    finally:
        summary['finished_at'] = datetime.now(timezone.utc).isoformat()
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
