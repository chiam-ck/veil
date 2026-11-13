"""Provider drift/outage recovery without weakening document validation."""
import hashlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from docx import Document
from conftest import WorkspaceClient as TestClient

from conftest import oracle_response
from veil import config, store
from veil.api import app
from veil.llm import LLMDetector, LLMSettings, ProviderError
from veil.provider_adapter import ChatCapabilities
from veil.provider_health import ProviderMonitor, provider_key, public_status
from veil.worker import process_next


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path/'service')
    store.init()
    return LLMSettings(endpoint='http://localhost/v1', model='test/recovery',
                       api_key='private-synthetic-key', allow_network=True, concurrency=1)


def detector_for(settings, handler=None):
    detector = LLMDetector(settings)
    detector.client.close()
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return handler(payload, len(requests)) if handler else httpx.Response(200, json=oracle_response(payload))
    detector.client = httpx.Client(transport=httpx.MockTransport(respond))
    return detector, requests


def due(monitor):
    with store.db() as db:
        db.execute('UPDATE provider_state SET next_probe=0,expires=0,lease_until=0 WHERE provider_key=?', (monitor.key,))


def document_job():
    jid = store.reserve('.docx')
    d = Document()
    d.core_properties.author = ''
    d.core_properties.last_modified_by = ''
    d.add_paragraph('Name: Amelia Quill')
    d.add_paragraph('Email: private@example.test')
    d.save(store.path(jid)/'original'/'source.docx')
    store.update(jid, 'queued', 'waiting for worker')
    return jid


def test_readiness_is_cached_shared_and_scoped_to_configuration(service):
    detector, requests = detector_for(service)
    monitor = ProviderMonitor(service)
    try:
        assert monitor.ensure_ready(detector)
        assert len(requests) == 2
        assert public_status(service)['status'] == 'ready'
        assert detector.calls == 0  # Probes do not inflate document usage.
        second, other_requests = detector_for(service)
        try:
            assert ProviderMonitor(service).ensure_ready(second)
            assert not other_requests
            assert provider_key(service) != provider_key(replace(service, model='other/model'))
            assert provider_key(service) != provider_key(replace(service, api_key='different-key'))
            assert provider_key(service) != provider_key(replace(service, max_output_tokens=2048))
            with store.db() as db:
                stored = str([dict(r) for r in db.execute('SELECT * FROM provider_state')])
            assert service.api_key not in stored and 'Amelia' not in stored
        finally:
            second.close()
    finally:
        detector.close()


def test_expired_profile_rechecks_capabilities(service):
    state = {'reject': True}
    def respond(payload, number):
        if state['reject'] and 'response_format' in payload:
            return httpx.Response(500, json={'detail': 'response_format is not supported with speculative decoding'})
        return httpx.Response(200, json=oracle_response(payload))
    detector, requests = detector_for(service, respond)
    monitor = ProviderMonitor(service)
    try:
        assert monitor.ensure_ready(detector)
        assert detector.capabilities.response_format is False
        count = len(requests)
        assert monitor.ensure_ready(detector) and len(requests) == count
        state['reject'] = False
        due(monitor)
        assert monitor.ensure_ready(detector)
        assert len(requests) == count+2 and detector.capabilities.response_format is True
        assert 'response_format' in requests[-1]
    finally:
        detector.close()


def test_probe_lease_prevents_duplicate_checks_and_recovers_abandoned_owner(service):
    detector, requests = detector_for(service)
    monitor = ProviderMonitor(service)
    with store.db() as db:
        db.execute("INSERT INTO provider_state(provider_key,status,owner,lease_until) VALUES (?,'checking','other-worker',?)",
                   (monitor.key, time.time()+60))
    try:
        assert not monitor.ensure_ready(detector) and not requests
        due(monitor)
        assert monitor.ensure_ready(detector) and len(requests) == 2
    finally:
        detector.close()


@pytest.mark.parametrize('failure', ['batch_positive', 'batch_negative', 'standalone_positive'])
def test_preflight_rejects_missing_detection_or_false_positive(service, failure):
    def respond(payload, number):
        body = oracle_response(payload)
        result = json.loads(body['choices'][0]['message']['content'])
        if failure == 'batch_positive' and 'results' in result:
            result['results']['0'] = []
        elif failure == 'batch_negative' and 'results' in result:
            result['results']['1'] = [{'type': 'ACCOUNT_ID', 'text': '123.45'}]
        elif failure == 'standalone_positive' and 'entities' in result:
            result['entities'] = []
        body['choices'][0]['message']['content'] = json.dumps(result)
        return httpx.Response(200, json=body)
    detector, requests = detector_for(service, respond)
    try:
        assert not ProviderMonitor(service).ensure_ready(detector)
        assert public_status(service)['status'] == 'waiting'
        assert detector.calls == 0
    finally:
        detector.close()


def test_bad_model_probe_never_claims_or_sends_documents(service, monkeypatch):
    detector, requests = detector_for(service, lambda payload, n: httpx.Response(503, json={'detail': 'private-server-error'}))
    monitor = ProviderMonitor(service)
    jobs = [document_job() for _ in range(3)]
    engine = SimpleNamespace(detector=detector, process=lambda *a, **k: pytest.fail('Document processing must wait'))
    try:
        assert not process_next(engine, monitor)
        assert len(requests) == 1
        for jid in jobs:
            assert store.get(jid)['status'] == 'waiting_provider'
            assert store.get(jid)['provider_attempts'] == 0
            assert not list((store.path(jid)/'output').iterdir())
        assert 'private@example.test' not in json.dumps(requests)
        assert not process_next(engine, monitor) and len(requests) == 1
        with TestClient(app) as client:
            with store.db() as db:
                db.executemany('UPDATE jobs SET owner_id=? WHERE id=?',[(client.workspace_owner,jid) for jid in jobs])
            batch = client.get('/api/jobs/'+jobs[0]).json()
            assert batch['status'] == 'waiting_provider'
            assert client.get('/api/jobs/'+jobs[0]+'/report').status_code == 409
            assert 'private-server-error' not in json.dumps(client.get('/api/provider/status').json())
    finally:
        detector.close()


def test_outage_recovers_same_job_and_keeps_original(service, engine, monkeypatch):
    state = {'down': True}
    def respond(payload, number):
        return httpx.Response(503, json={'detail': 'unavailable'}) if state['down'] else httpx.Response(200, json=oracle_response(payload))
    detector, requests = detector_for(service, respond)
    monkeypatch.setattr(engine, 'detector', detector)
    monitor = ProviderMonitor(service)
    jid = document_job()
    original = (store.path(jid)/'original'/'source.docx').read_bytes()
    try:
        assert not process_next(engine, monitor)
        assert store.get(jid)['status'] == 'waiting_provider'
        state['down'] = False
        due(monitor)
        assert process_next(engine, monitor)
        assert store.get(jid)['status'] == 'completed'
        assert (store.path(jid)/'original'/'source.docx').read_bytes() == original
        assert 'private@example.test' not in (store.path(jid)/'output'/'sanitised.txt').read_text()
        assert public_status(service)['status'] == 'ready'
    finally:
        detector.close()


def test_mid_document_failure_discards_partial_output_and_resumes(service, engine, monkeypatch):
    state = {'fail_document': True}
    def respond(payload, number):
        if 'amelia.quill@example.test' not in json.dumps(payload) and state['fail_document']:
            return httpx.Response(503, json={'detail': 'interrupted inference'})
        return httpx.Response(200, json=oracle_response(payload))
    detector, requests = detector_for(service, respond)
    monkeypatch.setattr(engine, 'detector', detector)
    monitor = ProviderMonitor(service)
    jid = document_job()
    try:
        assert process_next(engine, monitor)
        assert store.get(jid)['status'] == 'waiting_provider'
        assert store.get(jid)['provider_attempts'] == 1
        assert not list((store.path(jid)/'output').iterdir())
        state['fail_document'] = False
        due(monitor)
        assert process_next(engine, monitor)
        assert store.get(jid)['status'] == 'completed'
        report = json.loads((store.path(jid)/'output'/'report.json').read_text())
        assert report['provider_attempts'] == 1
    finally:
        detector.close()


def test_repeated_invalid_document_responses_are_bounded(service, engine, monkeypatch):
    def respond(payload, number):
        if 'amelia.quill@example.test' in json.dumps(payload):
            return httpx.Response(200, json=oracle_response(payload))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'not JSON'}}]})
    detector, requests = detector_for(service, respond)
    monkeypatch.setattr(engine, 'detector', detector)
    monitor = ProviderMonitor(service)
    jid = document_job()
    try:
        for attempt in range(3):
            if attempt:due(monitor)
            assert process_next(engine, monitor)
        assert store.get(jid)['status'] == 'failed'
        assert store.get(jid)['provider_attempts'] == 3
        assert json.loads((store.path(jid)/'output'/'report.json').read_text())['provider_attempts'] == 3
        assert (store.path(jid)/'original'/'source.docx').exists()
        assert sorted(p.name for p in (store.path(jid)/'output').iterdir()) == ['report.json']
        assert 'automatic retries' in store.get(jid)['reason']
    finally:
        detector.close()


def test_waiting_jobs_have_bounded_wait_and_normal_retention(service, monkeypatch):
    batch = store.reserve_batch(1)
    jid = store.reserve('.docx', batch, 0)
    original = store.path(jid)/'original'/'source.docx'
    original.write_bytes(b'SYNTHETIC ORIGINAL')
    store.update(jid, 'queued', 'waiting')
    store.wait_for_provider('Model unavailable; retrying', 'unavailable', time.time()+5)
    assert store.get_batch(batch)['counts']['waiting_provider'] == 1
    assert not store.get_batch(batch)['export_ready']
    with store.db() as db:
        db.execute('UPDATE jobs SET provider_wait_started=? WHERE id=?', (time.time()-config.PROVIDER_WAIT_TIMEOUT-1, jid))
    store.recover_and_retain()
    assert store.get(jid)['status'] == 'failed' and original.exists()
    assert store.get_batch(batch)['export_ready']
    assert 'automatic retry window' in store.get(jid)['reason']
    monkeypatch.setattr(config, 'RETENTION', -1)
    store.recover_and_retain()
    assert not original.exists()


def test_waiting_jobs_count_toward_queue_capacity(service, monkeypatch):
    monkeypatch.setattr(config, 'MAX_QUEUE', 1)
    jid = store.reserve('.docx');store.update(jid, 'queued', 'waiting')
    store.wait_for_provider('Waiting for model', 'unavailable', time.time()+5)
    with pytest.raises(OverflowError):store.reserve('.docx')


def test_optional_parameters_are_negotiated_and_output_cap_is_respected(service):
    def respond(payload, number):
        if 'temperature' in payload:
            return httpx.Response(400, json={'error': 'temperature is not supported; only the default value is supported'})
        if 'max_tokens' in payload:
            return httpx.Response(400, json={'error': 'max_tokens is not supported; use max_completion_tokens'})
        assert payload['max_completion_tokens'] == 256
        return httpx.Response(200, json=oracle_response(payload))
    detector, requests = detector_for(replace(service, max_output_tokens=256), respond)
    try:
        assert detector.find('ordinary memo') == []
        assert len(requests) == 3
        assert detector.capabilities.temperature is False
        assert detector.capabilities.token_parameter == 'max_completion_tokens'
        assert detector.parameter_fallbacks == 2
    finally:
        detector.close()


def test_live_worker_pauses_batch_during_outage_and_resumes_without_upload(service, monkeypatch):
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    state = {'down': True}
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            status = 503 if state['down'] else 200
            data = json.dumps({'error': 'temporary outage'} if state['down'] else oracle_response(payload)).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers();self.wfile.write(data)
        def log_message(self, *args):pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}/v1'
    values = {'VEIL_DATA': str(config.DATA), 'VEIL_LLM_ENDPOINT': endpoint,
              'VEIL_LLM_MODEL': 'test/live-recovery', 'VEIL_LLM_ALLOW_NETWORK': 'true',
              'VEIL_LLM_API_KEY': 'synthetic-test-key', 'VEIL_WORKERS': '1',
              'VEIL_PROVIDER_RETRY_SECONDS': '2', 'VEIL_PROVIDER_WAIT_TIMEOUT': '60',
              'VEIL_LLM_TIMEOUT': '5', 'VEIL_TIMEOUT': '60'}
    for key, value in values.items():monkeypatch.setenv(key, value)
    worker = subprocess.Popen([sys.executable, '-m', 'veil.worker'], env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with TestClient(app) as client:
            batch = store.reserve_batch(3,client.workspace_owner)
            jobs = []
            digests = {}
            for index in range(3):
                jid = store.reserve('.docx', batch, index)
                d = Document();d.core_properties.author='';d.add_paragraph('Email: private@example.test')
                p = store.path(jid)/'original'/'source.docx';d.save(p)
                digests[jid] = hashlib.sha256(p.read_bytes()).digest()
                store.update(jid, 'queued', 'waiting');jobs.append(jid)
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                result = client.get('/api/batches/'+batch).json()
                if result['counts']['waiting_provider']==3:break
                time.sleep(.1)
            assert result['counts']['waiting_provider']==3 and not result['finished']
            assert client.get('/api/provider/status').json()['status']=='waiting'
            assert requests and all('amelia.quill@example.test' in json.dumps(p) for p in requests)
            assert all(client.get('/api/jobs/'+jid+'/download/sanitised.docx').status_code==409 for jid in jobs)
            state['down'] = False
            deadline = time.monotonic()+25
            while time.monotonic()<deadline:
                result = client.get('/api/batches/'+batch).json()
                if result['finished']==3:break
                time.sleep(.1)
            assert result['counts']['completed']==3 and result['export_ready']
            assert [item['id'] for item in result['items']]==jobs
            assert client.get('/api/provider/status').json()['status']=='ready'
            for jid in jobs:
                assert hashlib.sha256((store.path(jid)/'original'/'source.docx').read_bytes()).digest()==digests[jid]
                assert 'private@example.test' not in client.get('/api/jobs/'+jid+'/download/sanitised.txt').text
    finally:
        worker.terminate();worker.wait(5)
        out, err = worker.communicate()
        server.shutdown();server.server_close();thread.join(3)
        assert b'private@example.test' not in out+err
