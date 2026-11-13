"""Coverage, response validation and backpressure for provider-independent batching."""
import json
import multiprocessing
from contextlib import contextmanager

import httpx
import pytest
from docx import Document
from lxml import etree as E

from veil import config
from veil.llm import LLMDetector, LLMError, LLMSettings
from veil.llm_queue import provider_slot
from veil.office_structure import W, A, structural_value, standard_package_path
from veil.engine import package


@contextmanager
def provider(tmp_path, monkeypatch, handler=None, payloads=None, **settings):
    monkeypatch.setattr(config, 'DATA', tmp_path / 'data')
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        if payloads is not None:
            payloads.append(payload)
        content = json.loads(payload['messages'][1]['content'])
        requests.append(content)
        if handler:
            return handler(content, len(requests))
        segments = content.get('segments')
        def entities(text):
            return [{'type': kind, 'text': value} for kind, value in (
                ('PERSON', 'Amelia Quill'), ('EMAIL_ADDRESS', 'private@example.test')) if value in text]
        if segments is None:
            result = {'entities': entities(content['text'])}
        else:
            result = {'results': {str(s['id']): entities(s['text']) for s in segments}}
        return completion(result)

    detector = LLMDetector(LLMSettings(endpoint='http://localhost/v1', model='test/model',
                                     allow_network=True, concurrency=1, **settings))
    detector.client.close()
    detector.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        yield detector, requests
    finally:
        detector.close()


def completion(result):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps(result)}}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})


@pytest.mark.parametrize('ns,tag,attribute,value,expected', [
    (W, 'sz', 'val', '24', True), (W, 'color', 'val', '12AbEF', True),
    (W, 'name', 'val', 'Heading 1', True), (W, 'style', 'styleId', 'Heading1Char', True),
    (W, 'name', 'val', 'Amelia Quill', False), (W, 'sz', 'val', 'private@example.test', False),
    (W, 'color', 'val', 'private@example.test', False),
    ('urn:custom', 'sz', 'val', '81234567', False),
    (W, 'author', 'val', '24', False), (W, 'rFonts', 'ascii', 'Amelia Quill', False),
    (W, 'ins', 'author', 'Normal', False), (W, 'ins', 'author', '81234567', False),
    (A, 'font', 'typeface', 'Amelia Quill', False),
])
def test_structure_checks_are_field_and_value_specific(ns, tag, attribute, value, expected):
    node = E.Element('{'+ns+'}'+tag)
    if ns == W and tag == 'name':
        E.Element('{'+W+'}style').append(node)
    key = '{'+ns+'}'+attribute if ns == W else attribute
    node.set(key, value)
    assert structural_value(node, key) is expected
    node.text = value
    assert not structural_value(node)


def test_standard_paths_do_not_admit_arbitrary_names():
    assert standard_package_path('word/styles.xml')
    assert standard_package_path('word/header2.xml')
    assert not standard_package_path('word/Amelia Quill.xml')
    assert not standard_package_path('word/media/private@example.test.jpg')
    assert not standard_package_path('word/../styles.xml')


def test_tiny_docx_needs_at_most_three_requests_and_redacts(engine, tmp_path, monkeypatch):
    source = tmp_path / 'in.docx'
    d = Document()
    d.core_properties.author = ''
    d.core_properties.last_modified_by = ''
    d.add_paragraph('Name: Amelia Quill')
    d.add_paragraph('Email: private@example.test')
    d.save(source)
    with provider(tmp_path, monkeypatch) as (detector, requests):
        monkeypatch.setattr(engine, 'detector', detector)
        result = engine.process(source, tmp_path / 'out')
        assert result['status'] == 'completed', result.get('reason')
        assert 1 <= len(requests) <= 3
        assert result['inspection']['locally_validated_structure_values'] > 500
        assert result['entity_counts'] == {'PERSON': 1, 'EMAIL_ADDRESS': 1}
        text = (tmp_path / 'out' / 'sanitised.txt').read_text()
        assert 'Amelia Quill' not in text and 'private@example.test' not in text
        assert 'Name:' in text and 'Email:' in text


def test_budget_batches_more_than_32_values_with_complete_coverage(tmp_path, monkeypatch):
    values = [(f'item-{i}', True, None) for i in range(120)]
    with provider(tmp_path, monkeypatch, input_token_budget=8192) as (detector, requests):
        detector.prefetch(values)
        assert max(len(r['segments']) for r in requests) > 32
        assert set(detector.cache) == set(values)
        assert all(detector._input_estimate(segments=r['segments']) <= 8192 for r in requests)
        before = len(requests)
        for value in values:
            assert detector.find(value[0], structured=value[1], context=value[2]) == []
        assert len(requests) == before


@pytest.mark.parametrize('suffix', ['docx', 'xlsx', 'pptx'])
def test_office_llm_path_preserves_hidden_metadata_comments_and_revision_coverage(engine, dataset, tmp_path, monkeypatch, suffix):
    from conftest import oracle_entities

    def oracle(content, number):
        segments = content.get('segments')
        def entities(segment):
            text = segment['text']
            return oracle_entities(text, segment.get('structured', False), segment.get('context'))
        if segments is None:
            result = {'entities': entities(content)}
        else:
            result = {'results': {str(s['id']): entities(s) for s in segments}}
        return completion(result)

    with provider(tmp_path, monkeypatch, handler=oracle) as (detector, requests):
        monkeypatch.setattr(engine, 'detector', detector)
        result = engine.process(dataset / 'development' / ('synthetic-00.'+suffix), tmp_path / 'out')
        assert result['status'] == 'completed', result.get('reason')
        assert requests
        parts = package(tmp_path / 'out' / ('sanitised.'+suffix))
        data = b' '.join(parts.values())
        for value in [b'Amelia Quill', b'person00@example.test', b'synthetic-author@example.test',
                      b'notes@example.test', b'hidden@example.test', b'orphan@example.test',
                      b'deleted@example.test', b'formula@example.test', b'comment@example.test']:
            assert value not in data


def test_multiple_slots_allow_configured_parallelism(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path)
    with provider_slot('http://localhost/v1', 2, 0.02):
        with provider_slot('http://localhost/v1', 2, 0.02):
            with pytest.raises(TimeoutError):
                with provider_slot('http://localhost/v1', 2, 0.02):
                    pytest.fail('Both endpoint slots are occupied')


def test_long_values_are_chunked_with_overlap_and_exact_offsets(tmp_path, monkeypatch):
    text = '甲'*1200 + 'private@example.test' + '乙'*1200
    with provider(tmp_path, monkeypatch) as (detector, requests):
        detector.prefetch([(text, False, None)])
        assert not requests  # Oversized segments remain uninspected until find().
        hits = detector.find(text)
        assert len(hits) == 1 and text[hits[0].start:hits[0].end] == 'private@example.test'
        assert len(requests) > 1
        assert all(detector._input_estimate(r['text'], r['structured'], r['context']) <= 4096 for r in requests)


@pytest.mark.parametrize('result', [
    {'results': {'0': []}}, {'results': {'0': [], '2': []}},
    {'results': {'0': [], '1': [], '2': []}}, {'results': []},
    {'results': {'0': [], '1': 'none'}},
    {'results': {'0': [{'type': 'PERSON', 'text': 'Invented'}], '1': []}},
    {'results': {'0': [], '1': []}, 'extra': True},
])
def test_batch_results_reject_missing_unknown_or_unaligned_data(result):
    with pytest.raises(ValueError):
        LLMDetector._batch_hits(result, [{'id': 0, 'text': 'Amelia Quill'}, {'id': 1, 'text': 'memo'}])


def test_duplicate_json_ids_block_output(tmp_path, monkeypatch):
    def duplicate(content, number):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"results":{"0":[],"0":[]}}'}}]})
    with provider(tmp_path, monkeypatch, handler=duplicate) as (detector, requests):
        with pytest.raises(LLMError):
            detector.prefetch([('ordinary memo', False, None)])


def test_quote_mismatch_rechecks_only_affected_segment(tmp_path, monkeypatch):
    def mixed_batch(content, number):
        if 'segments' in content:
            return completion({'results': {'0': [{'type': 'PERSON', 'text': 'Invented Name'}],
                                           '1': [{'type': 'EMAIL_ADDRESS', 'text': 'private@example.test'}]}})
        assert content['text'] == 'Name: Amelia Quill'
        return completion({'entities': [{'type': 'PERSON', 'text': 'Amelia Quill'}]})
    with provider(tmp_path, monkeypatch, handler=mixed_batch) as (detector, requests):
        detector.prefetch([('Name: Amelia Quill', False, None), ('private@example.test', False, None)])
        assert len(requests) == 2 and detector.alignment_retries == 1
        assert detector.find('Name: Amelia Quill')[0].kind == 'PERSON'
        assert detector.find('private@example.test')[0].kind == 'EMAIL_ADDRESS'


def test_failed_isolated_quote_check_still_blocks_and_does_not_cache(tmp_path, monkeypatch):
    def unaligned(content, number):
        entities = [{'type': 'PERSON', 'text': 'Invented Name'}]
        return completion({'results': {'0': entities}} if 'segments' in content else {'entities': entities})
    with provider(tmp_path, monkeypatch, handler=unaligned) as (detector, requests):
        with pytest.raises(LLMError, match='does not exactly match'):
            detector.prefetch([('Name: Amelia Quill', False, None)])
        assert len(requests) == 2 and not detector.cache


def test_isolated_recheck_keeps_already_verified_detections(tmp_path, monkeypatch):
    def mixed_answer(content, number):
        if 'segments' in content:
            return completion({'results': {'0': [{'type': 'PERSON', 'text': 'Amelia Quill'},
                                                 {'type': 'PERSON', 'text': 'Invented Name'}]}})
        return completion({'entities': []})
    with provider(tmp_path, monkeypatch, handler=mixed_answer) as (detector, requests):
        detector.prefetch([('Name: Amelia Quill', False, None)])
        assert detector.find('Name: Amelia Quill')[0].kind == 'PERSON'
        assert len(requests) == 2


def test_isolated_rechecks_are_bounded(tmp_path, monkeypatch):
    def unaligned_batch(content, number):
        if 'segments' in content:
            return completion({'results': {str(s['id']): [{'type': 'PERSON', 'text': 'Invented Name'}]
                                           for s in content['segments']}})
        return completion({'entities': []})
    with provider(tmp_path, monkeypatch, handler=unaligned_batch) as (detector, requests):
        with pytest.raises(LLMError, match='repeatedly'):
            detector.prefetch([(f'ordinary memo {i}', False, None) for i in range(9)])
        assert detector.alignment_retries == 8 and len(requests) == 9 and not detector.cache


def test_transient_429_retries_without_exposing_response_body(tmp_path, monkeypatch):
    def busy_then_ready(content, number):
        if number == 1:
            return httpx.Response(429, headers={'Retry-After': '0'}, json={'error': 'private-provider-detail'})
        return completion({'entities': []})
    with provider(tmp_path, monkeypatch, handler=busy_then_ready) as (detector, requests):
        assert detector.find('ordinary memo') == []
        assert len(requests) == 2 and detector.rate_limit_retries == 1


@pytest.mark.parametrize('status', [400, 422, 500])
def test_explicit_unsupported_format_retries_without_option_and_remembers_capability(tmp_path, monkeypatch, status):
    payloads = []
    def incompatible(content, number):
        if number == 1:
            return httpx.Response(status, json={'detail': 'Structured response_format is not supported with speculative decoding.'})
        return completion({'entities': []})
    with provider(tmp_path, monkeypatch, handler=incompatible, payloads=payloads) as (detector, requests):
        assert detector.find('ordinary memo') == []
        assert len(requests) == 2
        assert 'response_format' in payloads[0] and 'response_format' not in payloads[1]
        assert payloads[0]['messages'] == payloads[1]['messages']
        assert detector.describe()['response_format'] == 'prompt_json'
        assert detector.response_format_fallbacks == 1
        detector.reset()
        assert detector.find('another memo') == []
        assert 'response_format' not in payloads[-1]
        assert detector.calls == 1 and detector.response_format_fallbacks == 0


def test_unrelated_server_errors_do_not_disable_response_format(tmp_path, monkeypatch):
    def broken(content, number):
        return httpx.Response(500, json={'detail': 'Private provider diagnostic; inference unavailable'})
    with provider(tmp_path, monkeypatch, handler=broken) as (detector, requests):
        with pytest.raises(LLMError) as error:
            detector.find('ordinary memo')
        assert len(requests) == 1 and detector.response_format_fallbacks == 0
        assert 'Private provider' not in str(error.value)


@pytest.mark.parametrize('content', ['not JSON', '{"results":{"0":[]}}'])
def test_prompt_only_fallback_still_rejects_malformed_or_incomplete_batch(tmp_path, monkeypatch, content):
    def incompatible_then_invalid(user, number):
        if number == 1:
            return httpx.Response(500, json={'detail': 'response_format is not supported'})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
    with provider(tmp_path, monkeypatch, handler=incompatible_then_invalid) as (detector, requests):
        with pytest.raises(LLMError):
            detector.prefetch([('first memo', False, None), ('second memo', False, None)])
        assert len(requests) == 2 and not detector.cache


def test_prompt_only_json_fence_is_unwrapped_and_source_quotes_validated(tmp_path, monkeypatch):
    def fenced(content, number):
        if number == 1:
            return httpx.Response(500, json={'detail': 'response_format is not supported with speculative decoding'})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': '```json\n{"entities":[{"type":"PERSON","text":"Amelia Quill"}]}\n```'}}]})
    with provider(tmp_path, monkeypatch, handler=fenced) as (detector, requests):
        hits = detector.find('Name: Amelia Quill')
        assert len(hits) == 1 and hits[0].kind == 'PERSON'
        assert detector.json_fence_unwraps == 1


@pytest.mark.parametrize('content', [
    'Explanation\n```json\n{"entities":[]}\n```',
    '```json\n{"entities":[]}\n```\nExplanation',
    '```json\nnot valid JSON\n```',
    '```json\n{"entities":[],"entities":[]}\n```',
    '```json\n{"entities":[{"type":"PERSON","text":"Invented Name"}]}\n```',
    '```json5\n{"entities":[]}\n```',
])
def test_fence_support_does_not_accept_prose_invalid_json_or_invented_quotes(tmp_path, monkeypatch, content):
    def fenced(user, number):
        if number == 1:
            return httpx.Response(500, json={'detail': 'response_format unsupported'})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
    with provider(tmp_path, monkeypatch, handler=fenced) as (detector, requests):
        with pytest.raises(LLMError):
            detector.find('ordinary memo')
        assert not detector.cache


def test_429_retry_limit_and_nonretryable_errors(tmp_path, monkeypatch):
    for status, attempts in [(429, 5), (401, 1)]:
        def error(content, number):
            return httpx.Response(status, headers={'Retry-After': '0'}, json={'secret': 'private-provider-detail'})
        with provider(tmp_path, monkeypatch, handler=error) as (detector, requests):
            with pytest.raises(LLMError) as exc:
                detector.find('ordinary memo')
            assert len(requests) == attempts
            assert 'private-provider-detail' not in str(exc.value)


def _hold_provider_slot(ready, release):
    with provider_slot('http://localhost/v1/chat/completions', 1, 1):
        ready.set()
        release.wait(5)


def test_slots_coordinate_independent_processes_and_release_on_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path)
    ctx = multiprocessing.get_context('fork')
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_provider_slot, args=(ready, release))
    process.start()
    try:
        assert ready.wait(3)
        with pytest.raises(TimeoutError):
            with provider_slot('http://localhost/v1/chat/completions', 1, 0.02):
                pytest.fail('Another process already holds the only slot')
        with provider_slot('http://another-provider/v1/chat/completions', 1, 0.02):
            pass
    finally:
        release.set()
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join()
    assert process.exitcode == 0
    with provider_slot('http://localhost/v1/chat/completions', 1, 0.02):
        pass
