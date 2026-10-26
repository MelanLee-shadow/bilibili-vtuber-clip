from src.autoslice.review_errors import ContractRejection, ProviderFailure, review_error_record
"""Physical acquisition sharing and immutable winner replay, with no model calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest

from src.autoslice import native_context_witness as context
from src.autoslice import native_foreign_witness as native
from src.autoslice import targeted_relisten as materials
from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
from src.autoslice.local_asr_target_evidence import MODELS, digest
from src.autoslice.supplement_audio_budget import get_budget, start_budget
from tests.test_native_context_witness import _range_requests


def _sender(monkeypatch, calls, *, delay=0):
    from src.autoslice import diarized_transcription as transport
    monkeypatch.setattr(native, '_extract_exact_mp3',
        lambda source, output, start_ms, end_ms: output.write_bytes(b'fixed-exact-audio'))
    def send(audio, *, provider, duration_ms, before_request):
        before_request()
        calls.append(provider)
        serial = len(calls)
        time.sleep(delay)
        return {'provider': provider, 'model': MODELS[provider], 'status': 'OK',
            'input_audio_sha256': hashlib.sha256(audio).hexdigest(),
            'response_sha256': hashlib.sha256(str(serial).encode()).hexdigest(),
            'raw_response': {'text': '整句范围听写', 'serial': serial},
            'native_segments': [{'start_ms': 0, 'end_ms': 1000, 'text': '整句范围听写'}],
            'native_timeline': {'valid': True}, 'elapsed_seconds': serial / 10}
    monkeypatch.setattr(transport, 'transcribe_evidence', send)


def _wrapper(source, output, providers=('moss',), receipt=None):
    return context.build_independent_context_witnesses(lambda req: {}, source, output,
        providers, budget_receipt_path=receipt, native_observation_cache_root=output.parent)


@pytest.mark.parametrize('provider', ['mai', 'moss'])
def test_parallel_targets_share_one_send_and_validated_original(tmp_path, monkeypatch, provider):
    source = tmp_path / 'source.mp4'; source.write_bytes(b'synthetic-source')
    start_budget(source)
    calls = []; _sender(monkeypatch, calls, delay=.15)
    owner = _wrapper(source, tmp_path / 'independent', (provider,), tmp_path / 'budget.json')
    check, broad = _range_requests(hashlib.sha256(source.read_bytes()).hexdigest())
    from src.autoslice.jingting_chunker import parse_srt_cues
    cues = parse_srt_cues(check['whole_clip_current_srt'])
    start = threading.Barrier(3)
    def run(index):
        current = deepcopy(check); cue=cues[index]
        current.update(cue_indexes=[index+1], matched_start_ms=cue.start_ms,
            matched_end_ms=cue.end_ms, current_cue=cue.text, evidence_id=f'narrow-{index}')
        request = deepcopy(broad); request['evidence_id'] = f'target-{index}'
        request.pop('request_sha256'); request['request_sha256'] = digest(request)
        start.wait(timeout=5)
        return current, owner.observe_range_context(check_request=current, range_witness_request=request)[0]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(3)))
    rows=[row for current,row in results]
    assert calls == [provider], json.dumps(rows, ensure_ascii=False)
    assert all(row['status'] == 'OBSERVED' for row in rows), rows
    for current,row in results:
        valid, reason = validate_range_acoustic_observation(current['whole_clip_current_srt'], current, row)
        assert valid is not None, reason
    assert len({row['artifact_bindings']['provider_receipt_sha256'] for row in rows}) == 1
    assert get_budget(source).snapshot()['attempt_count'] == 1


def test_waiter_does_not_hold_budget_transaction(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.write_bytes(b'source')
    start_budget(source)
    calls = []; _sender(monkeypatch, calls, delay=.25)
    observer = native.build_native_foreign_witness(source_media=source, output_dir=tmp_path/'out', provider='moss')
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(observer, start_ms=1000, end_ms=3000)
        limit = time.monotonic() + 5
        while not calls and time.monotonic() < limit: time.sleep(.005)
        waiter = pool.submit(observer, start_ms=1000, end_ms=3000)
        def acquire():
            budget = get_budget(source)
            assert budget.lock.acquire(timeout=.1), 'waiter holds budget lock during HTTP'
            budget.lock.release()
        pool.submit(acquire).result(timeout=2)
        assert first.result()['response_sha256'] == waiter.result()['response_sha256']
    assert calls == ['moss']



def test_parallel_failed_send_is_shared_without_a_second_dispatch(tmp_path, monkeypatch):
    from src.autoslice import diarized_transcription as transport
    from src.autoslice.diarized_transcription import DiarizedTranscriptionError
    source=tmp_path/'source'; source.write_bytes(b'source')
    start_budget(source)
    calls=[]; _sender(monkeypatch,calls)
    def fail(audio, *, before_request, **kwargs):
        before_request(); calls.append('moss'); time.sleep(.15)
        raise DiarizedTranscriptionError('MOSS_HTTP_ERROR','offline failure',{'http_status':503})
    monkeypatch.setattr(transport,'transcribe_evidence',fail)
    observers=[native.build_native_foreign_witness(source_media=source,output_dir=tmp_path/'out',provider='moss') for _ in range(4)]
    barrier=threading.Barrier(4)
    def run(i):
        barrier.wait(timeout=5)
        with pytest.raises(DiarizedTranscriptionError) as exc:
            observers[i](start_ms=1000,end_ms=3000)
        return exc.value.reason_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run,range(4)))==['MOSS_HTTP_ERROR']*4
    assert calls==['moss']
    assert get_budget(source).snapshot()['attempt_count']==1


def test_legacy_caller_holding_budget_returns_to_owner_instead_of_waiting(tmp_path,monkeypatch):
    source=tmp_path/'source';source.write_bytes(b'legacy-source')
    start_budget(source);calls=[];_sender(monkeypatch,calls,delay=.2)
    observe=native.build_native_foreign_witness(source_media=source,output_dir=tmp_path/'out',provider='moss')
    with ThreadPoolExecutor(max_workers=1) as pool:
        active=pool.submit(observe,start_ms=1000,end_ms=3000)
        until=time.monotonic()+5
        while not calls and time.monotonic()<until:time.sleep(.005)
        assert calls
        with get_budget(source).lock:
            with pytest.raises(ValueError,match='NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED'):
                observe(start_ms=1000,end_ms=3000)
        assert active.result(timeout=3)['status']=='OBSERVED'
    assert calls==['moss']

def _conflicted(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.write_bytes(b'synthetic-source')
    start_budget(source)
    calls = []; _sender(monkeypatch, calls)
    publish = native.publish_directory
    def late_conflict(work, job):
        publish(work, job)
        raise ContractRejection('IMMUTABLE_ARTIFACT_CONFLICT',
                                material_recovery={'kind':'ARTIFACT_REPLAY_REQUIRED'})
    monkeypatch.setattr(native, 'publish_directory', late_conflict)
    owner = _wrapper(source, tmp_path/'independent', receipt=tmp_path/'budget.json')
    check, broad = _range_requests(hashlib.sha256(source.read_bytes()).hexdigest())
    row = owner.observe_range_context(check_request=check, range_witness_request=broad)[0]
    assert row['status'] == 'UNCERTAIN'
    assert calls == ['moss']
    # A different unresolved dispatch must survive replay without being sealed.
    get_budget(source).consume('mai', MODELS['mai'], 20000, 21000)
    native._persist_budget_receipt(source_media=source, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), receipt_path=tmp_path/'budget.json')
    monkeypatch.setattr(native, 'publish_directory', publish)
    monkeypatch.setattr(native, 'build_native_foreign_witness', lambda **kw: pytest.fail('replay attempted provider construction'))
    return source, check, broad, calls


def test_cold_conflict_replay_preserves_original_envelope_and_budget(tmp_path, monkeypatch):
    source, check, broad, calls = _conflicted(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    cold = _wrapper(source, tmp_path/'independent', receipt=tmp_path/'budget.json')
    rows = cold.observe_range_context(check_request=check, range_witness_request=broad, cache_only=True)
    assert rows[0]['status'] == 'OBSERVED', rows
    assert rows[0]['acquisition_replay']['new_provider_calls'] == 0
    assert rows[0]['acquisition_replay']['original_envelope_sha256']
    valid, reason = validate_range_acoustic_observation(check['whole_clip_current_srt'], check, rows[0])
    assert valid is not None, reason
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    assert calls == ['moss']
    assert json.loads((tmp_path/'budget.json').read_bytes())['budget']['pending_attempt_count']==1


@pytest.mark.parametrize('bad', ['missing', 'malformed', 'schema_failure', 'typed_schema_failure', 'conflicting_typed_failure', 'source', 'clock', 'budget', 'budget_identity'])
def test_cold_replay_rejects_missing_malformed_and_binding_drift(tmp_path, monkeypatch, bad):
    source, check, broad, calls = _conflicted(tmp_path, monkeypatch)
    if bad == 'missing':
        next(tmp_path.glob('native-exact/moss/*/input.mp3')).unlink()
    elif bad == 'malformed':
        next(tmp_path.glob('native-exact/moss/*/*.json')).write_text('{broken')
    elif bad in {'schema_failure', 'typed_schema_failure', 'conflicting_typed_failure'}:
        path = next((tmp_path/'independent/moss/native-context').glob('*.json'))
        doc = json.loads(path.read_bytes()); doc['witness']['reason_code'] = 'HEARING_SCHEMA_INVALID'
        if bad != 'schema_failure':
            doc['witness']['review_error'] = review_error_record(ProviderFailure(
                'HEARING_SCHEMA_INVALID', provider='moss', material_recovery=(
                    {'kind':'ARTIFACT_REPLAY_REQUIRED'} if bad == 'conflicting_typed_failure' else None)))
        path.write_text(json.dumps(doc))
    elif bad == 'source':
        source.write_bytes(b'other-source')
    elif bad == 'clock':
        check['source_media_timeline_offset_ms'] += 1
    elif bad=='budget_identity':
        wrong = start_budget(source)
        token=wrong.consume('moss',MODELS['moss'],22000,23000)
        wrong.finish_attempt(token,status='OBSERVED')
        other=tmp_path/'other-budget.json'
        native._persist_budget_receipt(source_media=source,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),receipt_path=other)
        (tmp_path/'budget.json').write_bytes(other.read_bytes())
    else:
        receipt = tmp_path/'budget.json'
        doc = json.loads(receipt.read_bytes()); doc['source_media_sha256'] = '0'*64
        receipt.write_text(json.dumps(doc))
    cold = _wrapper(source, tmp_path/'independent', receipt=tmp_path/'budget.json')
    rows = cold.observe_range_context(check_request=check, range_witness_request=broad, cache_only=True)
    assert rows and all(row['status'] != 'OBSERVED' for row in rows)
    if bad in {'schema_failure', 'typed_schema_failure', 'conflicting_typed_failure'}:
        assert rows[0]['reason_code']=='HEARING_SCHEMA_INVALID'
    assert calls == ['moss']


def test_existing_unknown_attempt_resumes_owner_without_purchase(tmp_path, monkeypatch):
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    rig = build_offline_rig(tmp_path, monkeypatch)
    # Acquire the real original range first; the UNKNOWN logical caller only
    # lost its return. Recovery is allowed to consume these retained files.
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    broad = build_witness_request(rig.check)
    broad.pop('syllable_count_hint', None); broad.pop('request_sha256', None)
    from src.autoslice.jingting_chunker import parse_srt_cues
    cues = parse_srt_cues(rig.srt)
    broad.update(cue_indexes=list(range(1,len(cues)+1)), matched_start_ms=cues[0].start_ms,
        matched_end_ms=cues[-1].end_ms, context_start_ms=cues[0].start_ms,
        context_end_ms=cues[-1].end_ms, evidence_id=digest({'physical_target':materials.physical_target(rig.check),
        'channel':'WIDE_CANDIDATE_BLIND_AUDIO'}))
    broad['request_sha256']=digest(broad)
    saved = rig.verifier.observe_range_context(check_request=rig.check, range_witness_request=broad, provider='gemini')
    sends_before = len(rig.transport.calls)
    calls = []
    class Owner:
        def relisten_budget_state(self): return rig.verifier.relisten_budget_state()
        def observe_range_context(self, **kwargs):
            calls.append(kwargs.get('cache_only', False))
            if not kwargs.get('cache_only'): raise TimeoutError('unknown dispatch')
            assert kwargs['range_witness_request']==broad
            return saved
    Owner.observe_range_context.available_providers=('gemini',)
    finding = deepcopy(rig.finding)
    with pytest.raises(ProviderFailure, match='OWNER_RECOVERY_REQUIRED'):
        materials.complete_target_materials(check=rig.check, finding=finding, adapters={'entity_verifier': Owner()})
    original = deepcopy(finding['target_material_attempts'][0])
    updated = materials.complete_target_materials(check=rig.check, finding=finding, adapters={'entity_verifier': Owner()})
    assert calls == [False, True]
    assert len(rig.transport.calls)==sends_before
    assert finding['target_material_attempts'][0] == original
    assert materials.target_material_exhaustion(updated)['exhausted']


@pytest.mark.parametrize('kind', ['IN_FLIGHT_OR_UNKNOWN', 'ARTIFACT_REPLAY_REQUIRED', 'TRANSPORT_OR_SCHEMA_FAILURE'])
def test_material_failure_is_pending_but_local_contract_error_is_engineering(tmp_path, monkeypatch, kind):
    from tests.test_exact_final_funnel import run_terminal, unresolved, SRT
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    seed = {**unresolved(), 'suggestion':'呀', 'proposed_full_cue':'我喜欢她呀'}
    rig = build_offline_rig(tmp_path, monkeypatch, srt=SRT, finding=seed, source_duration_ms=4000)
    from src.autoslice import exact_final_terminal_funnel as funnel
    def fail(**kwargs):
        err = ProviderFailure('TARGET_MATERIAL_CHANNEL_OWNER_RECOVERY_REQUIRED:WIDE_CANDIDATE_BLIND_AUDIO', provider='moss')
        err.material_recovery = {'kind': kind, 'new_model_call_authorized': False}
        raise err
    monkeypatch.setattr(funnel, 'complete_target_materials', fail, raising=False)
    monkeypatch.setattr(materials, 'complete_target_materials', fail)
    result = run_terminal([seed], lambda _: pytest.fail('judge before audio'), terminal_rig=rig)
    assert result['release_gate'] == 'BLOCK'
    assert 'FINAL_REVIEW_ENGINEERING_FAILURE' not in result['reason_codes'], result
    assert 'FINAL_REVIEW_FINDING_REVIEW_PENDING' in result['reason_codes']
    def broken(**kwargs): raise ContractRejection('NATIVE_RANGE_SOURCE_OR_SRT_DRIFT')
    monkeypatch.setattr(materials, 'complete_target_materials', broken)
    monkeypatch.setattr(funnel, 'complete_target_materials', broken)
    local = run_terminal([seed], lambda _: pytest.fail('judge before audio'), terminal_rig=rig)
    assert local['release_gate'] == 'BLOCK'
    assert 'FINAL_REVIEW_ENGINEERING_FAILURE' in local['reason_codes']



@pytest.mark.parametrize('status,code,detail,kind',[
    ('UNKNOWN','TRANSPORT_UNKNOWN','', 'IN_FLIGHT_OR_UNKNOWN'),
    ('UNCERTAIN','HEARING_SCHEMA_INVALID','', 'TRANSPORT_OR_SCHEMA_FAILURE'),
    ('UNCERTAIN','NATIVE_OBSERVER_FAILED','IMMUTABLE_ARTIFACT_CONFLICT', 'ARTIFACT_REPLAY_REQUIRED'),
    ('UNCERTAIN','NATIVE_OBSERVER_FAILED','NATIVE_COMPLETED_CACHE_DRIFT', None),
])
def test_owner_diagnostics_distinguish_actual_recovery_from_local_errors(tmp_path,monkeypatch,status,code,detail,kind):
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    rig=build_offline_rig(tmp_path,monkeypatch)
    def observe(**kwargs):
        failure = (ContractRejection(code) if kind is None else
            ProviderFailure(code, provider='moss', material_recovery={'kind':kind}))
        return [{'status':status,'reason_code':code,'reason_detail':detail,
                 'review_error':review_error_record(failure)}]
    observe.available_providers=('native',)
    monkeypatch.setattr(rig.verifier,'observe_range_context',observe)
    with pytest.raises(ContractRejection if kind is None else ProviderFailure) as failure:
        materials.complete_target_materials(check=rig.check,finding=deepcopy(rig.finding),adapters={'entity_verifier':rig.verifier})
    if kind is None:
        assert str(failure.value).startswith('TARGET_MATERIAL_CHANNEL_LOCAL_CONTRACT_ERROR:')
        assert failure.value.material_recovery is None
    else:
        assert failure.value.material_recovery['kind']==kind
        assert failure.value.material_recovery['new_model_call_authorized'] is False

def test_terminal_pending_does_not_become_contract_error():
    from src.autoslice.finding_lifecycle import classify
    view = classify({'exact_release_adjudication': {'status': 'ARGON_PENDING'}}, terminal=True)
    assert view['state'] == 'AWAITING_EVIDENCE'
    assert view['release_action'] == 'CONTINUE_REVIEW'
    unknown = classify({'exact_release_adjudication':{'status':'UNKNOWN'}}, terminal=True)
    assert unknown['state']=='CALL_FAILURE'
    assert unknown['release_action']=='CONTINUE_REVIEW'


def test_recorded_xiada_winners_and_loser_envelopes_reach_terminal_admission(tmp_path, monkeypatch):
    """Run separately on OCI3 with read permission to the sealed source path.

    The source path is part of the original budget identity. Do not rewrite
    that receipt to make a relocated synthetic source appear to be its owner.
    """
    import os
    import shutil
    recorded = Path('/home/user/vtuber-ci/native-singleflight-20261024/recorded.json')
    if os.geteuid() != 0 or not recorded.is_file():
        pytest.skip('sealed production source read permission required; separate OCI3 replay')
    packet = json.loads(recorded.read_bytes())
    source = Path(packet['source'])
    originals = recorded.parent/'original'
    for name in ('native-exact', 'independent-context'):
        shutil.copytree(originals/name, tmp_path/name)
    shutil.copy2(originals/'native-audio-budget.json', tmp_path/'budget.json')
    initial = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(native, 'build_native_foreign_witness', lambda **kw: pytest.fail('production replay dispatched'))
    monkeypatch.setattr(native, '_extract_exact_mp3', lambda *a, **kw: pytest.fail('production replay extracted'))
    owner = _wrapper(source, tmp_path/'independent-context', ('mai','moss'), tmp_path/'budget.json')
    expected_requests={attempt['request']['request_sha256'] for row in packet['findings']
        for attempt in row.get('target_material_attempts',[])}
    envelopes = [p for p in sorted((tmp_path/'independent-context/mai/native-context').glob('*.json'))
        if p.stem in expected_requests]
    assert len(envelopes) == 2
    from src.autoslice.exact_final_terminal_funnel import review_pass_receipt
    seen = []
    for path in envelopes:
        envelope = json.loads(path.read_bytes())
        broad = envelope['request']
        # Select the real retained current-check request, preserving its source,
        # clock and SRT. The envelope's ID belongs to cue 6 or cue 42.
        selected = next(row for row in packet['findings'] if any(
            attempt.get('request', {}).get('request_sha256') == path.stem
            for attempt in row.get('target_material_attempts', [])))
        matching = [check for check in packet['checks'] if
            check['source_media_timeline_offset_ms'] == broad['source_media_timeline_offset_ms']
            and check['cue_indexes']==[selected['cue_index']]]
        assert matching
        check = deepcopy(matching[0])
        rows = owner.observe_range_context(check_request=check, range_witness_request=broad, cache_only=True)
        assert len(rows) == 2 and all(row['status']=='OBSERVED' for row in rows), rows
        for row in rows:
            valid, reason = validate_range_acoustic_observation(check['whole_clip_current_srt'], check, row)
            assert valid is not None, reason
            assert row['acquisition_replay']['new_provider_calls']==0
            assert row['acquisition_replay']['new_audio_ms']==0
        resumed_finding = deepcopy(selected)
        check = materials.complete_target_materials(check=check, finding=resumed_finding,
            adapters={'entity_verifier':owner})
        assert resumed_finding['target_material_attempts'][-1]['recovery_only'] is True
        assert resumed_finding['target_material_attempts'][-1]['provider_dispatches']==[]
        check['target_material_contract'] = materials.TARGET_MATERIAL_CONTRACT
        check.pop('request_sha256',None); check['request_sha256']=digest(check)
        finding = next(row for row in packet['findings'] if row['cue_index']==check['cue_indexes'][0])
        receipt = review_pass_receipt(pass_index=1, srt_text=check['whole_clip_current_srt'],
            audit={'findings':[finding]}, repaired_text=check['whole_clip_current_srt'], repairs=[])
        proof = materials.compile_loop_stop_no_gain(check=check, finding=finding, pass_receipts=[receipt])
        prompts = []
        class ReachedTerminal(BaseException): pass
        def stop_at_terminal(prompt):
            prompts.append(prompt)
            raise ReachedTerminal()
        with pytest.raises(ReachedTerminal):
            materials.close_with_existing_editorial(srt_text=check['whole_clip_current_srt'],
                finding=finding, recovery_audit={'terminal_check_request':check, 'no_gain_proof':proof},
                prior=finding.get('exact_release_adjudication') or {}, clip_context={},
                entity_verifier=owner, judge_llm_call=stop_at_terminal)
        assert len(prompts)==1 and prompts[0].startswith('# 字幕材料穷尽终裁')
        seen.append([row['artifact_bindings']['provider_receipt_sha256'] for row in rows])
    assert seen[0]==seen[1]
    assert all(p.read_bytes()==raw for p,raw in initial.items())
    for item in packet['manifest']:
        assert hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()==item['sha256']
