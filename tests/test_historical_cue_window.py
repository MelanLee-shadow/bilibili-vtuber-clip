"""Saved source rows keep their own numbering after ordinary source hygiene."""
from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.chat_evidence import ChatEvidence, ReferentEntity, ReferentGroup
from src.autoslice.chat_repair import apply_audio_entity_verification
from src.autoslice.cue_split_hygiene import merge_release_grade_cues
from src.autoslice.jingting_chunker import parse_srt_cues
from tests.test_producer_current_text_auto_resume import _normal_transcript_entity_row
from tests.test_producer_text_pipeline_final_review import _adapters


PREFIX = ('1\n00:00:01,000 --> 00:00:02,000\n前句保留\n\n'
          '2\n00:00:02,000 --> 00:00:02,100\n哦\n\n')


def saved_entity(tmp_path, *, tail, start='05,000', end='07,000'):
    source = PREFIX + f'3\n00:00:{start} --> 00:00:{end}\n保留文子\n\n' + tail
    def verifier(request):
        return {**_normal_transcript_entity_row()['verdict'],
                'request_sha256': request['request_sha256']}
    output, audit = apply_audio_entity_verification(
        source, referent_groups=[ReferentGroup((ReferentEntity('文字', ('文子',)),),
                                              positions=('transcript_only',))],
        entity_verifier=verifier)
    assert len(audit['repairs']) == 1
    current, merges = merge_release_grade_cues(output)
    assert merges and len(parse_srt_cues(current)) == len(parse_srt_cues(output)) - 1
    saved = tmp_path / 'audit.json'
    saved.write_text(json.dumps(audit, ensure_ascii=False))
    restored = json.loads(saved.read_text())
    row = restored['repairs'][0]
    assert row['cue_indexes'] == [3]
    assert parse_srt_cues(current)[1].index == '2'
    return current, row, {'transcript_entity_audit': restored,
        'post_transcript_entity_output_srt': output,
        'post_transcript_entity_output_srt_sha256': hashlib.sha256(output.encode()).hexdigest()}, saved


@pytest.mark.parametrize('tail,start,end', [
    ('4\n00:00:09,000 --> 00:00:11,000\n后句保留\n', '05,000', '07,000'),
    ('', '19,100', '21,300'),  # The historical index exceeds current cue count.
])
def test_entity_writer_merge_save_restart_preserves_original_receipt(tmp_path, tail, start, end):
    current, row, authority, saved = saved_entity(tmp_path, tail=tail, start=start, end=end)
    original = saved.read_bytes()
    pipeline._validate_source_stage_transcript_entity_repair(row, authority=authority, srt_text=current)
    assert saved.read_bytes() == original
    assert authority['transcript_entity_audit']['repairs'][0] == row
    assert row['cue_binding']['cue_index'] == 3


@pytest.mark.parametrize('drift', ['missing', 'ambiguous', 'after', 'text_only_elsewhere'])
def test_entity_writer_rejects_nonunique_window_or_changed_after(tmp_path, drift):
    current, row, authority, saved = saved_entity(tmp_path, tail='')
    if drift == 'missing':
        current = current.replace('00:00:05,000', '00:00:05,001')
    elif drift == 'ambiguous':
        current += '\n3\n00:00:05,000 --> 00:00:07,000\n其他内容\n'
    elif drift == 'after':
        current = current.replace('保留文字', '不同文字')
    else:
        current = current.replace('00:00:05,000', '00:00:09,000').replace('00:00:07,000', '00:00:11,000')
    original = saved.read_bytes()
    with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_TRANSCRIPT_ENTITY_CURRENT_CUE_DRIFT'):
        pipeline._validate_source_stage_transcript_entity_repair(row, authority=authority, srt_text=current)
    assert saved.read_bytes() == original


@pytest.mark.parametrize('kind', ['danmaku', 'superchat'])
def test_chat_writer_uses_same_window_contract_after_merge(tmp_path, kind):
    source = PREFIX + '3\n00:00:05,000 --> 00:00:09,000\n因为李豆沙是直女\n'
    support = source.replace('直女', '侄女')
    padded = tmp_path / 'padded.mp4'
    padded.with_suffix('.asr_draft.srt').write_text(support)
    chat = [ChatEvidence(kind, 1000, '因为李豆沙是侄女', source_sha256='a' * 64)]
    result = pipeline._apply_entity_authority(
        srt_text=source, authoritative_chat=chat, support_srts=[support],
        referent_groups=[], verify_confusable_entity=lambda _: pytest.fail('provider forbidden'),
        code_switch_audit={}, term_boundary_moves=[], padded=padded, adapters=_adapters())
    current, merges = merge_release_grade_cues(result.srt_text)
    assert merges
    audit = json.loads(json.dumps(result.chat_authority_audit))
    rows = audit['applied']
    assert len(rows) == 1 and rows[0]['cue_indexes'] == [3]
    kwargs = dict(srt_text=current, clip_context={'whole_clip_draft_srt': source},
                  authoritative_chat=chat, independent_support_srts=[support],
                  expected_input_srt_sha256=audit['input_srt_sha256'])
    original = deepcopy(rows)
    pipeline._validate_source_stage_chat_reads(rows, **kwargs)
    assert rows == original
    # Both geometry and source bytes remain binding, independently of text.
    for changed in (current.replace('00:00:05,000', '00:00:05,001'),
                    current + '\n3\n00:00:05,000 --> 00:00:09,000\n其他内容\n',
                    current.replace('因为李豆沙是侄女', '不同内容')):
        with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_SOURCE_STAGE_APPLIED_CHAT_OR_TEXT_DRIFT'):
            pipeline._validate_source_stage_chat_reads(rows, **{**kwargs, 'srt_text': changed})
    damaged = deepcopy(rows)
    damaged[0]['source_sha256'] = 'b' * 64
    with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_SOURCE_STAGE_APPLIED_CHAT_OR_TEXT_DRIFT'):
        pipeline._validate_source_stage_chat_reads(damaged, **kwargs)


def test_entity_same_grid_still_passes(tmp_path):
    current, row, authority, _ = saved_entity(tmp_path, tail='')
    # The ordinary writer's post-entity bytes have not yet been merged.
    unmerged = PREFIX + '3\n00:00:05,000 --> 00:00:07,000\n保留文字\n'
    assert hashlib.sha256(unmerged.encode()).hexdigest() == authority['transcript_entity_audit']['output_srt_sha256']
    pipeline._validate_source_stage_transcript_entity_repair(row, authority=authority, srt_text=unmerged)


@pytest.mark.parametrize('drift', ['historical_index', 'original_bytes'])
def test_entity_retained_original_binding_survives_reindex_fix(tmp_path, drift):
    current, row, authority, _ = saved_entity(tmp_path, tail='')
    if drift == 'historical_index':
        # The legacy adapter must not legitimize a corrupted old ordinal just
        # because its copied time window still finds the right current text.
        row.pop('schema_version')
        row.pop('cue_binding')
        row['cue_indexes'] = [4]
    else:
        authority['post_transcript_entity_output_srt'] += 'changed bytes'
    with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_TRANSCRIPT_ENTITY'):
        pipeline._validate_source_stage_transcript_entity_repair(row, authority=authority, srt_text=current)


def test_pending_audio_request_builder_merge_save_restart(tmp_path, monkeypatch):
    from tests.test_semantic_audio_recovery import ordinary_audio_case
    from tests.test_retained_pending_audio_gate import _clip_context
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    source = PREFIX + '3\n00:00:05,000 --> 00:00:07,000\n保留了一句\n'
    context = _clip_context()
    discovery = pipeline.audit_final_subtitles(source, extract_json=json.loads,
        llm_call=lambda _: json.dumps({'findings': [{'cue': 3, 'kind': 'context',
            'suspect': '了', 'suggestion': None, 'proposed_full_cue': None,
            'repair_class': 'phonetic', 'why': '仍需完整话语的声音',
            'content_risk': 'factual_error', 'risk_basis': 'Synthetic utterance wording is unresolved.'}]}))
    assert len(discovery) == 1
    finding = discovery[0]
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    scaffold = _candidate_blind_missing_proposal_scaffold(source, finding)
    request = pipeline.build_context_adjudication_request(source, scaffold,
        clip_context=context, source_media_timeline_offset_ms=0)
    # Existing offline ordinary needs-audio shape; this test claims no new
    # acoustic observation, only preservation of the unfulfilled obligation.
    adjudication = ordinary_audio_case()[0]['exact_release_adjudication']
    adjudication['request'] = request
    adjudication['witness_judge']['judge']['check_request_sha256'] = request['request_sha256']
    finding['exact_release_adjudication'] = adjudication
    saved = tmp_path / 'pending.json'
    saved.write_text(json.dumps({'final_review_audit': {
        'findings': [finding], 'release_gate': 'BLOCK',
        'reviewed_srt_sha256': 'sha256:' + hashlib.sha256(source.encode()).hexdigest()}}))
    authority = json.loads(saved.read_text())
    current, report = merge_release_grade_cues(source)
    assert report and len(parse_srt_cues(current)) == 2
    before = saved.read_bytes()
    pending = pipeline._retained_pending_audio_findings(current, authority,
        clip_context=context, timeline_offset_ms=0)
    assert len(pending) == 1 and pending[0]['cue_index'] == 2
    assert pending[0]['exact_release_adjudication']['request'] == request
    assert saved.read_bytes() == before
    for changed in (current.replace('00:00:05,000', '00:00:05,001'),
                    current + '\n3\n00:00:05,000 --> 00:00:07,000\n保留了一句\n',
                    current.replace('保留了一句', '不同的话')):
        assert pipeline._retained_pending_audio_findings(changed, authority,
            clip_context=context, timeline_offset_ms=0) == []


def merged_history():
    from src.autoslice import exact_final_cpa_history as history
    from tests.test_producer_text_finalization import _normal_history_display_case, _typed_pass
    template = _normal_history_display_case(path='historical-window')[0]
    repair = deepcopy(template['passes'][0]['repairs'][0])
    source = (PREFIX + '3\n00:00:05,000 --> 00:00:07,000\n旧的文本\n\n'
              '4\n00:00:09,000 --> 00:00:11,000\n保留了一句\n')
    output = source.replace('旧的文本', '新的文本')
    repair.update(cue_index=3, before='旧的文本', after='新的文本',
        before_sha256='sha256:' + hashlib.sha256('旧的文本'.encode()).hexdigest(),
        after_sha256='sha256:' + hashlib.sha256('新的文本'.encode()).hexdigest(),
        matched_start_ms=5000, matched_end_ms=7000)
    repair['mutation_authority']['basis'] = 'CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED'
    repair['acoustic_witness_binding'] = {'judge': {'needs_audio': True}}
    receipt = history.build_run_audit([_typed_pass(pass_index=1, input_text=source,
        output_text=output, repairs=[repair])], [], final_srt_sha256=None)
    current, report = merge_release_grade_cues(output)
    assert report
    return source, current, history.with_release_grade_merge_successor(
        receipt, before=output, after=current, merge_report=report)


def test_legacy_history_nomination_uses_merge_successor_geometry(tmp_path):
    _, current, receipt = merged_history()
    saved = tmp_path / 'history.json'
    saved.write_text(json.dumps({'exact_final_cpa_self_heal': receipt}))
    authority = json.loads(saved.read_text())
    original = saved.read_bytes()
    pending = pipeline._pending_legacy_acoustic_convergence_findings(current, authority)
    assert len(pending) == 1 and pending[0]['cue'] == 2
    assert pending[0]['suspect'] == '新的文本'
    assert pending[0]['proposed_full_cue'] is None
    assert saved.read_bytes() == original


@pytest.mark.parametrize('corrupt_index', [False, True])
def test_history_without_grid_change_keeps_original_index_binding(corrupt_index):
    from src.autoslice import exact_final_cpa_history as history
    _, _, merged = merged_history()
    current = merged['release_grade_merge_successors'][0]['input_srt_text']
    receipt = history.build_run_audit(merged['passes'], [], final_srt_sha256=None)
    if corrupt_index:
        receipt['passes'][0]['repairs'][0]['cue_index'] = 4
    pending = pipeline._pending_legacy_acoustic_convergence_findings(
        current, {'exact_final_cpa_self_heal': receipt})
    if corrupt_index:
        assert pending == []
    else:
        assert len(pending) == 1 and pending[0]['cue'] == 3


def test_current_exact_nomination_keeps_old_request_and_new_target(tmp_path):
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    from tests.test_semantic_audio_recovery import ordinary_audio_case
    source, current, receipt = merged_history()
    finding = pipeline.audit_final_subtitles(source, extract_json=json.loads,
        llm_call=lambda _: json.dumps({'findings': [{'cue': 4, 'kind': 'context',
            'suspect': '了', 'suggestion': None, 'proposed_full_cue': None,
            'repair_class': 'phonetic', 'why': '保留原声音问题',
            'content_risk': 'factual_error', 'risk_basis': 'Synthetic utterance wording is unresolved.'}]}))[0]
    request = pipeline.build_context_adjudication_request(source,
        _candidate_blind_missing_proposal_scaffold(source, finding))
    adjudication = ordinary_audio_case()[0]['exact_release_adjudication']
    adjudication['request'] = request
    adjudication['witness_judge']['judge']['check_request_sha256'] = request['request_sha256']
    finding['exact_release_adjudication'] = adjudication
    saved = tmp_path / 'exact.json'
    saved.write_text(json.dumps({'exact_final_cpa_self_heal': receipt, 'final_review_audit': {
        'findings': [finding], 'release_gate': 'BLOCK',
        'reviewed_srt_sha256': 'sha256:' + hashlib.sha256(source.encode()).hexdigest()}}))
    authority = json.loads(saved.read_text())
    original = saved.read_bytes()
    pending = pipeline._pending_current_exact_findings(current, authority)
    assert len(pending) == 1 and pending[0]['cue'] == 3
    assert pending[0]['exact_release_adjudication']['request'] == request
    assert request['cue_indexes'] == [4]
    assert pending[0]['proposed_full_cue'] is None
    assert 'mutation_authority' not in pending[0]
    assert saved.read_bytes() == original
