"""Ordinary writers and their cold consumers must agree on the same input."""
from copy import deepcopy
import inspect
import json

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.chat_evidence import ChatEvidence
from tests.test_producer_text_pipeline_final_review import _adapters
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401
from tests.test_terminal_contract_roundtrip import produce


@pytest.mark.parametrize('name', ['李东莎', '李豆莎'])
@pytest.mark.parametrize('kind', ['danmaku', 'superchat'])
def test_real_entity_writer_input_is_the_chat_replay_before_image(tmp_path, monkeypatch, name, kind):
    source = f'1\n00:00:05,000 --> 00:00:09,000\n因为{name}是直女\n\n'
    support = source.replace(name, '李豆沙').replace('直女', '侄女')
    padded = tmp_path / 'padded.mp4'
    padded.with_suffix('.asr_draft.srt').write_text(support)
    chat = [ChatEvidence(kind, 1000, '因为李豆沙是侄女')]
    def forbidden(_):
        pytest.fail('This exact-copy input needs no provider')
    result = pipeline._apply_entity_authority(
        srt_text=source, authoritative_chat=chat, support_srts=[support],
        referent_groups=[], verify_confusable_entity=forbidden,
        code_switch_audit={}, term_boundary_moves=[], padded=padded, adapters=_adapters())
    audit = json.loads(json.dumps(result.chat_authority_audit))
    assert audit['applied'], audit
    assert audit['applied'][0]['before'] == ['因为李豆沙是直女']
    kwargs = dict(srt_text=result.srt_text, clip_context={'whole_clip_draft_srt': source},
                  authoritative_chat=chat, independent_support_srts=[support])
    if 'expected_input_srt_sha256' in inspect.signature(pipeline._validate_source_stage_chat_reads).parameters:
        kwargs['expected_input_srt_sha256'] = audit['input_srt_sha256']
    pipeline._validate_source_stage_chat_reads(audit['applied'], **kwargs)
    # Restore the old consumer's actual missing pre-chat stage, not the row.
    with monkeypatch.context() as old:
        old.setattr(pipeline, 'absorb_host_self_references', lambda text, **_: (text, {}))
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._validate_source_stage_chat_reads(audit['applied'], **kwargs)
    for key, value in [('before', ['伪造前像']), ('source_sha256', '0' * 64), ('evidence_id', 'other')]:
        damaged = deepcopy(audit['applied'])
        damaged[0][key] = value
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._validate_source_stage_chat_reads(damaged, **kwargs)


@pytest.mark.parametrize('retained', [False, True])
def test_chat_before_image_preserves_retained_draft_witness_policy(tmp_path, retained):
    source = ('1\n00:00:01,000 --> 00:00:03,000\n因为礼墨是侄女\n\n'
              '2\n00:00:05,000 --> 00:00:09,000\n小豆老公侄女不是你老公\n')
    support = source.replace('礼墨','李豆沙').replace('小豆老公侄女', '小豆老公；； 侄女')
    padded = tmp_path/'padded.mp4'
    padded.with_suffix('.asr_draft.srt').write_text(support)
    chat = [ChatEvidence('danmaku',1000,'小豆老公；； 侄女不是你老公')]
    result = pipeline._apply_entity_authority(srt_text=source, authoritative_chat=chat,
        support_srts=[support], referent_groups=[], verify_confusable_entity=lambda _: None,
        code_switch_audit={}, term_boundary_moves=[], padded=padded, adapters=_adapters(),
        retained_reviewed_draft=retained)
    audit = result.chat_authority_audit
    assert audit['applied']
    assert audit['self_reference_absorption_audit']['source_witness_available'] is (not retained)
    kwargs = dict(srt_text=result.srt_text, clip_context={'whole_clip_draft_srt':source},
                  authoritative_chat=chat, independent_support_srts=[support])
    if 'source_witness_available' in inspect.signature(pipeline._validate_source_stage_chat_reads).parameters:
        kwargs.update(source_witness_available=not retained, expected_input_srt_sha256=audit['input_srt_sha256'])
    pipeline._validate_source_stage_chat_reads(audit['applied'], **kwargs)
    if 'source_witness_available' in kwargs:
        with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_SOURCE_STAGE_CHAT_INPUT_DRIFT'):
            pipeline._validate_source_stage_chat_reads(audit['applied'], **{**kwargs,'source_witness_available':retained})


def terminal_edit(data, fragments):
    return {'choice': 'CONTEXT_FIT', 'grade': 'B', 'keep_route': None,
        'evidence_decisive': True, 'reason': '原声音义支持，尾音未逐字确认',
        'comparison_text': '我听到了，哼唱', 'status': 'RESOLVED', 'mode': 'faithful_paraphrase',
        'edit_start': data['edit_contract']['minimum_start'],
        'edit_end': data['edit_contract']['length'], 'replacement': '我听到了，哼唱',
        'reliable_fragments': [{'text': text, 'basis': '原音'} for text in fragments],
        'evidence_refs': ['audio'], 'meaning_basis': '上下文说明声音',
        'literal_uncertainty': '尾音', 'no_new_facts': True}


@pytest.mark.parametrize('fragments,valid', [
    (['我听到了', '到了'], False), (['我听到了', '我听'], False),
    (['哼唱', '我听到了'], False), (['未出现'], False),
    (['我听到了', '，哼唱'], True)])
def test_terminal_answer_and_editorial_share_literal_fragment_contract(rig, fragments, valid):
    from src.autoslice.editorial_subtitle import _decision
    from src.autoslice.semantic_terminal_adjudication import validate_terminal_answer
    _, captured = produce(rig, lambda data, refs: terminal_edit(data, fragments))
    request, answer = captured['request'], captured['answer']
    before = deepcopy(answer)
    for consumer in (validate_terminal_answer, _decision):
        if valid:
            consumer(request, answer, prompt=captured['prompt'])
        else:
            with pytest.raises(ValueError, match='EDITORIAL_RELIABLE_FRAGMENT_LOST'):
                consumer(request, answer, prompt=captured['prompt'])
    assert answer == before


@pytest.mark.parametrize('offset', [0, 1000])
def test_recut_editorial_writer_carries_open_question_to_current_text(tmp_path, monkeypatch, offset):
    from tests.test_span_producer_persistence import applied_history
    from tests.test_editorial_subtitle import run, answer
    from src.autoslice.span_finding_obligations import pending_findings, enforce_review, merge_pending
    from src.autoslice.recut_materialization import _write_source_range_srt
    from src.autoslice.review_evidence import SourceCue
    from src.autoslice.exact_final_terminal_funnel import terminal_check
    source, authority, _ = applied_history()
    old = pending_findings(source, authority)[0]
    # The ordinary materializer subtracts the delivery start from cue clocks.
    # The edit remains on exactly the same physical source window.
    recut = tmp_path / 'recut.srt'
    _write_source_range_srt([SourceCue(cue_id='source-question', source_start_ms=1000, source_end_ms=4000,
        text='你想，甲零呀')], offset, 4000, recut)
    current = recut.read_text()
    response = answer(mode='faithful_paraphrase', edit_start=0, edit_end=len('你想，甲零呀'),
                      replacement='你要，甲零呀', reliable_fragments=[{'text':'甲零呀','basis':'清楚原话'}],
                      evidence_refs=['audio'], meaning_basis='相同问答语境，句首音节不能逐字判定',
                      literal_uncertainty='句首读法')
    output, repairs, _, _ = run(tmp_path, monkeypatch, response=response, context={}, text=current, offset=offset)
    assert len(repairs) == 1 and '你要，甲零呀' in output
    authority['exact_final_cpa_self_heal']['passes'].append({
        'schema_version':'exact-final-cpa-self-heal-pass.v1', 'pass_index':2,
        'input_srt_sha256': 'sha256:' + pipeline.hashlib.sha256(current.encode()).hexdigest(),
        'output_srt_sha256': 'sha256:' + pipeline.hashlib.sha256(output.encode()).hexdigest(),
        'repairs':repairs})
    frozen = json.loads(json.dumps(authority))
    pending_kwargs = ({'timeline_offset_ms':offset}
        if 'timeline_offset_ms' in inspect.signature(pending_findings).parameters else {})
    pending = pending_findings(output, frozen, **pending_kwargs)
    assert len(pending) == 1
    row = pending[0]
    assert row['cue_index'] == 1 and row['suspect'] == '你要，甲零呀'
    assert row['span_obligation_status'] == 'UNRESOLVED'
    if offset:
        assert row['origin_span_obligation']['physical_cue_window_ms'] == [1000,4000]
    assert row['origin_span_obligation']['reanchor_chain']
    # Actual terminal request construction now sees current bytes and clock.
    check = terminal_check(output, row, clip_context={}, offset_ms=offset)
    assert check['current_cue'] == '你要，甲零呀'
    assert check['matched_start_ms'] + check['source_media_timeline_offset_ms'] == 1000
    merged = merge_pending(output, frozen, [old], timeline_offset_ms=offset)
    assert len(merged) == 1 and merged[0]['suspect'] == row['suspect']
    assert 'exact_release_adjudication' not in merged[0]
    audit = enforce_review(output, frozen, {'findings':[], 'release_gate':'PASS'}, timeline_offset_ms=offset)
    assert audit['release_gate'] == 'BLOCK' and audit['findings']
    assert frozen == authority
    # A nearby or ambiguous physical position cannot rescue the old question.
    for damaged in (output.replace('00:00:00,000' if offset else '00:00:01,000',
                                   '00:00:00,001' if offset else '00:00:01,001'),
                    output + '\n' + output.replace('1\n', '2\n', 1)):
        stale = pending_findings(damaged, frozen, timeline_offset_ms=offset)
        assert stale[0]['span_obligation_status'] == 'LOCATION_STALE'


@pytest.mark.parametrize('offset', [0, 500])
def test_literal_writer_without_obligation_marker_keeps_independent_question_open(tmp_path, offset):
    from tests.test_span_producer_persistence import applied_history
    from tests.test_span_phoneme_resolution import Observers
    from src.autoslice.final_review_auditor import adjudicate_context_finding, audit_final_subtitles
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    from src.autoslice.span_finding_obligations import pending_findings
    from src.autoslice.recut_materialization import _write_source_range_srt
    from src.autoslice.review_evidence import SourceCue
    _, authority, _ = applied_history()
    path=tmp_path/'literal.srt'
    _write_source_range_srt([SourceCue('literal-source',1000,4000,'你想，甲零呀')],offset,4000,path)
    text=path.read_text()
    # Independent ordinary discovery has no obligation marker. Its valid edit
    # cannot silently settle the earlier independently born audible question.
    raw={'cue':1,'kind':'context','repair_class':'spoken_unit','suspect':'你想，',
         'suggestion':'你很想，','proposed_full_cue':'你很想，甲零呀',
         'why':'Independent synthetic prefix question.', 'content_risk':'factual_error',
         'risk_basis':'Synthetic prefix wording must stay source-bound.'}
    discovered=audit_final_subtitles(text,llm_call=lambda _:json.dumps({'findings':[raw]}),
        extract_json=json.loads, source_media_timeline_offset_ms=offset)
    assert len(discovered)==1
    finding=discovered[0]
    verifier=Observers(heard='ni hen xiang jia ling ya',
                       transcripts={'mai':'你很想，甲零呀','moss':'你很想，甲零呀'})
    def model(prompt):
        needs='未调用音频证人' in prompt or 'text_first' in prompt or 'CURRENT_TEXT' in prompt
        if '\nSPAN_REVIEW_INPUT\n' in prompt:
            data=json.loads(prompt.split('\nSPAN_REVIEW_INPUT\n',1)[1].split('\nEND_SPAN_REVIEW_INPUT\n',1)[0])
            needs=not data['observations']
        return json.dumps({'choice':'PROPOSED','candidate_id':'PROPOSAL','needs_audio':needs,
                           'reason':'Synthetic source-bound prefix wording.'})
    output,adj=adjudicate_context_finding(text,finding,entity_verifier=verifier,judge_llm_call=model,
        source_media_timeline_offset_ms=offset, require_complete_utterance_support=True)
    assert adj['repaired'],adj.get('reason_code')
    finding['exact_release_adjudication']=adj
    produced,repairs=_apply_exact_final_cpa_repairs(text,{'findings':[finding]})
    assert produced==output and len(repairs)==1
    assert 'origin_span_obligation' not in repairs[0]
    authority['exact_final_cpa_self_heal']['passes'].append({'repairs':repairs})
    kwargs=({'timeline_offset_ms':offset}
        if 'timeline_offset_ms' in inspect.signature(pending_findings).parameters else {})
    pending=pending_findings(output,json.loads(json.dumps(authority)),**kwargs)
    assert len(pending)==1 and pending[0]['span_obligation_status']=='UNRESOLVED'
    assert pending[0]['suspect']=='你很想，甲零呀'
