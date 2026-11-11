import copy
import hashlib

import pytest

from src.autoslice import chat_authority_reconciliation as reconcile
from src.autoslice import semantic_terminal_adjudication as terminal
from src.autoslice.read_aloud_llm_verifier import build_cpa_read_aloud_verifier
from src.autoslice.review_errors import ContractRejection
from src.autoslice.chat_evidence import ChatEvidence
from tests.test_chat_authority_conflict_handoff import _chat_authority, _finding, SRT, CURRENT, OFFSET


def test_owner_branch_preserves_malformed_answer_and_never_grants_mutation(monkeypatch):
    monkeypatch.setattr(reconcile, 'validate_owner_request', lambda request: None)
    monkeypatch.setattr(terminal, 'terminal_prompt', lambda request: 'owner prompt')
    monkeypatch.setattr(terminal, 'request_terminal_answer', lambda call, prompt, request, **kw: ('{"choice":"invented"}', False))
    def reject(*args, **kwargs):
        raise ContractRejection('TERMINAL_CHOICE_CONTRACT_INVALID')
    monkeypatch.setattr(terminal, 'validate_terminal_answer', reject)
    calls = []
    verifier = build_cpa_read_aloud_verifier(lambda prompt: calls.append(prompt))
    result = verifier({'schema_version': 'chat-source-owner-reevaluation-request.v1',
                       'terminal_request': {}})
    assert result['status'] == 'UNCERTAIN'
    assert result['reason_code'] == 'TERMINAL_CHOICE_CONTRACT_INVALID'
    assert result['raw_completion'] == '{"choice":"invented"}'
    assert result['mutation_authorized'] is False
    assert calls == []


def test_owner_branch_keeps_unparseable_raw_response(monkeypatch):
    monkeypatch.setattr(reconcile, 'validate_owner_request', lambda request: None)
    monkeypatch.setattr(terminal, 'terminal_prompt', lambda request: 'owner prompt')
    monkeypatch.setattr(terminal, 'request_terminal_answer', lambda call, prompt, request, **kw: ('not-json', False))
    result = build_cpa_read_aloud_verifier(lambda prompt: None)(
        {'schema_version': 'chat-source-owner-reevaluation-request.v1', 'terminal_request': {}})
    assert result['status'] == 'UNCERTAIN' and result['raw_completion'] == 'not-json'
    assert result['mutation_authorized'] is False


def test_unbound_or_ambiguous_source_owner_claim_never_retires_chat():
    row = {'reconciliation': {'schema_version': 'chat-source-owner-exact-read-supersession.v1'}}
    owner = {'editorial_authority': {'source_owner_reconciliation': True}}
    reg = {'status': 'REGISTERED', 'owner_entity_repair_index': 0,
           'superseded_applied_indexes': [0]}
    audit = {'applied': [row], 'entity_repairs': [owner],
             'exact_final_cpa_surface_registrations': [reg]}
    srt = '1\n00:00:01,000 --> 00:00:02,000\n文字\n'
    assert not reconcile.validated_retired_chat_rows(audit, current_srt_text=srt, delivery_start_ms=0)
    audit['exact_final_cpa_surface_registrations'].append(copy.deepcopy(reg))
    assert not reconcile.validated_retired_chat_rows(audit, current_srt_text=srt, delivery_start_ms=0)


def test_final_surface_rejects_forged_source_retirement():
    from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces
    audit = {'applied': [{'reconciliation': {
        'schema_version': 'chat-source-owner-exact-read-supersession.v1'}}],
        'entity_repairs': [], 'exact_final_cpa_surface_registrations': []}
    assert not verify_chat_authority_final_surfaces(audit, final_text_srt=SRT,
        final_speaker_srt=SRT, delivery_start_ms=OFFSET, delivery_end_ms=12000)
    assert audit['final_verification_failure'] == 'CHAT_SOURCE_OWNER_RECONCILIATION_INVALID'


def test_source_event_reopens_original_recording_clock_and_rejects_drift(tmp_path, monkeypatch):
    source = tmp_path / 'chat.xml'
    source.write_text('<i><d p="10.042,1">你喜欢老男人</d></i>')
    row = _chat_authority()['applied'][0]
    row['source'] = str(source)
    row['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    row['evidence_id'] = ChatEvidence('danmaku', 42, CURRENT).evidence_id
    authority = {'applied': [row]}
    proof = {'projection': {'observations': [{'media_source_projection': {
        'media_source_binding': {'recording_start_ms': 10000}}}]}}
    monkeypatch.setattr(reconcile, 'validate_current_no_gain', lambda proof, check: proof)
    monkeypatch.setattr(reconcile, 'terminal_prompt_matches', lambda request, prompt: True)
    sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
    request = {'srt_sha256': sha(SRT), 'current_cue': CURRENT,
               'check_request': {'current_cue': CURRENT, 'cue_indexes': [1],
                                 'matched_start_ms': 1000, 'matched_end_ms': 2000,
                                 'source_media_timeline_offset_ms': OFFSET},
               'audio_gain_termination': proof}
    review = {'request': request, 'prompt': 'original', 'prompt_sha256': sha('original'),
              'raw_completion': '{}', 'completion_sha256': sha('{}')}
    finding = {'cue_index': 1, 'exact_release_adjudication': {'terminal_review': review}}
    evidence = reconcile.prepare_conflict_evidence(SRT, finding, authority, delivery_start_ms=OFFSET)
    assert evidence['prior_chat_authority']['applied_evidence_id'] == row['evidence_id']
    assert evidence['source_recording_start_ms'] == 10000
    request['check_request']['matched_end_ms'] += 1
    with pytest.raises(ContractRejection, match='CHAT_RECONCILIATION_TERMINAL_BINDING_DRIFT'):
        reconcile.prepare_conflict_evidence(SRT, finding, authority, delivery_start_ms=OFFSET)
    request['check_request']['matched_end_ms'] -= 1
    request['current_cue'] = request['check_request']['current_cue'] = 'different'
    with pytest.raises(ContractRejection, match='CHAT_RECONCILIATION_TERMINAL_BINDING_DRIFT'):
        reconcile.prepare_conflict_evidence(SRT, finding, authority, delivery_start_ms=OFFSET)
    request['current_cue'] = request['check_request']['current_cue'] = CURRENT
    source.write_text('<i><d p="10.042,1">different</d></i>')
    with pytest.raises(ContractRejection, match='CHAT_RECONCILIATION_SOURCE_DRIFT'):
        reconcile.prepare_conflict_evidence(SRT, finding, authority, delivery_start_ms=OFFSET)
    source.write_text('<i><d p="10.042,1">你喜欢老男人</d><d p="10.042,1">你喜欢老男人</d></i>')
    row['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ContractRejection, match='CHAT_RECONCILIATION_SOURCE_EVENT_INVALID'):
        reconcile.prepare_conflict_evidence(SRT, finding, authority, delivery_start_ms=OFFSET)
    proof['projection']['observations'].append({'media_source_projection': {
        'media_source_binding': {'recording_start_ms': 9000}}})
    with pytest.raises(ContractRejection, match='CHAT_RECONCILIATION_SOURCE_CLOCK_AMBIGUOUS'):
        reconcile.prepare_conflict_evidence(SRT, finding, authority, delivery_start_ms=OFFSET)


@pytest.mark.parametrize('human_protected', [None, frozenset({1})])
def test_source_owner_never_bypasses_unknown_or_present_human_protection(monkeypatch, human_protected):
    from src.autoslice import producer_package_finalization as producer
    monkeypatch.setattr(producer, 'adjudicated_proposed_full_cue', lambda *a, **kw: '不同文字')
    authority = {'applied': []}
    receipt = {'source_owner_reconciliation': True,
               'request': {'chat_source_conflict': {'original_applied': []}}}
    finding = {'cue_index': 1, 'exact_release_adjudication': {'editorial_authority': receipt}}
    after, repairs = producer._apply_exact_final_cpa_repairs(
        SRT, {'findings': [finding]}, protected_cue_indexes=frozenset({1}),
        human_protected_cue_indexes=human_protected, chat_authority_audit=authority)
    assert after == SRT and repairs == []
    assert finding['human_literal_proposal_veto']['mutation_authorized'] is False


def test_source_owner_uses_same_budget_and_does_not_call_when_disabled(monkeypatch):
    from src.autoslice.final_review_auditor import _source_owner_budget_adapter
    from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget
    from src.autoslice import final_review_provider_budget
    from src.autoslice import read_aloud_llm_verifier
    calls = []
    monkeypatch.setattr(final_review_provider_budget, 'replay_cached_context_adjudication', lambda *a, **kw: None)
    monkeypatch.setattr(read_aloud_llm_verifier, 'build_cpa_read_aloud_verifier',
                        lambda call: lambda req: calls.append(call) or {'status': 'VALIDATED'})
    def make_budget(allowed):
        return ContextAdjudicationBudget(limit=5, entity_verifier=None, clip_context={},
            source_media_timeline_offset_ms=0, judge_llm_call=lambda prompt: '{}',
            screen_read_probe=None, adjudicate_context=lambda *args, **kwargs: None,
            provider_calls_allowed=allowed)
    budget = make_budget(True)
    adapter = _source_owner_budget_adapter(budget)
    first = adapter(SRT, {'cue_index': 1}, {'id': 1})
    assert first['status'] == 'VALIDATED' and budget.provider_adjudication_count == 1
    second = adapter(SRT, {'cue_index': 1}, {'id': 1})
    assert second['same_input_replay']['status'] == 'PASS'
    assert budget.provider_adjudication_count == 1 and len(calls) == 1
    adapter(SRT, {'cue_index': 1}, {'id': 2})
    assert budget.provider_adjudication_count == 2 and len(calls) == 2
    disabled = make_budget(False)
    result = _source_owner_budget_adapter(disabled)(SRT, {'cue_index': 1}, {'id': 1})
    assert result['status'] == 'SKIPPED_PROVIDER_DISABLED'
    assert disabled.provider_adjudication_count == 0 and len(calls) == 2


def test_budget_cache_only_reopens_bound_source_owner_without_network(monkeypatch):
    from src.autoslice.final_review_provider_budget import replay_cached_context_adjudication
    from src.autoslice.context_assisted_audio import digest
    from src.autoslice import read_aloud_llm_verifier
    request = {'chat_source_conflict': {'original_applied': []}}
    finding = {'chat_source_owner_request': request, 'chat_source_owner_request_sha256': digest(request)}
    validated = []
    monkeypatch.setattr(reconcile, 'owner_answer_to_adjudication',
                        lambda answer, **kwargs: validated.append(kwargs))
    def cached(call):
        return lambda req: {'status': 'VALIDATED', 'checkpoint_hit': True,
            'terminal_request': req['terminal_request'], 'prompt_sha256': 'p', 'completion_sha256': 'c'}
    monkeypatch.setattr(read_aloud_llm_verifier, 'build_cpa_read_aloud_verifier', cached)
    result = replay_cached_context_adjudication(SRT, finding, entity_verifier=None,
        clip_context={}, source_media_timeline_offset_ms=0, adjudicate_context=lambda *a, **kw: None)
    assert result['provider_budget_replay']['provider_call_count'] == 0 and len(validated) == 1
    class ArgonEnabled:
        argon_escalation = object()
    result = replay_cached_context_adjudication(SRT, finding, entity_verifier=ArgonEnabled(),
        clip_context={}, source_media_timeline_offset_ms=0, adjudicate_context=lambda *a, **kw: None)
    assert result['provider_budget_replay']['provider_call_count'] == 0
    finding['chat_source_owner_request_sha256'] = 'wrong'
    assert replay_cached_context_adjudication(SRT, finding, entity_verifier=None,
        clip_context={}, source_media_timeline_offset_ms=0, adjudicate_context=lambda *a, **kw: None) is None
    finding['chat_source_owner_request_sha256'] = digest(request)
    monkeypatch.setattr(read_aloud_llm_verifier, 'build_cpa_read_aloud_verifier',
                        lambda call: lambda req: call('never send'))
    assert replay_cached_context_adjudication(SRT, finding, entity_verifier=None,
        clip_context={}, source_media_timeline_offset_ms=0, adjudicate_context=lambda *a, **kw: None) is None


def test_actual_owner_checkpoint_reopens_with_no_network_sentinel(tmp_path, monkeypatch):
    from src.autoslice import response_schemas
    from src.autoslice.context_assisted_audio import digest
    from src.autoslice.final_review_provider_budget import replay_cached_context_adjudication
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setattr(reconcile, 'validate_owner_request', lambda request: None)
    monkeypatch.setattr(reconcile, 'owner_answer_to_adjudication', lambda answer, **kw: {})
    monkeypatch.setattr(terminal, 'terminal_prompt', lambda request: 'bound owner prompt')
    monkeypatch.setattr(terminal, 'terminal_finding_materials', lambda *a, **kw: None)
    monkeypatch.setattr(terminal, 'validate_terminal_answer', lambda *a, **kw: 'CURRENT')
    monkeypatch.setattr(response_schemas, 'terminal_schema', lambda request: {})
    calls = []
    def transport(prompt):
        calls.append(prompt)
        return '{"choice":"CURRENT"}'
    transport.cpa_cache_identity = {'transport': 'recorded', 'models': ['owner'], 'effort': 'medium'}
    transport.completed_review_root = tmp_path / 'completed'
    request = {'chat_source_conflict': {'original_applied': []}}
    first = build_cpa_read_aloud_verifier(transport)(
        {'schema_version': 'chat-source-owner-reevaluation-request.v1', 'terminal_request': request})
    assert first['status'] == 'VALIDATED' and first['checkpoint_hit'] is False and len(calls) == 1
    finding = {'chat_source_owner_request': request, 'chat_source_owner_request_sha256': digest(request)}
    replay = replay_cached_context_adjudication(SRT, finding, entity_verifier=None,
        clip_context={}, source_media_timeline_offset_ms=0,
        adjudicate_context=lambda *a, **kw: None, judge_llm_call=transport)
    assert replay['checkpoint_hit'] is True
    assert replay['provider_budget_replay']['provider_call_count'] == 0
    assert len(calls) == 1


def test_terminal_funnel_hands_conflict_to_budget_owner_and_reuses_saved_answer(monkeypatch):
    from src.autoslice import editorial_subtitle
    from src.autoslice.exact_final_terminal_funnel import review_pass_receipt, terminalize_review
    original = _finding()
    original['exact_release_adjudication'] = {'status': 'UNCERTAIN', 'repaired': False}
    audit = {'findings': [original], 'reason_codes': ['FINAL_REVIEW_UNRESOLVED_FINDINGS'],
             'status': 'FLAGGED', 'release_gate': 'BLOCK'}
    receipt = review_pass_receipt(pass_index=1, srt_text=SRT, audit=audit,
                                  repaired_text=SRT, repairs=[])
    generic = {'status': 'UNCERTAIN', 'repaired': False,
               'reason_code': 'CHAT_AUTHORITY_CONFLICT_MUTATION_BLOCKED',
               'terminal_review': {'request': {}}}
    monkeypatch.setattr(editorial_subtitle, 'replay_terminal_adjudication', lambda *a, **kw: copy.deepcopy(generic))
    monkeypatch.setattr(reconcile, 'build_owner_request', lambda *a, **kw: {'bound': True})
    monkeypatch.setattr(reconcile, 'owner_answer_to_adjudication', lambda *a, **kw: {
        'status': 'UNCERTAIN', 'repaired': False, 'reason_code': 'OWNER_UNRESOLVED'})
    calls = []
    def owner(*args):
        calls.append(args)
        return {'status': 'VALIDATED', 'raw_completion': 'owner-answer'}
    adapters = {'clip_context': {}, 'source_media_timeline_offset_ms': OFFSET,
                'chat_authority_audit': _chat_authority(), 'source_owner': owner}
    first = terminalize_review(SRT, audit, pass_receipts=[receipt], adapters=adapters)
    assert len(calls) == 1
    saved = first['findings'][0]['exact_release_adjudication']['source_owner_answer']
    assert saved['raw_completion'] == 'owner-answer'
    second_audit = {**audit, 'findings': first['findings']}
    second = terminalize_review(SRT, second_audit, pass_receipts=[receipt], adapters=adapters)
    assert len(calls) == 1
    assert second['findings'][0]['exact_release_adjudication']['source_owner_answer'] == saved


def test_real_typed_successor_chain_and_wrong_order_are_checked(tmp_path, monkeypatch):
    from src.autoslice.canonical_json_hash import canonical_sha256
    from tests.test_editorial_subtitle import CONTEXT, SRT as FIRST_SRT, TEXT, answer, run
    context = {**CONTEXT, 'candidate_id': 'owner-successor-test'}
    first_dir = tmp_path / 'first'
    second_dir = tmp_path / 'second'
    first_dir.mkdir()
    second_dir.mkdir()
    intermediate, first_repairs, _, _ = run(first_dir, monkeypatch, context=context)
    assert len(first_repairs) == 1
    first = first_repairs[0]
    current = first['after']
    second_answer = answer(edit_start=len(TEXT)-2, edit_end=len(current), replacement='小花花',
        event_index=0, evidence_refs=['audio', 'event:0'], referent_basis='该次称呼与第一条弹幕对应',
        alternatives_considered='另一条称呼不对应此处')
    final, second_repairs, second_rows, _ = run(second_dir, monkeypatch, context=context,
                                                text=intermediate, response=second_answer)
    assert len(second_repairs) == 1, [(r.get('cue_index'),r.get('exact_release_adjudication',{}).get('reason_code')) for r in second_rows]
    second = second_repairs[0]
    def sha(text):
        return 'sha256:' + hashlib.sha256(text.encode()).hexdigest()
    def pass_row(index, before, after, repairs):
        return {'schema_version': 'exact-final-cpa-self-heal-pass.v1', 'pass_index': index,
                'input_srt_sha256': sha(before), 'output_srt_sha256': sha(after), 'repairs': repairs}
    authority = {'exact_final_cpa_self_heal': {'schema_version': 'exact-final-cpa-self-heal-audit.v1',
        'status': 'REVIEW_PENDING', 'passes': [pass_row(1, FIRST_SRT, intermediate, [first]),
                                                pass_row(2, intermediate, final, [second])]}}
    repair_sha = 'sha256:' + canonical_sha256(first, allow_nan=True)
    args = dict(receipt=first['editorial_authority'], repair_sha=repair_sha,
                expected_text=first['after'], current_srt_text=final,
                delivery_start_ms=5000, physical_window=(6000, 7000))
    assert reconcile._successor_preserves_owner(authority, **args)
    from src.autoslice.exact_final_cpa_history import build_run_audit, retained_runs
    previous_run = {'schema_version': 'exact-final-cpa-self-heal-audit.v1', 'status': 'PASS',
                    'passes': [pass_row(1, FIRST_SRT, intermediate, [first])],
                    'final_srt_sha256': sha(intermediate)}
    across_runs = {'exact_final_cpa_self_heal': build_run_audit(
        [pass_row(1, intermediate, final, [second])], retained_runs(previous_run))}
    assert reconcile._successor_preserves_owner(across_runs, **args)
    unlinked = copy.deepcopy(across_runs)
    unlinked['exact_final_cpa_self_heal']['passes'][0]['input_srt_sha256'] = 'sha256:' + 'e' * 64
    assert not reconcile._successor_preserves_owner(unlinked, **args)

    # Reuse the two real ordinary typed repairs to exercise the public owner
    # consumer. The first source receipt is stubbed only to isolate the chain;
    # its real XML/owner validation is covered by the separate Xiada replay.
    from src.autoslice import editorial_subtitle
    from src.autoslice.context_assisted_audio import digest
    from src.autoslice.chat_authority_ownership import chat_authority_owned_cue_indexes
    real_validate = editorial_subtitle.validate_authority
    old_row = {'mode': 'exact_span', 'matched_start_ms': 6000, 'matched_end_ms': 7000,
               'exact_text': first['before']}
    owner_first = copy.deepcopy(first)
    owner_receipt = owner_first['editorial_authority']
    owner_receipt['source_owner_reconciliation'] = True
    owner_receipt['request']['chat_source_conflict'] = {'original_applied': [copy.deepcopy(old_row)],
        'evidence': {'prior_chat_authority': {'applied_index': 0, 'delivery_start_ms': 5000}}}
    owner_first['judge']['authority_sha256'] = digest(owner_receipt)
    owner_sha = 'sha256:' + canonical_sha256(owner_first, allow_nan=True)
    claim = {'schema_version': 'chat-source-owner-exact-read-supersession.v1',
             'status': 'SUPERSEDED_BY_EXACT_FINAL_CPA', 'timing_immutable': True,
             'exact_final_repair_sha256': owner_sha, 'before_sha256': owner_first['before_sha256'],
             'after_sha256': owner_first['after_sha256']}
    owner_row = {'mode': 'exact_final_cpa_self_heal', 'decision_authority': 'CPA_JUDGE',
                 'timing_immutable': True, 'survived': True, 'mutation_authority': {'status': 'PASS'},
                 'superseded_applied_indexes': [0], 'exact_final_repair_sha256': owner_sha,
                 'before': [owner_first['before']], 'after': [owner_first['after']],
                 'matched_start_ms': 6000, 'matched_end_ms': 7000, 'editorial_authority': owner_receipt}
    registration = {'schema_version': 'exact-final-cpa-surface-registration.v1', 'status': 'REGISTERED',
                    'owner_entity_repair_index': 0, 'superseded_applied_indexes': [0],
                    'exact_final_repair_sha256': owner_sha}
    owned = {'applied': [{**old_row, 'reconciliation': claim}], 'entity_repairs': [owner_row],
             'exact_final_cpa_surface_registrations': [registration],
             'exact_final_cpa_self_heal': {'schema_version': 'exact-final-cpa-self-heal-audit.v1',
                 'status': 'REVIEW_PENDING', 'passes': [pass_row(1, FIRST_SRT, intermediate, [owner_first]),
                                                       pass_row(2, intermediate, final, [second])]}}
    def validate_first_only(value, **kwargs):
        if value.get('source_owner_reconciliation') is True:
            value = copy.deepcopy(value)
            value.pop('source_owner_reconciliation')
            value['request'].pop('chat_source_conflict')
        return real_validate(value, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(editorial_subtitle, 'validate_authority', validate_first_only)
        assert reconcile.validated_retired_chat_rows(owned, current_srt_text=final, delivery_start_ms=5000) == frozenset({0})
        assert not chat_authority_owned_cue_indexes(final, owned, delivery_start_ms=5000)
        bad = copy.deepcopy(owned)
        bad['exact_final_cpa_self_heal']['passes'][1]['repairs'][0]['before'] = 'wrong'
        assert not reconcile.validated_retired_chat_rows(bad, current_srt_text=final, delivery_start_ms=5000)
    wrong = copy.deepcopy(authority)
    wrong['exact_final_cpa_self_heal']['passes'][1]['repairs'][0]['before'] = '错误前像'
    with pytest.raises(ValueError):
        reconcile._successor_preserves_owner(wrong, **args)
    # Isolate the physical-source check after the canonical validator: a
    # source change must not be accepted merely because the text chain fits.
    with monkeypatch.context() as m:
        m.setattr(reconcile, '_validate_successor_chain', lambda *a, **kw: None)
        foreign = copy.deepcopy(authority)
        check = foreign['exact_final_cpa_self_heal']['passes'][1]['repairs'][0]['editorial_authority']['request']['check_request']
        check['clip_context_binding']['candidate_id'] = 'foreign-candidate'
        assert not reconcile._successor_preserves_owner(foreign, **args)
        ambiguous = copy.deepcopy(authority)
        second_pass = ambiguous['exact_final_cpa_self_heal']['passes'][1]
        second_pass['repairs'].append(copy.deepcopy(second_pass['repairs'][0]))
        assert not reconcile._successor_preserves_owner(ambiguous, **args)


def test_successor_validation_memo_rechecks_source_changes(tmp_path, monkeypatch):
    from src.autoslice import final_review_contract
    from src.autoslice.continuation_subchecks import cold_read_session
    from src.autoslice.validation_memo import note_hashed_file
    source = tmp_path / 'source.xml'
    source.write_text('source-one')
    calls = []
    def validate(authority, *, expected_srt_sha256, allow_pending):
        calls.append(expected_srt_sha256)
        note_hashed_file(source, hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(final_review_contract, '_validate_exact_final_cpa_self_heal', validate)
    authority = {'exact_final_cpa_self_heal': {'passes': []}}
    with cold_read_session():
        reconcile._validate_successor_chain(authority, SRT)
        reconcile._validate_successor_chain(authority, SRT)
        assert len(calls) == 1
        source.write_text('source-two')
        reconcile._validate_successor_chain(authority, SRT)
        assert len(calls) == 2


def test_retiring_one_source_row_does_not_unprotect_a_different_cue(monkeypatch):
    from src.autoslice.chat_authority_ownership import chat_authority_owned_cue_indexes
    srt = ('1\n00:00:01,000 --> 00:00:02,000\n甲\n\n'
           '2\n00:00:03,000 --> 00:00:04,000\n乙\n')
    audit = {'applied': [{'mode': 'exact_span', 'matched_start_ms': 1000, 'matched_end_ms': 2000},
                         {'mode': 'exact_span', 'matched_start_ms': 3000, 'matched_end_ms': 4000}]}
    monkeypatch.setattr(reconcile, 'validated_retired_chat_rows', lambda *a, **kw: frozenset({0}))
    assert chat_authority_owned_cue_indexes(srt, audit, delivery_start_ms=0) == frozenset({2})
