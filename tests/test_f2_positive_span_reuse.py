"""Positive ordinary span ruling must survive an unrelated text version."""
import hashlib,json,socket
import pytest
from pathlib import Path
from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.clip_context import build_clip_context
from tests.test_span_phoneme_resolution import Observers,model_for,SOURCE_SHA
from tests.test_f1_ruling_reuse import provider
from tests.test_wpb_finding_context import WORDS


@pytest.mark.parametrize('distant_text',['远处无人相关的云朵已经散开','另一个话题的观众还在喝水'])
def test_positive_span_ruling_replays_after_unrelated_patch(tmp_path,monkeypatch,distant_text):
    def forbidden(*a,**k):raise AssertionError('NO_NETWORK_ALLOWED')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'runtime'))
    def stamp(ms):return f'00:00:{ms//1000:02},{ms%1000:03}'
    source='1\n00:00:01,000 --> 00:00:04,000\n你想，甲0呀\n\n'
    source+='\n\n'.join(f'{i}\n{stamp(5000+(i-2)*2000)} --> {stamp(6000+(i-2)*2000)}\n{text}'
        for i,text in enumerate(WORDS[1:],2))+'\n'
    memory=tmp_path/'speech.json'
    from src.autoslice.speech_memory_ledger import SCHEMA_VERSION
    memory.write_text(json.dumps({'schema_version':SCHEMA_VERSION,'entries':[]}))
    context=build_clip_context(candidate_id='span-positive',
        spec={'date':'2026-10-16','pieces':[{'start_ms':0,'end_ms':40000,'source_media_sha256':SOURCE_SHA}]},
        draft_srt=source,authoritative_chat=(),topic_resolution={},session_topic_authorities=(),
        speech_memory_ledger_path=memory,source_truth_ledger_path=None)
    from src.autoslice.llm_client import CpaCompletion
    physical=model_for()
    def model(prompt):return CpaCompletion(physical(prompt),'top')
    model.prompts=physical.prompts
    model.cpa_cache_identity=provider().cpa_cache_identity
    model.completed_review_root=tmp_path/'shared'
    verifier=Observers()
    finding={'cue_index':1,'suspect':'甲0','suggestion':'甲零','repair_class':'phonetic',
        'proposed_full_cue':'你想，甲零呀','base_text_sha256':hashlib.sha256('你想，甲0呀'.encode()).hexdigest()}
    first,audit=adjudicate_context_finding(source,finding,entity_verifier=verifier,
        judge_llm_call=model,clip_context=context,require_complete_utterance_support=True)
    assert audit.get('repaired') is True,audit
    calls=len(model.prompts)
    changed=source.replace(WORDS[-1],distant_text)
    second,replayed=adjudicate_context_finding(changed,finding,entity_verifier=verifier,
        judge_llm_call=model,clip_context=context,require_complete_utterance_support=True)
    old=audit.get('witness_judge',{}).get('judge',{})
    new=replayed.get('witness_judge',{}).get('judge',{})
    report={'first_repaired':audit.get('repaired'),'second_repaired':replayed.get('repaired'),
        'first_model_calls':calls,'final_model_calls':len(model.prompts),
        'second_policy_branch':replayed.get('policy_branch'),'second_judge_status':new.get('status'),
        'served_from_cache':new.get('served_from_cache'),
        'first_request_sha256':old.get('check_request_sha256'),'second_request_sha256':new.get('check_request_sha256'),
        'first_compilation_blocked_reason':old.get('span_compilation',{}).get('blocked_reason'),
        'second_compilation_blocked_reason':new.get('span_compilation',{}).get('blocked_reason'),
        'raw_reply_request_sha256':new.get('span_review',{}).get('request_sha256'),
        'raw_unchanged':old.get('span_review_raw_completion')==new.get('span_review_raw_completion')}
    (tmp_path/'span-reuse.diag.json').write_text(json.dumps(report,indent=2))
    assert len(model.prompts)==calls,'UNCHANGED_PHYSICAL_M_REDISPATCHED'
    assert replayed.get('repaired') is True,replayed
    assert '你想，甲零呀' in second and distant_text in second
    from src.autoslice.span_phoneme_resolution import validated_resolution,validate_persisted_judge_scopes
    resolution=replayed['span_context_resolution']
    validated_resolution(resolution,current='你想，甲0呀',proposed='你想，甲零呀',srt_text=changed)
    validate_persisted_judge_scopes(replayed)
    assert resolution['original_resolution']['request']['whole_clip_current_srt']==source
    assert resolution['request']['whole_clip_current_srt']==changed
    assert new['span_review']['request_sha256']==old['span_review']['request_sha256']
    assert new['prompt_sha256']==old['prompt_sha256'] and new['completion_sha256']==old['completion_sha256']
    assert resolution['outside_findings']==resolution['original_resolution']['outside_findings']
    assert resolution['outside_findings']  # Unresolved prefix questions are retained.
    from copy import deepcopy
    forged=deepcopy(resolution)
    forged['judge']['span_review']['request_sha256']=replayed['request']['request_sha256']
    with pytest.raises(ValueError):validated_resolution(forged)
