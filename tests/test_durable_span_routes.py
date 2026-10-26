"""Real transport-shaped counterexamples + unmocked ordinary consumer controls."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import socket
import subprocess
import sys

import pytest

from src.autoslice.final_review_auditor import (
    adjudicate_context_finding, build_context_adjudication_request, persist_review_audit,
)
from src.autoslice.span_phoneme_resolution import (
    range_observations, validate_persisted_judge_scopes,
)
from src.autoslice.semantic_retry_progress import digest
from tests.test_span_phoneme_resolution import Observers, model_for, run_case, srt
from src.autoslice.review_evidence_store import load_review


@pytest.fixture(autouse=True)
def offline(_hermetic_autoslice_base, tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path/'runtime'))
    def no(*args, **kwargs):
        raise AssertionError('External provider/network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', no)
    monkeypatch.setattr(socket, 'create_connection', no)


def cold(path, expected_sha_set=None):
    command = '''import json,sys,socket
from pathlib import Path
from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
from src.autoslice.review_evidence_store import load_review
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('network'))
v=load_review(Path(sys.argv[1]));count=validate_persisted_judge_scopes(v)
if len(sys.argv)>2:
 expected=set(json.loads(sys.argv[2]));h=v['executed_span_judgments']
 assert {x['completion_sha256'] for x in h['expected_executions']}==expected
 assert set(h['outcomes'])=={x['execution_id'] for x in h['expected_executions']}
print('COLD_OUTCOME_COVERAGE',count)
'''
    args=[sys.executable,'-B','-c',command,str(path)]
    if expected_sha_set is not None:args.append(json.dumps(sorted(expected_sha_set)))
    return subprocess.run(args, capture_output=True, text=True, timeout=45)


def test_all_actual_initial_candidate_replies_survive_public_writer_and_cold_gate(tmp_path):
    model=model_for();raws=[]
    def observed(prompt):
        raw=model(prompt);raws.append(raw);return raw
    observed.cpa_cache_identity={'transport':'fixture','models':['durable-test'],'effort':'high'}
    output,audit,*_=run_case(model=observed)
    assert '甲零' in output and audit['repaired']
    # Expectation comes from actual bottom-boundary replies, not surviving .judge.
    expected={hashlib.sha256(raw.encode()).hexdigest() for raw in raws}
    assert len(expected)>=3
    path=tmp_path/'review.json';persist_review_audit(path,audit)
    result=cold(path,expected);assert result.returncode==0,result.stderr
    hist=load_review(path)['executed_span_judgments']
    assert any('span_compilation' in j for j in hist['outcomes'].values())
    missing=deepcopy(audit);h=missing['executed_span_judgments']
    h['outcomes'].pop(next(iter(h['outcomes'])))
    h['history_sha256']=digest({k:v for k,v in h.items() if k!='history_sha256'})
    persist_review_audit(tmp_path/'missing.json',missing)
    bad=cold(tmp_path/'missing.json');assert bad.returncode!=0 and 'COVERAGE_MISMATCH' in bad.stderr
    # Exact cached replies retain identity and complete coverage without re-dispatch.
    before=len(raws);again=run_case(model=observed)[1]
    assert len(raws)==before
    validate_persisted_judge_scopes(again)


@pytest.mark.parametrize('which',range(13))
def test_actual_d162_raw_scope_is_never_lost_even_when_it_is_stale_for_new_request(tmp_path,which):
    """Original raw bytes are intentionally not rewritten to fit a new scope.

    This proves coverage/rejection for real-shaped answers, not audio truth or
    repair acceptance. Separate provider-shaped positive tests exercise repair.
    """
    path=os.environ.get('VTUBER_D162_ANALYSIS')
    if path is None:pytest.skip('Original d162 analysis required')
    rows=load_review(Path(path)/'RAW-SPAN-COVERAGE.json')['rows']
    row=rows[which];raw=row['current_raw_completion']
    assert hashlib.sha256(raw.encode()).hexdigest()==row['completion_sha256']
    calls=[]
    def observed(prompt):calls.append(prompt);return raw
    observed.cpa_cache_identity={'transport':'fixture','models':['original-d162-raw'],'effort':'high'}
    _,audit,*_=run_case(model=observed)
    assert calls and not audit['repaired']
    path=tmp_path/'review.json';persist_review_audit(path,audit)
    check=cold(path,{row['completion_sha256']});assert check.returncode==0,check.stderr
    history=audit['executed_span_judgments']
    assert all(j['span_compilation']['resolution'] is None for j in history['outcomes'].values())
    assert all(j['span_review_raw_completion']==raw for j in history['outcomes'].values())


@pytest.mark.parametrize('provider',['mai','moss'])
@pytest.mark.parametrize('word_timing',[False,True])
def test_context_only_range_completes_route_but_needs_native_target_timing(tmp_path,provider,word_timing):
    from tests.test_independent_context_adjudication import _native_range_artifacts
    old,finding,clip,_q,raw,paths=_native_range_artifacts(tmp_path,provider,word_timing=word_timing)
    current=old.replace('第一句','第一句已改')
    request=build_context_adjudication_request(current,
        {**finding,'_prior_range_acoustic_observations':[raw]},clip_context=clip,
        source_media_timeline_offset_ms=100)
    supplied=request['context_only_range_acoustic_observations'][0]
    # 41:1489-1492 (admin ruling): native ASR saw no subtitle text, so a
    # text-only revision keeps the strict observation with current_cue_texts.
    assert supplied['status']=='OBSERVED' and supplied['verdict']['status']=='OBSERVED'
    assert supplied['text_revision']=='TEXT_ONLY_REVISION_AT_IDENTICAL_CUE_GRID'
    assert supplied['current_cue_texts'][0]['text']=='第一句已改'
    native,receipts,invalid=range_observations(request)
    assert not invalid and len(receipts)==1 and receipts[0]['status']=='COMPLETED'
    assert bool(native) is word_timing
    assert receipts[0]['target_projection']==('NATIVE_TIMED_OR_EXACT_CROP' if word_timing else 'CONTEXT_ONLY_NO_TARGET_TIMING')
    before={str(p):p.read_bytes() for p in paths.values()}
    assert {str(p):p.read_bytes() for p in paths.values()}==before
    paths['raw_response'].write_bytes(b'changed raw')
    n,r,bad=range_observations(request)
    assert not n and not r and bad


def test_normal_producer_typed_history_retains_all_executed_scopes_then_cold_rejects_missing(tmp_path,monkeypatch):
    from tests.test_wholefilm_gate_three_gaps import test_real_producer_plurality_persistence_and_fresh_process_gate
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    test_real_producer_plurality_persistence_and_fresh_process_gate(tmp_path,monkeypatch,False,False)
    root=tmp_path/'case';a=load_review(root/'authority.json')
    _validate_exact_final_cpa_self_heal(a,expected_srt_sha256='sha256:'+hashlib.sha256((root/'current.srt').read_bytes()).hexdigest())
    repair=a['exact_final_cpa_self_heal']['passes'][0]['repairs'][0]
    h=repair['executed_span_judgments'];assert len(h['expected_executions'])>=3
    h['outcomes'].pop(next(iter(h['outcomes'])))
    h['history_sha256']=digest({k:v for k,v in h.items() if k!='history_sha256'})
    path=root/'damaged-typed-history.json';persist_review_audit(path,a)
    result=cold(path);assert result.returncode!=0 and 'COVERAGE_MISMATCH' in result.stderr
    with pytest.raises(ValueError,match='SPAN_COMPILATION_INVALID'):
        _validate_exact_final_cpa_self_heal(a,expected_srt_sha256='sha256:'+hashlib.sha256((root/'current.srt').read_bytes()).hexdigest())


def test_unlocatable_public_producer_consumes_preexisting_native_without_reacquisition(tmp_path,monkeypatch):
    from tests.test_wholefilm_gate_three_gaps import test_real_producer_plurality_persistence_and_fresh_process_gate
    test_real_producer_plurality_persistence_and_fresh_process_gate(
        tmp_path,monkeypatch,False,False,initial_unlocatable=True)
    root=tmp_path/'case';a=load_review(root/'authority.json')
    calls=load_review(root/'OBSERVED-CALLS.json')
    assert sum(x[0]=='mai' for x in calls)==1 and sum(x[0]=='moss' for x in calls)==1
    history=a['exact_final_cpa_self_heal']['passes'][0]['repairs'][0]['executed_span_judgments']
    assert any((j.get('span_compilation')or{}).get('blocked_reason')=='SPAN_LOCALIZATION_UNLOCATABLE'
               for j in history['outcomes'].values())
    assert any((j.get('span_compilation')or{}).get('resolution') for j in history['outcomes'].values())


@pytest.mark.parametrize('defect',[None,'source','language','transcript','unsupported'])
def test_failed_mandarin_uses_normal_original_language_protocol_not_fake_pinyin(tmp_path,defect):
    from src.autoslice.exact_source_transcript_contract import seal_exact_source_transcript_observation
    from src.autoslice.entity_audio_verifier import _prepare_audio_span
    from tests.test_span_phoneme_resolution import SOURCE_SHA, sha
    class Mixed(Observers):
        def __call__(self,target):
            self.calls.append(('primary-invalid',deepcopy(target)))
            return {'schema_version':'subtitle-span-acoustic-witness.v1','request_sha256':target['request_sha256'],
                    'witness_protocol':'blind_pinyin','status':'UNCERTAIN','reason_code':'WITNESS_REPORT_INVALID'}
    observed_text='他说，他说，全然違います' if defect=='unsupported' else '他说，他说，ミトコンドリアです'
    verifier=Mixed(transcripts={'mai':observed_text,'moss':observed_text})
    native_calls=[]
    def native(request):
        native_calls.append(deepcopy(request))
        assert not {'current_cue','proposed_cue','candidate_entities'} & request.keys()
        span=_prepare_audio_span(request=request,context_mode=True,source_media_timeline_offset_ms=0,
                                 source_duration_ms=10000,witness_mode=True)
        observation=seal_exact_source_transcript_observation(request=request,
            exact_transcript='他说，他说，ミトコンドリアです' if defect not in {'transcript','unsupported'} else '他说，他说，全然違います',
            audible_language='ja' if defect!='language' else 'zh',source_media_sha256=SOURCE_SHA,
            audio_clip_sha256=sha(b'original-language-audio'),provider='agy',model='fixture-original-language',
            response_sha256=sha('fixture-original-language-response'),timeline_binding=span.timeline_binding)
        if defect=='source':observation['source_media_sha256']='b'*64
        return observation
    text='他说，他说，米头康多里亚です'
    finding={'cue_index':1,'suspect':'米头康多里亚','suggestion':'ミトコンドリア',
             'proposed_full_cue':'他说，他说，ミトコンドリアです','repair_class':'phonetic'}
    output,audit=adjudicate_context_finding(srt(text),finding,entity_verifier=verifier,
        exact_source_transcript_provider=native,judge_llm_call=model_for(target_quote='ミトコンドリア',blind_quote=None,question_span=[0,5]),
        require_complete_utterance_support=True)
    assert len(native_calls)==1
    if defect in {None,'transcript'}:
        # When CURRENT has zero support, one dissenting native route does not
        # erase two valid supporting routes. Preserve dissent, do not force unanimity.
        assert audit['repaired'] and audit['policy_branch']=='CONTEXT_PHONEME_FIT'
        assert audit['primary_protocol_recovery']['prior_primary_reason']=='WITNESS_REPORT_INVALID'
        assert audit['span_context_resolution']['outside_findings']
        assert output==srt('他说，他说，ミトコンドリアです')
        if defect=='transcript':
            assert any(not r['compatible'] for r in audit['span_context_resolution']['comparisons'])
    else:assert not audit['repaired'] and output==srt(text)


def test_cached_audio_proposal_phase_is_admitted_once_without_reset(tmp_path):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder,RetryNeedsPlan
    from src.autoslice.missing_proposal_bootstrap import collect_proposal_audio,prepare_missing_proposal_candidate
    from src.autoslice.final_review_auditor import _derive_single_span_edit
    from tests.test_span_phoneme_resolution import SOURCE_SHA
    context={'candidate_id':'synthetic-cache-proposal','pieces':[{'source_media_sha256':SOURCE_SHA}]}
    text=srt('什么');finding={'cue_index':1,'suspect':'什么','proposed_full_cue':None,'repair_class':'phonetic',
                            'why':'Synthetic shen me support versus old conflicting observation; do not automatically restore old wording.'}
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    scaffold=_candidate_blind_missing_proposal_scaffold(text,finding)
    request=build_context_adjudication_request(text,scaffold,clip_context=context)
    model_calls=[]
    def response(prompt):
        model_calls.append(prompt)
        return '{"status":"UNRESOLVED","decision":"UNRESOLVED","needs_audio":true,"reason":"Synthetic boundary conflict remains."}'
    response.cpa_cache_identity={'transport':'fixture','models':['proposal-cache'],'effort':'high'}
    for i in range(3):
        recorder=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
        recorder.wrap(response)('## 音素与原语言证据（数据，不是裁决） CURRENT_TEXT_PINYIN_WEAK '+str(i))
    class Cached(Observers):
        def __call__(self,q):raise AssertionError('Cached proposal must not acquire new audio')
        def probe_witness_cache(self,q):
            value=Observers.__call__(self,q);value['served_from_cache']=True;return value
    cache=Cached(heard='shen me')
    audio=collect_proposal_audio(request=request,finding=finding,entity_verifier=cache,transcript_provider=None,cached_only=True)
    recorder=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
    recorder.plan_cached_proposal_audio(check_request=request,proposal_audio=audio)
    assert recorder.plan and recorder.material['cached_audio_proposal_basis']['new_acquisition'] is False
    from src.autoslice.missing_proposal_bootstrap import _proposal_audio_recorded_call
    prompt='## 音素与原语言证据（数据，不是裁决） '+json.dumps(audio,ensure_ascii=False,sort_keys=True)
    call=_proposal_audio_recorded_call(recorder.wrap(response),request,audio)
    call(prompt);assert len(model_calls)==4
    fresh=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
    fresh.plan_cached_proposal_audio(check_request=request,proposal_audio=audio)
    assert fresh.plan is None
    with pytest.raises(RetryNeedsPlan):fresh.wrap(response)(prompt+' same evidence new phrasing')
    assert len(model_calls)==4
    # The full public preparation really probes saved audio before a text-only
    # repeated proposal. Its unresolved result must not be relabelled as repair.
    next_rec=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
    _,audit,_=prepare_missing_proposal_candidate(srt_text=text,finding=finding,structured_chat='',clip_context=context,
        llm_call=next_rec.wrap(response),derive_single_span_edit=_derive_single_span_edit,check_request=request,entity_verifier=cache)
    assert audit.get('proposal_audio',{}).get('phonetic_evidence',{}).get('heard_pinyin')=='shen me'


@pytest.mark.parametrize('damage',[None,'raw','clock'])
def test_real_unlocatable_reply_reaches_existing_recovery_gate_not_an_invented_pass(damage):
    from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
    path=os.environ.get('VTUBER_D162_ANALYSIS')
    if path is None:pytest.skip('Original d162 analysis required')
    source=Path(path)/'terminal-readback-d1622536/files/fourcase-off/sister/review-03.json'
    ad=load_review(source)['findings'][18]['exact_release_adjudication']
    assert ad['policy_branch']=='SPAN_LOCALIZATION_UNLOCATABLE'
    if damage=='raw':ad['witness_judge']['judge']['span_review_raw_completion']+=' '
    if damage=='clock':ad['request']['matched_start_ms']+=1
    recovery=history_audio_recovery_from_adjudication(ad,request=ad['request'])
    assert recovery is not None
    if damage is None:
        assert recovery['status']=='BLOCK' and recovery['needs_audio'] is True
        assert not ad['repaired'] and ad['span_pending_questions']['outside_findings']
    else:assert recovery['status']=='INVALID'


def test_new_audio_request_reaches_ordinary_value_planner_in_same_public_call(tmp_path, monkeypatch):
    # The former hash-only Observers fixture cannot prove an actual hearing.
    # Use the complete normal send-boundary protocol before requesting closure.
    import test_targeted_relisten_public_contract as protocol
    rig = protocol.build_offline_rig(tmp_path / "public-no-gain", monkeypatch)
    normal = protocol.public_judge(rig)
    calls = []
    def unlocatable(prompt):
        calls.append(prompt)
        if prompt.startswith("# CPA 补听范围选择"):
            return '{"decision":"NO_NEW_AUDIO","reason":"Synthetic hearing does not localize target."}'
        return normal(prompt)
    text, audit = adjudicate_context_finding(
        rig.srt, rig.finding, entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=unlocatable, require_complete_utterance_support=True)
    assert text == rig.srt and not audit["repaired"]
    assert len(rig.transport.calls) == 1
    assert rig.transport.calls[0]["class"] == "CANDIDATE_BLIND_AUDIO"
    assert any(p.startswith("# CPA 补听范围选择") for p in calls)
    recovered = audit["semantic_audio_recovery"]
    assert recovered["status"] == "BLOCK"
    assert recovered["reason_code"] == "RELISTEN_NO_GAIN_OBSERVATION_PROOF_REQUIRED"
    assert recovered["provider_calls_this_attempt"] == 0
    assert recovered["new_effective_observations_this_attempt"] == 0
    assert audit["witness_judge"]["judge"]["needs_audio"] is True
    # The original gate remains separate from the diagnosis. Persisting the
    # actual audit must not recurse or manufacture an editorial authority.
    assert "editorial_authority" not in audit
    persist_review_audit(tmp_path / "no-new-range-review.json", audit)
    saved = load_review(tmp_path / "no-new-range-review.json")
    assert saved["repaired"] is False and saved["witness_judge"]["judge"]["needs_audio"] is True
