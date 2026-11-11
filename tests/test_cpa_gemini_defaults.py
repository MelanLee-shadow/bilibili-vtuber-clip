"""Default routing and the two temporary protocol exceptions, offline only."""
import base64
from contextlib import nullcontext
import hashlib
import json
import subprocess

import pytest

from src.autoslice import agy_gemini_client as agy
from src.autoslice import cpa_gemini_client as cpa
from src.autoslice import gemini_file_api as files
from src.autoslice.argon_escalation_contract import MODEL, canonical
from test_cpa_gemini_client import Response, environment, rows
from test_gemini_file_api import FakeTransport
from test_argon_escalation_transport import transport
from test_argon_hearing_schema_single_definition import make_wire


@pytest.fixture
def defaults(environment, monkeypatch):
    environment.pop(cpa.ENABLE_ENV)
    monkeypatch.delenv(cpa.ENABLE_ENV)
    return environment


@pytest.fixture
def wire():
    # Other offline plugins forbid even loopback TCP. Exercise the same real
    # HTTP builder through an in-memory connection without relaxing that guard.
    captured = make_wire()
    captured.response_bytes = b'{"kept":"native legacy bytes"}\n'
    captured.plan = lambda _: {'body': captured.response_bytes}
    return captured


@pytest.mark.parametrize('audio', [None, b'\x00exact-audio\xff'])
def test_unset_switch_routes_shared_text_and_audio_to_cpa(defaults, monkeypatch, audio):
    captured = []
    def server(request, **kwargs):
        captured.append(request)
        return Response()
    monkeypatch.setattr(cpa, '_open', server)
    assert cpa.enabled() and cpa.enabled(defaults)
    assert agy.generate_content(prompt='same question', key='unused', model='gemini-3.8-flash',
        inline_data=audio, mime_type='audio/mpeg') == 'accepted'
    assert len(captured) == 1
    assert captured[0].full_url.startswith('https://fake-cpa.invalid/v1beta/')
    if audio is not None:
        part = json.loads(captured[0].data)['contents'][0]['parts'][1]['inline_data']
        assert base64.b64decode(part['data']) == audio
    assert [r['status'] for r in rows(defaults)] == ['DISPATCH_RESERVED', 'RESPONDED']
    assert rows(defaults)[-1]['key_tier'] == 'UNKNOWN'


@pytest.mark.parametrize('size', [15_000_000, 20_000_001])
def test_oversized_files_use_existing_upload_and_record_actual_transport(defaults, monkeypatch, tmp_path, size):
    media = tmp_path / 'large.mp4'
    with media.open('wb') as handle:
        handle.truncate(size)
    fake = FakeTransport()
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: pytest.fail('oversized file sent to CPA'))
    env = {**defaults, 'GEMINI_API_KEY': 'synthetic-free-key'}
    outcome = files.run_free_key_file_call(media_path=media,
        expected_sha256=hashlib.sha256(media.read_bytes()).hexdigest(), expected_bytes=size,
        mime_type='video/mp4', prompt='whole file', model='gemini-3.8-flash', video_fps=5.0,
        environment=env, timeout_seconds=30, poll_timeout_seconds=30, transport_factory=lambda: fake)
    assert len(fake.uploaded) == size
    assert outcome.evidence['transport'] == 'google_files_api'
    assert outcome.evidence['cpa_exception'] == 'files_upload'
    assert outcome.evidence['paid_backup_used'] is False
    ledger = rows(defaults)
    assert [r['status'] for r in ledger] == ['DISPATCH_RESERVED', 'RESPONDED']
    assert ledger[-1]['transport'] == 'google_files_api'
    assert ledger[-1]['key_tier'] == 'free' and ledger[-1]['cost_usd'] == 'UNKNOWN'
    assert 'synthetic-free-key' not in json.dumps(ledger)


@pytest.mark.parametrize('size,cpa_expected', [(12, True), (13, False)])
def test_files_route_uses_exact_serialized_boundary(defaults, monkeypatch, tmp_path, size, cpa_expected):
    prompt, model, mime, fps = '中文', 'gemini-3.8-flash', 'video/mp4', 5.0
    overhead = len(json.dumps(files._inline_request(prompt=prompt, model=model,
        mime_type=mime, video_fps=fps), ensure_ascii=False).encode())
    monkeypatch.setattr(cpa, 'MAX_REQUEST_BYTES', overhead + 16)
    media = tmp_path / 'small.mp4'
    media.write_bytes(b'x' * size)
    calls, fake = [], FakeTransport()
    def server(request, **kwargs):
        calls.append(request)
        assert len(request.data) == overhead + 16
        return Response()
    monkeypatch.setattr(cpa, '_open', server)
    outcome = files.run_free_key_file_call(media_path=media,
        expected_sha256=hashlib.sha256(media.read_bytes()).hexdigest(), expected_bytes=size,
        mime_type=mime, prompt=prompt, model=model, video_fps=fps,
        environment={**defaults, 'GEMINI_API_KEY': 'synthetic'}, timeout_seconds=30,
        poll_timeout_seconds=30, transport_factory=lambda: fake)
    assert bool(calls) is cpa_expected
    assert outcome.evidence['transport'] == ('cpa_gemini_native' if cpa_expected else 'google_files_api')


def test_explicit_off_environment_reaches_small_files_legacy_path(defaults, monkeypatch, tmp_path):
    media = tmp_path / 'small.mp4'
    media.write_bytes(b'exact')
    fake = FakeTransport()
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: pytest.fail('explicit OFF ignored'))
    monkeypatch.setattr(files, 'build_generation_config',
        lambda **kw: pytest.fail('explicit OFF constructed CPA sizing request'))
    outcome = files.run_free_key_file_call(media_path=media,
        expected_sha256=hashlib.sha256(media.read_bytes()).hexdigest(), expected_bytes=5,
        mime_type='video/mp4', prompt='q', model='gemini-3.8-flash', video_fps=5.0,
        environment={cpa.ENABLE_ENV: '0', 'GEMINI_API_KEY': 'synthetic'},
        timeout_seconds=30, poll_timeout_seconds=30, transport_factory=lambda: fake)
    assert fake.uploaded == b'exact'
    assert outcome.evidence['key_tier'] == 'free'
    assert not cpa.enabled({cpa.ENABLE_ENV: '0'})


@pytest.mark.parametrize('switch', ['0', '1'])
def test_invalid_file_model_retains_local_typed_validation(defaults, monkeypatch, tmp_path, switch):
    media = tmp_path / 'small.mp4'
    media.write_bytes(b'exact')
    fake = FakeTransport()
    monkeypatch.setattr(files, 'build_generation_config',
        lambda **kw: pytest.fail('invalid model reached generation config'))
    with pytest.raises(files.GeminiFileApiError, match='GEMINI_API_REQUEST_INVALID'):
        files.run_free_key_file_call(media_path=media,
            expected_sha256=hashlib.sha256(media.read_bytes()).hexdigest(), expected_bytes=5,
            mime_type='video/mp4', prompt='q', model='', video_fps=5.0,
            environment={**defaults, cpa.ENABLE_ENV: switch, 'GEMINI_API_KEY': 'synthetic'},
            timeout_seconds=30, poll_timeout_seconds=30,
            transport_factory=lambda: fake)
    assert fake.calls == [] and fake.uploaded == b''


@pytest.mark.parametrize('method,path,body', [
    ('POST', f'models/{MODEL}:batchGenerateContent', b'{"batch":{"input_config":{}}}'),
    ('GET', 'batches/job-1', None),
    ('GET', 'batches?pageSize=10', None),
    ('GET', f'models/{MODEL}', None),
])
def test_argon_lifecycle_uses_original_https_bytes_and_sidecar_receipt(defaults, wire, method, path, body):
    client, resolved = transport(wire)
    route = {'configured_key_count': 1, 'accepted_key_ordinal': 1,
             'accepted_key_tier': 'free', 'paid_backup_policy': None}
    if path.startswith('models/') and method == 'GET':
        from src.autoslice.argon_escalation_transport import ArgonHttpTransport
        client = ArgonHttpTransport(lambda _: 'synthetic', connection_factory=wire.factory)
        result = client.model_metadata(timeout_s=2)
    else:
        result = client.request(method, path, body, route=route, timeout_s=2)
    assert result.status == 200
    assert result.body == wire.response_bytes
    assert len(wire.requests) == 1
    assert wire.requests[0]['path'] == '/v1beta/' + path
    assert wire.requests[0]['body'] == (body or b'')
    assert rows(defaults)[-1]['transport'] == 'google_argon_rest'
    assert rows(defaults)[-1]['cpa_exception'] == 'argon_lifecycle'


def test_argon_realtime_uses_cpa_without_legacy_resolver(defaults, monkeypatch):
    from src.autoslice.argon_escalation_transport import ArgonHttpTransport
    calls = []
    def server(request, **kwargs):
        calls.append(request)
        return Response()
    monkeypatch.setattr(cpa, '_open', server)
    client = ArgonHttpTransport(lambda _: pytest.fail('legacy key resolver reached'))
    route = {'configured_key_count': 1, 'accepted_key_ordinal': 1,
             'accepted_key_tier': 'free', 'paid_backup_policy': None}
    result = client.request('POST', f'models/{MODEL}:generateContent', canonical({'contents': []}),
        route=route, timeout_s=2)
    assert result.status == 200 and len(calls) == 1
    assert rows(defaults)[-1]['transport'] == 'cpa_gemini_native'
    assert rows(defaults)[-1]['key_tier'] == 'UNKNOWN'


def test_attested_agy_exception_keeps_real_process_boundary(defaults, monkeypatch, tmp_path):
    monkeypatch.setattr(agy, 'runtime_provider_slot', lambda **kw: nullcontext())
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'native', '')
    run = agy.run_local_agy(['synthetic-agy', '--model', 'Gemini 4 Argon'], cwd=tmp_path,
        timeout=30, command_runner=runner, cpa_operation='argon_lifecycle')
    assert run.launched and run.stdout == 'native'
    assert len(calls) == 1 and run.transport_receipt is None


def test_unknown_operations_and_cpa_errors_never_gain_a_direct_fallback(defaults, monkeypatch):
    with pytest.raises(cpa.CpaGeminiError, match='UNSUPPORTED_OPERATION'):
        cpa.require_operation('new_unregistered_protocol')
    calls = []
    def server(request, **kwargs):
        calls.append(request)
        raise cpa.CpaGeminiError(503, 'synthetic')
    monkeypatch.setattr(cpa, '_open', server)
    monkeypatch.setattr(agy.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('direct fallback'))
    with pytest.raises(cpa.CpaGeminiError):
        agy.generate_content(prompt='q', key='unused', model='gemini-3.8-flash')
    assert len(calls) == 1


def test_exception_ledger_failure_prevents_model_dispatch(defaults, monkeypatch, wire):
    monkeypatch.setattr(cpa, '_append', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk')))
    client, _ = transport(wire)
    with pytest.raises(OSError):
        client.model_metadata(timeout_s=2)
    assert wire.requests == []


def test_cpa_compatibility_token_is_never_a_google_exception_key():
    assert files.configured_free_keys({'GEMINI_API_KEY': 'CPA_MANAGED',
        'GEMINI_API_KEY_2': 'synthetic-real-free'}) == [('GEMINI_API_KEY_2', 'synthetic-real-free')]


def test_argon_exception_credential_loader_ignores_stale_compatibility_token(defaults, monkeypatch):
    from scripts import session_autoslice as launcher
    from src.autoslice.argon_escalation_runtime import load_credentials
    monkeypatch.setenv('GEMINI_API_KEY', 'CPA_MANAGED')
    monkeypatch.setattr(launcher, 'load_gemini_credentials', lambda: {'GEMINI_API_KEY': 'synthetic-real-free'})
    assert load_credentials()['GEMINI_API_KEY'] == 'synthetic-real-free'


def test_batch_lifecycle_realtime_continuation_preserves_original_key_and_accounting(defaults, wire, tmp_path):
    from src.autoslice.argon_escalation_runtime import ArgonRuntime
    from src.autoslice.argon_escalation_transport import ArgonHttpTransport
    runtime = ArgonRuntime(runtime_root=tmp_path, config={})
    assert runtime.transport_factory(lambda _: 'synthetic')._cpa_operation == 'argon_lifecycle'
    resolved = []
    client = ArgonHttpTransport(lambda route: (resolved.append(route) or 'synthetic-original-key'),
        connection_factory=wire.factory, cpa_operation='argon_lifecycle')
    route = {'configured_key_count': 1, 'accepted_key_ordinal': 1,
             'accepted_key_tier': 'free', 'paid_backup_policy': None}
    result = client.request('POST', f'models/{MODEL}:generateContent', canonical({'contents': []}),
        route=route, timeout_s=2)
    assert result.status == 200 and resolved == [route]
    assert {k.lower(): v for k, v in wire.requests[0]['headers'].items()}['x-goog-api-key'] == 'synthetic-original-key'
    assert rows(defaults)[-1]['transport'] == 'google_argon_rest'
    assert rows(defaults)[-1]['key_tier'] == 'free'
    assert rows(defaults)[-1]['cpa_exception'] == 'argon_lifecycle'


def test_explicit_files_capability_probe_does_not_silently_use_inline(defaults, monkeypatch, tmp_path):
    media = tmp_path / 'small.mp4'
    media.write_bytes(b'exact')
    fake = FakeTransport()
    monkeypatch.setattr(cpa, '_open', lambda *a, **kw: pytest.fail('Files probe used CPA'))
    outcome = files.run_free_key_file_call(media_path=media,
        expected_sha256=hashlib.sha256(media.read_bytes()).hexdigest(), expected_bytes=5,
        mime_type='video/mp4', prompt='q', model='gemini-3.8-flash', video_fps=5.0,
        environment={**defaults, 'GEMINI_API_KEY': 'synthetic'}, operation='files_upload',
        timeout_seconds=30, poll_timeout_seconds=30, transport_factory=lambda: fake)
    assert fake.uploaded == b'exact'
    assert outcome.evidence['transport'] == 'google_files_api'
    assert rows(defaults)[-1]['transport'] == 'google_files_api'


def test_off_retains_exact_legacy_argon_constructor_seam(defaults, monkeypatch, tmp_path):
    from src.autoslice import argon_escalation_runtime as runtime
    monkeypatch.setenv(cpa.ENABLE_ENV, '0')
    captured, sentinel = [], object()
    def legacy_constructor(resolver):
        captured.append(resolver)
        return sentinel
    monkeypatch.setattr(runtime, 'ArgonHttpTransport', legacy_constructor)
    controller = runtime.ArgonRuntime(runtime_root=tmp_path, config={})
    resolver = lambda _: 'synthetic'
    assert controller.transport_factory(resolver) is sentinel
    assert captured == [resolver]
