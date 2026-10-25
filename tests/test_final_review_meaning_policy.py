"""Current meaning evidence classifies risk; unresolved findings still need adjudication."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice.final_review_contract import (
    FinalReviewContractError, is_keep_current_disclosed, is_noncritical_disclosed,
    pending_noncritical_repair, validate_final_review_release,
)
from src.autoslice.final_review_meaning_policy import POLICY_VERSION, current_meaning_equivalent
from src.autoslice.producer_package_finalization import _postburn_current_risk_partition
from tests.test_final_review_auditor import _srt
from tests.test_producer_current_text_auto_resume import _completed_current_review


def equivalent(detail='数量词一可有可无', meaning='说话人请求喝一些水'):
    return {'status': 'EQUIVALENT', 'current_meaning': meaning, 'alternative_meaning': meaning,
        'shared_meaning': meaning, 'uncertain_detail': detail,
        'context_basis': '当前整句及邻句都是同一请求，数量词一不改变动作、主体、对象或意图。',
        'evidence_cue_ids': [1]}


def _audit(text, rows, *, disclosed=False):
    result, _ = _completed_current_review(text)
    result['meaning_policy'] = POLICY_VERSION
    result.update(findings=[] if disclosed else rows, validated_finding_count=0 if disclosed else len(rows),
        unresolved_findings_disclosed=rows if disclosed else [])
    return result


@pytest.mark.parametrize('target,context', [
    ('喝口水', '前后都在暂停聊天去喝水。'),
    ('喝一口水', '前后都在暂停聊天去喝水。'),
    ('凌云是我的朋友昵称。', '这是同一位已介绍的朋友，未知昵称拼写没有改变人物所指。'),
])
def test_generated_equivalence_reopens_as_unresolved_public_obligation(tmp_path, target, context):
    text = _srt(target)
    assessment = equivalent()
    if '昵称' in target:
        assessment = equivalent('同一朋友昵称的未知拼写', '介绍同一位朋友的昵称')
        assessment['context_basis'] = context
    payload = {'findings': [{'cue': 1, 'kind': 'context', 'repair_class': 'disclosure_only',
        'proposed_full_cue': None, 'why': '仅保留有依据的等价细节。',
        'content_risk': 'noncritical_uncertainty', 'risk_basis': context,
        'meaning_equivalence': assessment}]}
    calls = []
    def reviewer(prompt):
        calls.append(prompt)
        assert POLICY_VERSION in prompt and '改变或破坏原意' in prompt
        return json.dumps(payload, ensure_ascii=False)
    rows = auditor.audit_final_subtitles(text, llm_call=reviewer, extract_json=json.loads,
        candidate_context_text=context)
    path = tmp_path/'review.json'
    auditor.persist_review_audit(path, {'findings': rows})
    loaded = json.loads(path.read_text())['findings']
    assert len(calls) == 1 and is_noncritical_disclosed(loaded[0])
    assert not pending_noncritical_repair(loaded[0])
    original_rows, saved_bytes = deepcopy(loaded), path.read_bytes()
    # Exercise the real public adjudicator for both a failed model call and a
    # completed but unresolved reply. Meaning equivalence supplies neither answer.
    for outcome in ('ERROR', 'OUTCOME_UNKNOWN'):
        judge_calls = []
        def unavailable_judge(prompt):
            judge_calls.append(prompt)
            if outcome == 'ERROR':
                raise RuntimeError('synthetic offline adjudication failure')
            return json.dumps({'status': 'UNRESOLVED', 'proposed_cue': None,
                'decision': 'UNRESOLVED', 'replacement_text': None, 'needs_audio': True,
                'reason': 'Synthetic unresolved response; original listening outcome remains unknown.'})
        pending, resolved = auditor.adjudicate_exact_release_findings(
            text, loaded, entity_verifier=lambda request: {'status': 'OUTCOME_UNKNOWN'},
            judge_llm_call=unavailable_judge)
        assert judge_calls and len(pending) == 1 and not resolved
        assert pending[0].get('exact_release_adjudication')
        assert not is_keep_current_disclosed(pending[0])
        assert _postburn_current_risk_partition(pending) == (pending, [])
        with pytest.raises(FinalReviewContractError):
            validate_final_review_release(_audit(text, pending, disclosed=True))
        assert loaded == original_rows and path.read_bytes() == saved_bytes
    assert text == _srt(target)


@pytest.mark.parametrize('context,target', [
    ('小林把书借给了小周，小周是借入方。', '小周把书借给了小林。'),
    ('讨论电阻串联后同一电流的路径。', '这两个电阻在串流。'),
])
def test_new_no_candidate_meaning_damage_gets_current_critical_classification_and_blocks(tmp_path, context, target):
    text = _srt(context, target)
    calls = []
    def reviewer(prompt):
        calls.append(prompt)
        row = {'cue': 2, 'kind': 'context', 'repair_class': 'phonetic', 'proposed_full_cue': None,
            'why': '现文与整片关系或主题词冲突，尚无可靠完整替换。',
            'content_risk': 'noncritical_uncertainty',
            'risk_basis': '没有可靠替换，所以不确定。'}
        if len(calls) > 1:
            assert '施受' in prompt and '句意等价' in prompt
            row.update(content_risk='meaning_reversal', risk_basis='当前句改变施受关系或破坏主题术语，非反义也改变原意。')
        return json.dumps({'findings': [row]}, ensure_ascii=False)
    rows = auditor.audit_final_subtitles(text, llm_call=reviewer, extract_json=json.loads,
        candidate_context_text=context)
    assert len(calls) == 2 and rows[0]['content_risk'] == 'meaning_reversal'
    path = tmp_path/'generated.json';auditor.persist_review_audit(path, {'findings': rows})
    loaded = json.loads(path.read_text())['findings']
    assert not is_keep_current_disclosed(loaded[0])
    assert _postburn_current_risk_partition(loaded) == (loaded, [])
    pending, resolved = auditor.adjudicate_exact_release_findings(text, loaded, entity_verifier=lambda _: {})
    assert pending and not resolved and not is_keep_current_disclosed(pending[0])
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(_audit(text, pending))


@pytest.mark.parametrize('assessment', [None, {'status': 'UNRESOLVED'}, {'status': 'MEANING_CHANGED'}])
def test_wrong_noncritical_label_without_positive_equivalence_is_not_a_current_review(assessment):
    text = _srt('这句话在上下文里没有可解释的意思。')
    raw = {'findings': [{'cue': 1, 'kind': 'context', 'repair_class': 'phonetic',
        'proposed_full_cue': None, 'content_risk': 'noncritical_uncertainty',
        'risk_basis': '听不清且没有候选，不代表现文正确。', 'why': '当前语义损坏。',
        'meaning_equivalence': assessment}]}
    with pytest.raises(auditor.FinalReviewAuditError, match='MEANING_EQUIVALENCE_REQUIRED'):
        auditor.audit_final_subtitles(text, llm_call=lambda _: json.dumps(raw), extract_json=json.loads)


@pytest.mark.parametrize('drift', ['srt', 'cue', 'proposal', 'context', 'policy'])
def test_saved_equivalence_cannot_close_a_different_current_question(drift):
    text = _srt('喝口水')
    rows = auditor.audit_final_subtitles(text, llm_call=lambda _: json.dumps({'findings': [{
        'cue': 1, 'kind': 'context', 'repair_class': 'disclosure_only', 'proposed_full_cue': None,
        'why': '数量词未影响句意。', 'content_risk': 'noncritical_uncertainty',
        'risk_basis': '前后同一喝水请求。', 'meaning_equivalence': equivalent()}]}), extract_json=json.loads)
    row = deepcopy(rows[0])
    if drift == 'srt': row['current_content_risk']['reviewed_srt_sha256'] = 'sha256:'+'a'*64
    elif drift == 'cue': row['cue_index'] = 2
    elif drift == 'proposal': row['proposed_full_cue'] = '她给他倒水'
    elif drift == 'context': row['current_content_risk']['review_context_sha256'] = 'sha256:'+'a'*64
    else: row['current_content_risk']['policy_version'] = 'old-policy'
    assert not current_meaning_equivalent(row, expected_srt_sha256='sha256:'+hashlib.sha256(text.encode()).hexdigest())
    if drift != 'srt': assert not is_keep_current_disclosed(row)
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(_audit(text, [row], disclosed=True))


def test_legacy_clean_and_typed_current_do_not_supply_missing_meaning_judgment():
    text = _srt('无可靠替换的语义损坏。')
    old, _ = _completed_current_review(text)
    old.pop('meaning_policy', None)
    with pytest.raises(FinalReviewContractError, match='MEANING_POLICY_REVIEW_REQUIRED'):
        validate_final_review_release(old)
    row = {'content_risk': 'noncritical_uncertainty', 'risk_basis': '没有反义或事实指控。',
        'repair_class': 'phonetic', 'proposed_full_cue': None,
        'exact_release_adjudication': {'status': 'OBSERVED', 'repaired': False,
            'policy_branch': 'KEEP_CURRENT', 'timing_immutable': True,
            'mutation_authority': {'status': 'NOT_APPLIED'}}}
    assert pending_noncritical_repair(row) and not is_keep_current_disclosed(row)


@pytest.mark.parametrize('key', ['exact_release_adjudication', 'context_audio_adjudication'])
@pytest.mark.parametrize('failure', ['ERROR', 'OUTCOME_UNKNOWN', 'APPLIED', 'BLOCK'])
def test_positive_equivalence_cannot_hide_a_required_typed_failure(key, failure):
    text = _srt('喝口水')
    row, = auditor.audit_final_subtitles(text, llm_call=lambda _: json.dumps({'findings': [{
        'cue': 1, 'kind': 'context', 'repair_class': 'phonetic', 'proposed_full_cue': None,
        'why': '数量词差异', 'content_risk': 'noncritical_uncertainty',
        'risk_basis': '同一喝水请求', 'meaning_equivalence': equivalent()}]}), extract_json=json.loads)
    row[key] = {'status': failure if failure in {'ERROR', 'OUTCOME_UNKNOWN'} else 'OBSERVED',
        'policy_branch': 'JUDGE_KEEPS_CURRENT', 'repaired': False, 'timing_immutable': True,
        'mutation_authority': {'status': failure if failure in {'APPLIED', 'BLOCK'} else 'NOT_APPLIED'}}
    assert not is_noncritical_disclosed(row) and not is_keep_current_disclosed(row)


@pytest.mark.parametrize('safe', [False, True])
def test_ordinary_exact_producer_consumes_meaning_contract_before_release(monkeypatch, safe):
    from src.autoslice import producer_text_pipeline as pipeline
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass
    text = _srt('说话人正在暂停聊天喝水。', '喝口水' if safe else '这两个电阻在串流。')
    correction = _correction_pass()
    correction['boundary_semantic_review'] = _completed_current_review(text)[0]['boundary_semantic_review']
    monkeypatch.setattr(pipeline, 'discover_candidate_pronoun_findings', lambda *a, **k: ([], {}))
    monkeypatch.setattr(pipeline, 'clip_context_prompt_text', lambda _: '当前上下文包含完整两句，不能仅凭没候选判无风险。')
    item = {'cue': 2, 'kind': 'context', 'repair_class': 'disclosure_only' if safe else 'phonetic',
        'proposed_full_cue': None, 'why': '当前整句语义判断。',
        'content_risk': 'noncritical_uncertainty' if safe else 'meaning_reversal',
        'risk_basis': '数量词差异不影响同一请求。' if safe else '主题术语破坏，尚无可靠替换。'}
    if safe:
        item['meaning_equivalence'] = equivalent()
        item['meaning_equivalence']['evidence_cue_ids'] = [1, 2]
    result = pipeline._run_exact_final_release_review(srt_text=text, correction_audit=correction,
        adapters=_adapters(), authoritative_chat=(), selection_hook='', clip_context={},
        final_review_llm=lambda _: json.dumps({'findings': [item]}),
        verify_confusable_entity=lambda _: {})
    assert result['meaning_policy'] == POLICY_VERSION

    if safe:
        assert result['release_gate'] == 'BLOCK' and result['findings']
        assert not result['unresolved_findings_disclosed']
        with pytest.raises(FinalReviewContractError):
            validate_final_review_release(result)
    else:
        assert result['release_gate'] == 'BLOCK' and result['findings']
        with pytest.raises(FinalReviewContractError):
            validate_final_review_release(result)


@pytest.mark.parametrize('valid', [False, True])
def test_candidate_risk_second_review_requires_equivalence_effect_and_current_binding(valid):
    from src.autoslice.final_review_candidate_risk import complete_candidate_content_risks, candidate_risk_assessment_valid
    from src.autoslice.jingting_chunker import parse_srt_cues
    text = _srt('这位朋友昵称叫凌云。')
    row = {'cue_index': 1, 'base_text_sha256': hashlib.sha256('这位朋友昵称叫凌云。'.encode()).hexdigest(),
        'kind': 'entity', 'suspect': '凌云', 'repair_class': 'source_backed_entity',
        'proposed_full_cue': '这位朋友昵称叫林云。', 'content_risk': 'noncritical_uncertainty',
        'risk_basis': '没有伤害或事实冲突。', 'candidate_provenance': {'kind': 'saved_source_candidate'}}
    decision = {'cue': 1, 'content_risk': 'noncritical_uncertainty',
        'content_effect': 'context_supported_meaning_equivalence' if valid else 'no_supported_critical_effect',
        'risk_basis': '同一已介绍朋友，昵称字形差异未改变所指。', 'evidence_cue_ids': [1]}
    if valid:
        decision['meaning_equivalence'] = equivalent('同一朋友昵称字形', '介绍同一位朋友')
        decision['meaning_equivalence']['context_basis'] = '当前句明确是在介绍同一位朋友的昵称，不改变人物或行为关系。'
    def review(prompt):
        assert 'V2_MEANING_EQUIVALENCE' in prompt and POLICY_VERSION in prompt
        return json.dumps({'findings': [decision]})
    if not valid:
        with pytest.raises(ValueError):
            complete_candidate_content_risks([row], srt_text=text, cues=parse_srt_cues(text),
                prompt='完整当前字幕和源观察', llm_call=review, extract_json=json.loads)
    else:
        complete_candidate_content_risks([row], srt_text=text, cues=parse_srt_cues(text),
            prompt='完整当前字幕和源观察', llm_call=review, extract_json=json.loads)
        assert candidate_risk_assessment_valid(row) and current_meaning_equivalent(row)
        row['proposed_full_cue'] = '另外一个人在说话。'
        assert not candidate_risk_assessment_valid(row) and not current_meaning_equivalent(row)
