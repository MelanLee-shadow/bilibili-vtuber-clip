"""Ordinary write/read regression checks; provider callables are offline fakes."""
import importlib.util
import json
from pathlib import Path

import pytest

from src.autoslice import nested_media_acoustic_observer as observer
from src.autoslice import nested_media_acoustic_runtime as runtime
from src.autoslice import agy_three_part_acoustic as agy
from tests.test_nested_media_acoustic_runtime import _build, _run, _observer_json, _cpa_json
from tests.test_nested_media_acoustic_observer import _inputs
from tests.test_agy_three_part_acoustic import _descriptor, _patch_local_tools
from tests.test_producer_nested_acoustic import _prepared, _run as _producer_run
from src.autoslice.producer_nested_acoustic import current_caption_audio_roles


def _snapshot(root):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file() and p.name != 'observer.lock'}


def _callbacks(module=runtime, **overrides):
    args = dict(audio_generate=lambda **kw: _observer_json(), cpa_call=lambda p: _cpa_json(),
                cpa_call_identity={'transport': 'cpa', 'models': ['judge'], 'effort': 'medium'},
                provider='gemini', model='audio-v1', transport='api',
                transport_module='module', transport_module_sha256='sha256:'+'a'*64)
    args.update(overrides)
    return module.build_runtime_acoustic_callbacks(**args)


def test_module_bytes_and_deployment_location_do_not_change_identity(tmp_path):
    original = _callbacks()
    source = Path(runtime.__file__).read_text() + '\n# Unrelated disabled feature.\n'
    path = tmp_path/'relocated_runtime.py'
    path.write_text(source)
    spec = importlib.util.spec_from_file_location('src.autoslice.relocated_runtime', path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert original.runtime_identity == _callbacks(module).runtime_identity
        changed = _callbacks(module, transport_module='other.module',
                             transport_module_sha256='sha256:'+'b'*64)
        assert original.runtime_identity == changed.runtime_identity
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize('field,value', [
    ('provider','other'), ('model','other'), ('transport','other'),
    ('observer_prompt','changed observer prompt'), ('cpa_prompt','changed CPA prompt'),
    ('cpa_call_identity', {'transport':'cpa','models':['other'],'effort':'medium'}),
])
def test_wire_protocol_changes_identity(field, value):
    assert _callbacks().runtime_identity != _callbacks(**{field:value}).runtime_identity


@pytest.mark.parametrize('name', ['RESPONSE_ADAPTER_CONTRACT_VERSION', 'OBSERVER_SCHEMA', 'CPA_SCHEMA'])
def test_explicit_contract_and_schema_change_identity(monkeypatch, name):
    before = _callbacks().runtime_identity
    monkeypatch.setattr(runtime, name, getattr(runtime,name)+'.changed')
    assert before != _callbacks().runtime_identity


def test_byte_only_change_replays_ordinary_receipt_readonly(tmp_path, monkeypatch):
    values = _inputs(tmp_path)
    values.update(candidate_id='unseen-candidate', observation_id='unseen-cue')
    first, calls, _ = _build(values, observer_response=_observer_json(), cpa_response=_cpa_json())
    result = _run(values, first)
    before = _snapshot(Path(values['evidence_root']))
    changed_module = tmp_path/'changed-runtime.py'
    changed_module.write_text(Path(runtime.__file__).read_text()+'\n# Unrelated comment.\n')
    monkeypatch.setattr(runtime, '__file__', str(changed_module))
    second, later_calls, _ = _build(values, observer_response=_observer_json(), cpa_response=_cpa_json())
    replayed = _run(values, second)
    assert replayed['status'] == 'PASS'
    assert replayed['request_sha256'] == result['request_sha256']
    assert later_calls == {'observer':0, 'cpa':0}
    assert _snapshot(Path(values['evidence_root'])) == before


def test_legacy_receipt_replays_without_resigning_and_rejects_protocol_change(tmp_path, monkeypatch):
    # Emit the historical identity through the actual ordinary writer. Only the
    # representation is old; every persisted binding comes from its producer.
    values = _inputs(tmp_path)
    callbacks, _, _ = _build(values, observer_response=_observer_json(), cpa_response=_cpa_json())
    modern = dict(callbacks.runtime_identity)
    legacy = {k:v for k,v in modern.items() if k not in (
        'identity_sha256','response_adapter_contract_version','observer_schema','cpa_schema')}
    legacy.update(transport_module='old.module', transport_module_sha256='sha256:'+'1'*64,
                  response_adapter_sha256='sha256:'+'2'*64)
    legacy = observer._runtime_identity(legacy)
    legacy_callbacks = runtime.RuntimeAcousticCallbacks(
        observe_audio=lambda req: callbacks.observe_audio({**req,'runtime_identity':modern}),
        cpa_judge=lambda req, obs: callbacks.cpa_judge({**req,'runtime_identity':modern},obs),
        runtime_identity=legacy)
    original = _run(values, legacy_callbacks)
    assert original['status'] == 'PASS'
    before = _snapshot(Path(values['evidence_root']))
    second, calls, _ = _build(values, observer_response=_observer_json(), cpa_response=_cpa_json())
    replayed = _run(values, second)
    assert replayed['status'] == 'PASS'
    assert replayed['request_sha256'] == original['request_sha256']
    assert calls == {'observer':0,'cpa':0}
    assert before == _snapshot(Path(values['evidence_root']))
    for field in ('provider','model','transport','observer_prompt_sha256','cpa_prompt_sha256',
                  'response_adapter_contract_version','observer_schema','cpa_schema'):
        changed_identity = dict(second.runtime_identity)
        changed_identity.pop('identity_sha256')
        changed_identity[field] = ('sha256:'+'f'*64 if field.endswith('_sha256')
                                   else changed_identity[field]+'.changed')
        request = json.loads(Path(original['request_path']).read_text())
        request['runtime_identity'] = observer._runtime_identity(changed_identity)
        assert observer._compatible_receipt_request(Path(original['request_dir']).parent, request) is None


def test_agy_byte_change_keeps_prepared_cache_and_schema_change_does_not(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    args = dict(descriptor=descriptor, candidate_id='candidate', observation_id='cue-13',
                evidence_root=tmp_path/'evidence')
    first = agy.prepare_three_part_attachments(**args)
    before = _snapshot(first.job_dir)
    changed_module = tmp_path/'changed-agy.py'
    changed_module.write_text(Path(agy.__file__).read_text()+'\n# Unrelated disabled branch.\n')
    monkeypatch.setattr(agy, '__file__', str(changed_module))
    second = agy.prepare_three_part_attachments(**args)
    assert first.job_dir == second.job_dir
    assert before == _snapshot(first.job_dir)
    monkeypatch.setattr(agy, '_structured_schema_text', lambda: '{"changed":true}\n')
    third = agy.prepare_three_part_attachments(**args)
    assert third.job_dir != first.job_dir


@pytest.mark.parametrize('field', ['transport_contract_version','attachment_prompt_sha256','model','wire_schema_sha256'])
def test_transport_protocol_guard(field):
    binding = {'observer_transport':'agy_three_part','agy':{'model':'original'}}
    before = observer._transport_protocol(binding)
    if field == 'model':
        binding['agy']['model'] = 'changed'
    else:
        binding[field] = 'changed'
    assert before != observer._transport_protocol(binding)


def test_deployment_audit_is_separate_from_protocol_and_unknown_fields_are_guards():
    binding = {'observer_transport':'agy_three_part','agy':{'model':'model','binary':'/old/agy'},
               'ffmpeg':{'binary':'/old/ffmpeg','conversion':'black mp4'},'job_dir':'/old/job',
               'adapter_source_sha256':'sha256:'+'1'*64}
    before = observer._transport_protocol(binding)
    binding.update(job_dir='/new/job', adapter_source_sha256='sha256:'+'2'*64)
    binding['agy']['binary'] = '/new/agy'
    assert before == observer._transport_protocol(binding)
    binding['future_protocol_field'] = 'changed'
    assert before != observer._transport_protocol(binding)


def test_modern_identity_cannot_reintroduce_legacy_source_fingerprints():
    identity = _callbacks().runtime_identity
    enriched = {**identity,'transport_module':'different.module',
                'transport_module_sha256':'sha256:'+'1'*64,
                'response_adapter_sha256':'sha256:'+'2'*64}
    assert observer._runtime_identity(enriched) == identity


def test_modern_identity_normalizes_full_transport_audit_at_the_shared_boundary():
    binding = {'observer_transport':'agy_three_part','agy':{'model':'fixture','binary':'/old/agy'},
               'ffmpeg':{'conversion':'black mp4','binary':'/old/ffmpeg'},
               'job_dir':'/old/job','adapter_source_sha256':'sha256:'+'1'*64}
    identity = _callbacks(transport_binding=binding).runtime_identity
    enriched = {**identity,'transport_binding':{**binding,'job_dir':'/new/job',
                'adapter_source_sha256':'sha256:'+'2'*64}}
    assert observer._runtime_identity(enriched) == identity
    enriched['transport_binding']['future_protocol_field'] = 'changed'
    with pytest.raises(observer.NestedMediaAcousticObserverError,match='RUNTIME_IDENTITY_INVALID'):
        observer._runtime_identity(enriched)


def test_legacy_adapter_version_is_fixed_when_current_contract_advances(tmp_path, monkeypatch):
    before = _callbacks().runtime_identity
    monkeypatch.setattr(runtime, 'RESPONSE_ADAPTER_CONTRACT_VERSION', 'nested-acoustic-response.v2')
    assert observer._protocol_identity(before) != observer._protocol_identity(_callbacks().runtime_identity)


def test_old_receipt_cannot_rebind_media_or_window_and_corruption_is_rejected(tmp_path):
    values = _inputs(tmp_path)
    first, _, _ = _build(values, observer_response=_observer_json(), cpa_response=_cpa_json())
    result = _run(values, first)
    request = json.loads(Path(result['request_path']).read_text())
    directory = Path(result['request_dir']).parent
    changed = json.loads(json.dumps(request))
    changed['interval_ms'] = [101,201]
    assert observer._compatible_receipt_request(directory,changed) is None
    changed = json.loads(json.dumps(request))
    changed['audio_parts'][2]['sha256'] = 'sha256:'+'f'*64
    assert observer._compatible_receipt_request(directory,changed) is None
    path = Path(result['observer_response_path'])
    payload = json.loads(path.read_text())
    payload['source_media_speech'] = 'ABSENT'
    path.write_text(json.dumps(payload))
    with pytest.raises(observer.NestedMediaAcousticObserverError, match='CACHED_RECEIPT_INVALID'):
        _run(values,first)


def test_legacy_inflight_request_is_not_redispatched(tmp_path):
    values = _inputs(tmp_path)
    callbacks, _, _ = _build(values, observer_response=_observer_json(), cpa_response=_cpa_json())
    legacy = {k:v for k,v in callbacks.runtime_identity.items() if k not in (
        'identity_sha256','response_adapter_contract_version','observer_schema','cpa_schema')}
    legacy.update(transport_module='old.module',transport_module_sha256='sha256:'+'1'*64,
                  response_adapter_sha256='sha256:'+'2'*64)
    def interrupted(_request):
        raise KeyboardInterrupt('simulated crash after request persistence')
    old_callbacks = runtime.RuntimeAcousticCallbacks(interrupted, callbacks.cpa_judge,
                                                    observer._runtime_identity(legacy))
    with pytest.raises(KeyboardInterrupt):
        _run(values,old_callbacks)
    before = _snapshot(Path(values['evidence_root']))
    second, calls, _ = _build(values,observer_response=_observer_json(),cpa_response=_cpa_json())
    result = _run(values,second)
    assert result['reason_code'] == 'PRIOR_DISPATCH_INCOMPLETE'
    assert result['status'] == 'EVIDENCE_ONLY'
    assert calls == {'observer':0,'cpa':0}
    assert before == _snapshot(Path(values['evidence_root']))


def test_producer_persistence_and_role_consumer_survive_byte_only_deployment(tmp_path, monkeypatch):
    values, _, spec = _prepared(tmp_path)
    scope = {'target_interval_ms':[100,200],'target_cue_indexes':[1],
             'target_audio_offset_ms':[0,100]}
    raw_observer = json.dumps({**json.loads(_observer_json()),**scope})
    raw_cpa = json.dumps({**json.loads(_cpa_json()),**scope})
    first, _, _ = _build(values,observer_response=raw_observer,cpa_response=raw_cpa)
    audit = _producer_run(values,first,spec)
    assert audit['observations'][0]['observer_result']['status'] == 'PASS'
    old_sha = audit['observations'][0]['observer_result']['request_sha256']
    receipt_dir = Path(audit['observations'][0]['observer_result']['request_dir'])
    before = _snapshot(receipt_dir)
    original_srt = values['final_srt_path'].read_bytes()
    changed = tmp_path/'new-deployment.py'
    changed.write_text(Path(runtime.__file__).read_text()+'\n# Deployment-only change.\n')
    monkeypatch.setattr(runtime,'__file__',str(changed))
    second, calls, _ = _build(values,observer_response=raw_observer,cpa_response=raw_cpa)
    replay = _producer_run(values,second,spec)
    assert replay['observations'][0]['observer_result']['request_sha256'] == old_sha
    assert calls == {'observer':0,'cpa':0}
    roles = current_caption_audio_roles(candidate_id='candidate',source_media=values['outer_media_path'],
        subtitle_path=values['final_srt_path'],acoustic_audit=replay)
    assert roles['rows'][0]['role'] == 'source_media'
    assert roles['rows'][0]['action'] == 'KEEP'
    assert before == _snapshot(receipt_dir)
    assert values['final_srt_path'].read_bytes() == original_srt
