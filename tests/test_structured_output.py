"""Offline transport contracts: no provider, credentials, or production writes.

Test dependency: python3 -m pip install -r tests/requirements-structured-output.txt
"""
from dataclasses import replace
from contextlib import nullcontext
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import agy_gemini_client as gemini, llm_client as llm
from src.autoslice import response_schemas as schemas
from src.autoslice import structured_output as output
from src.autoslice.argon_escalation_contract import (
    make_inner_request, parse_hearing_document, sha, structured_argon_request,
)
from src.autoslice import argon_escalation_storage as storage
from test_argon_escalation_witness import case as case, FakeProvider, finish
from tests.test_exact_final_funnel import terminal_rig  # noqa: F401


class Response(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())


def test_cpa_native_schema_and_cache_identity(monkeypatch):
    Draft202012Validator = pytest.importorskip('jsonschema').Draft202012Validator
    bodies = []
    monkeypatch.setenv('STRUCTURED_TEST_KEY', 'fictional')
    def post(request, timeout):
        bodies.append(json.loads(request.data))
        return Response({'output_text': '{"needs_audio":false,"reason":"ok","status":"CURRENT_SUPPORTED","proposed_cue":null,"topic_summary":"test","proposal_basis":null,"topic_support":null}'})
    monkeypatch.setattr(llm.urllib.request, 'urlopen', post)
    config = llm.LlmConfig(transport='direct', api_mode='responses', api_base='https://fictional.invalid/v1',
        api_key_env='STRUCTURED_TEST_KEY', model='offline-model')
    schema = schemas.proposal_schema()
    assert llm._cpa_cache_identity(config) == llm._cpa_cache_identity(replace(config, response_schema=schema))
    raw = output.call_json(llm.build_llm_call(config), 'same original prompt', response_schema=schema)
    fmt = bodies[0]['text']['format']
    assert fmt['type'] == 'json_schema' and fmt['strict'] is True
    assert set(fmt['schema']['required']) == set(fmt['schema']['properties'])
    Draft202012Validator(fmt['schema']).validate(json.loads(raw))
    assert bodies[0]['input'] == 'same original prompt'


def test_cpa_command_receives_format_without_prompt_change(tmp_path, monkeypatch):
    seen = {}
    def run(argv, **kwargs):
        seen.update(kwargs)
        seen['prompt'] = Path(argv[-2]).read_text()
        seen['format'] = json.loads(Path(argv[-2] + '.response-format.json').read_text())
        Path(argv[-1]).write_text('{}')
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(llm.subprocess, 'run', run)
    call = llm.build_llm_call(llm.LlmConfig(transport='command',
        command_template='mock-command {prompt_file} {completion_file}'))
    output.call_json(call, 'original', response_schema=schemas.proposal_schema())
    assert seen['format']['type'] == 'json_schema'
    assert seen['prompt'] == 'original'
    assert seen['env'] is None


@pytest.mark.parametrize('response_format', [{'type': 'json_object'}, None])
def test_cpa_shell_reads_request_sidecar_before_ambient_format(tmp_path, response_format):
    from test_llm_via_cpa import _run_bridge
    (tmp_path / 'prompt.txt.response-format.json').write_text(json.dumps(response_format))
    result, capture, _, _ = _run_bridge(tmp_path, prompt_text='Return JSON',
        env_overrides={'AUTOSLICE_RESPONSE_FORMAT': '{"type":"json_schema","name":"stale"}'})
    assert result.returncode == 0
    if response_format:
        assert capture['bodies'][0]['text']['format'] == response_format
    else:
        assert 'text' not in capture['bodies'][0]


def test_cpa_ssh_carries_sidecar_as_request_field(tmp_path, monkeypatch):
    from scripts import llm_via_runtime_cpa_ssh as bridge
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('original JSON prompt')
    fmt = output.openai_format(output.output_contract(schemas.proposal_schema()))
    Path(str(prompt) + '.response-format.json').write_text(json.dumps(fmt))
    def run(argv, **kwargs):
        request = json.loads(kwargs['input'])
        assert request['response_format'] == fmt
        assert request['prompt'] == 'original JSON prompt'
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {'status': 'PASS', 'completion': '{}', 'diagnostics': {}}), '')
    monkeypatch.setattr(bridge.subprocess, 'run', run)
    bridge.run_bridge(prompt_file=prompt, completion_file=tmp_path / 'completion.txt',
        ssh_host='offline', runtime_root=tmp_path, model='same-model', effort='medium', attempts=1)
    assert (tmp_path / 'completion.txt').read_text() == '{}'


def test_cpa_remote_request_rebuilds_sidecar_without_environment_mutation(tmp_path, monkeypatch):
    from scripts import cpa_request_body, llm_via_runtime_cpa_ssh as bridge
    from src.autoslice import provider_slots
    fmt = {'type': 'json_object'}
    child_env = {'CPA_BASE_URL': 'https://fictional.invalid/v1', 'CPA_API_KEY': 'fictional'}
    monkeypatch.setattr(llm, 'runtime_cpa_command_environment', lambda root: child_env)
    monkeypatch.setattr(provider_slots, 'runtime_provider_slot', lambda **kwargs: nullcontext())
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'runtime_root': str(tmp_path),
        'prompt': 'original JSON prompt', 'model': 'same-model', 'effort': 'medium',
        'attempts': 1, 'response_format': fmt})))
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    monkeypatch.setattr(sys, 'path', list(sys.path))
    seen = []
    def run(argv, **kwargs):
        prompt = Path(argv[2])
        body = cpa_request_body.build_body(prompt.read_text(), argv[4], argv[5],
            cpa_request_body.load_response_format(prompt))
        seen.append(body)
        assert kwargs['env'] is child_env
        assert 'AUTOSLICE_RESPONSE_FORMAT' not in child_env
        Path(argv[3]).write_text('{}')
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(bridge.subprocess, 'run', run)
    exec(compile(bridge.REMOTE_PROGRAM, '<offline-remote-bridge>', 'exec'), {})
    assert len(seen) == 1 and seen[0]['text']['format'] == fmt
    assert seen[0]['input'] == 'original JSON prompt'
    assert json.loads(sys.stdout.getvalue())['completion'] == '{}'


@pytest.mark.parametrize('with_schema', [False, True])
def test_cpa_shell_uses_shared_format(tmp_path, with_schema):
    from test_llm_via_cpa import _run_bridge
    fmt = output.openai_format(output.output_contract(schemas.proposal_schema())) if with_schema else {'type': 'json_object'}
    result, capture, _, _ = _run_bridge(tmp_path, env_overrides={'AUTOSLICE_RESPONSE_FORMAT': json.dumps(fmt)})
    assert result.returncode == 0
    if with_schema:
        assert capture['bodies'][0]['text']['format'] == fmt
    else:
        assert 'text' not in capture['bodies'][0]
        assert 'JSON_MODE_SKIPPED_NO_JSON_WORD' in result.stderr


def test_cpa_shell_explicit_json_object_with_prompt_word(tmp_path):
    from test_llm_via_cpa import _run_bridge
    result, capture, _, _ = _run_bridge(tmp_path, prompt_text='Return JsOn only',
        env_overrides={'AUTOSLICE_RESPONSE_FORMAT': '{"type":"json_object"}'})
    assert result.returncode == 0
    assert capture['bodies'][0]['text']['format'] == {'type': 'json_object'}


@pytest.mark.parametrize('schema', [None, schemas.assisted_audio_schema()])
def test_gemini_inline_native_format(monkeypatch, schema):
    bodies = []
    def post(request, timeout):
        bodies.append(json.loads(request.data))
        return Response({'candidates': [{'content': {'parts': [{'text': '{}'}]}}]})
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', post)
    raw = gemini.generate_content(prompt='original', key='fictional', model='gemini-3.8-flash',
        inline_data=b'original-audio', mime_type='audio/mpeg', response_schema=schema)
    assert raw == '{}'  # candidates[].content.parts[].text, not the API envelope.
    config = bodies[0]['generationConfig']
    assert config['responseMimeType'] == 'application/json'
    if schema:
        assert config['responseJsonSchema'] == schema
    assert bodies[0]['contents'][0]['parts'][0] == {'text': 'original'}
    assert bodies[0]['contents'][0]['parts'][1]['inline_data']['data']


def test_gemini_text_opt_out_and_context_does_not_leak():
    with output.response_contract(schemas.proposal_schema()):
        config = gemini.build_generation_config(model='gemini-3.8-flash', response_mime_type=None)
        assert 'responseMimeType' not in config and 'responseJsonSchema' not in config
    assert output.current_contract()['schema'] is None


def test_bound_gemini_files_keeps_original_json_mode():
    from src.autoslice.gemini_file_api import UrllibGeminiFileTransport
    bodies = []
    def post(request, timeout):
        bodies.append(json.loads(request.data))
        return Response({'candidates': []})
    UrllibGeminiFileTransport(urlopen=post).generate(key='fictional', model='gemini-3.8-flash',
        file_uri='https://generativelanguage.googleapis.com/v1beta/files/example', mime_type='video/mp4',
        prompt='original', video_fps=5.0, timeout_seconds=30)
    assert bodies[0]['generationConfig']['responseMimeType'] == 'application/json'
    assert 'responseJsonSchema' not in bodies[0]['generationConfig']


def test_argon_generate_batch_and_old_cache_remain_bound(case):
    engine, spec, handle, _ = case
    task, manifest, inner = engine._load(handle['task_id'], spec)
    assert inner == make_inner_request(handle['binding'],
        storage.get_blob(task, handle['binding']['input_audio_sha256']))
    assert manifest['inner_request_sha256'] == sha(inner)
    for mode in ('batch', 'realtime'):
        semantic = engine._body(handle['task_id'], inner, mode)
        wire = json.loads(structured_argon_request(semantic))
        if mode == 'batch':
            wire = wire['batch']['inputConfig']['requests']['requests'][0]['request']
        assert wire['generationConfig']['responseMimeType'] == 'application/json'
        assert wire['generationConfig']['responseJsonSchema'] == schemas.hearing_schema()
        assert wire['contents'] == json.loads(inner)['contents']
    fake = FakeProvider(handle)
    receipt = finish(case, fake)
    calls = len(fake.calls)
    assert finish(case, fake) == receipt
    assert len(fake.calls) == calls  # Frozen legacy manifest remains a zero-call cache hit.
    starts = [json.loads(p.read_text()) for p in (task / 'calls').glob('*.start.json')]
    assert any(r.get('wire_request_sha256') and r.get('structured_output') for r in starts)


def test_hearing_schema_consistent_but_cross_field_rejected(case):
    Draft202012Validator = pytest.importorskip('jsonschema').Draft202012Validator
    _, _, handle, _ = case
    raw = FakeProvider(handle).payload['candidates'][0]['content']['parts'][0]['text']
    heard = json.loads(raw)
    schema = schemas.hearing_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(heard)
    assert parse_hearing_document(heard, handle['binding']) == heard
    invalid = {**heard, 'status': 'UNCERTAIN', 'reason': None}
    Draft202012Validator(schema).validate(invalid)
    with pytest.raises(ValueError, match='HEARING_STATUS_CONTRADICTION'):
        parse_hearing_document(invalid, handle['binding'])
    for key in schema['required']:
        missing = {k: v for k, v in heard.items() if k != key}
        assert not Draft202012Validator(schema).is_valid(missing)
        with pytest.raises(ValueError):
            parse_hearing_document(missing, handle['binding'])


def assistance():
    from src.autoslice import context_assisted_audio as assisted
    value = {'schema_version': assisted.ASSISTANCE_SCHEMA, 'observation_class': assisted.OBSERVATION_CLASS,
        'independent_witness': False, 'mutation_authorized': False, 'prior_blind_observation_ids': ['a' * 64],
        'question': 'which word?', 'context_summary': '', 'proper_names': [], 'hypotheses': [],
        'sources': {'cue:1': {'source_kind': 'CURRENT_SUBTITLE_CONTEXT_NOT_TRUTH', 'text': 'context',
            'text_sha256': assisted.text_sha('context')}}}
    value['assistance_sha256'] = assisted.digest(value)
    return value


def assisted_answer():
    return {'schema_version': 'context-assisted-audio-response.v1', 'status': 'OBSERVED',
        'target_audible': True, 'heard': 'heard', 'full_transcript': 'heard', 'inferred': [], 'uncertain': [],
        'segments': [{'segment': 'complete_block', 'start_ms': 0, 'end_ms': 1000, 'transcript': 'heard'}],
        'audio_observations': []}


def test_assisted_schema_preserves_cross_field_and_source_checks():
    Draft202012Validator = pytest.importorskip('jsonschema').Draft202012Validator
    from src.autoslice.context_assisted_audio import validate_response
    answer = assisted_answer()
    schema = schemas.assisted_audio_schema()
    Draft202012Validator(schema).validate(answer)
    assert validate_response(answer, assistance())['independent_witness'] is False
    answer['inferred'] = [{'text': 'not heard', 'source_refs': ['invented-source']}]
    Draft202012Validator(schema).validate(answer)
    with pytest.raises(ValueError, match='ASSISTED_AUDIO_INFERENCE_SOURCE_INVALID'):
        validate_response(answer, assistance())
    answer['inferred'] = []
    answer['segments'][0]['transcript'] = 'inferred words'
    with pytest.raises(ValueError, match='ASSISTED_AUDIO_INFERENCE_IN_TRANSCRIPT'):
        validate_response(answer, assistance())


def test_agy_schema_flags_and_native_stream(tmp_path):
    schema = schemas.assisted_audio_schema()
    argv = gemini.agy_argv('fictional-agy', job_dir=tmp_path, model='same-model',
        prompt='original', print_timeout='30s', response_schema=schema)
    assert argv[argv.index('--output-format') + 1] == 'stream-json'
    assert json.loads(Path(argv[argv.index('--json-schema') + 1]).read_text()) == schema
    raw = json.dumps({'type': 'result', 'result': {'status': 'SUCCESS', 'structured_output': assisted_answer()}})
    assert json.loads(output.parse_agy_stream(raw)) == assisted_answer()
    for invalid in (raw + '\n' + raw, '{"type":"error"}', '{"type":"result","result":{"status":"ERROR"}}'):
        with pytest.raises(ValueError):
            output.parse_agy_stream(invalid)


@pytest.mark.parametrize('succeeds', [True, False])
def test_agy_one_format_retry_same_model_and_one_observation(tmp_path, succeeds):
    calls = []
    argv = gemini.agy_argv('fictional-agy', job_dir=tmp_path, model='same-model',
        prompt='same audio and prompt', print_timeout='30s', json_mode=True)
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '{}', '')
    def validate(_completed):
        if not succeeds or len(calls) == 1:
            raise ValueError('ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID')
    result = gemini.run_local_agy(argv, cwd=tmp_path, timeout=30, command_runner=run, response_validator=validate)
    assert result.format_rejected is (not succeeds)
    assert len(calls) == 2
    assert calls[0][calls[0].index('--model') + 1] == calls[1][calls[1].index('--model') + 1]
    assert calls[0] == calls[1]
    ledger = json.loads((tmp_path / 'agy.format-attempts.json').read_text())
    assert ledger['physical_calls'] == 2 and ledger['observation_count'] == 1


@pytest.mark.parametrize('succeeds', [True, False])
def test_cpa_format_retry_inside_existing_attempt_ledger(tmp_path, monkeypatch, succeeds):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setenv('AUTOSLICE_CPA_USAGE_FILE', str(tmp_path / 'usage.jsonl'))
    monkeypatch.setenv('STRUCTURED_TEST_KEY', 'fictional')
    bodies = []
    def post(request, timeout):
        bodies.append(json.loads(request.data))
        raw = '{"ok":true}' if succeeds and len(bodies) == 2 else 'invalid'
        return Response({'output_text': raw})
    monkeypatch.setattr(llm.urllib.request, 'urlopen', post)
    config = llm.LlmConfig(transport='direct', api_mode='responses', api_base='https://fictional.invalid/v1',
        api_key_env='STRUCTURED_TEST_KEY', model='same-model')
    call = llm.build_llm_call(config)
    recorder = SemanticAttemptRecorder(srt_text='1\n00:00:01,000 --> 00:00:02,000\nheard\n',
        finding={'cue_index': 1, 'why': 'question'}, clip_context={'source_media_sha256': 'a' * 64}, offset_ms=0)
    # A complete ruling is retained before parsing, even if another roll of
    # the same question would return valid JSON. Reuse revalidates the original.
    for _ in range(2):
        with pytest.raises(llm.LlmJsonParseError):
            output.call_json(recorder.wrap(call), 'original prompt', validate=llm.extract_json_object)
    assert len(bodies) == 1 and bodies[0]['input'] == 'original prompt'
    from src.autoslice.cpa_runtime import cpa_attempt_count
    assert cpa_attempt_count(tmp_path / 'usage.jsonl') == 1
    entries = list((tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json'))
    assert len(entries) == 1 and json.loads(entries[0].read_text())['status'] == 'COMPLETED'
    assert output.call_json(recorder.wrap(call), 'original prompt') == 'invalid'
    assert len(bodies) == 1  # Replay adds neither inference nor another observation.


def test_network_failure_is_never_a_format_retry():
    calls = []
    def fail(prompt):
        calls.append(prompt)
        raise OSError('offline')
    with pytest.raises(OSError):
        output.format_retry_call('original', fail, validate=json.loads)
    assert len(calls) == 1


def test_hidden_cpa_wrapper_does_not_multiply_retries(monkeypatch):
    monkeypatch.setenv('STRUCTURED_TEST_KEY', 'fictional')
    bodies = []
    def post(request, timeout):
        bodies.append(json.loads(request.data))
        return Response({'output_text': 'invalid'})
    monkeypatch.setattr(llm.urllib.request, 'urlopen', post)
    config = llm.LlmConfig(transport='direct', api_mode='responses', api_base='https://fictional.invalid/v1',
        api_key_env='STRUCTURED_TEST_KEY', model='same-model')
    transport = llm.build_llm_call(config)
    def opaque_wrapper(prompt):
        return transport(prompt)
    with pytest.raises(llm.LlmJsonParseError):
        llm.call_and_extract_json_with_parse_retry('original', llm_call=opaque_wrapper,
            extract_json=llm.extract_json_object)
    assert len(bodies) == 2
    assert bodies[0]['input'] == bodies[1]['input'] == 'original'


def test_parse_retry_transport_does_not_retry_schema_failure(monkeypatch):
    monkeypatch.setenv('STRUCTURED_TEST_KEY', 'fictional')
    bodies = []
    def post(request, timeout):
        bodies.append(json.loads(request.data))
        return Response({'output_text': '{}'})
    monkeypatch.setattr(llm.urllib.request, 'urlopen', post)
    call = llm.build_llm_call(llm.LlmConfig(transport='direct', api_mode='responses',
        api_base='https://fictional.invalid/v1', api_key_env='STRUCTURED_TEST_KEY', model='same-model'))
    def extract(_raw):
        raise ValueError('SCHEMA_INVALID')
    with pytest.raises(ValueError, match='SCHEMA_INVALID'):
        llm.call_and_extract_json_with_parse_retry('original', llm_call=call, extract_json=extract)
    assert len(bodies) == 1


def test_opaque_command_does_not_enable_outer_format_retry(monkeypatch):
    calls = []
    def command(prompt, config):
        calls.append(prompt)
        return 'invalid'
    monkeypatch.setattr(llm, '_call_command', command)
    call = llm.build_llm_call(llm.LlmConfig(transport='command', command_template='unknown-command'))
    with pytest.raises(llm.LlmJsonParseError):
        llm.call_and_extract_json_with_parse_retry('original', llm_call=lambda prompt: call(prompt),
            extract_json=llm.extract_json_object)
    assert len(calls) == 1


def test_gemini_retry_keeps_key_audio_and_one_observation(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_CPA_USAGE_FILE', str(tmp_path / 'usage.jsonl'))
    requests = []
    def post(request, timeout):
        requests.append(request)
        raw = 'invalid' if len(requests) == 1 else '{}'
        return Response({'candidates': [{'content': {'parts': [{'text': raw}]}}]})
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', post)
    gemini.generate_content(prompt='original', key='fictional', model='gemini-3.8-flash',
        inline_data=b'same-audio', mime_type='audio/wav', response_validator=llm.extract_json_object)
    assert len(requests) == 2 and requests[0].headers == requests[1].headers
    first, second = [json.loads(request.data) for request in requests]
    assert first['contents'][0]['parts'][1:] == second['contents'][0]['parts'][1:]
    assert second['contents'] == first['contents']
    rows = [json.loads(line) for line in (tmp_path / 'usage.jsonl').read_text().splitlines()]
    assert len([r for r in rows if r['event_type'] == 'audio_http_attempt']) == 2
    assert [r['attempt'] for r in rows if r['event_type'] == 'audio_format_attempt'] == [1, 2]
    assert len({r['observation_input_sha256'] for r in rows}) == 1


def test_exhausted_gemini_format_does_not_rotate_key_or_model(monkeypatch):
    monkeypatch.setattr(gemini, 'free_api_keys', lambda: ['fictional-one', 'fictional-two'])
    def no_paid(*args, **kwargs):
        raise AssertionError('format failure cannot open paid admission')
    monkeypatch.setattr(gemini.gemini_backup_policy, 'paid_attempt_allowed', no_paid)
    calls = []
    failures = []
    def post(request, timeout):
        calls.append(request)
        return Response({'candidates': [{'content': {'parts': [{'text': 'invalid'}]}}]})
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', post)
    def observe(key):
        return gemini.generate_content(prompt='original', key=key, model='gemini-3.8-flash',
            inline_data=b'audio', mime_type='audio/wav', response_validator=llm.extract_json_object)
    result = gemini.run_gemini_key_ladder(item_key='same-observation', purpose='offline',
        observe=observe, record_failure=failures.append)
    assert not result.accepted and len(calls) == 2 and len(failures) == 1
    assert failures[0].key_ordinal == 1 and isinstance(failures[0].exception, output.FormatRetryExhausted)
    assert failures[0].exception.completion == 'invalid'


@pytest.mark.parametrize('raw', ['invalid', '{"type":"result","result":{"status":"ERROR","structured_output":null}}'])
def test_assisted_agy_exhaustion_does_not_fall_through_to_gemini(tmp_path, monkeypatch, raw):
    from src.autoslice import entity_audio_verifier as audio
    monkeypatch.setattr(audio, '_entity_observation_prompt', lambda **kwargs: ('original', False, True))
    monkeypatch.setattr(audio, '_run_gemini_web_if_enabled', lambda *args, **kwargs: None)
    monkeypatch.setattr(gemini, 'run_local_agy', lambda *args, **kwargs: gemini.AgyRun(
        completed=subprocess.CompletedProcess(['fictional-agy'], 0, raw, ''),
        failure_category=None, format_rejected=True))
    def no_api(**kwargs):
        raise AssertionError('another model cannot repair exhausted AGY format')
    monkeypatch.setattr(audio, '_run_gemini_api_fallback', no_api)
    path = tmp_path / 'input.mp4'
    path.write_bytes(b'audio')
    result = audio._observe_entity_audio(request={'schema_version': 'context-assisted-audio-request.v1',
        'assistance': assistance(), 'target_audio_start_ms': 0, 'target_audio_end_ms': 1000},
        candidates=[], audio_path=path, job_dir=tmp_path, recording_date='2026-10-22', timely_context='',
        binary='fictional-agy', model='same-model', timeout='30s', agy_quota_circuit=audio._AgyQuotaCircuitBreaker())
    assert result.provider == 'agy'
    if result.observed is not None:
        Draft202012Validator = pytest.importorskip('jsonschema').Draft202012Validator
        assert not Draft202012Validator(schemas.assisted_audio_schema()).is_valid(result.observed)


def test_agy_second_attempt_timeout_still_records_both_physical_calls(tmp_path):
    argv = gemini.agy_argv('fictional-agy', job_dir=tmp_path, model='same-model',
        prompt='original', print_timeout='30s', json_mode=True)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 0, 'invalid', '')
    def invalid(completed):
        raise ValueError('ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID')
    result = gemini.run_local_agy(argv, cwd=tmp_path, timeout=30, command_runner=run, response_validator=invalid)
    assert result.completed is None and len(calls) == 2
    receipt = json.loads((tmp_path / 'agy.format-attempts.json').read_text())
    assert receipt['physical_calls'] == 2 and receipt['observation_count'] == 1
    assert receipt['attempts'][-1]['status'] == 'OUTCOME_UNKNOWN'


def test_cpa_visual_json_and_text_keep_same_pixel_identity(tmp_path, monkeypatch):
    from src.autoslice.cpa_frame_witness import batch_jpeg_vision_probe
    path = tmp_path / 'frame.jpg'
    path.write_bytes(b'original-jpeg')
    bodies = []
    def post(request, timeout):
        bodies.append(json.loads(request.data))
        return Response({'output_text': '{}'})
    monkeypatch.setattr(llm.urllib.request, 'urlopen', post)
    kwargs = dict(api_base='https://fictional.invalid/v1', api_key='fictional')
    first = batch_jpeg_vision_probe([path], 'Return JSON', **kwargs)
    second = batch_jpeg_vision_probe([path], 'Return JSON', json_mode=False, **kwargs)
    assert bodies[0]['text']['format'] == {'type': 'json_object'}
    assert 'text' not in bodies[1]
    assert first['prompt_sha256'] == second['prompt_sha256']


def test_schema_projections_are_valid():
    Draft202012Validator = pytest.importorskip('jsonschema').Draft202012Validator
    from src.autoslice.subtitle_acceptability_policy import load_policy
    closed = schemas.closed_set_schema({'CURRENT', 'PROPOSED', 'NEITHER'})
    terminal = schemas.terminal_schema(load_policy())
    for schema in (closed, terminal, schemas.proposal_schema(), schemas.proposal_schema(convergence=True)):
        Draft202012Validator.check_schema(schema)
        Draft202012Validator.check_schema(output.strict_openai_schema(schema))


def test_schema_projections_preserve_conditional_fields():
    from src.autoslice.subtitle_acceptability_policy import load_policy
    closed = schemas.closed_set_schema({'CURRENT', 'PROPOSED', 'NEITHER'})
    terminal = schemas.terminal_schema(load_policy())
    assert {'span_review', 'registered_name_decisions', 'candidate_id'} <= set(closed['properties'])
    assert 'needs_audio' not in terminal['properties']
    assert set(terminal['properties']['choice']['enum']) == set(load_policy()['terminal_choices'])
    assert {'event_index', 'screen_index', 'meaning_equivalence'} <= set(terminal['properties'])


def test_terminal_live_consumer_still_rejects_cross_field(monkeypatch, terminal_rig):
    Draft202012Validator = pytest.importorskip('jsonschema').Draft202012Validator
    from test_exact_final_funnel import run_terminal, unresolved, answer
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    from src.autoslice.subtitle_acceptability_policy import load_policy
    good = answer()
    Draft202012Validator(schemas.terminal_schema(load_policy())).validate(good)
    assert run_terminal([unresolved()], lambda prompt: json.dumps(good),
        terminal_rig=terminal_rig)['release_gate'] == 'PASS'
    bad = {**good, 'grade': 'B', 'evidence_decisive': False}
    Draft202012Validator(schemas.terminal_schema(load_policy())).validate(bad)
    result = run_terminal([unresolved()], lambda prompt: json.dumps(bad), terminal_rig=terminal_rig)
    assert result['release_gate'] == 'BLOCK'
