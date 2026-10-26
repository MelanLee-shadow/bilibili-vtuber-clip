"""WP-B dependency changes, durable raw reuse and legacy read compatibility."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import finding_context as context
from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint
from tests.test_f1_ruling_reuse import provider

WORDS = ['清晨开门晒太阳', '山路尽头有灯光', '海边今天刮大风', '乌鸦停在老树上',
         '厨房飘出咖啡香', '远处传来了钟声', '小猫躲进纸箱里', '门口新开一家店',
         '明天准备去游泳', '今天月亮照不到我', '车站有人在等车', '妈妈买了新裙子',
         '窗帘换成浅蓝色', '桌子摆在阳台上', '朋友带来一束花']


def srt(words=WORDS, *, shift=0):
    def stamp(ms):
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    return '\n\n'.join(f'{i}\n{stamp(10000+i*2000+shift)} --> {stamp(11000+i*2000+shift)}\n{word}'
                       for i, word in enumerate(words, 1)) + '\n'


def check(words=WORDS, *, offset=1000, shift=0):
    text=srt(words,shift=shift)
    return {'whole_clip_current_srt':text, 'cue_indexes':[3], 'current_cue':words[2],
        'proposed_cue':'海边今天刮微风','matched_start_ms':16000+shift,'matched_end_ms':17000+shift,
        'source_media_timeline_offset_ms':offset,'edit_span_binding':{'codepoint_span':[5,7]},
        'clip_context_binding':{'candidate_id':'clip','source_media_sha256s':['a'*64],
                                'context_sha256':'unrelated whole-context digest'}}


def materials(request, **kwargs):
    return context.materials_for_check(request,contracts={'meaning':'policy.v1','answer':'terminal.v2'},
        model_identity={'transport':'recorded','models':['top'],'effort':'high'},**kwargs)


def test_unrelated_text_directory_round_and_labels_do_not_rename_question_or_material():
    old=check()
    words=[*WORDS[:-1],'云层慢慢散开了']
    new=check(words)
    new.update(run_directory='/other/run',pass_index=99,repair_class='disclosure_only')
    new['clip_context_binding']['context_sha256']='different deployment and whole-SRT binding'
    new['clip_context_binding']['candidate_id']='another display identifier'
    assert context.question_key(old)==context.question_key(new)
    assert materials(old)==materials(new)
    # G6 source-clock representation changes stay one P and one local M.
    shifted=check(offset=6000,shift=-5000)
    assert context.question_key(old)==context.question_key(shifted)
    assert materials(old)==materials(shifted)


@pytest.mark.parametrize('change',['local_text','candidate','observation','contract','model','source','window','span'])
def test_changed_dependencies_have_new_material_or_physical_question(change):
    old=check(); before=materials(old)
    current=deepcopy(old);kwargs={}
    if change=='local_text':
        words=list(WORDS);words[1]='昨晚突然下大雨';current=check(words)
    elif change=='candidate':current['proposed_cue']='海边今天下大雨'
    elif change=='observation':kwargs['observation_ids']=['new-bound-observation']
    elif change=='source':current['clip_context_binding']['source_media_sha256s']=['b'*64]
    elif change=='window':current['matched_end_ms']+=1
    elif change=='span':current['edit_span_binding']['codepoint_span']=[0,7]
    after=materials(current,**kwargs)
    if change in {'contract','model'}:
        field='contracts' if change=='contract' else 'model_identity'
        after[field]={'changed':'new version'}
        after['materials_sha256']=context.digest({k:v for k,v in after.items()
            if k not in {'physical_question_sha256','materials_sha256'}})
    assert context.validate_materials(after)
    assert before['materials_sha256']!=after['materials_sha256']


def test_one_scope_covers_explicit_evidence_pronoun_and_turn_dependencies():
    request=check()
    graph=context.relation_graph(request['whole_clip_current_srt'],relations=[
        {'kind':'pronoun_chain','cue_indexes':[3,12]},
        {'kind':'speaker_turn','cue_indexes':[3,8]}])
    bound=context.context_for_check(request,finding={'evidence_cue_ids':['cue:14']},graph=graph)
    assert {3,8,12,14}<=set(bound['scope_cue_indexes'])
    from src.autoslice.prompt_materials import finding_cue_projection
    assert finding_cue_projection({**request,'finding_context':bound})==context.cue_projection(bound)
    assert context.cue_projection(bound)['cue:12']==WORDS[11]


def test_real_refrain_graph_creation_and_removal_invalidate_connected_questions():
    before=check()
    words=list(WORDS)
    words[2]=words[9]=words[12]=words[13]='今天月亮照不到我'
    # Keep the target text constant across the graph-changing patch.
    previous=list(WORDS);previous[2]='今天月亮照不到我'
    before=check(previous);after=check(words)
    left=context.relation_graph(before['whole_clip_current_srt'])
    right=context.relation_graph(after['whole_clip_current_srt'])
    prior_context=context.context_for_check(before,graph=left)
    assert any(row['kind']=='refrain' and {3,10,13,14}<=set(row['cue_indexes']) for row in right['relations'])
    assert 13 not in prior_context['scope_cue_indexes']
    assert context.affected_findings(left,right,{10,13,14},{'target':prior_context})==['target']
    assert context.affected_findings(right,left,{10,13,14},
        {'target':context.context_for_check(after,graph=right)})==['target']
    assert materials(before)['materials_sha256']!=materials(after)['materials_sha256']


def checkpoint(root, call, request, *, prompt, round_index, parser_path=Path(__file__)):
    return CompletedReviewCheckpoint.for_ruling(stage='semantic-terminal',prompt=prompt,llm_call=call,
        inputs={'finding_materials':materials(request),'pass_index':round_index,'run_dir':str(root/str(round_index))},
        parser_path=parser_path,root=root/'shared')


def test_same_pm_reuses_original_raw_across_pass_directory_and_parser_and_restart(tmp_path):
    call=provider('original negative raw response')
    first=checkpoint(tmp_path,call,check(),prompt='old full SRT and old run directory',round_index=1)
    assert first.response(call,preserve_raw=True)=='original negative raw response'
    original={p.name:p.read_bytes() for p in first.directory.glob('*.json')}
    words=[*WORDS[:-1],'云层慢慢散开了']
    parser=tmp_path/'new-parser.py';parser.write_text('# independent current consumer')
    second=checkpoint(tmp_path,call,check(words),prompt='new full SRT and new deployment',round_index=7,parser_path=parser)
    assert second.directory==first.directory
    assert second.response(call,preserve_raw=True)=='original negative raw response'
    assert second.hit and len(call.calls)==1
    assert original=={p.name:p.read_bytes() for p in first.directory.glob('*.json')}
    data=tmp_path/'materials.json';data.write_text(json.dumps(materials(check(words))))
    # A new interpreter shares only the durable original bytes and typed key.
    code=('from pathlib import Path; import json,sys; '
        'from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint; '
        'from tests.test_f1_ruling_reuse import provider; '
        'c=provider("must never be called"); '
        'p=CompletedReviewCheckpoint.for_ruling(stage="semantic-terminal",prompt="another run",'
        'llm_call=c,inputs={"finding_materials":json.loads(Path(sys.argv[1]).read_text())},'
        'parser_path=Path(sys.argv[2]),root=Path(sys.argv[3])); '
        'assert p.response(c)=="original negative raw response"; assert not c.calls; print("REPLAYED")')
    child=subprocess.run([sys.executable,'-B','-c',code,str(data),str(parser),str(tmp_path/'shared')],
                         capture_output=True,text=True,timeout=30)
    assert child.returncode==0,child.stderr
    assert child.stdout.strip()=='REPLAYED'


def test_same_physical_question_labels_merge_but_channels_providers_and_models_stay_distinct(tmp_path):
    call=provider()
    left=check();right=deepcopy(left)
    right.update(repair_class='disclosure_only',content_risk='noncritical',pass_index=12)
    right['proposed_cue']='another candidate'
    assert context.question_key(left)==context.question_key(right)
    merged=materials(left,candidates=[left['proposed_cue'],right['proposed_cue']])
    reordered=materials(right,candidates=[right['proposed_cue'],left['proposed_cue']])
    assert merged==reordered
    key=context.effect_key(merged,channel='terminal',provider='CPA',model=call.cpa_cache_identity)
    assert key!=context.effect_key(merged,channel='closed-set',provider='CPA',model=call.cpa_cache_identity)
    assert key!=context.effect_key(merged,channel='terminal',provider='other',model=call.cpa_cache_identity)
    assert key!=context.effect_key(merged,channel='terminal',provider='CPA',model={'models':['other'],'effort':'high'})


def test_invalid_typed_materials_are_not_a_legacy_cache_miss(tmp_path):
    value=materials(check());value['local_context']['cues'][0][-1]='forged text'
    with pytest.raises(ValueError,match='FINDING_MATERIALS_BINDING_DRIFT'):
        CompletedReviewCheckpoint.for_ruling(stage='terminal',prompt='anything',llm_call=provider(),
            inputs={'finding_materials':value},parser_path=Path(__file__),root=tmp_path)


def test_typed_fallback_keeps_actual_model_and_outer_owner_scope(tmp_path):
    from src.autoslice.completed_review_checkpoint import ruling_material_scope
    from src.autoslice.llm_client import CpaCompletion
    call=provider(CpaCompletion('original fallback bytes','fallback'))
    with ruling_material_scope({'observation':'one'}):
        first=checkpoint(tmp_path,call,check(),prompt='first rendering',round_index=1)
        first.response(call,preserve_raw=True)
        # A fallback result is never attributed to the requested top model.
        second=checkpoint(tmp_path,call,check(),prompt='second rendering',round_index=2)
        assert second.load() is None
        call.cpa_cache_identity['models']=['fallback']
        actual=checkpoint(tmp_path,call,check(),prompt='third rendering',round_index=3)
        assert actual.response(call)=='original fallback bytes'
        assert actual.hit and len(call.calls)==1
    with ruling_material_scope({'observation':'other'}):
        assert checkpoint(tmp_path,call,check(),prompt='third rendering',round_index=3).load() is None


def test_stale_graph_and_missing_dependency_contracts_are_rejected():
    request=check();graph=context.relation_graph(request['whole_clip_current_srt'])
    words=list(WORDS);words[-1]='云层慢慢散开了'
    with pytest.raises(ValueError,match='CONTEXT_RELATION_INVALID'):
        context.context_for_check(check(words),graph=graph)
    with pytest.raises(ValueError,match='FINDING_MATERIALS_BINDING_DRIFT'):
        context.materials_for_check(request,contracts={},model_identity={'models':['test']})


def test_self_consistent_context_cannot_hide_changed_retained_text():
    request=check();bound=context.context_for_check(request)
    words=list(WORDS);words[1]='云层慢慢散开了'
    from src.autoslice.prompt_materials import finding_cue_projection
    with pytest.raises(ValueError,match='FINDING_CONTEXT_BINDING_DRIFT'):
        finding_cue_projection({**check(words),'finding_context':bound})
    bound['cues']=bound['cues'][1:]
    bound['scope_cue_indexes']=bound['scope_cue_indexes'][1:]
    bound['context_sha256']=context.digest({k:v for k,v in bound.items() if k!='context_sha256'})
    with pytest.raises(ValueError,match='FINDING_CONTEXT_BINDING_DRIFT'):
        finding_cue_projection({**request,'finding_context':bound})


def test_request_context_is_explicit_opt_in_and_uses_the_same_projection(monkeypatch):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    finding={'cue_index':3,'suspect':'大风','suggestion':'微风','proposed_full_cue':'海边今天刮微风'}
    clip={'candidate_id':'clip','pieces':[{'source_media_sha256':'a'*64}]}
    monkeypatch.delenv('AUTOSLICE_FINAL_REVIEW_MODE',raising=False)
    legacy=build_context_adjudication_request(srt(),finding,clip_context=clip)
    assert 'finding_context' not in legacy
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    current=build_context_adjudication_request(srt(),finding,clip_context=clip)
    assert context.cue_projection(current['finding_context'])['cue:3']==WORDS[2]
    assert current['whole_clip_current_srt']==legacy['whole_clip_current_srt']


@pytest.mark.parametrize('change,expected',[('unrelated',None),('local','LOCAL_TEXT'),('source','QUESTION_DRIFT')])
def test_reentry_compares_local_context_and_keeps_source_guard(monkeypatch,change,expected):
    from src.autoslice import finding_lifecycle as lifecycle,targeted_relisten
    # This test isolates dependency routing; proof validity remains owned by
    # validate_no_gain and is exercised on real recorded proofs in WP-A/replay.
    monkeypatch.setattr(targeted_relisten,'validate_no_gain',lambda proof,check:proof)
    old=check();old['finding_context']=context.context_for_check(old)
    proof={'projection':{'observations':[]},'proof_sha256':'proof'}
    row={'lifecycle':{'schema_version':lifecycle.SCHEMA,'state':'KEEP'},
         'exact_release_adjudication':{'terminal_review':{'request':{
            'check_request':old,'audio_gain_termination':proof,
            'srt_sha256':hashlib.sha256(old['whole_clip_current_srt'].encode()).hexdigest(),
            'clip_context_sha256':'old whole context'}}}}
    words=list(WORDS);words[14 if change=='unrelated' else 1]='云层慢慢散开了'
    new=check(words)
    if change=='source':new['clip_context_binding']['source_media_sha256s']=['b'*64]
    new['finding_context']=context.context_for_check(new)
    new['request_sha256']=context.digest(new)
    if expected is None:
        with pytest.raises(ValueError,match='FINDING_REENTRY_NEW_MATERIAL_REQUIRED'):
            lifecycle.reenter_terminal_review(row,check_request=new,no_gain_proof=proof,clip_context={})
    elif expected=='QUESTION_DRIFT':
        with pytest.raises(ValueError,match='FINDING_REENTRY_QUESTION_DRIFT'):
            lifecycle.reenter_terminal_review(row,check_request=new,no_gain_proof=proof,clip_context={})
    else:
        result=lifecycle.reenter_terminal_review(row,check_request=new,no_gain_proof=proof,clip_context={})
        assert result['lifecycle']['state']=='AWAITING_EVIDENCE'
        assert result['lifecycle_reentries'][-1]['material_changes']==[expected]
