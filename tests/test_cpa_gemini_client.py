"""Offline gateway contract; no real credential, provider or socket is used."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
from importlib.machinery import SourceFileLoader
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import urllib.error

import pytest

from src.autoslice import agy_gemini_client as legacy
from src.autoslice import cpa_gemini_client as cpa


class Response(io.BytesIO):
    def __init__(self, text='accepted', trace='1729850000-0123456789abcdef-request'):
        super().__init__(json.dumps({'candidates': [{'content': {'parts': [{'text': text}]}}],
            'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 3}}).encode())
        self.status = 200
        self.headers = {'X-CPA-TRACE-ID': trace}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for name in ('GEMINI_API_KEY', 'GEMINI_API_KEY_2', 'GEMINI_API_KEY_3', 'GEMINI_KEY_BACKUP',
                 'AUTOSLICE_CPA_USAGE_FILE', 'AUTOSLICE_CPA_GEMINI_AUTH_TIERS'):
        monkeypatch.delenv(name, raising=False)
    values = {cpa.ENABLE_ENV: '1', 'CPA_BASE_URL': 'https://fake-cpa.invalid',
        'CPA_API_KEY': 'synthetic-gateway-token', 'AUTOSLICE_CPA_GEMINI_LEDGER': str(tmp_path / 'ledger.jsonl')}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def rows(environment):
    return [json.loads(line) for line in Path(environment['AUTOSLICE_CPA_GEMINI_LEDGER']).read_text().splitlines()]


def historical(name):
    path = Path(__file__).parent / 'fixtures/cpa_gemini_baseline' / (name + '.py.txt')
    fullname = 'src.autoslice._cpa_baseline_' + name
    loader = SourceFileLoader(fullname, str(path))
    spec = importlib.util.spec_from_loader(fullname, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    loader.exec_module(module)
    return module


@pytest.mark.parametrize('attachments', [[], [(b'\x00whole-audio\xff', 'audio/mpeg')]])
def test_native_text_and_exact_inline_audio(environment, attachments):
    captured = []
    def server(request, **kwargs):
        captured.append(request)
        return Response()
    completion = cpa.generate(prompt='same question', model='gemini-3.8-flash', purpose='offline',
        inline_parts=attachments, environment=environment, opener=server)
    assert completion.text == 'accepted'
    request = captured[0]
    assert request.full_url == 'https://fake-cpa.invalid/v1beta/models/gemini-3.8-flash:generateContent'
    assert request.get_header('X-goog-api-key') is None
    assert request.get_header('Authorization') == 'Bearer synthetic-gateway-token'
    body = json.loads(request.data)
    assert body['contents'][0]['parts'][0] == {'text': 'same question'}
    if attachments:
        import base64
        part = body['contents'][0]['parts'][1]['inline_data']
        assert base64.b64decode(part['data']) == attachments[0][0]
        assert part['mime_type'] == attachments[0][1]
    ledger = rows(environment)
    assert [row['status'] for row in ledger] == ['DISPATCH_RESERVED', 'RESPONDED']
    assert ledger[0]['request_id'] == ledger[1]['request_id']
    assert ledger[1]['key_tier'] == ledger[1]['cost_usd'] == ledger[1]['upstream_attempts'] == 'UNKNOWN'
    assert 'synthetic-gateway-token' not in json.dumps(ledger)
    assert 'same question' not in json.dumps(ledger)


@pytest.mark.parametrize('status,delay', [(429, 7200), (503, None), (403, None)])
def test_cooldown_quota_and_server_failure_are_not_client_retries(environment, status, delay):
    calls = []
    def server(request, **kwargs):
        calls.append(request)
        headers = {'Retry-After': str(delay)} if delay is not None else {}
        raise urllib.error.HTTPError(request.full_url, status, 'synthetic', headers, io.BytesIO(b'not logged'))
    with pytest.raises(cpa.CpaGeminiError) as error:
        cpa.generate(prompt='q', model='gemini-3.8-flash', purpose='offline',
            environment=environment, opener=server)
    assert error.value.code == status
    assert error.value.retry_after_seconds == delay
    assert len(calls) == 1
    assert rows(environment)[-1]['status'] == 'HTTP_ERROR'
    assert rows(environment)[-1]['retry_after_seconds'] == delay


def test_fake_cpa_503_to_paid_is_one_gateway_call_and_one_accounted_attempt(environment):
    # This simulates the server's routing outcome, not a claim about live CPA.
    upstream = []
    gateway = []
    environment['AUTOSLICE_CPA_GEMINI_AUTH_TIERS'] = json.dumps({'0123456789abcdef': 'paid'})
    def server(request, **kwargs):
        gateway.append(request)
        upstream.extend([('subscription', 503), ('free', 429), ('paid', 200)])
        return Response()
    result = cpa.generate(prompt='q', model='gemini-4-argon', purpose='offline',
        environment=environment, opener=server)
    assert len(gateway) == 1 and len(upstream) == 3
    terminal = rows(environment)[-1]
    assert terminal['key_tier'] == 'paid'
    assert terminal['cost_usd'] == terminal['upstream_attempts'] == 'UNKNOWN'
    assert result.receipt['identity_model'] == 'gemini-4-argon'


def test_failed_paid_attempt_is_also_accounted(environment):
    environment['AUTOSLICE_CPA_GEMINI_AUTH_TIERS'] = json.dumps({'0123456789abcdef': 'paid'})
    def server(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 503, 'synthetic',
            {'X-CPA-TRACE-ID': '1729850000-0123456789abcdef-request'}, None)
    with pytest.raises(cpa.CpaGeminiError):
        cpa.generate(prompt='q', model='gemini-3.8-flash', purpose='offline',
            environment=environment, opener=server)
    assert rows(environment)[-1]['key_tier'] == 'paid'


def test_ledger_failure_prevents_dispatch(environment, monkeypatch):
    calls = []
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(cpa, '_append', fail)
    with pytest.raises(OSError):
        cpa.generate(prompt='q', model='gemini-3.8-flash', purpose='offline',
            environment=environment, opener=lambda *a, **kw: calls.append(a))
    assert calls == []


@pytest.mark.parametrize('model,expected,level', [
    ('Gemini 3.8 Flash (High)', 'gemini-3.8-flash', 'HIGH'),
    ('gemini-3.8-flash-low', 'gemini-3.8-flash', 'LOW'),
    ('Gemini 4 Argon', 'gemini-4-argon', None),
    ('gemini-3.6-flash', 'gemini-3.6-flash', None)])
def test_names_change_spelling_without_upgrading_identity(environment, model, expected, level):
    calls = []
    result = cpa.generate(prompt='q', model=model, purpose='offline', environment=environment,
        opener=lambda request, **kwargs: (calls.append(request) or Response()))
    assert result.receipt['identity_model'] == model
    assert result.receipt['wire_model'] == expected
    assert json.loads(calls[0].data)['generationConfig'].get('thinkingConfig', {}).get('thinkingLevel') == level


@pytest.mark.parametrize('parts', [[], [(b'audio-bytes', 'audio/mpeg')]])
def test_switch_off_request_bytes_match_real_historical_baseline(environment, monkeypatch, parts):
    monkeypatch.setenv(cpa.ENABLE_ENV, '0')
    original = historical('agy_gemini_client')
    requests = []
    def send(request, **kwargs):
        requests.append((request.full_url, request.data, sorted(request.header_items()), kwargs))
        return Response()
    monkeypatch.setattr(legacy.urllib.request, 'urlopen', send)
    for module in (original, legacy):
        monkeypatch.setattr(module, 'runtime_provider_slot', lambda **kw: nullcontext())
        assert module.generate_content(prompt='原问题', key='synthetic-legacy-key', model='gemini-3.8-flash',
            inline_parts=parts, thinking={'thinkingLevel': 'HIGH'}) == 'accepted'
    assert requests[0] == requests[1]


def test_switch_off_agy_request_and_receipt_identity_match_baseline(environment, monkeypatch, tmp_path):
    monkeypatch.setenv(cpa.ENABLE_ENV, '0')
    original = historical('agy_gemini_client')
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, 'accepted', '')
    outcomes = []
    for module in (original, legacy):
        monkeypatch.setattr(module, 'runtime_provider_slot', lambda **kw: nullcontext())
        argv = module.agy_argv('synthetic-agy', job_dir=tmp_path, model='Gemini 3.8 Flash (High)',
            prompt='unchanged', print_timeout='5m')
        outcome = module.run_local_agy(argv, cwd=tmp_path, timeout=30, command_runner=runner)
        outcomes.append((outcome.returncode, outcome.stdout, outcome.stderr, outcome.failure_category))
    assert calls[0] == calls[1]
    assert outcomes[0] == outcomes[1]


def test_switch_off_frame_receipt_and_cache_identity_match_baseline(environment, monkeypatch):
    from src.autoslice import agy_frame_witness, completed_review_checkpoint
    monkeypatch.setenv(cpa.ENABLE_ENV, '0')
    original = historical('agy_frame_witness')
    identities = []
    def ruling_response(**kwargs):
        identities.append((kwargs['stage'], kwargs['prompt'], kwargs['inputs'], kwargs['llm_call'].cpa_cache_identity))
        return kwargs['llm_call'](kwargs['prompt']), False
    monkeypatch.setattr(completed_review_checkpoint, 'ruling_response', ruling_response)
    monkeypatch.setattr(legacy, 'runtime_provider_slot', lambda **kw: nullcontext())
    monkeypatch.setattr(legacy.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 0, 'blue', ''))
    first = original._vision_qa({}, b'identical-pixels', 'What color?', timeout_seconds=30)
    second = agy_frame_witness._vision_qa({}, b'identical-pixels', 'What color?', timeout_seconds=30)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert identities[0] == identities[1]


def test_enabled_ladder_never_reads_or_rotates_google_keys(environment, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy key management was reached')
    monkeypatch.setattr(legacy, 'free_api_keys', forbidden)
    monkeypatch.setattr(legacy.gemini_backup_policy, 'paid_backup_key', forbidden)
    monkeypatch.setattr(legacy.gemini_backup_policy, 'record_free_chain_failure', forbidden)
    for status in (429, 503):
        calls = []
        def observe(token):
            calls.append(token)
            raise cpa.CpaGeminiError(status, 'synthetic')
        outcome = legacy.run_gemini_key_ladder(item_key='same', purpose='offline', observe=observe)
        assert not outcome.accepted
        assert calls == ['CPA_MANAGED']


def test_enabled_local_job_has_real_native_receipt_without_agy_trace(environment, monkeypatch, tmp_path):
    media = tmp_path / 'input.mp3'
    media.write_bytes(b'whole-input')
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: Response('{"ok":true}'))
    argv = legacy.agy_argv('must-not-launch', job_dir=tmp_path, model='Gemini 3.8 Flash (High)',
        prompt='legacy file instructions', print_timeout='5m')
    def forbidden(*args, **kwargs):
        raise AssertionError('AGY launched')
    run = legacy.run_local_agy(argv, cwd=tmp_path, timeout=30, command_runner=forbidden,
        cpa_prompt='same substantive question', cpa_files=[media], cpa_output_path=tmp_path / 'output.json')
    assert run.stdout == '{"ok":true}'
    assert run.launched is False
    assert run.transport_receipt['identity_model'] == 'Gemini 3.8 Flash (High)'
    cpa.validate_file_receipt(receipt=run.transport_receipt, prompt='same substantive question',
        model='Gemini 3.8 Flash (High)', files=[media], text=run.stdout)
    assert not (tmp_path / 'agy.log').exists()
    media.write_bytes(b'changed')
    with pytest.raises(ValueError, match='RECEIPT_MISMATCH'):
        cpa.validate_file_receipt(receipt=run.transport_receipt, prompt='same substantive question',
            model='Gemini 3.8 Flash (High)', files=[media], text=run.stdout)


def test_batch_is_explicitly_unsupported_without_credential_or_network_access(environment, tmp_path):
    from src.autoslice.argon_escalation_runtime import ArgonRuntime
    def forbidden(*args, **kwargs):
        raise AssertionError('credentials or direct transport reached')
    runtime = ArgonRuntime(runtime_root=tmp_path, config={}, credential_loader=forbidden, transport_factory=forbidden)
    with pytest.raises(cpa.CpaGeminiError, match='UNSUPPORTED_OPERATION:batch_lifecycle'):
        runtime.advance(root=tmp_path / 'new-job', spec=None)
    assert not (tmp_path / 'new-job').exists()


@pytest.mark.parametrize('process_switch', [True, False])
def test_files_api_becomes_one_inline_cpa_call(environment, monkeypatch, tmp_path, process_switch):
    from src.autoslice.gemini_file_api import run_free_key_file_call
    media = tmp_path / 'input.mp4'
    media.write_bytes(b'whole-video')
    requests = []
    def server(request, **kwargs):
        requests.append(request)
        return Response()
    monkeypatch.setattr(cpa, '_open', server)
    if not process_switch:
        monkeypatch.delenv(cpa.ENABLE_ENV)
    result = run_free_key_file_call(media_path=media, expected_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
        expected_bytes=media.stat().st_size, mime_type='video/mp4', prompt='q', model='gemini-3.8-flash',
        video_fps=5.0, environment=environment, timeout_seconds=30, poll_timeout_seconds=30,
        transport_factory=lambda: pytest.fail('Google Files transport reached'))
    assert result.text == 'accepted'
    assert result.evidence['paid_backup_used'] == 'UNKNOWN'
    assert result.evidence['cleanup_status'] == 'NO_REMOTE_FILE_CREATED'
    assert len(requests) == 1


def test_native_file_route_retains_video_request_validation(environment, monkeypatch, tmp_path):
    from src.autoslice.gemini_file_api import run_free_key_file_call, GeminiFileApiError
    media = tmp_path / 'input.mp4'
    media.write_bytes(b'whole-video')
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: pytest.fail('invalid request dispatched'))
    with pytest.raises(GeminiFileApiError) as rejected:
        run_free_key_file_call(media_path=media, expected_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
            expected_bytes=media.stat().st_size, mime_type='video/mp4', prompt='q', model='gemini-3.8-flash',
            video_fps=0, environment=environment, timeout_seconds=30, poll_timeout_seconds=30)
    assert rejected.value.reason_code == 'GEMINI_API_REQUEST_INVALID'
    assert not Path(environment['AUTOSLICE_CPA_GEMINI_LEDGER']).exists()


def test_oversized_inline_is_local_rejection_not_google_fallback(environment):
    requests = []
    with pytest.raises(ValueError, match='REQUEST_TOO_LARGE'):
        cpa.generate(prompt='q', model='gemini-3.8-flash', purpose='offline',
            inline_parts=[(b'x' * cpa.MAX_REQUEST_BYTES, 'audio/mpeg')], environment=environment,
            opener=lambda *a, **kw: requests.append(a))
    assert requests == []
    assert not Path(environment['AUTOSLICE_CPA_GEMINI_LEDGER']).exists()


def test_three_part_normal_consumer_replays_native_response_with_identical_identity(environment, monkeypatch, tmp_path):
    from tests.test_agy_three_part_acoustic import _descriptor, _patch_local_tools, _provider_payload, _request
    from src.autoslice import agy_three_part_acoustic as adapter
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    def judge(_):
        return '{}'
    judge.cpa_cache_identity = {'transport': 'fixture.cpa', 'models': ['fixture'], 'effort': 'medium'}
    arguments = dict(descriptor=descriptor, candidate_id='candidate', observation_id='cue-13',
                     evidence_root=tmp_path / 'evidence', cpa_call=judge)
    monkeypatch.setenv(cpa.ENABLE_ENV, '0')
    off = adapter.build_three_part_callbacks(**arguments)
    monkeypatch.setenv(cpa.ENABLE_ENV, '1')
    on = adapter.build_three_part_callbacks(**arguments)
    assert off.runtime_identity == on.runtime_identity
    calls = []
    def server(request, **kwargs):
        calls.append(request)
        return Response(json.dumps(_provider_payload()))
    monkeypatch.setattr(cpa, '_open', server)
    observed = on.observe_audio(_request(descriptor, on.runtime_identity))
    assert observed['status'] == 'OBSERVED'
    replay = adapter.build_three_part_callbacks(**arguments)
    assert replay.runtime_identity == on.runtime_identity
    assert replay.observe_audio(_request(descriptor, replay.runtime_identity))['status'] == 'OBSERVED'
    assert len(calls) == 1


def test_lrc_cold_consumer_checks_native_audio_lyrics_prompt_and_response(environment, monkeypatch, tmp_path):
    from src.autoslice.song_common import validate_audio_lrc_execution_metadata
    audio = tmp_path / 'input.mp3'
    lyrics = tmp_path / 'lyrics.jsonl'
    prompt = tmp_path / 'prompt.md'
    output = tmp_path / 'output.json'
    audio.write_bytes(b'exact audio')
    lyrics.write_text('{"lrc_index":0,"text":"歌词"}\n')
    prompt.write_text('Listen to the entire audio; use untimed lyric indices only.')
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: Response('{"rows":[]}'))
    completion = cpa.generate_files(prompt=prompt.read_text(), files=[audio, lyrics],
        model='Gemini 3.8 Flash (High)', purpose='offline')
    output.write_text(completion.text)
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'model': 'Gemini 3.8 Flash (High)', 'cpa_transport_receipt': dict(completion.receipt),
        'observation_input': {'audio_path': str(audio), 'audio_sha256': sha(audio),
                             'lyrics_path': str(lyrics), 'lyrics_sha256': sha(lyrics)},
        'artifacts': {'prompt_path': str(prompt), 'prompt_sha256': sha(prompt),
                      'provider_raw_output_path': str(output), 'provider_raw_output_sha256': sha(output)}}
    assert cpa.valid_lrc_transport(manifest)
    assert validate_audio_lrc_execution_metadata(provider='agy', model=manifest['model'], agy_rc=0,
        provider_fallback_used=False, agy_failure_category=None, sandbox=False, cpa_transport_valid=True) is None
    audio.write_bytes(b'different audio')
    assert not cpa.valid_lrc_transport(manifest)
    assert validate_audio_lrc_execution_metadata(provider='agy', model=manifest['model'], agy_rc=0,
        provider_fallback_used=False, agy_failure_category=None, sandbox=False, cpa_transport_valid=False) is not None


def test_switch_off_ignores_leftover_cpa_receipt_when_persisting_lrc(environment, monkeypatch, tmp_path):
    from src.autoslice import agy_lrc_alignment as lrc
    monkeypatch.delenv(cpa.ENABLE_ENV)
    audio, lyrics, prompt, raw = [tmp_path / name for name in ('input.mp3', 'lyrics.jsonl', 'prompt.md', 'raw.json')]
    for path in (audio, lyrics, prompt, raw):
        path.write_text('{}')
    # A damaged native artifact must not even be parsed by a legacy execution.
    (tmp_path / 'cpa-gemini.receipt.json').write_text('invalid stale JSON')
    digest = hashlib.sha256(b'{}').hexdigest()
    job = lrc._PreparedAgyLrcJob(source_origin_path=str(audio), job_dir=tmp_path,
        attempt_id='offline', media_path=audio, source_sha=digest, duration_ms=1,
        lrc_path=lyrics, lrc_sha=digest, prompt_path=prompt, prompt_sha=digest,
        audio_path=audio, audio_sha=digest, audio_duration_ms=1, lyrics_path=lyrics,
        started_at='offline', stdout_path=tmp_path / 'agy.stdout', stderr_path=tmp_path / 'agy.stderr',
        provider_raw_output_path=raw)
    execution = lrc._AgyLrcExecution(payload={}, provider=lrc.AGY_AUDIO_LRC_PROVIDER,
        model=lrc.AGY_AUDIO_LRC_MODEL, provider_fallback_used=False, agy_rc=0,
        agy_failure_category=None, agy_failure_detail=None, configured_key_count=None,
        accepted_key_ordinal=None, accepted_key_tier=None, paid_backup_policy=None,
        api_audio_path=None, api_audio_sha256=None, api_audio_duration_ms=None,
        gemini_api_raw_attempts=[], provider_raw_output_path=raw, prompt_path=prompt, prompt_sha256=digest)
    result = lrc._persist_agy_lrc_run(job=job, lrc=SimpleNamespace(lines=[]),
                                   candidate_id='offline', execution=execution)
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest['sandbox'] is True
    assert manifest['direct_audio_input'] is False
    assert 'cpa_transport_receipt' not in manifest
    assert 'tool_trace_path' in manifest['observation_input']


def test_fresh_and_local_jobs_use_native_interface_without_ssh(environment, monkeypatch, tmp_path):
    from src.autoslice.full_session_transcription import _run_fresh_agy_job
    from src.autoslice.jingting_remote_runner import _run_local_agy_job
    media = tmp_path / 'input.mp4'
    media.write_bytes(b'exact-video')
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: Response('1\n00:00:00,000 --> 00:00:01,000\n原话\n'))
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy process/SSH reached')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    first = _run_fresh_agy_job('must-not-ssh', job_dir='/must-not-create', media_path=media, prompt='q',
        output_name='output.srt', stage='fresh', model='Gemini 3.8 Flash (High)',
        poll_deadline_seconds=30, poll_interval_seconds=1)
    second = _run_local_agy_job(tmp_path / 'job', input_path=media, prompt='q', output_name='output.srt',
        model='Gemini 3.8 Flash (High)', short_prompt='legacy', print_timeout='5m',
        poll_deadline_seconds=30, poll_interval_seconds=1, stage='local')
    assert first == second
    assert (tmp_path / 'job/output.srt').read_text() == second
    assert len([row for row in rows(environment) if row['status'] == 'RESPONDED']) == 2


def test_legacy_transcription_cli_native_default_is_current_model(environment, monkeypatch, tmp_path):
    from scripts.cpa_gemini_transcribe import main
    media = tmp_path / 'input.mp4'
    media.write_bytes(b'exact full capture')
    srt, notes = tmp_path / 'output.srt', tmp_path / 'notes.json'
    payload = {'srt': '1\n00:00:00,000 --> 00:00:01,000\n原话\n',
               'notes': {'talking_confirmed': True, 'funny_moments': [], 'topics': [], 'notes': ''}}
    monkeypatch.delenv('AGY_MODEL', raising=False)
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: Response(json.dumps(payload)))
    monkeypatch.setattr(sys, 'argv', ['cpa_gemini_transcribe.py', 'talk', str(media), str(srt), str(notes)])
    main()
    assert srt.read_text() == payload['srt']
    assert json.loads(notes.read_text()) == payload['notes']
    assert rows(environment)[-1]['wire_model'] == 'gemini-3.8-flash'


def test_physical_capability_cannot_reuse_files_proof_for_cpa_transport(environment, monkeypatch, tmp_path):
    from src.autoslice import final_media_review_gemini_api_runtime as runtime
    from src.autoslice import final_media_review_code_binding as binding
    monkeypatch.setattr(binding, 'select_code_repo', lambda *a, **kw: (tmp_path / 'repo', None))
    monkeypatch.setattr(runtime, '_bound_regular', lambda path, **kw: (path, 'a' * 64))
    monkeypatch.setattr(runtime, '_external_tool', lambda *a, **kw: (Path('/fake/tool'), 'b' * 64))
    legacy_env = {runtime.MODEL_ENV: 'gemini-3.8-flash', cpa.ENABLE_ENV: '0', 'GEMINI_API_KEY': 'synthetic'}
    native_env = {runtime.MODEL_ENV: 'gemini-3.8-flash', cpa.ENABLE_ENV: '1'}
    old, old_keys = runtime._manifest(tmp_path, legacy_env)
    new, new_keys = runtime._manifest(tmp_path, native_env)
    assert (old['provider'], old['model']) == (new['provider'], new['model'])
    assert old['capability_id'] != new['capability_id']
    assert old['endpoint_family'] != new['endpoint_family']
    assert old_keys == 1 and new_keys == 0


def test_sentinel_rejects_a_receipt_for_the_other_wire_protocol(environment, tmp_path):
    from scripts import final_media_review_gemini_api_adapter as adapter
    from src.autoslice.final_media_review_gemini_api_runtime import ENDPOINT_FAMILY, selected_endpoint_family
    media, prompt, request = tmp_path / 'input.wav', tmp_path / 'prompt.md', tmp_path / 'request.json'
    media.write_bytes(b'exact audio')
    prompt.write_text('Read the hidden audio token.')
    value = {'schema_version': adapter.SENTINEL_REQUEST_SCHEMA, 'challenge_id': 'fixture',
        'provider': 'gemini_api', 'model': 'gemini-3.8-flash', 'endpoint_family': ENDPOINT_FAMILY,
        'coverage': 'RAW_AUDIO_ONLY_HIDDEN_NONCE', 'input_sha256': hashlib.sha256(media.read_bytes()).hexdigest(),
        'prompt_sha256': hashlib.sha256(prompt.read_bytes()).hexdigest(), 'answer_disclosed_to_adapter': False}
    request.write_text(json.dumps(value))
    with pytest.raises(adapter.AdapterError) as rejected:
        adapter._sentinel_request(request, input_path=media, prompt_path=prompt, environment=environment)
    assert rejected.value.safe_reason == 'GEMINI_API_SENTINEL_REQUEST_INVALID'
    value['endpoint_family'] = selected_endpoint_family(environment)
    request.write_text(json.dumps(value))
    observed, _ = adapter._sentinel_request(request, input_path=media, prompt_path=prompt, environment=environment)
    assert observed == value


def test_gemini_transports_never_manufacture_two_independent_witnesses(environment, monkeypatch):
    from src.autoslice.semantic_terminal_adjudication import grade_a_witnesses, independent_wide_identity
    from src.autoslice.discovery_channels import model_identity_key
    first = {'observation_id': 'subscription', 'receipt_kind': 'blind_range',
             'raw_observation': {'provider': 'agy', 'model': 'Gemini 3.8 Flash (High)'}}
    second = {'observation_id': 'api', 'receipt_kind': 'blind_range',
              'raw_observation': {'provider': 'gemini_api', 'model': 'models/gemini-3.8-flash'}}
    request = {'terminal_policy': {'grades': {'A': {'min_independent_wide_witnesses': 2}}},
               'audio_gain_termination': {'projection': {'observations': [first, second]}}}
    assert len(grade_a_witnesses(request, ['subscription', 'api'])) == 1
    contract = {'receipt_kind': 'blind_range', 'independence_fields': ['provider', 'model']}
    merged = {**first, 'raw_observation': {**first['raw_observation'],
        'quality_and_provider': [second['raw_observation']]}}
    assert independent_wide_identity(merged, contract) == ('gemini', 'gemini-3.8-flash')
    assert model_identity_key('Gemini 3.8 Flash (High)') == model_identity_key('gemini-3.8-flash-low')
    # A different actual model still contributes its own independent identity.
    different = {**second, 'observation_id': 'argon',
                 'raw_observation': {'provider': 'gemini_api', 'model': 'gemini-4-argon'}}
    request['audio_gain_termination']['projection']['observations'] = [first, different]
    assert len(grade_a_witnesses(request, ['subscription', 'argon'])) == 2
    # OFF preserves the baseline's exact comparison, including its aliases.
    monkeypatch.delenv(cpa.ENABLE_ENV)
    request['audio_gain_termination']['projection']['observations'] = [first, second]
    assert len(grade_a_witnesses(request, ['subscription', 'api'])) == 2
    assert independent_wide_identity(merged, contract) is None
    assert model_identity_key('Gemini 3.8 Flash (High)') == 'gemini 3.8 flash (high)'
    assert model_identity_key('models/gemini-3.8-flash:latest') == 'gemini-3.8-flash'


def test_switch_off_preserves_exact_legacy_transport_injection_signature(environment, monkeypatch):
    from src.autoslice import agy_frame_witness, completed_review_checkpoint
    monkeypatch.setenv(cpa.ENABLE_ENV, '0')
    calls = []
    def exact_legacy_transport(argv, *, cwd, timeout):
        calls.append((argv, cwd, timeout))
        return SimpleNamespace(completed=subprocess.CompletedProcess(argv, 0, 'blue', ''),
            failure_category=None, launch_error_type=None)
    monkeypatch.setattr(legacy, 'run_local_agy', exact_legacy_transport)
    monkeypatch.setattr(completed_review_checkpoint, 'ruling_response',
        lambda **kwargs: (kwargs['llm_call'](kwargs['prompt']), False))
    answer = agy_frame_witness._vision_qa({}, b'pixels', 'What color?', timeout_seconds=30)
    assert answer['status'] == 'OBSERVED'
    assert answer['answer'] == 'blue'
    assert len(calls) == 1
