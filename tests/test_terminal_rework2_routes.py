"""T9 route reachability against a real terminal request producer."""

from copy import deepcopy
import json

import pytest

from src.autoslice.semantic_terminal_adjudication import (
    available_keep_routes,
    validate_terminal_answer,
)
from tests.test_targeted_relisten_public_contract import build_offline_rig
from tests.test_terminal_contract_roundtrip import c_reply, produce


CURRENT = "你喜欢，嘿嘿嘿"
PROPOSED = "你喜欢老男人"
KEEP_B = "B_EVIDENCE_SUPPORTS_CURRENT"
KEEP_C = "C_MEANING_EQUIVALENT"


def _stamp(ms):
    return f"00:{ms // 60000:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _cue25_srt():
    texts = [f"上下文第{i}句" for i in range(1, 25)]
    texts.extend([CURRENT, "我不喜欢老男人", "你喜欢老男人", "我也喜欢"])
    rows = []
    for index, text in enumerate(texts, 1):
        start = (index - 1) * 900
        rows.append(f"{index}\n{_stamp(start)} --> {_stamp(start + 800)}\n{text}")
    return "\n\n".join(rows) + "\n"


def _cue25_finding():
    return {
        "cue_index": 25,
        "suspect": "，嘿嘿嘿",
        "suggestion": "老男人",
        "proposed_full_cue": PROPOSED,
        "repair_class": "spoken_unit",
        "content_risk": "noncritical_uncertainty",
        "risk_basis": "目标句尾部笑声的逐字写法不确定，但不改变完整句意。",
        "span_start_codepoint": 3,
        "span_end_codepoint": 7,
    }


def _cue25_rig(tmp_path, monkeypatch, name):
    return build_offline_rig(
        tmp_path / name,
        monkeypatch,
        srt=_cue25_srt(),
        finding=_cue25_finding(),
        source_duration_ms=30_000,
    )


def _patch_cue25_range_plans(monkeypatch):
    import tests.test_targeted_relisten_public_contract as public_contract
    from src.autoslice import context_assisted_audio

    monkeypatch.setattr(public_contract, "SRT", _cue25_srt())

    def range_plan(_planner=None):
        return {
            "decision": "OBSERVE",
            "provider": "gemini",
            "first_cue": 23,
            "last_cue": 27,
            "observation_class": "CANDIDATE_BLIND_AUDIO",
            "purpose": "FULL_QA",
            "reason": "把目标前后完整问答作为一块原音，区分句首和尾部笑声。",
            "question": "目标句是否完整，尾部是否只是非语义笑声？",
        }

    def assisted_plan(_planner=None):
        return {
            "decision": "OBSERVE",
            "provider": "gemini",
            "first_cue": 23,
            "last_cue": 27,
            "observation_class": context_assisted_audio.OBSERVATION_CLASS,
            "purpose": "SOURCED_CONTEXT",
            "reason": "用相邻问答核对目标句的交际作用。",
            "question": "目标句是否完整，尾部是否只是非语义笑声？",
            "context_refs": ["cue:24", "cue:26"],
            "context_summary": "相邻字幕围绕目标句的问答关系。",
            "proper_names": [],
            "include_candidates": True,
        }

    monkeypatch.setattr(public_contract, "observe_plan", range_plan)
    monkeypatch.setattr(public_contract, "assisted_plan", assisted_plan)


def _c_equivalence(data, refs):
    return {
        **c_reply(data, refs),
        "comparison_text": data["current"],
        "meaning_equivalence": {
            "status": "EQUIVALENT",
            "current_meaning": "同一提问对象",
            "alternative_meaning": "尾部笑声的字形存在不确定",
            "shared_meaning": "句子仍然指向同一对象",
            "uncertain_detail": "笑声的逐字写法",
            "context_basis": "相邻问答保留完整交际作用",
            "evidence_cue_ids": [25, 26, 27],
        },
    }


def _b_supported(data, refs):
    assert data["current_support_observation_refs"]
    return {
        **c_reply(data, refs),
        "grade": "B",
        "keep_route": KEEP_B,
        "evidence_decisive": True,
        "comparison_text": data["current"],
        "evidence_refs": data["current_support_observation_refs"],
        "meaning_equivalence": None,
    }


def test_cue25_real_producer_supports_substantive_words_without_literal_laughter(
    tmp_path, monkeypatch
):
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, "partial-native")
    # The native producers recover only the spoken prefix. The blind witness
    # remains the unrelated/mismatching pinyin shape from the real producer.
    audit, captured = produce(rig, _c_equivalence, native=("你喜欢", "你喜欢"))
    data = captured["data"]
    schema = captured["response_schema"]
    prompt = captured["prompt"]
    target_material = captured["request"]["audio_gain_termination"]["material_exhaustion"][
        "target_material"
    ]

    assert audit["policy_branch"] == "SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP"
    assert target_material["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert data["current"] == CURRENT
    assert data["current_support_observation_refs"]
    assert set(data["keep_routes"]) == {KEEP_B, KEEP_C}
    assert KEEP_B in schema["properties"]["keep_route"]["enum"]
    assert KEEP_B in prompt
    assert KEEP_C in prompt
    assert available_keep_routes(captured["request"]) == data["keep_routes"]
    assert validate_terminal_answer(captured["request"], captured["answer"]) == "CURRENT"

    with pytest.raises(ValueError, match="TERMINAL_CURRENT_OBSERVATION_SUPPORT_REQUIRED"):
        validate_terminal_answer(
            captured["request"],
            {
                **captured["answer"],
                "grade": "B",
                "keep_route": KEEP_B,
                "evidence_decisive": True,
                "evidence_refs": ["audio"],
                "comparison_text": CURRENT,
            },
        )


def test_cue25_supported_mirror_exposes_and_accepts_b_route(tmp_path, monkeypatch):
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, "supported-native")
    audit, captured = produce(rig, _b_supported, native=True)
    data = captured["data"]

    assert data["current_support_observation_refs"]
    assert set(data["keep_routes"]) == {KEEP_B, KEEP_C}
    assert captured["response_schema"]["properties"]["keep_route"]["enum"] == [KEEP_B, KEEP_C, None]
    assert KEEP_B in captured["prompt"]
    assert validate_terminal_answer(captured["request"], captured["answer"]) == "CURRENT"


def test_b_keep_roundtrip_does_not_require_literal_laughter(tmp_path, monkeypatch):
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, 'partial-b')
    audit, captured = produce(rig, _b_supported, native=('你喜欢', '你喜欢'))
    assert audit.get('keep_route') == KEEP_B, audit
    assert audit.get('repaired') is False
    from src.autoslice.final_review_contract import is_release_resolved_keep
    row = {**audit['rebuilt_finding'], 'exact_release_adjudication': audit}
    assert is_release_resolved_keep(row)
    assert captured['request']['current_cue'] == CURRENT
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"


def test_grade_a_transport_description_and_validator_share_real_witness_identity(tmp_path, monkeypatch):
    from src.autoslice.semantic_terminal_adjudication import grade_a_witness_contract, grade_a_witnesses
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, "a-witness-identity")
    _, captured = produce(rig, _b_supported, native=True)
    request, answer = captured['request'], captured['answer']
    contract = grade_a_witness_contract(request)
    assert json.dumps(contract, sort_keys=True) in captured['response_schema']['properties']['grade']['description']
    observations = request['audio_gain_termination']['projection']['observations']
    wide = [r for r in observations if r.get('receipt_kind') == contract['receipt_kind']]
    identity = tuple(wide[0]['raw_observation'].get(key) for key in contract['independence_fields'])
    refs = [r['observation_id'] for r in wide if tuple(r['raw_observation'].get(key)
        for key in contract['independence_fields']) == identity]
    assert len(grade_a_witnesses(request, refs)) == 1
    # Preserve real complete-current support while overclaiming its A grade.
    with pytest.raises(ValueError, match='TERMINAL_A_INDEPENDENT_WIDE_EVIDENCE_REQUIRED'):
        validate_terminal_answer(request, {**answer, 'grade': 'A', 'evidence_refs':
            list(dict.fromkeys([*refs, *[r for r in answer['evidence_refs']
                if r not in {w['observation_id'] for w in wide}]]))})


def test_t4_real_cue25_closed_tail_does_not_replace_unresolved_full_span(tmp_path, monkeypatch):
    from src.autoslice.audio_question_identity import same_audio_question
    from src.autoslice.exact_review_parallel import group_exact_review_agenda
    from src.autoslice.exact_final_terminal_funnel import unresolved_review_rows
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.final_review_contract import is_release_resolved_keep
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, 'closed-tail-agenda')
    adjudication, captured = produce(rig, _c_equivalence, native=('你喜欢', '你喜欢'))
    tail = {**adjudication['rebuilt_finding'], 'exact_release_adjudication': adjudication}
    full = {**_cue25_finding(), 'suspect': CURRENT, 'suggestion': CURRENT,
        'proposed_full_cue': CURRENT, 'span_start_codepoint': 0, 'span_end_codepoint': 7}
    terminal = captured['request']
    full_check = build_context_adjudication_request(terminal['check_request']['whole_clip_current_srt'], full,
        clip_context=terminal['terminal_context'],
        source_media_timeline_offset_ms=terminal['source_media_timeline_offset_ms'],
        require_complete_utterance_support=True)
    # G6 deliberately ignores character spans when the whole cue and actual
    # source-clock target are equal. Never fake a different physical window.
    assert same_audio_question(full_check, terminal['check_request'], compare_proposed=False, strict_source=True)[0]
    assert is_release_resolved_keep(tail)
    review = {'findings': [full], 'unresolved_findings_disclosed': [tail]}
    agenda = unresolved_review_rows(review)
    assert agenda == [full]
    assert group_exact_review_agenda(agenda, request_for_finding=lambda _: full_check) == [[0]]
    assert review['unresolved_findings_disclosed'] == [tail]


@pytest.mark.parametrize('tail_only', [False, True])
def test_cue25_simultaneously_pending_spans_share_full_cue_request_and_individual_gates(
    tmp_path, monkeypatch, tail_only
):
    from src.autoslice.context_assisted_audio import digest
    from src.autoslice.exact_final_terminal_funnel import terminalize_review, review_pass_receipt
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.span_finding_obligations import validate_release_obligations
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, 'pending-spans')
    _, captured = produce(rig, _c_equivalence, native=('你喜欢', '你喜欢'))
    tail = deepcopy(captured['request']['audio_gain_termination']['stopped_finding'])
    full = {**deepcopy(tail), 'suspect': CURRENT, 'suggestion': CURRENT,
        'proposed_full_cue': CURRENT, 'span_start_codepoint': 0, 'span_end_codepoint': 7}
    # Put the narrower member first: the owner must still ask about the whole
    # cue, whose physical window and text cover the union [0, 7).
    review = {'findings': [tail, full], 'unresolved_findings_disclosed': [],
        'reason_codes': ['FINAL_REVIEW_UNRESOLVED_FINDINGS']}
    receipt = review_pass_receipt(pass_index=1, srt_text=rig.srt, audit=review,
        repaired_text=rig.srt, repairs=[])
    calls = []
    def call(prompt):
        data = json.loads(prompt.rsplit('\n', 1)[1])
        refs = json.loads(next(line.split('：', 1)[1] for line in prompt.splitlines()
            if line.startswith('可引用编号')))
        calls.append(data)
        assert data['current'] == CURRENT
        assert {item['finding_sha256'] for item in data['terminal_group_questions']} == {
            digest(tail), digest(full)}
        answer = _c_equivalence(data, refs)
        return json.dumps(answer, ensure_ascii=False)
    adapters = {
        'clip_context': rig.context, 'source_media_timeline_offset_ms': rig.offset_ms,
        'prior_range_acoustic_observations': captured['request']['check_request'].get(
            'context_only_range_acoustic_observations', []), 'judge_llm_call': call}
    if tail_only:
        import tests.test_targeted_relisten_public_contract as public_contract
        tail_srt = rig.srt.replace(CURRENT, CURRENT[3:])
        monkeypatch.setattr(public_contract, 'SRT', tail_srt)
        narrow_rig = build_offline_rig(tmp_path / 'tail-only', monkeypatch,
            srt=tail_srt, finding={**_cue25_finding(), 'suspect': CURRENT[3:],
                'suggestion': CURRENT[3:], 'proposed_full_cue': CURRENT[3:],
                'span_start_codepoint': 0, 'span_end_codepoint': len(CURRENT[3:])},
            source_duration_ms=30_000)
        narrow_answer, narrow = produce(narrow_rig, _c_equivalence)
        assert narrow['data']['current'] == CURRENT[3:]
        assert is_release_resolved_keep({**narrow_answer['rebuilt_finding'],
            'exact_release_adjudication': narrow_answer})
        def owner(*args):
            calls.append(narrow['data'])
            return deepcopy(narrow_answer)
        adapters['terminal_owner'] = owner
    result = terminalize_review(rig.srt, review, pass_receipts=[receipt], adapters=adapters)
    assert len(calls) == 1
    if tail_only:
        assert result['findings']
        assert not any(is_release_resolved_keep(row) for row in result['findings'])
        assert not result['unresolved_findings_disclosed']
        assert all(row['terminal_funnel_error']['reason_code'] ==
            'TERMINAL_GROUP_ANSWER_QUESTION_MISMATCH' for row in result['findings'])
    else:
        assert result['findings'] == []
        rows = result['unresolved_findings_disclosed']
        assert len(rows) == 2 and all(is_release_resolved_keep(row) for row in rows)
        assert [(row['span_start_codepoint'], row['span_end_codepoint']) for row in rows] == [(3, 7), (0, 7)]
        for row in rows:
            terminal = row['exact_release_adjudication']
            check = terminal['terminal_review']['request']['check_request']
            assert check['matched_audio_text'] == check['current_cue'] == CURRENT
            assert check['matched_start_ms'] == captured['request']['check_request']['matched_start_ms']
            assert check['matched_end_ms'] == captured['request']['check_request']['matched_end_ms']
            assert len(terminal['terminal_question_originals']) == 2
            assert terminal['mutation_authority']['status'] == 'NOT_APPLIED'
        validate_release_obligations(result)


def test_model_receipt_catalogue_roundtrip_preserves_hearings_and_context(tmp_path, monkeypatch):
    from src.autoslice.semantic_terminal_adjudication import (
        _compact_model_receipt_metadata, _factor_quality_metadata, _factor_repeated_readings,
    )
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, 'receipt-catalogue')
    _, captured = produce(rig, _c_equivalence)
    original_request = deepcopy(captured['request'])
    data = deepcopy(captured['data'])
    for row in data['observations']:
        raw = row['raw_observation']
        raw['quality_and_provider'] = [data.get('quality_catalogue', [])[item['quality_index']]
            if 'quality_index' in item else item for item in raw['quality_and_provider']]
        for reading in raw['readings']:
            if 'text_ref' in reading:
                reading['text'] = data['reading_texts'][reading.pop('text_ref')]
    context = data['whole_session_and_session_context']
    context['source_fact'] = '有来源的长语境事实' * 600
    before = deepcopy(data)
    _compact_model_receipt_metadata(data)
    assert context['source_fact'] == data['whole_session_and_session_context']['source_fact']
    indexes = data['stopped_finding_evidence']['recorded_evidence']['observation_indexes']
    previous = before['stopped_finding_evidence']['recorded_evidence']
    expected = previous if isinstance(previous, list) else [
        before['observations'][i]['observation_id'] for i in previous['observation_indexes']]
    assert [data['observations'][i]['observation_id'] for i in indexes] == expected
    assert data['observations'] == before['observations']
    # Repeated protocol diagnostics and text remain reversible per hearing;
    # sharing them never merges the independent observation rows.
    for row in data['observations']:
        row['raw_observation']['quality_and_provider'].extend([
            {'reason': '离线重复诊断完整保留' * 30, 'status': 'UNCERTAIN'}] * 6)
        row['raw_observation']['readings'].extend([
            {'field': 'transcript', 'text': '离线长转写逐字保留' * 30}] * 6)
    original_observations = deepcopy(data['observations'])
    _factor_quality_metadata(data)
    _factor_repeated_readings(data)
    restored = deepcopy(data['observations'])
    for row in restored:
        raw = row['raw_observation']
        raw['quality_and_provider'] = [data['quality_catalogue'][item['quality_index']]
            if 'quality_index' in item else item for item in raw['quality_and_provider']]
        for reading in raw['readings']:
            if 'text_ref' in reading:
                reading['text'] = data['reading_texts'][reading.pop('text_ref')]
    assert restored == original_observations
    assert captured['request'] == original_request
