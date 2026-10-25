"""Ordinary provider receipts project validated recording ancestry without new hearing.

Synthetic full-protocol providers and actual provenance writers; not an acoustic
quality benchmark. The frozen production case is replayed separately read-only.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.autoslice import independent_context_adjudication as independent
from src.autoslice import native_context_witness as native
from src.autoslice import semantic_audio_recovery as recovery
from src.autoslice import targeted_relisten as policy
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from tests.test_final_recut_range_source_binding import package, install_gemini, sha, write
from tests.test_native_context_witness import _range_requests, _install_range_writer
from tests.test_independent_context_adjudication import _source_context_provenance
from tests.test_targeted_relisten_public_contract import (
    build_offline_rig, acquire_narrow, pending_finding, acquire_range, observe_plan,
)


@pytest.fixture
def tmp_path(tmp_path, request):
    root = os.environ.get('AUTOSLICE_RELISTEN_TEST_ARTIFACTS')
    if root:
        tmp_path = Path(root) / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
        tmp_path.mkdir(parents=True, exist_ok=False)
    return tmp_path


def snapshot(root):
    return {str(p.relative_to(root)): sha(p) for p in root.rglob('*') if p.is_file()}


def make_case(tmp_path, monkeypatch, kind='padded', provider='gemini'):
    if kind == 'direct':
        media = recording = tmp_path / 'direct.mp4'
        media.write_bytes(b'synthetic direct recording')
    elif kind == 'source_context':
        _, media, recording, _, _, _, _ = _source_context_provenance(tmp_path)
    else:
        final, recording, padded, _ = package(tmp_path, 2 if kind.endswith('_chain') else 1)
        media = final if kind.startswith('final') else padded
    check, broad = _range_requests(sha(recording))
    out = tmp_path / 'out'; out.mkdir()
    calls = []
    if provider == 'gemini':
        observe = install_gemini(monkeypatch, media, out, calls)
    else:
        _install_range_writer(monkeypatch, media, tmp_path / 'cache', dispatches=calls)
        wrapper = native.build_independent_context_witnesses(
            fallback=lambda _: pytest.fail('range must not manufacture a narrow witness'),
            source_media=media, output_dir=out, providers=(provider,),
            native_observation_cache_root=tmp_path / 'cache')
        observe = lambda a, b: wrapper.observe_range_context(check_request=a, range_witness_request=b)
    raw = observe(check, broad)[0]
    valid, reason = independent.validate_range_acoustic_observation(check['whole_clip_current_srt'], check, raw)
    assert valid is not None, reason
    return media, recording, check, broad, raw, valid, calls, observe


@pytest.mark.parametrize('provider', ['gemini', 'mai', 'moss'])
@pytest.mark.parametrize('kind', ['direct', 'padded', 'padded_chain', 'source_context', 'final', 'final_chain'])
def test_validated_media_representations_enter_normal_relisten_projection(tmp_path, monkeypatch, kind, provider):
    media, recording, check, broad, raw, valid, calls, observe = make_case(tmp_path, monkeypatch, kind, provider)
    files = snapshot(tmp_path)
    projection = policy.evidence_projection(check, ranges=[raw])
    assert projection['effective_distinct_observations'] == 1, projection['rejected_observations']
    row, = projection['observations']
    assert row['source_media_sha256'] == sha(media)
    assert row['raw_observation']['artifact_bindings'] == valid['artifact_bindings']
    assert projection['authority'] == 'EVIDENCE_ONLY' and projection['mutation_authorized'] is False
    assert row['raw_observation']['evidence_scope'] == 'RANGE_CONTEXT_ONLY'
    assert row['input_information']['source_window_ms'] == [5100, 19100]
    assert snapshot(tmp_path) == files  # the projection is a read-only consumer
    if kind == 'direct':
        assert 'media_source_projection' not in row
    else:
        source = row['media_source_projection']
        assert source['media_sha256'] != sha(recording)
        assert source['media_source_binding'] == valid['media_source_binding']
        assert source['media_target_ms'] == [10100, 14100]
        assert source['recording_sha256s'] == [sha(recording)]
        assert source['recording_clock_rebased'] is False
        if kind == 'final_chain':
            parent = source['media_source_binding']['parent_media_source_binding']
            assert [(x['recording_start_ms'], x['recording_end_ms']) for x in parent['pieces']] == [
                (100000, 120000), (140000, 160000)]
    again = observe(check, broad)[0]
    replay = policy.evidence_projection(check, ranges=[raw, again, deepcopy(raw)])
    assert replay['effective_distinct_observations'] == 1 and len(replay['observations']) == 1
    assert replay['observations'][0]['observation_id'] == row['observation_id']
    assert len(calls) == 1  # cache reuse is not another hearing


def test_cross_piece_target_preserves_two_recording_clocks_and_gap(tmp_path, monkeypatch):
    media, recording, _, _ = package(tmp_path, 2)
    check, _ = _range_requests(sha(recording))
    check.update(whole_clip_current_srt=(
        '1\n00:00:12,000 --> 00:00:13,000\n第一句\n\n'
        '2\n00:00:14,000 --> 00:00:17,000\n当前句\n\n'
        '3\n00:00:18,000 --> 00:00:21,000\n第三句\n'),
        matched_start_ms=14000, matched_end_ms=17000, context_start_ms=12000, context_end_ms=21000,
        source_media_timeline_offset_ms=0)
    broad = build_witness_request({**check, 'cue_indexes': [1, 2, 3],
        'matched_start_ms': 12000, 'matched_end_ms': 21000})
    out=tmp_path/'out';out.mkdir();calls=[]
    raw=install_gemini(monkeypatch, media, out, calls)(check,broad)[0]
    result=policy.evidence_projection(check,ranges=[raw])
    assert result['effective_distinct_observations']==1,result['rejected_observations']
    projection=result['observations'][0]['media_source_projection']
    assert projection['media_target_ms']==[14000,17000]
    assert projection['media_crop_ms']==[12000,21000]
    assert projection['recording_clock_rebased'] is False
    parent=projection['media_source_binding']['parent_media_source_binding']
    assert [(x['recording_start_ms'], x['recording_end_ms']) for x in parent['pieces']]==[
        (100000,120000),(140000,160000)]
    assert len(calls)==1


@pytest.mark.parametrize('damage', ['different_source','offset','target','raw','media','piece',
    'provenance_hash','missing_provenance','fake_supplied_binding','final_absolute_clock'])
def test_source_clock_and_original_bytes_fail_closed(tmp_path,monkeypatch,damage):
    media, recording, check, broad, raw, valid, calls, _ = make_case(tmp_path,monkeypatch,'final')
    altered=deepcopy(raw); current=deepcopy(check)
    if damage in {'different_source','fake_supplied_binding'}:
        current['clip_context_binding']['source_media_sha256s']=['f'*64]
        if damage=='fake_supplied_binding':
            altered['media_source_binding']={'role':'PRODUCER_PADDED_MEDIA_FROM_RECORDING',
                'media_sha256':sha(media),'recording_sha256':'f'*64,'recording_start_ms':0,'recording_end_ms':999999}
    elif damage=='offset':current['source_media_timeline_offset_ms']+=1
    elif damage=='target':current['matched_end_ms']=50000
    elif damage=='raw':Path(raw['artifact_bindings']['raw_response_path']).write_text('{}')
    elif damage=='media':media.write_bytes(b'tampered media')
    elif damage=='piece':(tmp_path/'piece-0.mp4').write_bytes(b'tampered piece')
    elif damage=='missing_provenance':media.with_suffix('.provenance.json').unlink()
    else:
        proof=media.with_suffix('.provenance.json');obj=json.loads(proof.read_text())
        if damage=='final_absolute_clock':obj['final_recut']['absolute_source_start_ms']+=1
        else:obj['final_recut']['source_sha256']='f'*64
        write(proof,obj)
    result=policy.evidence_projection(current,ranges=[altered])
    assert result['effective_distinct_observations']==0 and result['rejected_observations']
    assert len(calls)==1


def test_supplied_recording_hash_is_not_used_as_validation_authority(tmp_path,monkeypatch):
    media, recording, check, _, raw, valid, calls, _=make_case(tmp_path,monkeypatch)
    poisoned=deepcopy(raw)
    poisoned['media_source_binding']={'role':'DIRECT_RECORDING_MEDIA','media_sha256':'f'*64}
    result=policy.evidence_projection(check,ranges=[poisoned])
    assert result['effective_distinct_observations']==1
    assert result['observations'][0]['media_source_projection']['media_source_binding']==valid['media_source_binding']
    assert result['observations'][0]['source_media_sha256']==sha(media)
    assert len(calls)==1


def _completed_observation_under_unknown_wrapper(tmp_path,monkeypatch):
    rig=build_offline_rig(tmp_path,monkeypatch)
    witness=acquire_narrow(rig)
    finding=pending_finding(rig,rig.check,witness)
    original_state=rig.verifier.relisten_budget_state
    # No reliable owner snapshot is supplied, but the normal provider returns
    # complete byte-bound artifacts. This models the original outer UNKNOWN.
    rig.verifier.relisten_budget_state=lambda:None
    _,unknown,_=acquire_range(rig,finding,rig.check,observe_plan())
    rig.verifier.relisten_budget_state=original_state
    assert unknown['execution_status']=='UNKNOWN' and unknown['provider_calls_this_attempt'] is None
    assert unknown['reason_code']=='RELISTEN_DISPATCH_PROOF_REQUIRED'
    assert unknown['native_observations'] and unknown['consumer_check_request']
    assert len(rig.transport.calls)==2
    return rig,finding,unknown


def test_normal_recovery_reuses_complete_original_unknown_without_reclassifying_account(tmp_path,monkeypatch):
    rig,finding,unknown=_completed_observation_under_unknown_wrapper(tmp_path,monkeypatch)
    original=deepcopy(unknown); state=rig.verifier.relisten_budget_state(); files=snapshot(tmp_path)
    finding['exact_release_adjudication']['semantic_audio_recovery']=unknown
    def forbidden(*a,**k):pytest.fail('a retained observation must not dispatch audio or ask a new planner')
    rig.verifier.observe_range_context=forbidden
    result,audit=recovery.recover_semantic_audio_range(srt_text=rig.srt,finding=finding,
        check_request=rig.check,entity_verifier=rig.verifier,llm_call=forbidden)
    assert audit['status']=='OBSERVED' and audit['execution_status']=='REUSED_BOUND_OBSERVATION'
    assert audit['provider_calls_this_attempt']==audit['new_effective_observations_this_attempt']==0
    proof=audit['retained_observation_replay']
    assert proof['original_execution_status']=='UNKNOWN' and proof['original_provider_calls'] is None
    assert proof['accounting_reclassified'] is False
    assert len(result['_prior_range_acoustic_observations'])==1
    assert unknown==original and rig.verifier.relisten_budget_state()==state
    assert len(rig.transport.calls)==2 and snapshot(tmp_path)==files


@pytest.mark.parametrize('damage',['missing_consumer','consumer_hash','changed_context','wrong_clock',
    'missing_raw','execution_incomplete','unknown_no_return','plan_changed'])
def test_unknown_or_drift_is_not_manufactured_into_a_usable_observation(tmp_path,monkeypatch,damage):
    rig,_,unknown=_completed_observation_under_unknown_wrapper(tmp_path,monkeypatch)
    saved=deepcopy(unknown);check=deepcopy(unknown['consumer_check_request'])
    if damage=='missing_consumer':saved.pop('consumer_check_request')
    elif damage=='consumer_hash':saved['consumer_check_request']['request_sha256']='f'*64
    elif damage=='changed_context':
        # A cue-grid change (41: retained range replay keeps its consumed content, i.e. the
        # whole-clip cue grid for candidate-blind rows; whole-SRT bytes are not identity).
        import re as _re
        srt=check['whole_clip_current_srt'];stamps=list(_re.finditer(r'(\d\d):(\d\d):(\d\d),(\d\d\d)\s*$',srt,_re.M))
        last=stamps[-1];h,m,sec,ms=(int(x) for x in last.groups());total=((h*60+m)*60+sec)*1000+ms+10
        stamp='%02d:%02d:%02d,%03d'%(total//3600000,total//60000%60,total//1000%60,total%1000)
        check['whole_clip_current_srt']=srt[:last.start()]+stamp+srt[last.end():]
    elif damage=='wrong_clock':check['source_media_timeline_offset_ms']+=1
    elif damage=='unknown_no_return':saved['audio_adapter_returned']=False
    elif damage=='plan_changed':saved['plan']['purpose']='WORD_BOUNDARY'
    elif damage=='missing_raw':Path(saved['native_observations'][0]['artifact_bindings']['raw_response_path']).unlink()
    else:
        row=saved['native_observations'][0];p=Path(row['artifact_bindings']['execution_path']);v=json.loads(p.read_text());v['status']='UNKNOWN';write(p,v)
        row['artifact_bindings']['execution_sha256']=sha(p)
    before=deepcopy(saved)
    observed,report=policy.replay_retained_range_observations(check,saved)
    assert not observed and report['status']=='REJECTED'
    assert report['accounting_reclassified'] is False and saved==before
    assert report['new_provider_calls']==report['new_effective_observations']==0
    assert len(rig.transport.calls)==2


def test_text_only_revision_at_the_same_grid_replays_the_retained_blind_range(tmp_path,monkeypatch):
    """Guimi 165d111 cue28/116: a text-only revision elsewhere is not RELISTEN_INPUT_BINDING_DRIFT."""
    rig,_,unknown=_completed_observation_under_unknown_wrapper(tmp_path,monkeypatch)
    saved=deepcopy(unknown);check=deepcopy(unknown['consumer_check_request'])
    check['whole_clip_current_srt']+='\nchanged'
    before=deepcopy(saved)
    observed,report=policy.replay_retained_range_observations(check,saved)
    assert report['status']=='VERIFIED_RETAINED_OBSERVATION' and observed
    assert report['accounting_reclassified'] is False and saved==before
    assert report['new_provider_calls']==report['new_effective_observations']==0
    assert len(rig.transport.calls)==2


def test_normal_fresh_padded_observation_uses_original_dispatch_accounting(tmp_path,monkeypatch):
    from tests.test_independent_context_adjudication import _padded_recording_provenance
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    rig=build_offline_rig(tmp_path,monkeypatch)
    recording_sha,*_= _padded_recording_provenance(tmp_path,rig.source)
    rig.context=build_clip_context(candidate_id=rig.context['candidate_id'],
        spec={'date':'2026-10-19','pieces':[{'start_ms':438350,'end_ms':617760,
              'source_media_sha256':'sha256:'+recording_sha}]},draft_srt=rig.srt,
        authoritative_chat=(),topic_resolution={},session_topic_authorities=(),
        speech_memory_ledger_path=tmp_path/'empty-speech-memory.json',source_truth_ledger_path=None)
    rig.check=build_context_adjudication_request(rig.srt,rig.finding,clip_context=rig.context,
                                                source_media_timeline_offset_ms=0)
    witness=acquire_narrow(rig)
    finding=pending_finding(rig,rig.check,witness)
    _,audit,_=acquire_range(rig,finding,rig.check,observe_plan())
    assert audit['status']=='OBSERVED',audit.get('reason_code')
    assert audit['execution_status']=='COMPLETED_USABLE_OBSERVATION'
    assert audit['provider_calls_this_attempt']==audit['new_effective_observations_this_attempt']==1
    # K01 (41:1489-1492): the earlier narrow blind witness was heard on the
    # same padded media of this recording. It stays a valid observation
    # instead of being dropped as source drift, so the projection holds the
    # narrow witness plus this attempt's range observation.
    after=audit['observation_projection_after']
    assert after['effective_distinct_observations']==2
    assert sorted(r['receipt_kind'] for r in after['observations'])==['blind_pinyin','blind_range']
    assert after['rejected_observations']==[]
    assert len(rig.transport.calls)==2
    state=rig.verifier.relisten_budget_state()
    assert state['receipt']['budget']['attempts'][-1]['status']=='OBSERVED'
