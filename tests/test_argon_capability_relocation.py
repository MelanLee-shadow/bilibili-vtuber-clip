"""Relocate retained engines, not acquisitions: public consumers and fail-closed controls.

All hearing/metadata replies below are isolated synthetic protocols. No live
provider or credential is used; existing source-bound fixtures own the inputs.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.autoslice import argon_escalation_agy as native
from src.autoslice import argon_escalation_storage as store
from src.autoslice.argon_escalation_contract import KeyPolicy, canonical, sha, strict_json
from src.autoslice.argon_escalation_integration import (
    bind_evidence, material_identity, spec_from_envelope, transcript_witness,
    validate_consumer_evidence,
)
from src.autoslice.argon_escalation_witness import ArgonEscalationWitness, ProviderTransportError
from test_argon_consumer_integration import scene, offline
from tests.argon_admission_support import admission, SYNTHETIC_METADATA
from test_argon_escalation_witness import case, FakeProvider


@pytest.fixture
def tmp_path(tmp_path, request):
    artifacts = os.environ.get('AUTOSLICE_RELISTEN_TEST_ARTIFACTS')
    if artifacts:
        tmp_path = Path(artifacts) / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
        tmp_path.mkdir(parents=True, exist_ok=False)
    return tmp_path


def inventory(root):
    return {str(p.relative_to(root)): sha(p.read_bytes())
            for p in root.rglob('*') if p.is_file()}


def accounting_material(accounting):
    """A readback locator moves; the actual ledger bytes and usage must not."""
    value = deepcopy(accounting)
    for history in value['histories']:
        location = Path(history.pop('ledger_path'))
        assert sha(location.read_bytes()) == history['ledger_sha256']
    return value


def counts(scene):
    return (len(scene.provider.calls), len(scene.provider.metadata_calls),
            scene.loader_calls, scene.ordinary_calls, len(scene.prompts),
            len(scene.relocation_catalog), len(scene.relocation_audio))


def complete_public(scene, monkeypatch, route):
    """Enter through the existing producer factory and ordinary public finding."""
    scene.after_choice = 'NEITHER'
    scene.relocation_catalog, scene.relocation_audio = [], []
    monkeypatch.setattr(native, 'availability', lambda: ('/offline-protocol/agy', True))

    def catalog(argv, **kwargs):
        assert argv == ['/offline-protocol/agy', 'models']
        assert not (Path(kwargs['cwd']) / 'agy-input').exists()
        scene.relocation_catalog.append(argv)
        raw = (b'gemini-4-argon\tGemini 4 Argon\n' if route in {'native', 'api_quota'}
               else SYNTHETIC_METADATA['native']['stdout'].encode())
        return subprocess.CompletedProcess(argv, 0, raw, b'')

    def audio(argv, **kwargs):
        job = Path(kwargs['cwd'])
        prompt, audio_bytes = (job/'prompt.md').read_text(), (job/'input.wav').read_bytes()
        assert audio_bytes[:4] == b'RIFF' and argv[argv.index('--model')+1] == native.AGY_MODEL
        scene.relocation_audio.append({'argv': argv, 'audio_sha256': sha(audio_bytes)})
        if route == 'api_quota':
            return subprocess.CompletedProcess(argv, 1, '', 'quota exhausted')
        binding = json.loads(prompt[prompt.rfind('{'):])
        reply = {'schema_version': 'argon-blind-hearing.v1', **binding, 'status': 'OBSERVED',
                 'transcript': scene.provider.argon_text, 'uncertainties': [],
                 'speech_role': 'unknown', 'reason': None}
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply, ensure_ascii=False), '')

    monkeypatch.setattr(native, 'execute_model_catalog', catalog)
    monkeypatch.setattr(native, 'execute_command', audio)
    if route == 'api_second_key':
        scene.credentials['GEMINI_API_KEY_2'] = 'fictional-free-two'
        scene.provider.post_status = 429
    _, audit = scene.run()
    if route == 'api_second_key':
        scene.provider.post_status = 200
    # At most two remaining normal advances: quota successor, then retained GET.
    for _ in range(2):
        if 'argon_escalation_evidence' in audit.get('request', {}):
            break
        _, audit = scene.run(scene.build())
    request = audit['request']
    envelope = request['argon_escalation_evidence']
    result = validate_consumer_evidence(request, envelope)
    assert result['status'] == 'OBSERVED' and result['mutation_authorized'] is False
    if route == 'api_second_key':
        assert [c['route']['accepted_key_ordinal'] for c in scene.provider.calls
                if c['method'] == 'POST'] == [1, 2]
    return request, envelope, result


def relocate(scene, request, envelope):
    copied = scene.root / 'retained' / 'unrelated-depth' / 'engine-copy'
    copied.parent.mkdir(parents=True)
    shutil.copytree(envelope['engine_root'], copied)
    moved = deepcopy(envelope)
    moved['engine_root'] = str(copied)
    moved.pop('envelope_sha256')
    moved['envelope_sha256'] = sha(canonical(moved))
    other = deepcopy(request)
    other['proposed_candidates'] = [row for row in other['proposed_candidates']
                                    if row['source'].get('kind') != 'argon_target_transcript']
    return copied, moved, other


@pytest.mark.parametrize('route', ['api', 'native', 'api_quota', 'api_second_key'])
def test_public_relocation_preserves_identity_semantic_stop_and_readonly_cold(scene, monkeypatch, route):
    request, envelope, result = complete_public(scene, monkeypatch, route)
    copied, moved, other = relocate(scene, request, envelope)
    owner = Path(envelope['consumer_root'])
    before = (inventory(owner), inventory(copied), counts(scene))
    rebound = bind_evidence(other, moved)
    verified = validate_consumer_evidence(rebound, moved)
    assert material_identity(request, envelope) == material_identity(rebound, moved)
    assert verified['result_sha256'] == result['result_sha256']
    assert accounting_material(verified['accounting']) == accounting_material(result['accounting'])
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    from src.autoslice.acoustic_witness_adjudication import judge_word_choice
    recorder = SemanticAttemptRecorder(srt_text=scene.srt, finding=scene.finding,
                                       clip_context=scene.context, offset_ms=1000)
    verdict = judge_word_choice(check_request=rebound, witness=transcript_witness(rebound, moved),
                                llm_call=recorder.wrap(scene.judge))
    assert verdict['reason_code'] == 'JUDGE_NO_NEW_EVIDENCE' and recorder.plan is None
    assert before == (inventory(owner), inventory(copied), counts(scene))

    # A fresh interpreter must find the same original capability scope without
    # reading a live key, rewriting a seal, adding usage, or querying a provider.
    payload = scene.root / 'relocated-consumer.json'
    payload.write_text(json.dumps({'request': rebound, 'envelope': moved}, ensure_ascii=False))
    script = '''
import json, sys, os
from pathlib import Path
from src.autoslice import argon_escalation_runtime as runtime
from src.autoslice import argon_escalation_agy as native
from src.autoslice.argon_escalation_integration import validate_consumer_evidence, material_identity
from src.autoslice.argon_escalation_transport import ArgonHttpTransport
def forbidden(*a, **k): raise AssertionError('COLD_CALL_FORBIDDEN')
runtime.load_credentials = forbidden
native.execute_command = native.execute_model_catalog = forbidden
ArgonHttpTransport.request = ArgonHttpTransport.model_metadata = forbidden
v = json.loads(Path(sys.argv[1]).read_bytes())
r = validate_consumer_evidence(v['request'], v['envelope'])
assert os.getpid() != int(sys.argv[2])
print(json.dumps({'status':r['status'], 'result_sha256':r['result_sha256'],
 'material':material_identity(v['request'],v['envelope']), 'accounting':r['accounting'],
 'pid':os.getpid(), 'parent_pid':int(sys.argv[2])}))
'''
    process = subprocess.run([sys.executable, '-B', '-c', script, str(payload), str(os.getpid())],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True,
                             text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    cold = json.loads(process.stdout)
    assert cold['material'] == material_identity(request, envelope)
    assert cold['accounting'] == verified['accounting']
    assert accounting_material(cold['accounting']) == accounting_material(result['accounting'])
    assert before == (inventory(owner), inventory(copied), counts(scene))
    (scene.root/'relocation-cold.json').write_text(json.dumps({
        'route': route, 'cold': cold, 'original': envelope, 'relocated': moved,
        'semantic_stop': verdict['reason_code'], 'counts_before_after_equal': True,
        'original_and_copied_files_unchanged': True,
        'original_file_hashes': before[0], 'copied_file_hashes': before[1]}, ensure_ascii=False))


def tamper(scope, task, origin, envelope, drift):
    """Mutate isolated synthetic fixtures only; retained production evidence is never touched."""
    if drift in {'scope', 'seal', 'credential', 'missing_credential'}:
        name = {'scope':'routing-policy.json', 'seal':'routing-policy-seal.json',
                'credential':'credential-1.json', 'missing_credential':'credential-1.json'}[drift]
        p = scope / name
        if drift.startswith('missing'):
            p.unlink()
        else:
            value = store.get_json(p)
            if drift == 'scope':
                value['scope'] = 'comparison'
                p.write_bytes(canonical(value))
                seal = scope/'routing-policy-seal.json'
                seal.write_bytes(canonical({'schema_version':'argon-routing-policy-seal.v1',
                                             'routing_policy_sha256':sha(p.read_bytes())}))
            elif drift == 'seal':
                value['routing_policy_sha256'] = '0'*64
                p.write_bytes(canonical(value))
            else:
                value['binding'] = '0'*64
                p.write_bytes(canonical(value))
        return
    if drift.startswith('predecessor'):
        first = scope/'attempt-1/engine'/task.name
        if drift == 'predecessor_missing':
            (first/'capability-agy.json').unlink()
        else:
            pointer = store.get_json(first/'capability-agy.json')
            receipt = strict_json(store.get_blob(first, pointer['receipt_sha256']))
            (first/'blobs'/(receipt['raw_sha256']+'.bin')).write_bytes(b'corrupted predecessor raw')
        return
    if drift == 'original_request_identity':
        e = ArgonEscalationWitness(origin.parent, read_only=True)
        spec = spec_from_envelope(envelope)
        _, manifest, inner = e._load(origin.name, spec)
        ledger = e._ledger(origin, manifest, 'batch', e._body(origin.name, inner, 'batch'))
        value = store.get_json(ledger.path)
        value['events'][-1]['metadata']['different_original_execution'] = True
        value.pop('record_sha256')
        from src.autoslice.content_bound_request_ledger import _digest
        value['record_sha256'] = _digest(value)
        ledger.path.write_bytes(canonical(value))
        return
    if drift.startswith('native_'):
        name = {'native_audio':'input.wav', 'native_prompt':'prompt.md'}[drift]
        (task/'agy-input'/name).write_bytes(b'wrong retained native input')
        return
    mode = 'agy' if (task/'agy-run.json').exists() else 'api-batch'
    pointer_path = task/('capability-'+mode+'.json')
    pointer = store.get_json(pointer_path)
    receipt = strict_json(store.get_blob(task, pointer['receipt_sha256']))
    if drift == 'raw':
        (task/'blobs'/(receipt['raw_sha256']+'.bin')).write_bytes(b'corrupted copied raw')
    elif drift == 'intent':
        p = task/('capability-'+mode+'.intent.json')
        value = store.get_json(p); value['binding']['request_sha256'] = '0'*64
        p.write_bytes(canonical(value))
    elif drift == 'missing_capability':
        pointer_path.unlink()
    elif drift == 'model':
        receipt['binding']['requested_model'] = 'gemini-3.8-flash'
        receipt['binding_sha256'] = sha(canonical(receipt['binding']))
        pointer['binding_sha256'] = receipt['binding_sha256']
        pointer['receipt_sha256'] = store.blob(task, canonical(receipt))
        pointer_path.write_bytes(canonical(pointer))
    else:
        raise AssertionError(drift)


@pytest.mark.parametrize('route,drift', [
    ('api','scope'), ('api','seal'), ('api','credential'), ('api','missing_credential'),
    ('api','raw'), ('api','intent'), ('api','model'), ('api','missing_capability'),
    ('api','predecessor_missing'), ('api','predecessor_raw'), ('api','original_request_identity'),
    ('native','scope'), ('native','seal'), ('native','raw'), ('native','intent'),
    ('native','model'), ('native','missing_capability'), ('native','native_audio'), ('native','native_prompt'),
])
def test_relocated_public_consumer_keeps_every_capability_guard(scene, monkeypatch, route, drift):
    request, envelope, _ = complete_public(scene, monkeypatch, route)
    copied, moved, other = relocate(scene, request, envelope)
    # Establish the permitted input before introducing exactly one new fault.
    assert validate_consumer_evidence(bind_evidence(other, moved), moved)['status'] == 'OBSERVED'
    scope = Path(envelope['consumer_root'])/'batch'
    task, origin = copied/envelope['task_id'], Path(envelope['engine_root'])/envelope['task_id']
    tamper(scope, task, origin, envelope, drift)
    before = (inventory(Path(envelope['consumer_root'])), inventory(copied), counts(scene))
    with pytest.raises((ValueError, OSError, KeyError)):
        bind_evidence(other, moved)
    assert before == (inventory(Path(envelope['consumer_root'])), inventory(copied), counts(scene))


@pytest.mark.parametrize('route', ['api', 'native'])
def test_relocated_unknown_readback_does_not_dispatch_or_invent_usage(admission, tmp_path, route):
    s = admission
    if route == 'native':
        s.catalog_outcome, s.audio_unknown = 'supported', True
    else:
        s.provider.post_error = ProviderTransportError('HTTP_TIMEOUT')
    first = s.advance()
    assert first['result']['status'] == ('UNKNOWN' if route == 'native' else 'SUBMIT_UNKNOWN')
    copied = tmp_path/'unknown-copy'
    shutil.copytree(first['engine_root'], copied)
    before = (inventory(s.root), inventory(copied), len(s.provider.calls),
              len(s.provider.metadata_calls), len(s.catalog_calls), len(s.audio_calls), s.loader_calls)
    engine = ArgonEscalationWitness(copied, read_only=True)
    for _ in range(2):
        readback = engine.read_status(first['result']['task_id'], expected=s.spec, acquisition_root=s.root)
        assert readback['state'] in {'SUBMIT_DISPATCHED','SUBMIT_AMBIGUOUS'}
        assert readback['result'] is None
    with pytest.raises((ValueError, KeyError)):
        engine.validate_evidence(first['result']['task_id'], first['result'],
                                 expected=s.spec, acquisition_root=s.root)
    assert before == (inventory(s.root), inventory(copied), len(s.provider.calls),
                      len(s.provider.metadata_calls), len(s.catalog_calls), len(s.audio_calls), s.loader_calls)
    assert first['result']['accounting']['inference_reservations'] == 1


def test_original_v1_completed_copy_stays_readonly_without_new_capability(case, tmp_path):
    _, spec, _, _ = case
    root = tmp_path/'original-v1'
    engine = ArgonEscalationWitness(root/'attempt-1/engine')
    handle = engine.prepare(spec)
    provider = FakeProvider(handle)
    engine.advance(handle['task_id'], expected=spec, provider=provider, key_policy=KeyPolicy(1))
    result = engine.advance(handle['task_id'], expected=spec, provider=provider, key_policy=KeyPolicy(1))
    assert result['status'] == 'OBSERVED'
    store.put_json(root/'routing-policy.json', {'schema_version':'argon-routing-policy.v1',
        'scope':'production','comparison':False, 'comparison_identity':{
            'comparison_plan':None,'comparison_grant':None,'comparison_case_ids':None}})
    copied = tmp_path/'v1-copy'
    shutil.copytree(engine.root, copied)
    before = (inventory(root), inventory(copied), len(provider.calls))
    cold = ArgonEscalationWitness(copied, read_only=True)
    verified = cold.validate_evidence(handle['task_id'], result, expected=spec, acquisition_root=root)
    assert verified['result_sha256'] == result['result_sha256']
    assert before == (inventory(root), inventory(copied), len(provider.calls))
    assert not (root/'routing-policy-seal.json').exists() and not list(copied.rglob('capability-*.json'))
