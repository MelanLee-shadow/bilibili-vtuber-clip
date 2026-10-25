"""Per-task owner proofs through the ordinary offline audio rig."""
from copy import deepcopy
import hashlib

import pytest

from src.autoslice import targeted_relisten as owner, finding_context
from tests.test_targeted_relisten_public_contract import (
    rig,three_observations,acquire_range,observe_plan,
)


def prepared(rig):
    factory=getattr(owner,'compile_task_stop_no_gain',None)
    assert callable(factory),'TASK_STOP_OWNER_API_UNAVAILABLE'
    _,check,finding,_=three_observations(rig)
    _,recovery,_=acquire_range(rig,finding,check,lambda data:observe_plan(1,3))
    check=recovery['terminal_check_request']
    projection=owner.evidence_projection(check)
    materials=finding_context.materials_for_check(check,
        contracts={'test':'actual-owner-protocol.v1'},
        model_identity={'transport':'recorded','models':['offline'],'effort':'high'},
        observation_ids=[row['observation_id'] for row in projection['observations']])
    version=hashlib.sha256(check['whole_clip_current_srt'].encode()).hexdigest()
    proof=factory(check=check,projection=projection,finding_materials=materials,text_version_sha256=version)
    return check,materials,version,proof


def test_task_stop_uses_real_exhaustion_without_a_round_receipt(rig):
    check,materials,version,proof=prepared(rig)
    assert proof['proof_kind']=='TASK_STOP'
    assert 'pass_receipts' not in proof and 'repair_count' not in proof
    assert proof['material_exhaustion']['unknown_accounting']=='RETAINED'
    assert proof['material_exhaustion']['completed_channels']==owner.target_material_exhaustion(check,proof['projection'])['completed_channels']
    assert owner.validate_no_gain(proof,check,finding_materials=materials,text_version_sha256=version)==proof


@pytest.mark.parametrize('field',['finding_key','text_version_sha256','material_exhaustion','projection'])
def test_task_stop_self_consistent_forgery_still_reopens_owner_evidence(rig,field):
    check,materials,version,proof=prepared(rig)
    altered=deepcopy(proof)
    if field=='finding_key':altered[field]['materials_sha256']='0'*64
    elif field=='text_version_sha256':altered[field]='0'*64
    elif field=='material_exhaustion':altered[field]['completed_channels']={}
    else:altered[field]['observations']=[]
    altered['proof_sha256']=owner.digest({k:v for k,v in altered.items() if k!='proof_sha256'})
    with pytest.raises(ValueError):
        owner.validate_no_gain(altered,check,finding_materials=materials,text_version_sha256=version)


@pytest.mark.parametrize('missing',['finding_materials','text_version_sha256'])
def test_task_stop_cold_consumer_requires_independent_current_binding(rig,missing):
    check,materials,version,proof=prepared(rig)
    options={'finding_materials':materials,'text_version_sha256':version}
    del options[missing]
    with pytest.raises(ValueError,match='TASK_STOP_CURRENT_MATERIALS_REQUIRED'):
        owner.validate_no_gain(proof,check,**options)


@pytest.mark.parametrize('field',['contracts','model_identity','bound_observation_ids'])
def test_task_stop_old_proof_cannot_authorize_new_materials(rig,field):
    check,materials,version,proof=prepared(rig)
    changed=deepcopy(materials)
    if field=='contracts':changed[field]['test']='changed-policy.v2'
    elif field=='model_identity':changed[field]['models']=['other-actual-model']
    else:changed[field]=sorted({*changed[field],'new-observation'})
    changed['materials_sha256']=finding_context.digest({k:v for k,v in changed.items()
        if k not in {'physical_question_sha256','materials_sha256'}})
    with pytest.raises(ValueError,match='TASK_STOP_PROOF_DRIFT'):
        owner.validate_no_gain(proof,check,finding_materials=changed,text_version_sha256=version)


def test_task_stop_rejects_materials_for_different_current_context(rig):
    check,materials,version,proof=prepared(rig)
    changed=deepcopy(materials)
    changed['local_context']['cues'][0][-1]='different material text'
    changed['materials_sha256']=finding_context.digest({k:v for k,v in changed.items()
        if k not in {'physical_question_sha256','materials_sha256'}})
    with pytest.raises(ValueError,match='TASK_STOP_MATERIALS_DRIFT'):
        owner.compile_task_stop_no_gain(check=check,finding_materials=changed,text_version_sha256=version)


def test_task_stop_reopens_original_audio_receipts(rig):
    check,materials,version,proof=prepared(rig)
    # The ordinary audio provider owns the raw and its hash. Corrupt its bytes,
    # not a derived counter, and require the cold consumer to reject.
    row=next(row for row in proof['projection']['observations'] if row['receipt_kind']=='blind_pinyin')
    from pathlib import Path
    Path(row['raw_observation']['relisten_artifacts']['response_path']).write_text('{}')
    with pytest.raises(ValueError,match='TASK_STOP_OBSERVATION_DRIFT'):
        owner.validate_no_gain(proof,check,finding_materials=materials,text_version_sha256=version)


def test_task_stop_is_not_a_loop_receipt_or_release_authority(rig):
    check,materials,version,proof=prepared(rig)
    assert proof['authority']=='EVIDENCE_ONLY' and proof['mutation_authorized'] is False
    assert proof['literal_transcription_authorized'] is False
    assert 'stopped_finding' not in proof and 'pass_receipts' not in proof
    with pytest.raises(ValueError,match='TASK_STOP_TEXT_VERSION_DRIFT'):
        owner.compile_task_stop_no_gain(check=check,finding_materials=materials,text_version_sha256='0'*64)


def test_task_stop_persisted_proof_reopens_in_fresh_interpreter(rig,tmp_path):
    import json,os,subprocess,sys
    from pathlib import Path
    check,materials,version,proof=prepared(rig)
    path=tmp_path/'task-stop-cold-input.json'
    path.write_text(json.dumps({'check':check,'materials':materials,'version':version,'proof':proof}))
    script='''import json,socket,sys
from src.autoslice.targeted_relisten import validate_no_gain
def forbidden(*a,**k):raise AssertionError("COLD_NO_NETWORK")
socket.socket.connect=forbidden
x=json.load(open(sys.argv[1]))
assert validate_no_gain(x["proof"],x["check"],finding_materials=x["materials"],text_version_sha256=x["version"])==x["proof"]
print("COLD_TASK_STOP_REOPENED")
'''
    child=subprocess.run([sys.executable,'-B','-c',script,str(path)],text=True,capture_output=True,
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},timeout=30)
    assert child.returncode==0,child.stderr
    assert child.stdout.strip()=='COLD_TASK_STOP_REOPENED'


def test_task_stop_missing_required_audio_channel_stays_blocked(rig):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from tests.test_targeted_relisten_public_contract import SRT,FINDING
    check=build_context_adjudication_request(SRT,FINDING,clip_context=rig.context)
    projection=owner.evidence_projection(check)
    materials=finding_context.materials_for_check(check,contracts={'test':'actual-owner-protocol.v1'},
        model_identity={'transport':'recorded','models':['offline'],'effort':'high'})
    with pytest.raises(owner.ProviderFailure,match='TARGET_MATERIAL_CHANNELS_REQUIRED'):
        owner.compile_task_stop_no_gain(check=check,projection=projection,finding_materials=materials,
            text_version_sha256=hashlib.sha256(SRT.encode()).hexdigest())



def test_task_can_stop_after_a_sibling_patch_without_a_zero_change_round(rig):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from tests.test_targeted_relisten_public_contract import FINDING
    check,materials,version,old=prepared(rig)
    changed=check['whole_clip_current_srt'].replace('问的是前半句还是尾音','问的是前半句话还是尾音')
    current=build_context_adjudication_request(changed,FINDING,clip_context=rig.context)
    for key in ('prior_acoustic_observations','context_only_range_acoustic_observations'):
        if key in check:current[key]=deepcopy(check[key])
    current['request_sha256']=owner.digest({k:v for k,v in current.items() if k!='request_sha256'})
    projection=owner.evidence_projection(current)
    fresh=finding_context.materials_for_check(current,contracts=materials['contracts'],
        model_identity=materials['model_identity'],
        observation_ids=[row['observation_id'] for row in projection['observations']])
    new_version=hashlib.sha256(changed.encode()).hexdigest()
    proof=owner.compile_task_stop_no_gain(check=current,projection=projection,
        finding_materials=fresh,text_version_sha256=new_version)
    assert proof['proof_kind']=='TASK_STOP' and 'pass_receipts' not in proof
    assert owner.validate_no_gain(proof,current,finding_materials=fresh,text_version_sha256=new_version)==proof
    with pytest.raises(ValueError):
        owner.validate_no_gain(old,current,finding_materials=fresh,text_version_sha256=new_version)
