"""The C task owner passes current M/text into the native TASK_STOP compiler."""
import hashlib

import pytest

from tests.test_targeted_relisten_public_contract import rig,three_observations,acquire_range,observe_plan
from src.autoslice import finding_closure_shell as shell, exact_final_terminal_funnel as funnel
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice import targeted_relisten as owner


def test_real_c_stop_callback_uses_task_inputs_and_never_a_zero_change_round(rig,monkeypatch):
    _,check,finding,_=three_observations(rig)
    _,recovery,_=acquire_range(rig,finding,check,lambda data:observe_plan(1,3))
    check=recovery['terminal_check_request'];text=check['whole_clip_current_srt']
    inputs=shell.OwnerInputs('a'*64,0,RetainedObject.from_value(rig.context),
        RetainedObject.from_value({'test':'actual-owner.v1'}),
        RetainedObject.from_value({'transport':'recorded','models':['actual-offline'],'effort':'high'}))
    current=shell.ExistingFindingOwners(inputs);seen=[]
    def terminalize(srt,audit,*,pass_receipts,adapters,task_materials):
        assert srt==text and pass_receipts==[]
        material=task_materials(check,finding)
        bound=dict(check);bound['task_stop_materials']=material
        bound.pop('request_sha256',None);bound['request_sha256']=owner.digest(bound)
        proof=owner.compile_task_stop_no_gain(check=bound,finding_materials=material,
            text_version_sha256=hashlib.sha256(srt.encode()).hexdigest())
        assert owner.validate_current_no_gain(proof,bound)==proof
        assert proof['proof_kind']=='TASK_STOP' and 'pass_receipts' not in proof
        assert proof['material_exhaustion']['unknown_accounting']=='RETAINED'
        absent=dict(bound);absent.pop('task_stop_materials')
        with pytest.raises(ValueError,match='TASK_STOP_CURRENT_MATERIALS_REQUIRED'):
            owner.validate_current_no_gain(proof,absent)
        seen.append(proof)
        # The stop proof is evidence only: this probe deliberately returns no
        # ruling, and the real C cardinality guard must still refuse release.
        return {'findings':[],'unresolved_findings_disclosed':[]}
    monkeypatch.setattr(funnel,'terminalize_review',terminalize)
    monkeypatch.setattr(funnel,'review_pass_receipt',lambda **_:pytest.fail('INVENTED_LOOP_PASS'))
    with pytest.raises(shell.ClosureContractError,match='OWNER_TASK_CARDINALITY'):
        current._terminal_from_snapshot(text,RetainedObject.from_value(finding),{},None)
    assert len(seen)==1

from tests.test_exact_final_funnel import terminal_rig, SRT, unresolved, answer


def test_task_stop_runs_the_real_terminal_and_original_cold_keep_gate(terminal_rig):
    import json
    from src.autoslice.finding_context import materials_for_check
    from src.autoslice.final_review_contract import is_release_resolved_keep
    def call(prompt):return json.dumps(answer(),ensure_ascii=False)
    def materials(check,row):
        projection=owner.evidence_projection(check,include_target_text=True)
        return materials_for_check(check,finding=row,contracts={'test':'actual-terminal-owner.v1'},
            model_identity={'transport':'recorded','models':['offline-actual'],'effort':'high'},
            observation_ids=[v['observation_id'] for v in projection['observations']])
    result=funnel.terminalize_review(SRT,{'findings':[unresolved()],
        'validated_finding_count':1,'status':'FLAGGED','release_gate':'BLOCK',
        'reason_codes':['FINAL_REVIEW_UNRESOLVED_FINDINGS']},pass_receipts=[],
        adapters={'clip_context':terminal_rig.context,'entity_verifier':terminal_rig.verifier,
            'judge_llm_call':call,'source_media_timeline_offset_ms':terminal_rig.offset_ms},
        task_materials=materials)
    assert result['findings']==[] and result['release_gate']=='PASS', result.get('terminal_funnel')
    row,=result['unresolved_findings_disclosed']
    assert is_release_resolved_keep(row)
    request=row['exact_release_adjudication']['terminal_review']['request']
    proof=request['audio_gain_termination']
    assert proof['proof_kind']=='TASK_STOP' and proof['authority']=='EVIDENCE_ONLY'
    assert owner.validate_current_no_gain(proof,request['check_request'])==proof
    damaged=json.loads(json.dumps(row))
    damaged['exact_release_adjudication']['terminal_review']['request']['check_request'].pop('task_stop_materials')
    assert not is_release_resolved_keep(damaged)

    unjudged=json.loads(json.dumps(row))
    unjudged['exact_release_adjudication'].pop('terminal_review')
    assert not is_release_resolved_keep(unjudged)
