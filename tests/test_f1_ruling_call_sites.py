"""Class coverage: ordinary structured dispatch, raw persistence and consumers."""
import json

import pytest

from src.autoslice.llm_client import extract_json_object
from src.autoslice.structured_output import call_json


def provider(raw):
    calls = []
    def call(prompt):
        calls.append(prompt)
        return raw
    call.cpa_cache_identity = {'transport': 'recorded', 'models': ['same-model'], 'effort': 'high'}
    call.calls = calls
    return call


def test_new_structured_judge_uses_shared_default_without_registration(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"decision":"REJECT"}')
    call.cpa_resource_pressure = {'provider_call_count': 1}
    assert extract_json_object(call_json(call, 'same question JSON')) == {'decision': 'REJECT'}
    call.cpa_resource_pressure = {'provider_call_count': 99}
    assert call_json(call, 'same question JSON') == '{"decision":"REJECT"}'
    assert len(call.calls) == 1
    observations = list((tmp_path / 'cache/completed-reviews').rglob('*.json'))
    assert len(observations) == 1
    before = {p: p.read_bytes() for p in observations}
    with pytest.raises(ValueError, match='CURRENT_CONSUMER_REJECTION'):
        call_json(call, 'same question JSON', validate=lambda _: (_ for _ in ()).throw(
            ValueError('CURRENT_CONSUMER_REJECTION')))
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert len(call.calls) == 1


def test_creative_opt_out_has_no_ruling_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"visual_brief":"draft"}')
    for _ in range(2):
        call_json(call, 'write a new visual brief JSON', reuse=False)
    assert len(call.calls) == 2
    assert not (tmp_path / 'cache/completed-reviews').exists()


def test_producer_rescore_cache_uses_shared_material_key_and_preserves_no_dispatch_guard(tmp_path, monkeypatch):
    from src.autoslice.normal_text_generation_checkpoint import completed_generation
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"score":7}')
    material = {'audio_sha256': 'a' * 64, 'context': 'same source'}
    legacy = tmp_path / '.normal-text-generations/legacy.json'
    legacy.parent.mkdir()
    legacy.write_bytes(b'original legacy observation')
    def run(version, attempt=1, *, allow_dispatch=True, candidate_id='same-candidate'):
        return completed_generation(artifact_root=tmp_path / 'versions' / version,
            candidate_id=candidate_id, lane='selection_rescore', attempt=attempt,
            prompt='rescore this candidate JSON', call=call,
            dependencies=lambda: dict(material), allow_dispatch=allow_dispatch)
    assert extract_json_object(run('a' * 40)) == {'score': 7}
    assert extract_json_object(run('b' * 40, 9, allow_dispatch=False)) == {'score': 7}
    assert len(call.calls) == 1
    material['audio_sha256'] = 'b' * 64
    with pytest.raises(ValueError, match='TEXT_CHECKPOINT_NO_COMPLETE_RESPONSE'):
        run('b' * 40, allow_dispatch=False)
    assert len(call.calls) == 1
    run('b' * 40)
    run('b' * 40, candidate_id='new-candidate')
    call.semantic_policy_version = 'new-rule'
    run('b' * 40, candidate_id='new-candidate')
    assert len(call.calls) == 4
    with pytest.raises(ValueError, match='TEXT_CHECKPOINT_IDENTITY_INVALID'):
        run('a' * 40, 0)
    with pytest.raises(ValueError, match='TEXT_CHECKPOINT_UNSAFE_DIRECTORY'):
        run('../unsafe')
    assert len(call.calls) == 4
    assert legacy.read_bytes() == b'original legacy observation'


def test_producer_rescore_still_rejects_material_drift_during_dispatch(tmp_path, monkeypatch):
    from src.autoslice.normal_text_generation_checkpoint import completed_generation
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    material = {'audio_sha256': 'a' * 64}
    def call(prompt):
        material['audio_sha256'] = 'b' * 64
        return '{"score":7}'
    call.cpa_cache_identity = {'transport': 'recorded', 'models': ['same-model'], 'effort': 'high'}
    with pytest.raises(ValueError, match='TEXT_CHECKPOINT_INPUT_DRIFT'):
        completed_generation(artifact_root=tmp_path, candidate_id='same-candidate',
            lane='selection_rescore', attempt=1, prompt='rescore JSON', call=call,
            dependencies=lambda: dict(material))


def test_pronoun_new_entry_raw_reaches_strict_consumer_and_never_repairs_contract(tmp_path, monkeypatch):
    from src.autoslice.pronoun_consistency import discover_candidate_pronoun_findings, CandidatePronounAuditError
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider(json.dumps({'decisions': [{'occurrence_id': 'cue-1-occurrence-1',
        'current_token': '他', 'replacement_token': '它', 'action': 'KEEP_CURRENT',
        'referent': 'HOST', 'reason': 'invalid action/token pair'}]}))
    for _ in range(2):
        with pytest.raises(CandidatePronounAuditError) as caught:
            discover_candidate_pronoun_findings('1\n00:00:00,000 --> 00:00:01,000\n他\n',
                policy_text='女性用她', candidate_context_text='主播在自述',
                llm_call=call, extract_json=extract_json_object)
        assert caught.value.reason_code == 'CANDIDATE_PRONOUN_DECISION_CONTRACT_INVALID'
        assert caught.value.failure_layer == 'CONTRACT_ERROR'
    assert len(call.calls) == 1


@pytest.mark.parametrize('change', ['candidate', 'audio', 'context', 'rule'])
def test_default_generator_distinguishes_true_material_changes(tmp_path, monkeypatch, change):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"decision":"REJECT"}')
    call.completed_review_binding = {'candidate': '甲', 'audio': 'a' * 64, 'context': '原文'}
    call_json(call, 'same physical question JSON')
    if change == 'rule':
        call.semantic_policy_version = 'new-rule'
    else:
        call.completed_review_binding[change] = 'substantively changed'
    call_json(call, 'same physical question JSON')
    assert len(call.calls) == 2


def test_vision_ruling_cache_binds_pixels_and_current_consumer(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from src.autoslice import cpa_frame_witness as vision
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setattr(vision, 'runtime_provider_slot', lambda **_: nullcontext())
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"output_text":"{\\"decision\\":\\"REJECT\\"}"}'
    def send(request, **_):
        calls.append(request)
        return Response()
    monkeypatch.setattr(vision.urllib.request, 'urlopen', send)
    args = dict(question='same visual ruling JSON', api_base='https://offline.invalid/v1',
        api_key='fictional', model='same-model', timeout_seconds=10, max_tokens=100)
    first = vision._vision_qa({}, b'first image', **args)
    replay = vision._vision_qa({}, b'first image', **args)
    assert first['answer'] == replay['answer'] == '{"decision":"REJECT"}'
    assert not first.get('served_from_cache') and replay['served_from_cache']
    assert len(calls) == 1
    changed = vision._vision_qa({}, b'new image', **args)
    assert changed['prompt_sha256'] != first['prompt_sha256'] and len(calls) == 2


def test_wrapper_preserves_material_policy_and_endpoint_bindings(tmp_path, monkeypatch):
    from src.autoslice.completed_review_checkpoint import inherit_ruling_context
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"decision":"REJECT"}')
    call.completed_review_binding = {'audio': 'a' * 64}
    call.semantic_policy_version = 'rule-1'
    def wrapper(prompt): return call(prompt)
    inherit_ruling_context(wrapper, call)
    call_json(wrapper, 'same question JSON')
    call_json(wrapper, 'same question JSON')
    assert len(call.calls) == 1
    call.completed_review_binding['audio'] = 'b' * 64
    call_json(wrapper, 'same question JSON')
    assert len(call.calls) == 2
    from src.autoslice.final_review_provider_budget import with_cpa_resource_pressure
    pressured = with_cpa_resource_pressure(call, 99, 12)
    call_json(pressured, 'same question JSON')
    assert len(call.calls) == 2
    call.completed_review_binding['audio'] = 'c' * 64
    call_json(pressured, 'same question JSON')
    assert len(call.calls) == 3


def test_agy_raw_replay_runs_current_decoder_without_fallback(tmp_path, monkeypatch):
    import subprocess
    from src.autoslice import agy_frame_witness as vision, structured_output
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls = []
    raw = json.dumps({'type': 'result', 'result': {'status': 'SUCCESS',
                     'structured_output': {'match': False}}})
    monkeypatch.setattr(vision.agy_gemini_client, 'agy_argv', lambda *a, **k: ['agy'])
    def run(*args, **kwargs):
        calls.append(args)
        return vision.agy_gemini_client.AgyRun(completed=subprocess.CompletedProcess(['agy'], 0, raw, ''),
                                             failure_category=None)
    monkeypatch.setattr(vision.agy_gemini_client, 'run_local_agy', run)
    monkeypatch.setattr(vision, '_gemini_vision_fallback', lambda *a, **k: pytest.fail('contract reasked on fallback'))
    first = vision._vision_qa({}, b'exact pixels', 'return JSON', timeout_seconds=10)
    assert first['status'] == 'OBSERVED' and json.loads(first['answer']) == {'match': False}
    before = {p: p.read_bytes() for p in (tmp_path / 'cache/completed-reviews').rglob('*.json')}
    def reject(_): raise ValueError('CURRENT_DECODER_REJECTED')
    monkeypatch.setattr(structured_output, 'parse_agy_stream', reject)
    replay = vision._vision_qa({}, b'exact pixels', 'return JSON', timeout_seconds=10)
    assert replay['status'] == 'UNAVAILABLE' and replay['failure_layer'] == 'CONTRACT_ERROR'
    assert replay['served_from_cache'] and len(calls) == 1
    assert all(p.read_bytes() == data for p, data in before.items())


def test_unattributed_legacy_cache_is_preserved_without_promoting_it_to_requested_model(tmp_path, monkeypatch):
    import hashlib
    from src.autoslice import exact_final_convergence as convergence
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    prompt = 'same convergence question JSON'
    legacy = tmp_path / 'legacy-parsed-only.json'
    legacy.write_text(json.dumps({'schema_version': convergence.CACHE_SCHEMA,
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'completion_sha256': 'a' * 64, 'payload': {'choice': 'unattributed old result'}}))
    original = legacy.read_bytes()
    monkeypatch.setattr(convergence, '_cache_path', lambda _: legacy)
    call = provider('{"choice":"NEITHER"}')
    for _ in range(2):
        payload, _, _ = convergence._load_or_call_judge(prompt, judge_llm_call=call)
        assert payload == {'choice': 'NEITHER'}
    assert len(call.calls) == 1 and legacy.read_bytes() == original


def test_remote_provider_binding_survives_deployment_path_normalization(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"decision":"REJECT"}')
    call.provider_runtime_binding = {'provider_transport': 'ssh_runtime_cpa',
        'provider_runtime_host': 'origin-a', 'provider_credential_source': '/versions/aaa/cpa.env'}
    call_json(call, 'same JSON question')
    call.provider_runtime_binding['provider_credential_source'] = '/versions/bbb/cpa.env'
    call_json(call, 'same JSON question')
    assert len(call.calls) == 1
    call.provider_runtime_binding['provider_runtime_host'] = 'origin-b'
    call_json(call, 'same JSON question')
    assert len(call.calls) == 2


def test_default_json_contract_error_is_engineering_failure_and_replays_original(tmp_path, monkeypatch):
    from src.autoslice.llm_client import LlmJsonParseError
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('not JSON')
    for _ in range(2):
        with pytest.raises(LlmJsonParseError) as error:
            call_json(call, 'same question JSON', validate=extract_json_object)
        assert error.value.reason_code == 'LLM_JSON_NO_OBJECT'
        assert error.value.failure_layer == 'CONTRACT_ERROR'
    assert len(call.calls) == 1


def test_vision_fallback_raw_cannot_hit_under_top_model(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from src.autoslice import cpa_frame_witness as vision
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setattr(vision, 'runtime_provider_slot', lambda **_: nullcontext())
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"model":"fallback","output_text":"REJECT"}'
    def send(request, **_):
        calls.append(request)
        return Response()
    monkeypatch.setattr(vision.urllib.request, 'urlopen', send)
    args = dict(question='same JSON question', api_base='https://offline.invalid/v1',
                api_key='fictional', timeout_seconds=10, max_tokens=100)
    for _ in range(2):
        result = vision._vision_qa({}, b'exact pixels', model='top', **args)
        assert result['answer'] == 'REJECT'
        assert result['model'] == 'fallback' and result['requested_model'] == 'top'
    assert len(calls) == 2
    assert vision._vision_qa({}, b'exact pixels', model='fallback', **args)['answer'] == 'REJECT'
    assert len(calls) == 2


def test_malformed_vision_wire_is_retained_and_current_decoder_rejects_once(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from src.autoslice import cpa_frame_witness as vision
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setattr(vision, 'runtime_provider_slot', lambda **_: nullcontext())
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'original malformed wire\xff'
    def send(request, **_):
        calls.append(request)
        return Response()
    monkeypatch.setattr(vision.urllib.request, 'urlopen', send)
    args = dict(question='same JSON question', api_base='https://offline.invalid/v1',
                api_key='fictional', model='top', timeout_seconds=10, max_tokens=100)
    for _ in range(2):
        result = vision._vision_qa({}, b'exact pixels', **args)
        assert result['status'] == 'UNAVAILABLE' and result['reason_code'] == 'VISION_CALL_FAILED'
        assert result['failure_layer'] == 'CONTRACT_ERROR'
    assert len(calls) == 1


def test_enclosing_stage_cache_carries_audio_but_not_implementation_into_raw_key(tmp_path, monkeypatch):
    from src.autoslice import transcription_stage_cache as stage
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'base'))
    call = provider('{"decision":"REJECT"}')
    media = tmp_path / 'ordinary.mp4'
    # review() must see the declared provider, just like its ordinary adapter.
    def wrapped(prompt): return call_json(call, prompt)
    wrapped.cpa_cache_identity = call.cpa_cache_identity
    def run(audio):
        cache = stage.TranscriptionStageCache(media, audio)
        result = cache.review(prompt='same JSON question', source_srt='same saved SRT', cue_count=1,
            llm_call=wrapped, validate=extract_json_object)
        return result, cache
    first, _ = run(b'first audio')
    second, _ = run(b'new audio')
    assert first == second == {'decision': 'REJECT'} and len(call.calls) == 2
    monkeypatch.setattr(stage, '_file_sha', lambda _: 'f' * 64)
    third, cache = run(b'new audio')
    assert third == second and len(call.calls) == 2
    assert cache.audit['cpa']['served_from_cache'] and cache.audit['cpa']['provider_calls'] == 0


def test_parent_retention_guard_is_not_bypassed_by_shared_raw_cache(tmp_path, monkeypatch):
    from src.autoslice import completed_review_checkpoint as checkpoint
    from src.autoslice.pronoun_stage_trace import apply_pronoun_pass
    from src.autoslice.source_context_executor import AgyRunnerError
    from src.autoslice.transcription_stage_cache import TranscriptionStageCache
    marker = 'FICTIONAL-CREDENTIAL-CANARY-DO-NOT-STORE'
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'base'))
    monkeypatch.setenv('CPA_API_KEY', marker)
    original_sha = checkpoint._sha
    def safe_sha(payload):
        assert marker.encode() not in payload, 'forbidden echo was hashed'
        return original_sha(payload)
    monkeypatch.setattr(checkpoint, '_sha', safe_sha)
    call = provider('not JSON ' + marker)
    call.cpa_cache_identity['transport'] = 'cpa_command'
    cache = TranscriptionStageCache(tmp_path / 'ordinary.mp4', b'audio')
    with pytest.raises(AgyRunnerError):
        apply_pronoun_pass('1\n00:00:00,000 --> 00:00:01,000\n她来了\n',
            cpa_llm_call=call, display_name='李豆沙', required=True, resume=cache)
    assert len(call.calls) == 1  # Local retries only reconsume the volatile original.
    assert not list((tmp_path / 'base/cache/completed-reviews').rglob('*.json'))
    assert all(marker.encode() not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())


def test_declined_parent_identity_cannot_be_overridden_by_generic_cache(tmp_path, monkeypatch):
    from src.autoslice.completed_review_checkpoint import ruling_material_scope
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"decision":"REJECT"}')
    with ruling_material_scope(None, cache_allowed=False):
        call_json(call, 'same JSON question')
    assert len(call.calls) == 1 and not (tmp_path / 'cache/completed-reviews').exists()
