"""Ordinary writer -> persisted receipt -> handoff and release consumers; offline."""
from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice import producer_package_finalization as finalization
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import final_subtitle_audio_gate as gate
from src.autoslice.final_review_auditor import FinalReviewAuditError
from src.autoslice.jingting_chunker import parse_srt_cues
from tests.test_final_subtitle_audio_gate import (
    _record, _adapters, _postburn_adapters, _postburn_chat_audit, _postburn_record, _srt,
)
from test_h_release_wiring import _shipped
from test_targeted_relisten_public_contract import SRT, rig  # noqa: F401


@pytest.mark.parametrize('with_language',[False,True])
def test_rendered_writer_receipt_reaches_handoff_after_json_restart(tmp_path, monkeypatch, with_language):
    from tests.test_editorial_subtitle import run
    output, repairs, _, _ = run(tmp_path, monkeypatch)
    authority = {}
    finalization._register_exact_final_cpa_repairs(authority, repairs, delivery_start_ms=5000)
    rows = [(1000, 2000, parse_srt_cues(output)[0].text),
            (23000, 27000, "中段这里继续说明问题"), (42000, 46000, "最后我们明确完成收束")]
    record, srt, media = _record(tmp_path, rows=rows)
    record = gate.capture_final_subtitle_audio_check(record, srt, tmp_path/'evidence', 'candidate',
        adapters=_adapters(witness_rows=[(a+6200,b+6200,t) for a,b,t in rows], calls={}))
    chat = {**_postburn_chat_audit(srt), **authority}
    source_sha='sha256:'+hashlib.sha256(media.read_bytes()).hexdigest()
    if with_language:
        from src.autoslice import audio_language_discovery as language
        from src.autoslice import foreign_span_witness as foreign
        def unavailable(**kwargs):raise RuntimeError('offline observation unavailable')
        monkeypatch.setattr(foreign,'_witness_rows',unavailable)
        _,audit=language.repair_audio_language_candidates(media_path=media,
            srt_text=_srt([(a+5000,b+5000,t) for a,b,t in rows]),out_root=tmp_path,cid='candidate',
            authority=chat,llm_call=lambda _:pytest.fail('no provider in fixture'),
            scan_call=lambda **_: {'status':'OBSERVED','foreign_spans':[],
                'semantic_anomalies':[{'cue_index':1,'reason':'source window unresolved'}],'speech_turns':[]})
        chat['audio_language_discovery_audit']=audit
    _postburn_adapters(monkeypatch, json.dumps({'findings':[{
        'cue':1,'kind':'context','suspect':rows[0][2],'repair_class':'disclosure_only',
        'proposed_full_cue':None,'content_risk':'factual_error',
        'risk_basis':'此合成例的谢礼指代仍须复审。','why':'核对谢礼指代'}]}, ensure_ascii=False))
    path = tmp_path/'chat.json'
    with pytest.raises(SystemExit):
        finalization._run_postburn_subtitle_audio_review(cid='candidate', out_root=tmp_path,
            recut=finalization.FinalRecutArtifacts(tmp_path,media,srt,None,None),record=record,
            chat_authority_audit=chat,chat_authority_path=path,timeline_offset_ms=5000,
            source_evidence_spec={'source_piece_clock':{'padded_sha256':source_sha}})
    saved = json.loads(json.dumps(chat))
    result = pipeline.validated_postburn_priority_findings(final_srt_text=srt.read_text(),
        verified_authority_audit=saved,candidate_id='candidate',clip_context=None,timeline_offset_ms=5000,
        source_media_sha256=source_sha)
    assert result is not None and result[0][0]['cue_index']==1
    diagnostics=[]
    assert pipeline.validated_postburn_priority_findings(final_srt_text=srt.read_text(),
        verified_authority_audit=saved,candidate_id='candidate',clip_context=None,
        timeline_offset_ms=0,diagnostics=diagnostics) is None
    assert diagnostics[-1]['reason_code']=='POSTBURN_PRIORITY_CANDIDATE_CONTEXT_DRIFT'
    from src.autoslice import editorial_subtitle
    def inaccessible(*args,**kwargs):raise PermissionError('unreadable bound proof')
    monkeypatch.setattr(editorial_subtitle,'rendering_context',inaccessible)
    diagnostics=[]
    assert pipeline.validated_postburn_priority_findings(final_srt_text=srt.read_text(),
        verified_authority_audit=saved,candidate_id='candidate',clip_context=None,
        timeline_offset_ms=5000,diagnostics=diagnostics) is None
    assert diagnostics[-1]['reason_code']=='POSTBURN_PRIORITY_CONTEXT_INVALID_PermissionError'


@pytest.mark.parametrize('damage',[None,'srt','cue_bytes','different_type','different_span',
    'critical','meaning_reversal','no_ruling','no_exhaustion','no_v2','different_srt_ruling'])
def test_same_question_sub18_keep_reaches_postburn_partition(rig, damage):
    row = _shipped(rig)
    text = SRT
    nomination = {k:deepcopy(v) for k,v in row.items()
                  if k not in {'exact_release_adjudication','mutation_authority'}}
    preburn={'reviewed_srt_sha256':finalization._sha256_text(text),'release_gate':'PASS',
             'resolved_findings':[row]}
    if damage=='srt':text += '\n'
    elif damage=='cue_bytes':nomination['base_text_sha256']='f'*64
    elif damage=='different_type':nomination['repair_class']='editorial_rendering'
    elif damage=='different_span':nomination['suspect']='我听到了'
    elif damage in {'critical','meaning_reversal'}:nomination['content_risk']='factual_error' if damage=='critical' else damage
    elif damage=='no_ruling':preburn['resolved_findings']=[]
    elif damage=='no_exhaustion':
        row['exact_release_adjudication']['sub18_context_meaning_equivalent_keep'].pop('material_exhaustion')
    elif damage=='no_v2':row.pop('current_content_risk')
    elif damage=='different_srt_ruling':row['current_content_risk']['reviewed_srt_sha256']='sha256:'+'f'*64
    before=deepcopy((nomination,preburn))
    result=finalization._postburn_reuse_resolved_keeps([nomination],preburn=preburn,final_text=text)
    blocked,disclosed=finalization._postburn_current_risk_partition(result)
    if damage is None:
        assert not blocked and len(disclosed)==1
        assert disclosed[0]['postburn_keep_replay']['nomination']==nomination
    else:
        assert blocked==[nomination] and disclosed==[]
    assert (nomination,preburn)==before


def test_new_issue_at_same_cue_does_not_erase_retained_keep_evidence():
    # Two questions in one cue must both survive evidence accumulation.
    old={'cue_index':1,'base_text_sha256':'a'*64,'kind':'context','repair_class':'phonetic','suspect':'甲'}
    new={**old,'suspect':'乙'}
    result=finalization._current_postburn_resolution_evidence(
        exact_review={'resolved_findings':[new]},reviewed_srt_sha256='sha256:'+'b'*64,
        previous={'reviewed_srt_sha256':'sha256:'+'b'*64,'resolved_findings':[old]})
    assert result['resolved_findings']==[old,new] or result['resolved_findings']==[new,old]


@pytest.mark.parametrize('cache_form',['transport','ruling'])
def test_same_request_replay_consumes_receipt_when_ordinary_raw_cache_is_warm(tmp_path,monkeypatch,cache_form):
    record,srt,media=_postburn_record(tmp_path)
    calls=[]
    _postburn_adapters(monkeypatch,'{"findings":[]}',calls)
    llm=finalization.build_final_review_llm_call()
    llm.completed_review_root=tmp_path/'ordinary-completed-reviews'
    if cache_form=='ruling':llm.provider_runtime_binding={}
    chat=_postburn_chat_audit(srt)
    kwargs=dict(cid='candidate',out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path,media,srt,None,None),record=record,
        chat_authority_audit=chat,chat_authority_path=tmp_path/'candidate.chat-authority.json')
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    assert list(llm.completed_review_root.glob('*/*.json'))
    first=deepcopy(chat['postburn_subtitle_audio_review'])
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    replay=chat['postburn_subtitle_audio_review']
    assert replay['cache_reused'] and replay['cpa_calls'][0]['served_from_cache']
    assert replay['cpa_request']==first['cpa_request'] and len(calls)==1
    assert replay['discovered_findings']==first['discovered_findings']


@pytest.mark.parametrize('cache_form',['transport','ruling'])
def test_fresh_postburn_captures_inner_cache_answer_without_another_provider_call(tmp_path,monkeypatch,cache_form):
    record,srt,media=_postburn_record(tmp_path)
    calls=[]
    _postburn_adapters(monkeypatch,'{"findings":[]}',calls)
    llm=finalization.build_final_review_llm_call()
    llm.completed_review_root=tmp_path/'ordinary-completed-reviews'
    if cache_form=='ruling':llm.provider_runtime_binding={}
    def run(out):
        out.mkdir(exist_ok=True)
        chat=_postburn_chat_audit(srt)
        kwargs=dict(cid='candidate',out_root=out,
            recut=finalization.FinalRecutArtifacts(tmp_path,media,srt,None,None),record=record,
            chat_authority_audit=chat,chat_authority_path=out/'candidate.chat-authority.json')
        finalization._run_postburn_subtitle_audio_review(**kwargs)
        return chat,kwargs
    first,_=run(tmp_path/'first')
    second,kwargs=run(tmp_path/'fresh-outer-receipt')
    review=second['postburn_subtitle_audio_review']
    assert review['cpa_request']==first['postburn_subtitle_audio_review']['cpa_request']
    assert len(calls)==1 and len(review['cpa_calls'])==1
    assert review['cpa_calls'][0]['cache_source']=='ORDINARY_COMPLETED_REVIEW'
    assert finalization._postburn_cached_review_complete(review)
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    assert second['postburn_subtitle_audio_review']['cache_reused'] and len(calls)==1


@pytest.mark.parametrize('damage',[None,'request','dispatch','raw','ordinary_block'])
def test_cold_continuation_reaches_completed_parent_but_keeps_block(tmp_path,damage):
    from tests.test_producer_current_text_continuation import _semantic_blocked_authority, _srt
    chat,context,digest=_semantic_blocked_authority(tmp_path)
    parent=deepcopy(chat['postburn_subtitle_audio_review'])
    failed={**deepcopy(parent),'status':'AUDITOR_UNAVAILABLE','release_gate':'BLOCK',
        'reason_codes':['POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY'],'error_type':'ValueError',
        'cpa_calls':[],'findings':[],'validated_finding_count':0,
        'prior_postburn_subtitle_audio_review':parent}
    chat['postburn_subtitle_audio_review']=failed
    chat['final_review_audit'].update(status=failed['status'],release_gate='BLOCK',
        reason_codes=list(failed['reason_codes']),postburn_subtitle_audio_review=failed,
        findings=[],validated_finding_count=0)
    if damage=='request':failed['cpa_request']['candidate_id']='other'
    elif damage=='dispatch':failed['cpa_calls']=[{'dispatch_status':'RUNNING'}]
    elif damage=='raw':
        from pathlib import Path
        Path(parent['cpa_calls'][0]['response_path']).write_text('changed')
    elif damage=='ordinary_block':chat['final_review_audit']['reason_codes'].append('FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED')
    before=deepcopy(chat)
    if damage is None:
        result=pipeline._continuation_validate_existing_authority(chat,srt_text=_srt(),srt_sha256=digest,
            clip_context=context,candidate_id='relationship-candidate')
        assert result['release_gate']=='BLOCK'
    else:
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._continuation_validate_existing_authority(chat,srt_text=_srt(),srt_sha256=digest,
                clip_context=context,candidate_id='relationship-candidate')
    assert chat==before


@pytest.mark.parametrize('has_ruling',[True,False])
def test_ordinary_postburn_cpa_nomination_consumes_existing_sub18_keep(rig,monkeypatch,has_ruling):
    shipped=_shipped(rig)
    rows=[(c.start_ms,c.end_ms,c.text) for c in parse_srt_cues(SRT)]
    record,srt,media=_record(rig.tmp,rows=rows)
    assert srt.read_text()==SRT
    record=gate.capture_final_subtitle_audio_check(record,srt,rig.tmp/'evidence','candidate',
        adapters=_adapters(witness_rows=[(a+6200,b+6200,t) for a,b,t in rows],calls={}))
    chat=_postburn_chat_audit(srt)
    chat['final_review_audit']['resolved_findings']=[shipped] if has_ruling else []
    response={'findings':[{'cue':2,'kind':'context','suspect':'呜呜','repair_class':'spoken_unit',
        'proposed_full_cue':None,'why':'尾音字形需要核对','content_risk':'noncritical_uncertainty',
        'risk_basis':shipped['risk_basis'],
        'meaning_equivalence':shipped['current_content_risk']['meaning_equivalence']}]}
    _postburn_adapters(monkeypatch,json.dumps(response,ensure_ascii=False))
    kwargs=dict(cid='candidate',out_root=rig.tmp,
        recut=finalization.FinalRecutArtifacts(rig.tmp,media,srt,None,None),record=record,
        chat_authority_audit=chat,chat_authority_path=rig.tmp/'chat.json')
    if has_ruling:
        finalization._run_postburn_subtitle_audio_review(**kwargs)
        assert chat['postburn_subtitle_audio_review']['release_gate']=='PASS'
    else:
        with pytest.raises(SystemExit):finalization._run_postburn_subtitle_audio_review(**kwargs)
        assert chat['postburn_subtitle_audio_review']['release_gate']=='BLOCK'


@pytest.mark.parametrize('cause',[ValueError('POSTBURN_SUBTITLE_AUDIO_CPA_CACHE_FINDINGS_DRIFT'),
    RuntimeError('internal parser bug'),
    FinalReviewAuditError('FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID')])
def test_completed_replay_keeps_specific_failure_and_parent_without_dispatch(tmp_path,monkeypatch,cause):
    record,srt,media=_postburn_record(tmp_path)
    calls=[]
    _postburn_adapters(monkeypatch,'{"findings":[]}',calls)
    finalization.build_final_review_llm_call().completed_review_root=tmp_path/'ordinary-completed-reviews'
    chat=_postburn_chat_audit(srt)
    kwargs=dict(cid='candidate',out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path,media,srt,None,None),record=record,
        chat_authority_audit=chat,chat_authority_path=tmp_path/'candidate.chat-authority.json')
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    parent=deepcopy(chat['postburn_subtitle_audio_review'])
    def broken(*args,**kwargs):raise cause
    monkeypatch.setattr(finalization,'audit_final_subtitles',broken)
    with pytest.raises(SystemExit) as caught:
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    failed=chat['postburn_subtitle_audio_review']
    expected=('POSTBURN_SUBTITLE_AUDIO_CPA_REPLAY_PARSE_FAILED'
        if isinstance(cause,FinalReviewAuditError) else str(cause)
        if isinstance(cause,ValueError) else 'POSTBURN_SUBTITLE_AUDIO_CPA_REPLAY_PROGRAM_ERROR')
    assert failed['reason_codes']==[expected]
    assert failed['replay_error']['exception_type']==type(cause).__name__
    assert failed['replay_error']['traceback']
    if isinstance(cause,FinalReviewAuditError):
        assert failed['replay_error']['cause_reason_code']=='FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID'
    assert caught.value.__cause__ is cause and len(calls)==1
    restored=finalization._load_postburn_observation(tmp_path,'candidate')
    assert finalization._completed_postburn_before_parser_failure(restored)==parent
