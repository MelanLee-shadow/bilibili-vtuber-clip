"""Real call points with recorded providers; queued for OCI3, never API inference."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from threading import Event

import pytest
from tests.test_exact_final_funnel import terminal_rig  # noqa: F401

from src.autoslice import finding_closure as c, finding_closure_shell as shell, finding_context as context
from src.autoslice.finding_closure_ports import RetainedObject, PreparedFinding, RebaseRequest, ProjectionBasis
from src.autoslice.completed_review_checkpoint import ruling_response,CompletedReviewCheckpoint
from src.autoslice.cpa_model_ladder import CPA_MODEL_LADDER
from src.autoslice.llm_client import CpaCompletion
from tests.test_wpb_finding_context import check


def prepared(request, cue=3):
    request=deepcopy(request)
    request['finding_context']=context.context_for_check(request)
    material=context.materials_for_check(request,contracts={'meaning':'recorded'},
        model_identity={'transport':'recorded','models':[CPA_MODEL_LADDER[0]],'effort':'medium'})
    row=RetainedObject.from_value({'cue_index':cue,'proposed_full_cue':request['proposed_cue']})
    bound=RetainedObject.from_value(request);materials=RetainedObject.from_value(material)
    scope=tuple(request['finding_context']['scope_cue_indexes'])
    spec=c.TaskSpec(c.FindingKey(material['physical_question_sha256'],material['materials_sha256']),scope,
        c.local_text_sha(request['whole_clip_current_srt'],scope),row.sha256,bound.sha256,materials.sha256,bound.sha256)
    return PreparedFinding(spec,row,bound,materials,bound)


def proposal_inputs(finding):
    from src.autoslice.closed_set_proposal_rebuild import (
        _PROPOSAL_REBUILD_SCHEMA,_PROPOSAL_REBUILD_PROMPT,TOPIC_PHONEME_POLICY)
    data={'phonemes':{'pinyin':'recorded'},'asr':{'status':'UNKNOWN_RETAINED'},'topics':'recorded topic',
        'finding':{'repair_class':'disclosure_only'},'before':'前文','after':'后文'}
    typed=context.decision_materials(finding.check.value(),data=data,
        contracts={'proposal_schema':_PROPOSAL_REBUILD_SCHEMA,'instructions':_PROPOSAL_REBUILD_PROMPT,
            'policy':TOPIC_PHONEME_POLICY},model_identity=finding.materials.value()['model_identity'])
    return {'finding_materials':typed,'original_check_request':finding.check.value(),'consumption_input':data}


def router(tmp_path,monkeypatch,behavior):
    from src.autoslice import producer_final_review_transport as transport
    calls=[]
    def factory(*,runtime,effort,model=CPA_MODEL_LADDER[0],exact_model=False):
        def call(prompt):
            calls.append((model,prompt))
            return behavior(model,prompt)
        call.cpa_cache_identity={'transport':'recorded','models':[model],'effort':effort}
        call.provider_runtime_binding={'provider_transport':'recorded','provider_credential_source':'recorded-owner'}
        call.completed_review_root=tmp_path/'canonical'
        call.pinned_model_route=exact_model  # Recorded routes still consume the actual pin ABI.
        return call
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    monkeypatch.setattr(transport,'_local_runtime_call',factory)
    result=transport.build_final_review_llm_call(runtime_root=tmp_path,runtime_ssh_host='localhost')
    assert isinstance(result,shell.FinalReviewCall)
    return result,calls


def ask(call,finding,inputs):
    with shell.ruling_capture(call,finding) as capture:
        raw,hit=ruling_response(stage='proposal-rebuild',prompt='recorded proposal question',
            llm_call=call,inputs=inputs,parser_path=Path(__file__))
    return raw,hit,capture


@pytest.mark.parametrize('entry', ['direct', 'checkpoint'])
def test_canonical_c_route_uses_only_physical_shared_window(tmp_path,monkeypatch,entry):
    from src.autoslice import provider_slots as slots
    runtime=tmp_path/'runtime'
    monkeypatch.setenv('AUTOSLICE_BASE',str(runtime))
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY','20')
    def behavior(model,_prompt):
        with slots.runtime_provider_slot(provider='cpa',modality='text') as lease:
            state=json.loads((runtime/'provider-slots/window.json').read_text())
            assert state['active']==[lease.slot_index]
        return CpaCompletion('{"status":"NO_GAIN"}',model)
    call,calls=router(tmp_path,monkeypatch,behavior)
    controller=shell.ProviderController(shared_runtime=True)
    def forbidden(*_args,**_kwargs):
        pytest.fail('canonical C route must not add a process-private admission window')
    monkeypatch.setattr(controller,'acquire',forbidden)
    monkeypatch.setattr(controller,'finish',forbidden)
    call.controller=controller
    if entry=='direct':
        assert str(call('recorded'))=='{"status":"NO_GAIN"}'
    else:
        finding=prepared(check());inputs=proposal_inputs(finding)
        first,hit,_=ask(call,finding,inputs)
        assert not hit
        again,hit,_=ask(call,finding,inputs)
        assert hit and again==first
    call.drain()
    assert len(calls)==1 and not controller.windows
    state=json.loads((runtime/'provider-slots/window.json').read_text())
    assert state['active']==[] and state['limit']==21


def test_real_builder_checkpoint_returns_consumed_material_and_actual_fallback(tmp_path,monkeypatch):
    call,calls=router(tmp_path,monkeypatch,lambda _model,_prompt:CpaCompletion('{"status":"NO_GAIN"}',CPA_MODEL_LADDER[1]))
    finding=prepared(check());inputs=proposal_inputs(finding)
    raw,hit,capture=ask(call,finding,inputs)
    call.drain()
    assert not hit and len(calls)==1
    consumed,=capture.records
    assert consumed.check==finding.check
    assert consumed.model_identity.value()['models']==[CPA_MODEL_LADDER[1]]
    assert consumed.materials.value()['model_identity']['models']==[CPA_MODEL_LADDER[1]]
    assert shell.consumed_ruling_from_document(consumed.document())==consumed
    with shell.ruling_capture(call,finding) as active:
        active.records.extend(capture.records)
        row=shell.attach_f2_consumption({'raw_completion':raw,'proposed_full_cue':None})
    # A returned candidate can shrink; the provider's consumed set stays intact.
    assert finding.check.value()['proposed_cue'] in consumed.materials.value()['candidates']
    assert row['proposed_full_cue'] is None and row['consumed_ruling']['materials']==consumed.materials.value()
    again,hit,replay=ask(call,finding,inputs)
    assert hit and again==raw and len(calls)==1
    assert replay.records[0].checkpoint==consumed.checkpoint


def test_unknown_answer_model_is_retained_and_never_inferred_on_resume(tmp_path,monkeypatch):
    call,calls=router(tmp_path,monkeypatch,lambda _model,_prompt:'original unbound bytes')
    finding=prepared(check());inputs=proposal_inputs(finding)
    for _ in range(2):
        with pytest.raises(c.ClosureContractError,match='ANSWER_MODEL_UNKNOWN|PRIOR_TRANSPORT_RECOVERY_REQUIRED'):
            ask(call,finding,inputs)
    call.drain()
    assert len(calls)==1
    assert list((tmp_path/'canonical').rglob('winner.json'))
    assert any(json.loads(p.read_text()).get('response')=='original unbound bytes'
               for p in (tmp_path/'canonical').rglob('*.json'))


def test_invalid_json_reuses_raw_without_another_call(tmp_path,monkeypatch):
    call,calls=router(tmp_path,monkeypatch,lambda model,_prompt:CpaCompletion('invalid JSON original',model))
    finding=prepared(check());inputs=proposal_inputs(finding)
    for _ in range(2):
        raw,_hit,_capture=ask(call,finding,inputs)
        with pytest.raises(json.JSONDecodeError):json.loads(raw)
    call.drain()
    assert len(calls)==1


def test_transport_without_exact_model_pin_is_refused_before_dispatch(tmp_path,monkeypatch):
    call,calls=router(tmp_path,monkeypatch,lambda model,_prompt:CpaCompletion('must not call',model))
    primary=call.raw_call(call.route(CPA_MODEL_LADDER[0]))
    primary.pinned_model_route=False  # Canonical script currently expands TOP into fallback routes.
    finding=prepared(check())
    with pytest.raises(c.ClosureContractError,match='EXACT_MODEL_ROUTE_REQUIRED'):
        ask(call,finding,proposal_inputs(finding))
    call.drain()
    assert calls==[]
    assert not list((tmp_path/'canonical').rglob('winner.json'))


def test_hedge_runs_through_canonical_owner_and_retains_both_routes(tmp_path,monkeypatch):
    late=Event()
    def behavior(model,_prompt):
        if model==CPA_MODEL_LADDER[0]:assert late.wait(5)
        return CpaCompletion('raw '+model,model)
    call,calls=router(tmp_path,monkeypatch,behavior)
    history=tmp_path/'canonical/finding-closure-routing/time-spans.jsonl'
    history.parent.mkdir(parents=True)
    begin=datetime(2026,1,1,tzinfo=timezone.utc)
    history.write_text(''.join(json.dumps({'schema_version':'run-time-span.v1','span_id':str(i),'kind':'wait',
        'name':'recorded','model':CPA_MODEL_LADDER[0],'stage':'proposal-rebuild','status':'REPLIED',
        'cache_hit':False,'started_utc':begin.isoformat(),'ended_utc':(begin+timedelta(milliseconds=2)).isoformat()})+'\n'
        for i in range(30)))
    finding=prepared(check());inputs=proposal_inputs(finding)
    raw,_hit,capture=ask(call,finding,inputs)
    assert raw=='raw '+CPA_MODEL_LADDER[1] and not late.is_set()
    late.set();call.drain()
    assert len(calls)==2 and capture.records[0].model_identity.value()['models']==[CPA_MODEL_LADDER[1]]
    before=list(calls)
    assert ask(call,finding,inputs)[0]==raw and calls==before
    originals=[json.loads(p.read_text()).get('response') for p in (tmp_path/'canonical').rglob('*.json')]
    assert all('raw '+model in originals for model in CPA_MODEL_LADDER)


def test_changed_original_checkpoint_or_consumed_material_cannot_be_resigned(tmp_path,monkeypatch):
    call,_=router(tmp_path,monkeypatch,lambda model,_prompt:CpaCompletion('recorded answer',model))
    finding=prepared(check());_,_,capture=ask(call,finding,proposal_inputs(finding));call.drain()
    record=capture.records[0]
    value=record.document().value()
    value['materials']['contracts']['invented']='different policy'
    value['materials']['materials_sha256']=context.digest({k:v for k,v in value['materials'].items()
        if k not in {'materials_sha256','physical_question_sha256'}})
    with pytest.raises(c.ClosureContractError,match='CONSUMED_BINDING_DRIFT'):
        shell.consumed_ruling_from_document(RetainedObject.from_value(value))
    snapshot=record.checkpoint.value();original=snapshot['original']
    cp=CompletedReviewCheckpoint(Path(snapshot['root']),original['binding'],original['request'])
    for path in cp.directory.glob('*.json'):path.write_text('tampered original bytes')
    with pytest.raises(ValueError):shell.consumed_ruling_from_document(record.document())


def test_retention_decline_never_writes_a_routing_raw_copy(tmp_path,monkeypatch):
    from src.autoslice.completed_review_checkpoint import ruling_material_scope
    call,_=router(tmp_path,monkeypatch,lambda model,_prompt:CpaCompletion('declined original',model))
    finding=prepared(check())
    with ruling_material_scope({},retain=lambda _raw:False):
        with pytest.raises(c.ClosureContractError,match='RETENTION_DECLINED'):
            ask(call,finding,proposal_inputs(finding))
    with pytest.raises(c.ClosureContractError,match='RETENTION_DECLINED'):call.drain()
    assert not list((tmp_path/'canonical').rglob('winner.json'))
    assert not any('declined original' in p.read_text() for p in (tmp_path/'canonical').rglob('*.json'))


def drop_case():
    import hashlib
    current = "咳咳"
    base_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    srt_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n咳咳\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n保留下一句\n"
    )
    witness_request_sha256 = "a" * 64
    finding = {
        "cue_index": 1,
        "base_text_sha256": base_sha256,
        "proposed_full_cue": "",
        "repair_class": "acoustic_drop_cue",
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE",
            "witness_authority": "EVIDENCE_ONLY",
            "repaired": True,
            "timing_immutable": True,
            "policy_branch": "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE",
            "mutation_authority": {
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "PASS",
                "basis": "CPA_EXPLICIT_INAUDIBLE_DROP",
            },
            "request": {
                "schema_version": (
                    "subtitle-span-acoustic-check-request.v1"
                ),
                "request_sha256": "f" * 64,
                "base_text_sha256": base_sha256,
                "current_cue": current,
                "proposed_cue": "",
                "repair_class": "acoustic_drop_cue",
            },
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "OBSERVED",
                "request_sha256": witness_request_sha256,
                "target_audible": False,
            },
            "witness_judge": {
                "decision_authority": "CPA_JUDGE",
                "witness_authority": "EVIDENCE_ONLY",
                "selected_action": "DROP_CUE",
                "selected_repair_class": "acoustic_drop_cue",
                "selected_target_cue": "",
                "judge": {
                    "schema_version": "acoustic-witness-adjudication.v1",
                    "status": "JUDGED",
                    "choice": "DROP",
                    "prompt_sha256": "b" * 64,
                    "completion_sha256": "c" * 64,
                    "check_request_sha256": "f" * 64,
                    "decision_contract": (
                        "inaudible-current-proposed-drop.v1"
                    ),
                    "choice_set": ["CURRENT", "DROP", "PROPOSED"],
                }
            },
            "drop_authority": {
                "schema_version": (
                    "subtitle-cpa-inaudible-drop-authority.v1"
                ),
                "status": "PASS",
                "decision_authority": "CPA_JUDGE",
                "choice": "DROP",
                "decision_contract": "inaudible-current-proposed-drop.v1",
                "original_request_sha256": "sha256:" + "f" * 64,
                "effective_drop_request_sha256": "sha256:" + "f" * 64,
                "original_witness_request_sha256": (
                    "sha256:" + witness_request_sha256
                ),
                "effective_witness_request_sha256": (
                    "sha256:" + witness_request_sha256
                ),
                "judge_prompt_sha256": "sha256:" + "b" * 64,
                "judge_completion_sha256": "sha256:" + "c" * 64,
                "target_audible": False,
                "timing_immutable": True,
            },
        },
    }

    return srt_text,finding


def test_legal_drop_has_explicit_absence_and_reindexes_surviving_questions(monkeypatch):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    before,row=drop_case()
    after,repairs=_apply_exact_final_cpa_repairs(before,{'findings':[row]})
    request={'whole_clip_current_srt':before,'cue_indexes':[1],'current_cue':'咳咳','proposed_cue':'',
        'matched_start_ms':0,'matched_end_ms':1000,'source_media_timeline_offset_ms':0,
        'clip_context_binding':{'candidate_id':'recorded','source_media_sha256s':['a'*64]}}
    finding=prepared(request,cue=1)
    owners=shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,0,RetainedObject.from_value({}),
        RetainedObject.from_value({'meaning':'recorded'}),
        RetainedObject.from_value(finding.materials.value()['model_identity'])))
    basis=ProjectionBasis(before,after,RetainedObject.from_value({'repairs':repairs}),RetainedObject.from_value(row))
    absent,=owners.rebase(RebaseRequest(after,(finding,),(basis,)))
    assert absent.spec.presence=='ABSENT' and absent.projection is not None
    assert absent.check.value()['presence']=='ABSENT' and 'current_cue' not in absent.check.value()
    assert absent.spec.key.physical==finding.spec.key.physical
    assert c.task_closed(c.Task(absent.spec,c.State.EDIT_REVIEW,absent.spec.applied_ruling_ref))
    with pytest.raises(c.ClosureContractError,match='PROJECTION_WITHOUT_LEGAL_PATCH'):
        owners.rebase(RebaseRequest(after,(finding,)))
    bad=replace(basis,after_srt=after.replace('00:00:02,000','00:00:02,001'))
    with pytest.raises(c.ClosureContractError,match='PROJECTION_WITHOUT_LEGAL_PATCH'):
        owners.rebase(RebaseRequest(bad.after_srt,(finding,),(bad,)))


def test_real_provider_429_diagnostics_shrink_and_respect_retry_after():
    from src.autoslice.llm_client import LlmCallError
    controller=shell.ProviderController(clock=lambda:100.0)
    selected=shell.Route('recorded','text',RetainedObject.from_value({'models':['recorded']}),'recorded-key')
    controller.acquire(selected)
    failure=LlmCallError('recorded quota signal',provider_diagnostics={
        'provider_http_status':429,'provider_retry_after_seconds':7})
    controller.finish(selected,shell._feedback(failure))
    assert controller.windows[('recorded','text')].limit==10
    assert controller.windows[('recorded','text')].retry_at==107


def test_legacy_f2_catch_cannot_hide_original_program_exception(tmp_path,monkeypatch):
    boom=TypeError('recorded programming defect')
    def behavior(_model,_prompt):raise boom
    call,_=router(tmp_path,monkeypatch,behavior)
    finding=prepared(check())
    with shell.ruling_capture(call,finding) as captured:
        try:
            ruling_response(stage='proposal-rebuild',prompt='recorded',llm_call=call,
                inputs=proposal_inputs(finding),parser_path=Path(__file__))
        except TypeError:
            pass  # Simulate an old broad F2 catch; C still owns this failure.
        with pytest.raises(TypeError) as rejected:captured.raise_failure()
        assert rejected.value is boom
    call.drain()


def test_shared_audio_admission_hook_is_nested_once_and_uses_structured_feedback(monkeypatch):
    controller=shell.ProviderController(clock=lambda:10.0)
    from src.autoslice import provider_admission as admission_owner
    monkeypatch.setattr(admission_owner,'PROVIDER_CONTROLLER',controller)
    with shell.provider_admission('recorded','audio') as report:
        with shell.provider_admission('recorded','audio'):
            assert controller.windows[('recorded','audio')].inflight==1
        report([{'provider_diagnostics':{'provider_http_status':429,'provider_retry_after_seconds':3}}])
    assert controller.windows[('recorded','audio')].limit==10
    assert controller.windows[('recorded','audio')].retry_at==13
    assert controller.windows[('recorded','audio')].inflight==0


def test_cache_only_recovery_cannot_dispatch_when_the_raw_cache_is_missing(tmp_path,monkeypatch):
    call,calls=router(tmp_path,monkeypatch,lambda model,_prompt:CpaCompletion('must not dispatch',model))
    finding=prepared(check())
    with shell.provider_permission(False):
        with pytest.raises(c.ClosureContractError,match='COLD_REPLAY_FORBIDS_CALL'):
            ask(call,finding,proposal_inputs(finding))
    call.drain()
    assert calls==[]


def test_discovery_bundle_retains_all_candidates_ids_and_stable_whole_cue_p(monkeypatch,terminal_rig):
    from tests.test_exact_final_funnel import SRT as source_text
    from src.autoslice.finding_context import physical_question
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    first={'cue_index':1,'suspect':'啊','suggestion':'呀','proposed_full_cue':'我喜欢她呀',
        'repair_class':'disclosure_only','why':'recorded first nomination'}
    second={**first,'suggestion':'呢','proposed_full_cue':'我喜欢她呢','why':'recorded second nomination'}
    row={**first,'discovery_candidates':[first,second],
        'discovery_nominations':[{'observation_id':'nomination-a'},{'observation_id':'nomination-b'}]}
    owners=shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,terminal_rig.offset_ms,
        RetainedObject.from_value(terminal_rig.context),RetainedObject.from_value({'meaning':'recorded'}),
        RetainedObject.from_value({'transport':'recorded','models':[CPA_MODEL_LADDER[0]],'effort':'medium'})))
    finding=owners.prepare_finding(source_text,RetainedObject.from_value(row))
    assert {'我喜欢她呀','我喜欢她呢'}<=set(finding.materials.value()['candidates'])
    assert {'nomination-a','nomination-b'}<=set(finding.materials.value()['bound_observation_ids'])
    assert physical_question(finding.origin_check.value())['codepoint_span'] is None
    assert finding.origin_check.value()['scope_role']=='QUESTION_IDENTITY_ONLY'
    assert finding.origin_check.value()['mutation_authorized'] is False


def test_protected_human_cue_remains_protected_after_an_earlier_cue_is_dropped():
    before,_=drop_case()
    after='1\n00:00:02,000 --> 00:00:03,000\n保留下一句\n'
    owners=shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,0,RetainedObject.from_value({}),
        RetainedObject.from_value({'meaning':'recorded'}),
        RetainedObject.from_value({'models':[CPA_MODEL_LADDER[0]]}),
        protected_cues=frozenset({2}),protected_windows=((2000,3000),)))
    assert owners._protected_indexes(before)==frozenset({2})
    assert owners._protected_indexes(after)==frozenset({1})
    with pytest.raises(c.ClosureContractError,match='PROTECTED_WINDOW_REMOVED'):
        owners._protected_indexes(after.replace('00:00:02,000','00:00:02,001'))


def test_unrelated_drop_changes_rendering_indexes_without_reopening_a_closed_m():
    from tests.test_wpb_finding_context import WORDS
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    before,row=drop_case()
    for i in range(3,16):
        start=10+i*2
        before+=f'\n{i}\n00:00:{start:02},000 --> 00:00:{start+1:02},000\n{WORDS[i-1]}\n'
    after,repairs=_apply_exact_final_cpa_repairs(before,{'findings':[row]})
    last=parse_srt_cues(before)[-1]
    request={'whole_clip_current_srt':before,'cue_indexes':[15],'current_cue':last.text,'proposed_cue':last.text,
        'matched_start_ms':last.start_ms,'matched_end_ms':last.end_ms,'source_media_timeline_offset_ms':0,
        'clip_context_binding':{'candidate_id':'recorded','source_media_sha256s':['a'*64]}}
    original=prepared(request,cue=15)
    owners=shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,0,RetainedObject.from_value({}),
        RetainedObject.from_value({'meaning':'recorded'}),
        RetainedObject.from_value(original.materials.value()['model_identity'])))
    basis=ProjectionBasis(before,after,RetainedObject.from_value({'repairs':repairs}),RetainedObject.from_value(row))
    current,=owners.rebase(RebaseRequest(after,(original,),(basis,)))
    assert current.spec.key==original.spec.key and current.check==original.check
    assert current.spec.scope_cues!=original.spec.scope_cues
    assert c.local_content_sha(before,original.spec.scope_cues)==c.local_content_sha(after,current.spec.scope_cues)


@pytest.mark.parametrize('exact',[False,True])
def test_exact_model_bridge_is_opt_in_and_uses_original_ssh_transport(tmp_path,monkeypatch,exact):
    from types import SimpleNamespace
    from scripts import llm_via_runtime_cpa_ssh as bridge
    seen=[]
    prompt=tmp_path/'prompt';prompt.write_text('recorded question')
    def fake_run(command,**kwargs):
        seen.append((command,json.loads(kwargs['input'])))
        return SimpleNamespace(returncode=0,stdout=json.dumps({'status':'PASS','completion':'original',
            'diagnostics':{'provider_transport':'recorded'}}),stderr='')
    monkeypatch.setattr(bridge.subprocess,'run',fake_run)
    bridge.run_bridge(prompt_file=prompt,completion_file=tmp_path/'completion',ssh_host='recorded',
        runtime_root=tmp_path/'runtime',model=CPA_MODEL_LADDER[0],effort='medium',attempts=1,exact_model=exact)
    command,request=seen[0]
    assert command[:len(bridge.SSH_PREFIX)]==bridge.SSH_PREFIX
    assert request.get('exact_model',False) is exact
    if not exact:assert 'exact_model' not in request
    assert (tmp_path/'completion').read_text()=='original'
    # The real remote program forwards only an explicit opt-in argv flag.
    import ast
    tree=ast.parse(bridge.REMOTE_PROGRAM)
    dispatches=[n for n in ast.walk(tree) if isinstance(n,ast.Call)
        and isinstance(n.func,ast.Attribute) and n.func.attr=='run']
    assert any('--exact-model' in ast.unparse(n.args[0]) for n in dispatches)


def test_exact_model_pin_rejects_chains_before_a_bridge_dispatch(tmp_path,monkeypatch):
    from scripts import llm_via_runtime_cpa_ssh as bridge
    from src.autoslice.cpa_model_ladder import resolve_models
    assert resolve_models(CPA_MODEL_LADDER[0])==CPA_MODEL_LADDER
    assert resolve_models(CPA_MODEL_LADDER[0],exact_model=True)==CPA_MODEL_LADDER[:1]
    prompt=tmp_path/'prompt';prompt.write_text('question')
    calls=[]
    monkeypatch.setattr(bridge.subprocess,'run',lambda *_a,**_k:calls.append(1))
    for model in ('', ' '.join(CPA_MODEL_LADDER),'unknown'):
        with pytest.raises(ValueError,match='CPA_EXACT_MODEL_REQUIRES_ONE_CANONICAL_MODEL'):
            bridge.run_bridge(prompt_file=prompt,completion_file=tmp_path/'completion',ssh_host='recorded',
                runtime_root=tmp_path/'runtime',model=model,effort='medium',attempts=1,exact_model=True)
    assert not calls and not (tmp_path/'completion').exists()


def test_native_dispatch_uses_shared_admission_and_cache_hit_has_no_feedback(tmp_path,monkeypatch):
    from hashlib import sha256
    from src.autoslice import subtitle_audio_evidence as owner,diarized_transcription as native
    from src.autoslice.mai_transcription import MAI_MODEL
    controller=shell.ProviderController(clock=lambda:100.0)
    from src.autoslice import provider_admission as admission_owner
    monkeypatch.setattr(admission_owner,'PROVIDER_CONTROLLER',controller)
    calls=[];audio=b'recorded native input'
    def transcribe(data,**kwargs):
        assert controller.windows[('mai','audio')].inflight==1
        calls.append((data,kwargs))
        return {'provider':'mai','model':MAI_MODEL,'input_audio_sha256':sha256(data).hexdigest(),
            'native_segments':[],'raw_response':{'text':'recorded raw native bytes'}}
    monkeypatch.setattr(native,'transcribe_evidence',transcribe)
    path=tmp_path/'audio.mp3'
    first=owner.observe_secondary(audio,media_path=path,provider='mai',duration_ms=1000)
    assert first['served_from_cache'] is False and len(calls)==1
    before=controller.windows[('mai','audio')]
    assert before.inflight==0 and before.limit==21
    with shell.provider_permission(False):
        second=owner.observe_secondary(audio,media_path=path,provider='mai',duration_ms=1000)
    assert second['served_from_cache'] is True and len(calls)==1
    assert controller.windows[('mai','audio')]==before
    with shell.provider_permission(False),pytest.raises(c.ClosureContractError,match='COLD_REPLAY_FORBIDS_CALL'):
        owner.observe_secondary(b'a different uncached input',media_path=tmp_path/'other.mp3',
            provider='mai',duration_ms=1000)
    assert len(calls)==1 and controller.windows[('mai','audio')]==before


def test_native_structured_429_updates_same_controller_and_preserves_exception(tmp_path,monkeypatch):
    from src.autoslice import subtitle_audio_evidence as owner,diarized_transcription as native
    controller=shell.ProviderController(clock=lambda:100.0)
    from src.autoslice import provider_admission as admission_owner
    monkeypatch.setattr(admission_owner,'PROVIDER_CONTROLLER',controller)
    boom=native.DiarizedTranscriptionError('RECORDED_HTTP_429','original failure',
        metadata={'http_status':429,'retry_after_seconds':7})
    def transcribe(*_args,**_kwargs):raise boom
    monkeypatch.setattr(native,'transcribe_evidence',transcribe)
    with pytest.raises(native.DiarizedTranscriptionError) as caught:
        owner.observe_secondary(b'recorded input',media_path=tmp_path/'audio.mp3',provider='mai',duration_ms=1000)
    from src.autoslice.review_errors import ProviderFailure
    assert isinstance(caught.value, ProviderFailure)
    assert caught.value.__cause__ is boom
    assert caught.value.reason_code == boom.reason_code
    window=controller.windows[('mai','audio')]
    assert window.inflight==0 and window.limit==10 and window.retry_at==107
