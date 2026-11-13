"""Synthetic OpenAI-compatible oracle for document/transport tests, not model accuracy.

The oracle is test-only. Live Gemma coverage lives in scripts/test_live_llm.py.
It supplies known fixture spans so preservation tests need no external provider.
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from veil.engine import Engine
from veil.datasets import generate, NAMES
from veil.languages import AlignedText


def oracle_entities(text, structured=False, context=None):
    view = AlignedText(text)
    matches = []
    names = NAMES + ['Amelia Linden', '张伟', '陈怡君', '林小明', '林志明',
                     '李文静', '赵晴', '蒋文', '晓雨 Rowan Vale', '梁清']
    patterns = [('PERSON', re.escape(name)) for name in names]
    patterns += [
        ('EMAIL_ADDRESS', r'(?i)[A-Z0-9._%+\-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,}'),
        ('EMAIL_ADDRESS', r'(?:合成邮箱|至诚)@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}'),
        ('NRIC_FIN', r'(?i)(?<![A-Z0-9])[STFGM][0-9]{7}[A-Z](?![A-Z0-9])'),
        ('ACCOUNT_ID', r'(?:VEIL-[A-Z0-9-]{4,}|DEMO-[0-9]+|CUST-[0-9]+|TEST-CN-[0-9]+|ACCT-TW-[0-9]+|CN-[0-9]+|ABC-[0-9]+)'),
        ('ADDRESS', r'[0-9]{1,3} (?:Fictional Crescent|Imaginary Lane), Singapore [0-9]{6}'),
        ('ADDRESS', r'(?:上海市浦东新区丁香路88号|台北市大安区仁爱路88号|北京市朝阳区望京路18号|高雄市苓雅区四维路36号|虚构大道号1234)'),
    ]
    if not structured or context or '+' in view.text:
        patterns += [('PHONE_NUMBER', r'(?<![A-Za-z0-9])(?:\+65[ .-]?)?[689][0-9]{3}[ .-]?[0-9]{4}(?![0-9])'),
                     ('PHONE_NUMBER', r'(?<![A-Za-z0-9])(?:\+86[ .-]?)?1[3-9][0-9][ .-]?[0-9]{4}[ .-]?[0-9]{4}(?![0-9])')]
    for kind, pattern in patterns:
        for match in re.finditer(pattern, view.text):
            a, b = view.original_span(*match.span())
            matches.append((a, b, kind))
    accepted = []
    for a, b, kind in sorted(set(matches), key=lambda m: (-(m[1]-m[0]), m[0])):
        if not any(a < y and b > x for x, y, _ in accepted):
            accepted.append((a, b, kind))
    return [{'type': kind, 'text': text[a:b]} for a, b, kind in sorted(accepted)]


def oracle_response(payload):
    content = json.loads(payload['messages'][1]['content'])
    if 'segments' in content:
        result = {'results': {str(s['id']): oracle_entities(s['text'], s.get('structured', False), s.get('context'))
                              for s in content['segments']}}
    else:
        result = {'entities': oracle_entities(content['text'], content.get('structured', False), content.get('context'))}
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result, ensure_ascii=False)}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10}}


@pytest.fixture(scope='session', autouse=True)
def synthetic_llm_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            data = json.dumps(oracle_response(payload)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    values = {'VEIL_DETECTOR': 'llm', 'VEIL_LLM_ENDPOINT': f'http://127.0.0.1:{server.server_port}/v1',
              'VEIL_LLM_MODEL': 'test/synthetic-oracle', 'VEIL_LLM_API_KEY': 'synthetic-test-key',
              'VEIL_LLM_ALLOW_NETWORK': 'true', 'VEIL_LLM_CONCURRENCY': '1'}
    previous = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        for k, value in previous.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value

@pytest.fixture(scope='session')
def engine(synthetic_llm_server):
    engine = Engine()
    engine.detector.client.close()
    engine.detector.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=oracle_response(json.loads(request.content)))))
    yield engine
    engine.detector.close()

@pytest.fixture(scope='session')
def dataset(tmp_path_factory):
    p=tmp_path_factory.mktemp('synthetic');generate(p);return p

@pytest.fixture(scope="session")
def multilingual_dataset(tmp_path_factory):
    from veil.multilingual_dataset import generate
    p=tmp_path_factory.mktemp("multilingual");generate(p);return p

# Existing feature tests use a real anonymous workspace, without bypassing auth.
from fastapi.testclient import TestClient as RawTestClient
import hashlib
from veil import store
from veil.workspaces import COOKIE

class WorkspaceClient(RawTestClient):
    def __init__(self,app,**kwargs):
        kwargs.setdefault('base_url','https://testserver')
        kwargs.setdefault('headers',{'X-Veil-Request':'1'})
        super().__init__(app,**kwargs)
    def __enter__(self):
        super().__enter__()
        result=self.get('/api/workspace');assert result.status_code==200,result.text
        digest=hashlib.sha256(self.cookies[COOKIE].encode()).hexdigest()
        with store.db() as db:self.workspace_owner=db.execute('SELECT id FROM workspaces WHERE token_hash=?',(digest,)).fetchone()[0]
        return self

@pytest.fixture(autouse=True)
def private_cli_cookie_file(tmp_path,monkeypatch):
    monkeypatch.setenv('VEIL_WORKSPACE_COOKIE_FILE',str(tmp_path/'cli.cookies'))
