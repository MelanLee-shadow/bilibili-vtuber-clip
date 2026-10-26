"""Opt-in ordinary producers, original raw reuse, and bound relation owners."""
from copy import deepcopy
import json
import hashlib
from src.autoslice import acoustic_witness_adjudication as judge
from src.autoslice.jingting_chunker import parse_srt_cues
from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_witness

import pytest

from src.autoslice import finding_context as context
from tests.test_f1_ruling_reuse import provider
from tests.test_wpb_finding_context import check, WORDS


def bound(words=WORDS, *, shift=0, offset=1000, candidate_id='clip', chat=()):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    text=check(words,shift=shift,offset=offset)['whole_clip_current_srt']
    return build_context_adjudication_request(text,
        {'cue_index':3,'suspect':'大风','suggestion':'微风','proposed_full_cue':'海边今天刮微风','repair_class':'phonetic'},
        clip_context={'candidate_id':candidate_id,'pieces':[{'source_media_sha256':'a'*64}],
                      'structured_chat':list(chat)},
        source_media_timeline_offset_ms=offset,finding_dependency_graph=context.relation_graph(text))


def test_actual_word_judge_reuses_negative_raw_after_unrelated_patch_and_run(tmp_path,monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body,word_choice_contract
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_witness
    call=provider('original non-JSON reply');call.completed_review_root=tmp_path/'shared'
    witness=_semantic_cache_witness()
    old=bound()
    first=_judge_word_choice_body(llm_call=call,check_request=old,witness=witness)
    words=list(WORDS);words[-1]='远方轮船已经出海'
    new=bound(words)
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'another-run'))
    prompt=word_choice_contract(llm_call=call,check_request=new,witness=witness).prompt
    assert WORDS[2] in prompt and words[-1] not in prompt and WORDS[-1] not in prompt
    second=_judge_word_choice_body(llm_call=call,check_request=new,witness=witness)
    assert first['reason_code']==second['reason_code']=='JUDGE_CALL_FAILED'
    assert first['failure_layer']==second['failure_layer']=='CONTRACT_ERROR'
    assert first['provider_retry_attempted'] is second['provider_retry_attempted'] is False
    assert len(call.calls)==1
    _judge_word_choice_body(llm_call=call,
        check_request=bound(words,shift=-5000,offset=6000,candidate_id='another-display-name'),witness=witness)
    assert len(call.calls)==1
    words[1]='邻近文字已改变'
    _judge_word_choice_body(llm_call=call,check_request=bound(words),witness=witness)
    assert len(call.calls)==2


def test_sender_context_clock_alias_reuses_but_real_sender_event_change_is_material(tmp_path,monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    call=provider('original negative reply');call.completed_review_root=tmp_path/'shared'
    event={'sender':'Alice','text':'海边今天刮微风','source_event_id':'message-1',
           'source_sha256':'c'*64,'offset_ms':16500,'kind':'danmaku'}
    witness=_semantic_cache_witness()
    _judge_word_choice_body(llm_call=call,check_request=bound(chat=[event]),witness=witness)
    shifted={**event,'offset_ms':11500}
    _judge_word_choice_body(llm_call=call,
        check_request=bound(chat=[shifted],shift=-5000,offset=6000),witness=witness)
    assert len(call.calls)==1
    _judge_word_choice_body(llm_call=call,
        check_request=bound(chat=[{**shifted,'sender':'Bob','source_event_id':'message-2'}],shift=-5000,offset=6000),
        witness=witness)
    assert len(call.calls)==2


def test_actual_third_candidate_reuses_original_negative_raw_and_local_catalogue(tmp_path,monkeypatch):
    from src.autoslice.closed_set_proposal_rebuild import rebuild_candidate_after_neither
    from src.autoslice.final_review_auditor import _derive_single_span_edit
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    call=provider('original non-JSON reply')
    common={'finding':{'cue_index':3,'repair_class':'phonetic','why':'nearby candidate'},
        'witness':{'status':'UNCERTAIN'},'asr_attempts':{'routes_exhausted':True,'observations':[{'status':'UNCERTAIN'}]},
        'llm_call':call,'derive_single_span_edit':_derive_single_span_edit}
    first=rebuild_candidate_after_neither(request=bound(),**common)
    words=list(WORDS);words[-1]='远方轮船已经出海'
    second=rebuild_candidate_after_neither(request=bound(words),**common)
    assert first[0] is second[0] is None
    assert first[1]['reason_code']==second[1]['reason_code']=='PROPOSAL_REBUILD_PROVIDER_OR_JSON_UNAVAILABLE'
    assert len(call.calls)==1
    assert words[-1] not in call.calls[0] and WORDS[-1] not in call.calls[0]


def test_normal_terminal_producer_prompt_schema_and_cold_consumer_share_c(tmp_path,monkeypatch):
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    from tests.test_terminal_contract_roundtrip import produce,c_reply
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice.targeted_relisten import validate_no_gain
    from src.autoslice.prompt_materials import finding_cue_projection
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    rig=build_offline_rig(tmp_path/'rig',monkeypatch)
    _,captured=produce(rig,c_reply)
    request=captured['request']
    assert isinstance(request,dict),captured
    data,refs=terminal.terminal_materials(request)
    catalogue=finding_cue_projection(request['check_request'])
    assert {k:v for k,v in data['context_sources'].items() if k.startswith('cue:')}==catalogue
    assert captured['response_schema']['properties']['evidence_refs']['items']['enum']==list(refs)
    assert validate_no_gain(request['audio_gain_termination'],request['check_request'])
    assert terminal.validate_terminal_answer(request,captured['answer'],prompt=captured['prompt'])=='CURRENT'
    call=provider(json.dumps(captured['answer'],ensure_ascii=False));call.completed_review_root=tmp_path/'shared'
    prompt=terminal.terminal_prompt(request)
    first=terminal.request_terminal_answer(call,prompt,request)
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'other-run'))
    second=terminal.request_terminal_answer(call,prompt,request)
    assert first==second and len(call.calls)==1
    assert terminal.validate_terminal_answer(request,json.loads(second),prompt=prompt)=='CURRENT'


def test_pronoun_owner_bound_chain_enters_builder_and_stale_audit_is_rejected(monkeypatch):
    from tests.test_pronoun_referent_consistency import run,Scripted,d,srt
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    source=srt('她在等车','山边吹来一阵风','她带来了花')
    _,audit=run(source,Scripted([d('cue-1-occurrence-1','她','她','THIRD:朋友'),
                               d('cue-3-occurrence-1','她','她','THIRD:朋友')]))
    assert context.pronoun_relations(source,audit)==[{'kind':'pronoun_chain','cue_indexes':[1,3]}]
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    request=build_context_adjudication_request(source,
        {'cue_index':1,'suspect':'等车','suggestion':'等人','proposed_full_cue':'她在等人',
         '_candidate_pronoun_policy_audit':audit},
        clip_context={'candidate_id':'clip','pieces':[{'source_media_sha256':'a'*64}]})
    assert {'kind':'pronoun_chain','cue_indexes':[1,3]} in request['finding_context']['relations']
    with pytest.raises(ValueError,match='PRONOUN_CONTEXT_UNBOUND'):
        context.pronoun_relations(source.replace('花','书'),audit)


def test_speaker_owner_alignment_binds_turn_edges_and_current_grid(tmp_path):
    from tests.lidousha.test_addressee_attribution import _speaker_bundle,SourceCue
    from src.autoslice.addressee_attribution import rebuild_speaker_evidence,speaker_turn_relations,SpeakerEvidenceRejected
    from src.autoslice.speaker_common import HOST_SPEAKER,GUEST_SPEAKER
    from tests.test_pronoun_referent_consistency import srt
    texts=['门外下着小雨','我在房间看书','朋友端来了茶']
    source=srt(*texts)
    cues=[SourceCue(str(i),i*2000,i*2000+1000,text) for i,text in enumerate(texts,1)]
    segments=[(i,i*2000,i*2000+1000,HOST_SPEAKER if i<3 else GUEST_SPEAKER,text)
              for i,text in enumerate(texts,1)]
    record,raw,manifest=_speaker_bundle(tmp_path,cues,segments)
    evidence=rebuild_speaker_evidence(record,cues,speaker_srt_bytes=raw,speaker_manifest_bytes=manifest)
    assert speaker_turn_relations(evidence,current_srt=source)==[{'kind':'speaker_turn','cue_indexes':[1,2]}]
    # Text corrections do not change an already-bound source speaker clock.
    assert speaker_turn_relations(evidence,current_srt=source.replace('小雨','大雨'))
    with pytest.raises(SpeakerEvidenceRejected,match='SPEAKER_CONTEXT_GRID_DRIFT'):
        speaker_turn_relations(evidence,current_srt=source.replace('00:00:02,000','00:00:01,999'))
    altered=deepcopy(evidence);altered.speaker_evidence['alignment']['groups'][0]['segments'][0]['speaker']=GUEST_SPEAKER
    with pytest.raises(SpeakerEvidenceRejected,match='SPEAKER_DEPENDENCY_BINDING_DRIFT'):
        speaker_turn_relations(altered,current_srt=source)


@pytest.mark.parametrize('authority',['terminology','registered_names'])
def test_word_key_binds_the_exact_terminology_consumed_by_the_prompt(tmp_path,monkeypatch,authority):
    from src.autoslice import term_authority
    owner,name=(judge,'terminology_prompt_context') if authority=='terminology' else (term_authority,'registered_name_decision_prompt')
    call=provider('original negative raw');call.completed_review_root=tmp_path/'shared'
    monkeypatch.setattr(owner,name,lambda *args:'registered local fact version one')
    check=bound();witness=_semantic_cache_witness()
    judge._judge_word_choice_body(llm_call=call,check_request=check,witness=witness)
    monkeypatch.setattr(owner,name,lambda *args:'registered local fact version two')
    judge._judge_word_choice_body(llm_call=call,check_request=check,witness=witness)
    assert len(call.calls)==2
    assert 'registered local fact version one' in call.calls[0]
    assert 'registered local fact version two' in call.calls[1]


def roles(check, *, remote_role='host'):
    text=check['whole_clip_current_srt']
    cues=parse_srt_cues(text)
    return {'status':'EVIDENCE_ONLY','mutation_authorized':False,'uniform_host_display_only':True,
        'final_srt_sha256':'sha256:'+hashlib.sha256(text.encode()).hexdigest(),
        'source_media_sha256':'a'*64,
        'rows':[{'cue_index':i,'interval_ms':[c.start_ms,c.end_ms],
                 'role':remote_role if i==len(cues) else 'host','action':'KEEP'}
                for i,c in enumerate(cues,1)]}


def role_check(words=WORDS, *, remote_role='host',local_role='host',shift=0,offset=1000):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.finding_context import relation_graph
    check=bound(words,shift=shift,offset=offset)
    evidence=roles(check,remote_role=remote_role);evidence['rows'][1]['role']=local_role
    return build_context_adjudication_request(check['whole_clip_current_srt'],
        {'cue_index':3,'suspect':'大风','suggestion':'微风','proposed_full_cue':'海边今天刮微风',
         'repair_class':'phonetic','_caption_audio_roles':evidence},
        clip_context={'candidate_id':'clip','pieces':[{'source_media_sha256':'a'*64}]},
        source_media_timeline_offset_ms=offset,finding_dependency_graph=relation_graph(check['whole_clip_current_srt']))


def test_word_key_and_caption_role_prompt_only_depend_on_bound_c(tmp_path):
    call=provider('original negative raw');call.completed_review_root=tmp_path/'shared'
    check=role_check()
    witness=_semantic_cache_witness()
    judge._judge_word_choice_body(llm_call=call,check_request=check,witness=witness)
    words=list(WORDS);words[-1]='遥远码头的轮船已经出海'
    changed=role_check(words,remote_role='source_media')
    judge._judge_word_choice_body(llm_call=call,check_request=changed,witness=witness)
    assert len(call.calls)==1
    assert '"cue_index": 15' not in call.calls[0]
    judge._judge_word_choice_body(llm_call=call,
        check_request=role_check(words,remote_role='source_media',shift=-5000,offset=6000),witness=witness)
    assert len(call.calls)==1
    local=role_check(words,remote_role='source_media',local_role='source_media')
    judge._judge_word_choice_body(llm_call=call,check_request=local,witness=witness)
    assert len(call.calls)==2
    # A bad out-of-scope row must fail the owner's full receipt validation.
    from src.autoslice.producer_nested_acoustic import caption_audio_roles_context
    invalid=roles(local);invalid['rows'][-1]['action']='DELETE'
    assert caption_audio_roles_context(local['whole_clip_current_srt'],invalid,
        scope_cue_indexes=tuple(local['finding_context']['scope_cue_indexes']),source_offset_ms=1000)==''
