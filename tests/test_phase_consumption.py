"""eb0c actual counterexamples and normal public dispatch/persistence controls.

All model functions are lowest transport fixtures. Original recorded raw, issue
history, candidate text, clocks and final validators are never rewritten to PASS.
"""
from copy import deepcopy
from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest
from src.autoslice.review_evidence_store import load_review

from src.autoslice.final_review_auditor import (
    adjudicate_context_finding, build_context_adjudication_request, persist_review_audit,
)
from src.autoslice.span_phoneme_resolution import range_observations
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, digest
from tests.test_span_phoneme_resolution import Observers, model_for, run_case, srt


@pytest.fixture(autouse=True)
def offline(_hermetic_autoslice_base, tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path/'runtime'))
    def no(*args, **kwargs):
        raise AssertionError('No real provider/network allowed in recorded checks')
    monkeypatch.setattr(socket.socket, 'connect', no)
    monkeypatch.setattr(socket, 'create_connection', no)


@pytest.fixture
def real():
    root=os.environ.get('VTUBER_EB0C_ANALYSIS')
    if not root:pytest.skip('Explicit original eb0c evidence required')
    a=Path(root);d=a.parent/'terminal-consumption/terminal-readback-eb0cd086/files/fourcase-off'
    return a,d


def cold(path):
    code='''import json,sys,socket
from pathlib import Path
from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
from src.autoslice.review_evidence_store import load_review
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('network'))
v=load_review(Path(sys.argv[1]));print(validate_persisted_judge_scopes(v))
'''
    return subprocess.run([sys.executable,'-B','-c',code,str(path)],capture_output=True,text=True,timeout=45)


@pytest.mark.parametrize('damage',[None,'raw','clock','superseded'])
def test_actual_saved_initial_judge_reaches_pending_recovery_without_rewriting(real,damage):
    from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
    _,d=real;ad=load_review(d/'sister/review-04.json')['findings'][19]['exact_release_adjudication'];before=deepcopy(ad)
    if damage=='raw':
        h=ad['executed_span_judgments'];next(iter(h['outcomes'].values()))['span_review_raw_completion']+=' '
    if damage=='clock':ad['request']['matched_start_ms']+=1
    if damage=='superseded':
        ad['repaired']=True;ad['mutation_authority']={'status':'PASS'}
    result=history_audio_recovery_from_adjudication(ad,request=ad['request'])
    if damage is None:
        assert result is not None and result['status']=='BLOCK'
        assert result['gate']['completion_sha256']=='55cbf3fe9c9d13165fdf98d52aeaef7eeda51f65e0f625ba8bc882804312150c'
        assert result['original_request']['request_sha256']=='72d9478e99ce8107badd9430e576e768534e97584119c10b354211c9b59cbd4d'
        assert ad==before
    else:assert result is None or result['status']!='BLOCK'


def test_valid_segmented_AGY_range_completes_without_invented_native_word_timing(tmp_path):
    from tests.test_independent_context_adjudication import _segmented_range_artifacts
    text,finding,context,_q,raw,paths=_segmented_range_artifacts(tmp_path)
    q=build_context_adjudication_request(text,{**finding,'_prior_range_acoustic_observations':[raw]},clip_context=context,source_media_timeline_offset_ms=100)
    before={k:p.read_bytes() for k,p in paths.items()}
    native,receipts,bad=range_observations(q)
    assert not bad and len(receipts)==1 and receipts[0]['route']=='range:gemini'
    assert receipts[0]['status']=='COMPLETED' and not native
    assert receipts[0]['target_projection']=='CONTEXT_ONLY_NO_TARGET_TIMING'
    assert 'status' not in q['context_only_range_acoustic_observations'][0]['verdict']
    assert before=={k:p.read_bytes() for k,p in paths.items()}
    paths['raw_response'].write_bytes(b'changed')
    native,receipts,bad=range_observations(q);assert not native and not receipts and bad


@pytest.mark.parametrize('provider',['mai','moss'])
def test_retained_native_context_only_survives_next_persisted_authority(tmp_path,provider):
    from tests.test_independent_context_adjudication import _native_range_artifacts
    from src.autoslice.retained_range_context import persisted_native_range_artifacts
    old,finding,context,_q,raw,paths=_native_range_artifacts(tmp_path,provider)
    current=old.replace('第一句','第一句已改')
    q=build_context_adjudication_request(current,{**finding,'_prior_range_acoustic_observations':[raw]},clip_context=context,source_media_timeline_offset_ms=100)
    # 41:1489-1492 (admin ruling): strict observation survives the text-only revision.
    assert q['context_only_range_acoustic_observations'][0]['status']=='OBSERVED'
    assert q['context_only_range_acoustic_observations'][0]['text_revision']=='TEXT_ONLY_REVISION_AT_IDENTICAL_CUE_GRID'
    authority={'final_review_audit':{'request':q}};before=deepcopy(authority)
    references=persisted_native_range_artifacts(authority)
    assert len(references)==1
    future=build_context_adjudication_request(current,{**finding,'_prior_range_acoustic_observations':references},clip_context=context,source_media_timeline_offset_ms=100)
    assert len(future['context_only_range_acoustic_observations'])==1 and authority==before
    bad=build_context_adjudication_request(current,{**finding,'_prior_range_acoustic_observations':references},clip_context=context,source_media_timeline_offset_ms=101)
    assert not bad.get('context_only_range_acoustic_observations')


def test_real_unscoped_insertion_raw_has_typed_outcome_in_public_history(real,tmp_path):
    _,d=real
    path=d/'runtime-state/cache/judge-verdicts/60/60a6ecc6cf16fedc174c0f91a5efffc4e92e703cb9b8d6e5a630faae9bf24865.json'
    original=load_review(path)['verdict']
    a=real[0];rows=load_review(a/'RAW-EXCHANGES.json')['rows']
    raw=next(x['raw_completion'] for x in rows if x['terminal_document']['completion_sha256']==original['completion_sha256'])
    calls=[]
    def llm(prompt):calls.append(prompt);return raw
    text=srt('喜欢老');f={'cue_index':1,'suspect':'','suggestion':'女人','proposed_full_cue':'喜欢老女人','repair_class':'spoken_unit','span_start_codepoint':3,'span_end_codepoint':3}
    output,ad=adjudicate_context_finding(text,f,entity_verifier=Observers(heard='xi huan lao'),judge_llm_call=llm,require_complete_utterance_support=True)
    assert calls and output==text
    h=ad['executed_span_judgments'];assert h['expected_executions']
    assert all(x['span_review_raw_completion']==raw for x in h['outcomes'].values())
    assert all(x['span_compilation']['scope_status']=='NOT_APPLICABLE' for x in h['outcomes'].values())
    saved=tmp_path/'unscoped.json';persist_review_audit(saved,ad)
    p=cold(saved);assert p.returncode==0,p.stderr
    h['outcomes'].pop(next(iter(h['outcomes'])));h['history_sha256']=digest({k:v for k,v in h.items()if k!='history_sha256'})
    persist_review_audit(tmp_path/'missing.json',ad);p=cold(tmp_path/'missing.json');assert p.returncode!=0 and 'COVERAGE_MISMATCH'in p.stderr


@pytest.mark.parametrize('damage',[None,'target_duplicate','outside_quote'])
def test_target_and_outside_alignments_have_separate_meaning_in_normal_cold_path(tmp_path,damage):
    model=model_for(question_span=[0,1])
    def fixture(prompt):
        raw=json.loads(model(prompt));scope=raw.get('span_review')
        if scope:
            # The lowest provider fixture emits a new protocol-compliant response;
            # these are not edited originals and are not evidence of real accuracy.
            data=json.loads(prompt.split('\nSPAN_REVIEW_INPUT\n',1)[1].split('\nEND_SPAN_REVIEW_INPUT\n',1)[0])
            if scope.get('audio_questions') and data['observations']:
                obs=data['observations'][0];units=obs['text'].split() if obs['kind']=='blind_pinyin' else obs['text'];quote=units[0]
                ann={'observation_id':obs['observation_id'],'span':[0,1],'quote':quote}
                scope['audio_questions'][0]['observation_ids']=[obs['observation_id']]
                scope['audio_questions'][0]['observation_alignments']=[ann]
                if damage=='outside_quote':ann['quote']='not the original quote'
            if damage=='target_duplicate' and scope.get('alignments'):scope['alignments'].append(deepcopy(scope['alignments'][0]))
        return json.dumps(raw)
    output,ad,*_=run_case(model=fixture)
    if damage is None:
        assert ad['repaired'] and ad['span_context_resolution']['outside_findings']
        persist_review_audit(tmp_path/'scope.json',ad);p=cold(tmp_path/'scope.json');assert p.returncode==0,p.stderr
    else:assert not ad['repaired']


def test_new_scoped_initial_gate_is_available_before_normal_range_selector(tmp_path):
    from src.autoslice.semantic_retry_progress import RetryNeedsPlan
    base=model_for();calls=[]
    def llm(prompt):
        calls.append(prompt)
        if 'CPA 补听范围选择' in prompt:return '{"decision":"NO_NEW_AUDIO","reason":"Low-level fixture has no further discriminating audio."}'
        if 'CANDIDATE_BLIND_TRANSCRIPT' in prompt or 'candidate_blind_transcript' in prompt:
            raise RetryNeedsPlan('CONTINUATION_PLAN_ALREADY_UNRESOLVED')
        raw=json.loads(base(prompt))
        if 'span_review'in raw:raw['span_review']['target_needs_audio']=True
        return json.dumps(raw)
    observer=Observers();observer.observe_range_context=lambda **kw: (_ for _ in ()).throw(AssertionError('NO_NEW_AUDIO'))
    _,ad,*_=run_case(model=llm,verifier=observer)
    assert not ad['repaired']
    assert any('CPA 补听范围选择'in p for p in calls), {'calls':len(calls),'audit':ad}
    persist_review_audit(tmp_path/'initial-history.json',ad);assert cold(tmp_path/'initial-history.json').returncode==0


@pytest.mark.parametrize('name,index',[('stomach',8),('sister',19)])
def test_actual_issue_proposal_to_first_verification_dispatch_once_without_reset(real,tmp_path,name,index):
    from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
    from src.autoslice.independent_context_adjudication import bind_request
    _,d=real;run=load_review(d/name/'run.json');finding=deepcopy(run['reviews'][0]['findings'][index]);ad=finding['exact_release_adjudication'];q=deepcopy(ad['request']);w=deepcopy(ad['verdict']);text=(d/name/'final.srt').read_text()
    root=real[0].parent.parent/'fourcase-counterexample-fix-20261017'
    context=load_review(root/'SOURCE-CONTEXTS.json')[name]['content']
    rec=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
    old=d/'runtime-state/cache/semantic-cpa-attempts'/rec.issue
    rec.directory.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(old,rec.directory)
    originals={p.name:p.read_bytes()for p in old.glob('*.json')}
    calls=[]
    def transport(prompt):
        calls.append(prompt)
        return '{"choice":"NEITHER","needs_audio":true,"reason":"Actual-history low-level fixture, not a successful repair."}'
    transport.cpa_cache_identity=json.loads(next(iter(originals.values())))['request']['model']
    # The normal word-judge entry must dispatch; no final gate is mocked.
    _,_,audit=adjudicate_with_witness(check_request=q,witness=w,llm_call=rec.wrap(transport),clip_context=context)
    if name=='sister':
        # This original request is precisely the missing-range counterexample:
        # no evidence-bearing parent proposal can justify opening verification.
        # Retained-range public-writer checks above cover supplying the missing
        # material; never turn a weak text-only proposal into acoustic authority.
        assert not calls and 'proposal_verification_basis' not in rec.material
        for file,raw in originals.items():assert(rec.directory/file).read_bytes()==raw
        return
    assert len(calls)==1,{'events':rec.audit(),'material':rec.material,'judge':audit['judge']}
    assert rec.material['proposal_verification_basis']['new_acquisition'] is False
    assert audit['judge']['span_compilation']['resolution'] is None
    for file,raw in originals.items():assert(rec.directory/file).read_bytes()==raw
    again=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
    reworded=bind_request(q,reason='cosmetic caller rephrasing')
    _,_,last=adjudicate_with_witness(check_request=reworded,witness=w,llm_call=again.wrap(transport),clip_context=context)
    assert len(calls)==1 and last['judge']['status'] in {'JUDGE_NO_NEW_EVIDENCE','JUDGE_UNAVAILABLE'}


def test_actual_stomach34_reuses_candidate_not_proposal_then_verifies_current(real,tmp_path):
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    _,d=real;finding=deepcopy(load_review(d/'stomach/review-05.json')['findings'][0]);text=(d/'stomach/final.srt').read_text()
    context=load_review(real[0].parent.parent/'fourcase-counterexample-fix-20261017/SOURCE-CONTEXTS.json')['stomach']['content']
    rec=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
    originals=d/'runtime-state/cache/semantic-cpa-attempts'/rec.issue
    rec.directory.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(originals,rec.directory)
    before={p.name:p.read_bytes()for p in originals.glob('*.json')}
    audio=finding['exact_release_adjudication']['proposal_bootstrap']['proposal_audio']['phonetic_evidence']
    q=build_context_adjudication_request(text,_candidate_blind_missing_proposal_scaffold(text,finding),clip_context=context,require_complete_utterance_support=True)
    # Original candidate-free request matches; no alteration to this receipt.
    assert build_witness_request(q)['request_sha256']==audio['request_sha256']
    calls=[]
    class Cache:
        def __call__(self,request):
            assert request['request_sha256']==audio['request_sha256'];return deepcopy(audio)
        probe_witness_cache=__call__
        def observe_cached_independent_witnesses(self,q):return []
        def observe_range_context(self,**kw):raise AssertionError('fixture says no useful additional acquisition')
    def transport(prompt):
        calls.append(prompt)
        assert '# 字幕缺失候选重建' not in prompt
        if 'CPA 补听范围选择'in prompt:
            return '{"decision":"NO_NEW_AUDIO","reason":"Recorded-fixture boundary remains unresolved; no new sound supplied."}'
        return json.dumps({'choice':'CURRENT','needs_audio':True,'current_utterance_supported':False,
            'current_utterance_support_reason':'Fixture preserves current shen me support and unresolved old guo window attribution.',
            'reason':'Actual cached sound was already offered to proposal; this current comparison still needs the boundary question.'})
    transport.cpa_cache_identity=json.loads(next(iter(before.values())))['request']['model']
    output,ad=adjudicate_context_finding(text,finding,entity_verifier=Cache(),judge_llm_call=rec.wrap(transport),clip_context=context,require_complete_utterance_support=True)
    assert output==text and not ad['repaired'] and calls
    assert ad['proposal_bootstrap']['status']=='RECORDED_CANDIDATE_FOR_VERIFICATION'
    assert ad['proposal_bootstrap']['recorded_candidate']['candidate']=='什么'
    assert ad['executed_span_judgments']['expected_executions']
    assert any('CPA 补听范围选择'in p for p in calls),(rec.audit(),ad)
    persist_review_audit(tmp_path/'current-verification.json',ad);assert cold(tmp_path/'current-verification.json').returncode==0
    for name,raw in before.items():assert(rec.directory/name).read_bytes()==raw


@pytest.mark.parametrize('status',['STARTED','OUTCOME_UNKNOWN','ERROR'])
def test_actual_first_verification_unknown_or_failed_phase_not_reopened(real,tmp_path,status):
    from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
    _,d=real;f=deepcopy(load_review(d/'stomach/review-05.json')['findings'][8]);ad=f['exact_release_adjudication'];q=ad['request'];w=ad['verdict'];text=(d/'stomach/final.srt').read_text()
    context=load_review(real[0].parent.parent/'fourcase-counterexample-fix-20261017/SOURCE-CONTEXTS.json')['stomach']['content']
    rec=SemanticAttemptRecorder(srt_text=text,finding=f,clip_context=context,offset_ms=0);old=d/'runtime-state/cache/semantic-cpa-attempts'/rec.issue
    rec.directory.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(old,rec.directory)
    calls=[]
    def transport(prompt):calls.append(prompt);raise TimeoutError('Explicit low-level uncertain transport fixture')
    transport.cpa_cache_identity=load_review(next(old.glob('*.json')))['request']['model']
    adjudicate_with_witness(check_request=q,witness=w,llm_call=rec.wrap(transport),clip_context=context)
    assert len(calls)==1
    newest=next(p for p in rec.directory.glob('*.json')if not(old/p.name).exists());entry=load_review(newest)
    assert entry['status']=='OUTCOME_UNKNOWN';entry['status']=status;newest.write_text(json.dumps(entry))
    # Status variants are fixtures, not mutations of original runtime/receipts.
    again=SemanticAttemptRecorder(srt_text=text,finding=f,clip_context=context,offset_ms=0)
    from src.autoslice.independent_context_adjudication import bind_request
    changed=bind_request(q,reason='Same material, cosmetic question change')
    _,_,blocked=adjudicate_with_witness(check_request=changed,witness=w,llm_call=again.wrap(transport),clip_context=context)
    assert len(calls)==1 and blocked['judge']['status']=='JUDGE_UNAVAILABLE'


def test_public_producer_typed_persistence_and_cold_gate_remain_real(tmp_path,monkeypatch):
    from tests.test_wholefilm_gate_three_gaps import test_real_producer_plurality_persistence_and_fresh_process_gate
    test_real_producer_plurality_persistence_and_fresh_process_gate(tmp_path,monkeypatch,True,False)
    authority=load_review(tmp_path/'case/authority.json')
    from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
    assert validate_persisted_judge_scopes(authority)>0


def test_unscoped_legacy_cache_without_wire_raw_is_explicit_not_fake_model(tmp_path):
    calls=[]
    def llm(prompt):
        calls.append(prompt)
        return json.dumps({'choice':'CURRENT','needs_audio':True,'reason':'Synthetic original unscoped reply',
                           'current_utterance_supported':False,'current_utterance_support_reason':'unresolved'})
    llm.cpa_cache_identity={'transport':'fixture','models':['legacy-unscoped'],'effort':'high'}
    text=srt('喜欢老');finding={'cue_index':1,'suspect':'','suggestion':'女人','proposed_full_cue':'喜欢老女人','repair_class':'spoken_unit','span_start_codepoint':3,'span_end_codepoint':3}
    observer=Observers(heard='xi huan lao')
    adjudicate_context_finding(text,finding,entity_verifier=observer,judge_llm_call=llm,require_complete_utterance_support=True)
    root=Path(os.environ['AUTOSLICE_BASE'])/'cache/judge-verdicts';modified=0
    for p in root.glob('[a-f0-9][a-f0-9]/*.json'):
        v=load_review(p);j=v['verdict']
        for key in ['span_review_raw_completion','span_compilation','span_compilation_required']:j.pop(key,None)
        p.write_text(json.dumps(v));modified+=1
    assert modified
    before=len(calls)
    _,ad=adjudicate_context_finding(text,finding,entity_verifier=observer,judge_llm_call=llm,require_complete_utterance_support=True)
    assert len(calls)==before
    h=ad['executed_span_judgments'];missing=[e for e in h['expected_executions']if e.get('raw_status')=='LEGACY_RAW_UNAVAILABLE']
    assert missing
    assert all(e['raw_completion'] is None for e in missing)
    persist_review_audit(tmp_path/'legacy-missing-raw.json',ad);p=cold(tmp_path/'legacy-missing-raw.json');assert p.returncode==0,p.stderr
