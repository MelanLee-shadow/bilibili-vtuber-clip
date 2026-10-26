"""F2: ordinary producers, saved answers and actual release/apply consumers."""
from copy import deepcopy
import json

import pytest

from src.autoslice import finding_lifecycle as lifecycle
from tests.test_exact_final_funnel import terminal_rig, run_terminal, unresolved, answer, SRT  # noqa: F401
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401


def test_closed_state_transition_and_unknown_contract():
    assert set(lifecycle.ACTIONS) == set(lifecycle.State) == set(lifecycle.TRANSITIONS)
    for current in lifecycle.State:
        assert lifecycle.transition(current, current) == current
        for following in lifecycle.TRANSITIONS[current]:
            assert lifecycle.transition(current, following) == following
    with pytest.raises(lifecycle.LifecycleError, match='UNKNOWN_STATE'):
        lifecycle.transition('new_pending_spelling', lifecycle.State.KEEP)
    with pytest.raises(lifecycle.LifecycleError, match='TRANSITION_INVALID'):
        lifecycle.transition(lifecycle.State.EDIT_REVIEW, lifecycle.State.KEEP)
    row = unresolved()
    row['exact_release_adjudication']['status'] = 'NEW_UNKNOWN_STATUS'
    with pytest.raises(lifecycle.LifecycleError, match='UNKNOWN_RECEIPT_STATUS'):
        lifecycle.classify(row, terminal=True)


@pytest.mark.parametrize('status', ['UNCERTAIN', 'OBSERVED', 'MISSING_PROPOSAL_UNRESOLVED',
                                  'CONTEXT_REVIEW', 'ARGON_PENDING', 'RETRY_BLOCKED'])
def test_every_unfinished_receipt_stays_pending_and_closed_to_release(status):
    row = {'exact_release_adjudication': {'status': status, 'repaired': False}}
    original = deepcopy(row)
    view = lifecycle.classify(row, terminal=True)
    assert view['state'] == lifecycle.LEGACY_STAGES[status]
    assert view['release_action'] == 'CONTINUE_REVIEW'
    assert lifecycle.release_reason_codes([row], terminal=True) == [lifecycle.REVIEW_PENDING]
    assert view['new_model_call_authorized'] is False
    assert row == original


def test_call_failure_and_claimed_d_never_become_content_d():
    for code, expected in [('LlmCallError', 'CALL_FAILURE'), ('TERMINAL_UNRESOLVABLE', 'CONTRACT_ERROR')]:
        row = unresolved(code)
        assert lifecycle.classify(row, terminal=True)['state'] == expected
        assert lifecycle.release_reason_codes([row], terminal=True) == [lifecycle.ENGINEERING_FAILURE]


def test_original_funnel_exception_without_status_is_engineering_not_unknown_fallback():
    row = {'terminal_funnel_error': {'prior_evidence_retained': True,
                                   'reason_code': 'a future specific consumer error'}}
    original = deepcopy(row)
    routed = lifecycle.review_outcome({'findings': [row], 'reason_codes': []}, terminal=True)
    assert routed['findings'][0]['lifecycle']['state'] == 'CONTRACT_ERROR'
    assert routed['reason_codes'] == [lifecycle.ENGINEERING_FAILURE]
    assert routed['release_gate'] == 'BLOCK' and row == original
    row['terminal_funnel_error']['status'] = 'NEW_UNKNOWN_STATUS'
    with pytest.raises(lifecycle.LifecycleError, match='UNKNOWN_STATE'):
        lifecycle.classify(row, terminal=True)
    with pytest.raises(lifecycle.LifecycleError, match='LEGACY_ERROR_RECEIPT_INVALID'):
        lifecycle.classify({'terminal_funnel_error': {}}, terminal=True)


def test_unfinished_disclosed_row_returns_to_pending_not_pass():
    result = lifecycle.review_outcome({'findings': [], 'unresolved_findings_disclosed': [unresolved()],
        'reason_codes': [], 'release_gate': 'PASS'}, terminal=True)
    assert result['unresolved_findings_disclosed'] == []
    assert len(result['findings']) == 1
    assert result['reason_codes'] == [lifecycle.REVIEW_PENDING]


@pytest.mark.parametrize('has_finding,obligation_error,expected', [
    (True, lifecycle.REVIEW_PENDING, lifecycle.ENGINEERING_FAILURE),
    (False, lifecycle.REVIEW_PENDING, lifecycle.REVIEW_PENDING),
    (True, 'SPAN_COMPILATION_IDENTITY_DRIFT', 'SPAN_COMPILATION_IDENTITY_DRIFT'),
])
def test_release_obligation_preserves_specific_failure_and_integrity(monkeypatch, has_finding,
                                                                   obligation_error, expected):
    from tests.test_producer_current_text_auto_resume import _completed_current_review
    from src.autoslice import span_finding_obligations
    from src.autoslice.final_review_contract import validate_final_review_release, FinalReviewContractError
    audit, _ = _completed_current_review('1\n00:00:00,000 --> 00:00:00,100\n合成字幕\n')
    audit['findings'] = [unresolved('LlmCallError')] if has_finding else []
    audit['validated_finding_count'] = len(audit['findings'])
    def check_obligations(_audit):
        raise ValueError(obligation_error)
    monkeypatch.setattr(span_finding_obligations, 'validate_release_obligations', check_obligations)
    with pytest.raises(FinalReviewContractError, match=expected):
        validate_final_review_release(audit, expected_srt_sha256=audit['reviewed_srt_sha256'])


def test_stopped_legacy_review_preserves_pending_without_release_authority():
    from tests.test_producer_current_text_auto_resume import _completed_current_review
    from src.autoslice.final_review_contract import validate_final_review_release, FinalReviewContractError
    audit, _ = _completed_current_review('1\n00:00:00,000 --> 00:00:00,100\n合成字幕\n')
    audit.pop('terminal_funnel', None)
    audit['findings'] = [unresolved()]
    audit['validated_finding_count'] = 1
    closed = lifecycle.review_outcome(audit, terminal=True)
    assert closed['findings'][0]['lifecycle']['state'] == 'AWAITING_EVIDENCE'
    assert lifecycle.release_reason_codes(closed['findings']) == [lifecycle.REVIEW_PENDING]
    with pytest.raises(FinalReviewContractError, match=lifecycle.REVIEW_PENDING):
        validate_final_review_release(closed, expected_srt_sha256=closed['reviewed_srt_sha256'])
    # An unsupported saved KEEP is no authority; its original receipt still fails.
    closed['findings'][0]['lifecycle']['state'] = 'KEEP'
    assert lifecycle.release_reason_codes(closed['findings']) == [lifecycle.REVIEW_PENDING]


def test_d_explanation_is_not_a_candidate_utterance(monkeypatch, terminal_rig):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    response = {**answer('UNRESOLVABLE'), 'comparison_text': '可能说我不喜欢她，也可能喜欢；不能据此确定。'}
    result = run_terminal([unresolved()], lambda _: json.dumps(response, ensure_ascii=False),
                          terminal_rig=terminal_rig)
    assert result['findings'][0]['lifecycle']['state'] == 'CONTRACT_ERROR'
    assert result['reason_codes'] == [lifecycle.ENGINEERING_FAILURE]


@pytest.mark.parametrize('choice,expected', [('CURRENT', 'KEEP'), ('UNRESOLVABLE', 'TRUE_BLOCK')])
def test_real_terminal_producer_to_lifecycle_and_release_action(monkeypatch, terminal_rig, choice, expected):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    row = unresolved()
    if choice == 'UNRESOLVABLE':
        row.update(suspect='喜欢她啊', suggestion='不喜欢她', proposed_full_cue='我不喜欢她')
    result = run_terminal([row], lambda p: json.dumps(answer(choice), ensure_ascii=False),
                          terminal_rig=terminal_rig)
    rows = result['findings'] + result['unresolved_findings_disclosed']
    assert rows[0]['lifecycle']['state'] == expected
    assert lifecycle.classify(rows[0], terminal=True) == rows[0]['lifecycle']
    assert result['reason_codes'] == ([] if expected == 'KEEP' else [lifecycle.CONTENT_BLOCK])
    assert result['release_gate'] == ('PASS' if expected == 'KEEP' else 'BLOCK')


def test_engineering_repair_reuses_original_answer_without_new_call(monkeypatch, terminal_rig):
    from src.autoslice import semantic_terminal_adjudication as terminal
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    real = terminal.validate_terminal_answer
    calls = []
    def reply(prompt):
        calls.append(prompt)
        return json.dumps(answer(), ensure_ascii=False)
    def broken(*args, **kwargs):
        raise ValueError('SIMULATED_CONSUMER_CONTRACT_BUG')
    monkeypatch.setattr(terminal, 'validate_terminal_answer', broken)
    failed = run_terminal([unresolved()], reply, terminal_rig=terminal_rig)
    assert failed['findings'][0]['lifecycle']['state'] == 'CONTRACT_ERROR'
    original = deepcopy(failed['findings'][0]['exact_release_adjudication']['terminal_review'])
    monkeypatch.setattr(terminal, 'validate_terminal_answer', real)
    fixed = run_terminal(failed['findings'], reply, terminal_rig=terminal_rig)
    assert fixed['findings'] == [], fixed
    assert len(calls) == 1
    saved = fixed['unresolved_findings_disclosed'][0]['exact_release_adjudication']
    assert saved['terminal_review']['raw_completion'] == original['raw_completion']
    assert saved['terminal_review']['prompt'] == original['prompt']
    assert saved['terminal_review']['request'] == original['request']
    assert saved['terminal_answer_replay']['provider_calls'] == 0


def test_invalid_original_answer_stays_recoverable_and_is_not_reasked(monkeypatch, terminal_rig):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    calls = []
    def reply(prompt):
        calls.append(prompt)
        return json.dumps({**answer(), 'evidence_refs': ['invented-ref']}, ensure_ascii=False)
    first = run_terminal([unresolved()], reply, terminal_rig=terminal_rig)
    second = run_terminal(first['findings'], reply, terminal_rig=terminal_rig)
    assert len(calls) == 1
    assert second['reason_codes'] == [lifecycle.ENGINEERING_FAILURE]
    assert second['findings'][0]['lifecycle']['state'] == 'CONTRACT_ERROR'
    assert second['findings'][0]['exact_release_adjudication']['reason_code'] == 'TERMINAL_EVIDENCE_REFS_INVALID'


@pytest.mark.parametrize('prior_choice,prior_state', [('UNRESOLVABLE', 'TRUE_BLOCK'), ('CURRENT', 'KEEP')])
@pytest.mark.parametrize('group_size', [1, 2])
def test_completed_finding_reenters_with_new_bound_audio_before_keep(monkeypatch, terminal_rig,
                                                                  prior_choice, prior_state, group_size):
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    row = unresolved()
    if prior_choice == 'UNRESOLVABLE':
        row.update(suspect='喜欢她啊', suggestion='不喜欢她', proposed_full_cue='我不喜欢她')
    group = [{**deepcopy(row), 'why': row['why'] + f'（记录 {index}）'} for index in range(group_size)]
    first = run_terminal(group, lambda _: json.dumps(answer(prior_choice), ensure_ascii=False),
                         terminal_rig=terminal_rig)
    prior_rows = first['findings'] + first['unresolved_findings_disclosed']
    assert len(prior_rows) == group_size
    blocked = prior_rows[0]
    assert blocked['lifecycle']['state'] == prior_state
    original = deepcopy(blocked)
    check = blocked['exact_release_adjudication']['terminal_review']['request']['check_request']
    terminal_rig.transport.range_transcript = '我喜欢她啊'
    # The ordinary audio owner cuts a new narrower window, saves actual
    # synthetic-media/response bytes and returns its normal bound observation.
    ranges = terminal_rig.verifier.observe_range_context(check_request=check,
        range_witness_request=build_witness_request(check), provider='gemini')
    assert ranges
    from src.autoslice.exact_final_terminal_funnel import terminal_check
    from src.autoslice.targeted_relisten import evidence_projection
    next_check = terminal_check(SRT, blocked, clip_context=terminal_rig.context,
                                offset_ms=0, ranges=ranges)
    old_projection = evidence_projection(check, include_target_text=True)
    next_projection = evidence_projection(next_check, include_target_text=True)
    assert ({r['observation_id'] for r in next_projection['observations']}
            - {r['observation_id'] for r in old_projection['observations']}), {
                'rejected': next_projection['rejected_observations'], 'range_count': len(ranges)}
    calls = []
    def reply(prompt):
        calls.append(prompt)
        data = json.loads(prompt.rsplit('\n', 1)[1])
        assert data['current_support_observation_refs']
        return json.dumps({**answer(), 'grade': 'B', 'keep_route': 'B_EVIDENCE_SUPPORTS_CURRENT',
            'evidence_decisive': True, 'comparison_text': data['current'],
            'evidence_refs': data['current_support_observation_refs'], 'meaning_equivalence': None},
            ensure_ascii=False)
    resolved = run_terminal(prior_rows, reply, terminal_rig=terminal_rig, ranges=ranges)
    assert len(calls) == 1, [(r.get('terminal_funnel_error'), r['lifecycle'])
                             for r in resolved['findings']]
    assert blocked == original
    assert resolved['findings'] == [], [r['lifecycle']['detail'].get('reason_code')
                                        for r in resolved['findings']]
    assert len(resolved['unresolved_findings_disclosed']) == group_size
    from src.autoslice.context_assisted_audio import digest
    for before, after in zip(prior_rows, resolved['unresolved_findings_disclosed']):
        assert after['lifecycle']['state'] == 'KEEP'
        assert after['lifecycle_reentries'][-1]['previous_lifecycle'] == before['lifecycle']
        assert after['lifecycle_reentries'][-1]['previous_adjudication_sha256'] == digest(
            before['exact_release_adjudication'])
    kept = resolved['unresolved_findings_disclosed'][0]
    assert kept['lifecycle']['state'] == 'KEEP'
    assert kept['exact_release_adjudication']['stopped_adjudication'] == original['exact_release_adjudication']
    entry = kept['lifecycle_reentries'][-1]
    assert entry['previous_lifecycle'] == original['lifecycle']
    assert entry['material_changes'] == ['BOUND_OBSERVATION']
    assert set(entry['new_observation_ids']) == (
        {r['observation_id'] for r in next_projection['observations']}
        - {r['observation_id'] for r in old_projection['observations']})


def test_terminal_reentry_requires_real_new_material_and_preserves_same_answer(monkeypatch, terminal_rig):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    row = {**unresolved(), 'suspect': '喜欢她啊', 'suggestion': '不喜欢她',
           'proposed_full_cue': '我不喜欢她'}
    first = run_terminal([row], lambda _: json.dumps(answer('UNRESOLVABLE'), ensure_ascii=False),
                         terminal_rig=terminal_rig)
    blocked = first['findings'][0]
    request = blocked['exact_release_adjudication']['terminal_review']['request']
    with pytest.raises(ValueError, match='FINDING_REENTRY_NEW_MATERIAL_REQUIRED'):
        lifecycle.reenter_terminal_review(blocked, check_request=request['check_request'],
            no_gain_proof=request['audio_gain_termination'], clip_context=terminal_rig.context)
    changed_metadata = {**deepcopy(blocked), 'why': '改了说法，但没有新增原音或语境。'}
    def never_called(_):
        raise AssertionError('SAME_INPUT_MUST_REUSE_ORIGINAL_ANSWER')
    second = run_terminal([changed_metadata], never_called, terminal_rig=terminal_rig)
    result = second['findings'][0]
    assert result['lifecycle']['state'] == 'TRUE_BLOCK'
    assert 'lifecycle_reentries' not in result
    assert result['exact_release_adjudication']['terminal_review'] == blocked['exact_release_adjudication']['terminal_review']


def test_edit_has_no_release_exit_until_real_apply_and_new_review(monkeypatch, terminal_rig):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    from src.autoslice.editorial_subtitle import validate_repair
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    selected = {**answer('CONTEXT_FIT'), 'status': 'RESOLVED', 'mode': 'faithful_paraphrase',
        'edit_start': 0, 'edit_end': 5, 'replacement': '我喜欢她', 'no_new_facts': True,
        'meaning_basis': '保留喜欢她的核心意思', 'literal_uncertainty': '尾音无法确定',
        'reliable_fragments': [{'text': '我喜欢她', 'basis': '两句语境一致'}],
        'evidence_refs': ['audio', 'cue:2']}
    result = run_terminal([unresolved()], lambda p: json.dumps(selected, ensure_ascii=False),
                          terminal_rig=terminal_rig)
    assert result['findings'][0]['lifecycle']['state'] == 'EDIT_REVIEW'
    assert result['reason_codes'] == [lifecycle.EDIT_REQUIRED]
    changed, repairs = _apply_exact_final_cpa_repairs(SRT, result, clip_context=terminal_rig.context)
    assert changed != SRT and len(repairs) == 1
    validate_repair(repairs[0])
    assert result['release_gate'] == 'BLOCK'  # Applying a receipt cannot reuse the pre-edit review.
    assert lifecycle.transition('EDIT_REVIEW', 'AWAITING_DECISION') == lifecycle.State.AWAITING_DECISION


def test_window_narrowing_real_input_is_admitted_and_exact_repeat_rejected(rig):
    from tests import test_targeted_relisten_public_contract as protocol
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    witness = protocol.acquire_narrow(rig)
    finding = protocol.pending_finding(rig, rig.check, witness)
    output, first, _ = protocol.acquire_range(rig, finding, rig.check, protocol.observe_plan(1, 3))
    check = build_context_adjudication_request(rig.srt, output, clip_context=rig.context)
    finding = protocol.pending_finding(rig, check, witness)
    finding['exact_release_adjudication']['semantic_audio_recovery'] = first
    output, narrowed, _ = protocol.acquire_range(rig, finding, check,
                                                protocol.observe_plan(1, 2, purpose='WORD_BOUNDARY'))
    assert narrowed['status'] == 'OBSERVED', narrowed
    assert narrowed['admission']['input_information']['source_window_ms'] == [500, 4000]
    check = build_context_adjudication_request(rig.srt, output, clip_context=rig.context)
    finding = protocol.pending_finding(rig, check, witness)
    finding['exact_release_adjudication']['semantic_audio_recovery'] = narrowed
    count = len(rig.transport.calls)
    _, duplicate, _ = protocol.acquire_range(rig, finding, check,
        protocol.observe_plan(1, 2, reason='仅改了一句解释'))
    assert duplicate['reason_code'] == 'SEMANTIC_CONTINUATION_MATERIAL_EXHAUSTED'
    assert duplicate['no_gain_proof']['plan_exhaustion']['reason_code'] == 'RELISTEN_REPEATED_INPUT_NO_INFORMATION_GAIN'
    assert len(rig.transport.calls) == count


def test_changed_actual_model_is_new_input_but_a_plan_label_is_not(rig):
    from tests import test_targeted_relisten_public_contract as protocol
    from src.autoslice.native_context_witness import build_independent_context_witnesses
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    witness = protocol.acquire_narrow(rig)
    finding = protocol.pending_finding(rig, rig.check, witness)
    output, first, _ = protocol.acquire_range(rig, finding, rig.check, protocol.observe_plan(1, 2))
    check = build_context_adjudication_request(rig.srt, output, clip_context=rig.context)
    finding = protocol.pending_finding(rig, check, witness)
    finding['exact_release_adjudication']['semantic_audio_recovery'] = first
    before = len(rig.transport.calls)
    _, duplicate, _ = protocol.acquire_range(rig, finding, check,
        protocol.observe_plan(1, 2, model='invented-model-label'))
    assert duplicate['reason_code'] == 'RELISTEN_REPEATED_INPUT_NO_INFORMATION_GAIN'
    assert len(rig.transport.calls) == before
    from dataclasses import replace
    rig.local = replace(rig.local, model='different-offline-protocol-model')
    rig.verifier = build_independent_context_witnesses(fallback=rig.local, source_media=rig.source,
        output_dir=rig.output, providers=())
    _, actual, _ = protocol.acquire_range(rig, finding, check, protocol.observe_plan(1, 2))
    assert actual['status'] == 'OBSERVED', actual
    assert actual['admission']['input_information']['listening_model'] == rig.local.model
    assert actual['observation_projection_after']['effective_distinct_observations'] == 3


def test_substantive_support_and_sound_spelling_do_not_weaken_lexical_guards():
    from src.autoslice.semantic_terminal_adjudication import current_support_refs
    from src.autoslice.targeted_relisten import protect_clear_transcript
    request = {'current_cue': '你喜欢，嘿嘿嘿', 'check_request': {},
        'audio_gain_termination': {'projection': {'observations': [{
            'observation_id': 'actual', 'receipt_kind': 'native_transcript',
            'raw_observation': {'status': 'OBSERVED', 'transcript': '你喜欢'}}]}}}
    assert current_support_refs(request) == ['actual']
    request['current_cue'] = '你喜欢老男人，嘿嘿嘿'
    assert current_support_refs(request) == []
    request['current_cue'] = '哈哈镜'
    request['audio_gain_termination']['projection']['observations'][0]['raw_observation']['transcript'] = '镜'
    assert current_support_refs(request) == []
    with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
        protect_clear_transcript('哈哈镜', '镜')
    protect_clear_transcript('어, 아, 아', '呃，啊，啊')
    protect_clear_transcript('어, 아, 아', '')
    protect_clear_transcript('어, 不要走', '不要走')
    for original, edited in [('아이', '啊'), ('아니', '啊'), ('좋아요', '啊'), ('不要走', '要走')]:
        with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
            protect_clear_transcript(original, edited)


def test_no_unique_reading_and_pure_sounds_are_not_d():
    from src.autoslice.meaning_difference import classify_difference
    from src.autoslice.semantic_terminal_adjudication import written_meaning_conflict
    from src.autoslice.subtitle_acceptability_policy import load_policy
    policy = load_policy()
    for current, alternate in [('咳咳', '客客'), ('咋啦', '杂啦'), ('我知道了', '我知道了呀')]:
        diff = classify_difference(current, alternate, policy=policy, glossary_terms=[])
        assert not written_meaning_conflict(current, diff, policy)
    diff = classify_difference('我喜欢她', '我不喜欢她', policy=policy, glossary_terms=[])
    assert written_meaning_conflict('我喜欢她', diff, policy)


def test_mixed_language_nomination_preserves_source_and_no_speech_guards():
    from src.autoslice.closed_set_proposal_rebuild import topic_proposal_error
    payload = {'proposal_basis': 'topic_homophone', 'topic_support': ['日语感叹']}
    kwargs = {'current': '提自己', 'proposed': 'きつい',
              'evidence': {'evidence_level': 'CURRENT_TEXT_PINYIN_WEAK'}, 'topic_context': '日语感叹'}
    assert topic_proposal_error(payload, **kwargs) is None
    assert topic_proposal_error({**payload, 'topic_support': ['无来源']}, **kwargs) == 'TOPIC_PROPOSAL_SUPPORT_MISSING'
    assert topic_proposal_error(payload, **{**kwargs, 'evidence': {'evidence_level': 'NO_SPEECH_OBSERVED'}}) == 'TOPIC_PROPOSAL_NO_SPEECH'
    assert topic_proposal_error(payload, **{**kwargs, 'proposed': '买东西'}) == 'TOPIC_PROPOSAL_PHONEME_MISMATCH'


def test_gauntlet_already_recalls_bare_and_candidate_names(tmp_path):
    from scripts.predeploy_contract_gauntlet import source_files
    root = tmp_path / 'run'; root.mkdir()
    for name in ['review-flags.json', 'candidate.review-flags.json']:
        (root / name).write_text('{}')
    assert len(source_files(tmp_path)) == 2


def test_talk_failure_owner_can_resume_engineering_but_keeps_content_block(monkeypatch):
    from src.autoslice import talk_lane
    evidence = {'audit_loaded': True, 'audit_surfaces_consistent': True,
        'discovery': {'status': 'COMPLETE'}, 'boundary_semantic_review': {'status': 'PASS'},
        'correction_mutation_authority': {'status': 'PASS', 'failures': []},
        'correction_pass': {'status': 'COMPLETE'}, 'validated_finding_count': 1}
    for code,stage,recoverable in [(lifecycle.ENGINEERING_FAILURE,'final_review_engineering',True),
                                  (lifecycle.CONTENT_BLOCK,'final_review_content_block',False)]:
        monkeypatch.setattr(talk_lane, '_final_review_failure_evidence',
            lambda _: {**evidence, 'contract_reason_code': code, 'reason_codes': [code]})
        result = talk_lane._classify_final_review_release('synthetic failure envelope')
        assert result[1:3] == (stage,recoverable)
        if recoverable:
            assert result[3]['new_model_call_authorized'] is False
