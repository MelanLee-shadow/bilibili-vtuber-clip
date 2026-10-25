from pathlib import Path
import gzip
import json

import pytest
from tests import test_argon_consumer_integration as argon_consumer
from tests import test_targeted_relisten_public_contract as relisten_protocol
from tests import test_argon_targeted_relisten as argon_protocol

scene = argon_consumer.scene
rig = relisten_protocol.rig

FIXTURE = Path(__file__).parent / 'fixtures/exact_final_funnel/sister.json.gz'








SRT = '1\n00:00:01,000 --> 00:00:02,000\n我喜欢她啊\n\n2\n00:00:02,000 --> 00:00:03,000\n我喜欢她\n'
CONTEXT = {'candidate_id': 'offline', 'source_media_sha256': 'a' * 64,
    'pieces': [{'start_ms': 0, 'end_ms': 3000, 'source_media_sha256': 'a' * 64}]}


@pytest.fixture
def terminal_rig(tmp_path, monkeypatch):
    """The real offline relisten owner, bound to this funnel's two-cue SRT."""
    return relisten_protocol.build_offline_rig(
        tmp_path, monkeypatch, srt=SRT, finding={
            'cue_index': 1, 'suspect': '啊', 'suggestion': '呀',
            'proposed_full_cue': '我喜欢她呀', 'repair_class': 'disclosure_only',
            'why': '尾音不能确定，否定与指代在上下文中一致',
        }, source_duration_ms=4000)


def unresolved(reason='JUDGE_REQUESTS_MORE_AUDIO'):
    return {'cue_index': 1, 'suspect': '啊', 'suggestion': '', 'repair_class': 'disclosure_only',
        'why': '尾音不能确定，否定与指代在上下文中一致',
        'exact_release_adjudication': {'status': 'UNCERTAIN', 'repaired': False,
            'reason_code': reason}}


def answer(choice='CURRENT'):
    result = {'choice': choice, 'grade': 'C', 'keep_route': 'C_MEANING_EQUIVALENT' if choice == 'CURRENT' else None,
        'reason': '上下文支持现文，尾音不改变意思',
        'evidence_refs': ['cue:1', 'cue:2'], 'comparison_text': '我喜欢她',
        'meaning_equivalence': {'status': 'EQUIVALENT', 'current_meaning': '我喜欢她',
            'alternative_meaning': '我喜欢她', 'shared_meaning': '表达喜欢她',
            'uncertain_detail': '尾音', 'context_basis': '后句重述喜欢她', 'evidence_cue_ids': [2]}}
    if choice == 'UNRESOLVABLE':
        result.update(grade='D', reason='否定有分歧，语境定不下', comparison_text='我不喜欢她',
            evidence_decisive=False, context_can_decide=False, written_meaning_error='可能把不喜欢写成喜欢')
    return result


def run_terminal(rows, call, *, terminal_rig=None, srt_text=SRT, ranges=()):
    from src.autoslice.exact_final_terminal_funnel import review_pass_receipt, terminalize_review
    audit = {'findings': rows, 'validated_finding_count': len(rows),
        'reason_codes': ['FINAL_REVIEW_UNRESOLVED_FINDINGS'], 'status': 'FLAGGED', 'release_gate': 'BLOCK'}
    receipt = review_pass_receipt(pass_index=1, srt_text=srt_text, audit=audit, repaired_text=srt_text, repairs=[])
    adapters = {'clip_context': CONTEXT, 'judge_llm_call': call,
                'prior_range_acoustic_observations': ranges}
    if terminal_rig is not None:
        adapters.update(entity_verifier=terminal_rig.verifier,
                        clip_context=terminal_rig.context,
                        source_media_timeline_offset_ms=terminal_rig.offset_ms)
    return terminalize_review(srt_text, audit, pass_receipts=[receipt], adapters=adapters)


def test_no_modification_routes_every_unresolved_through_real_terminal(monkeypatch, terminal_rig):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    from src.autoslice.final_review_contract import is_release_resolved_keep
    prompts = []
    def call(prompt):
        prompts.append(prompt)
        return json.dumps(answer(), ensure_ascii=False)
    rows = [unresolved(code) for code in ('JUDGE_REQUESTS_MORE_AUDIO',
        'JUDGE_UNCERTAIN_KEEP_CURRENT', 'ARGON_PROVIDER_SLOT_BUSY',
        'ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED', 'HISTORY_CONVERGENCE_AUDIO_GATE_INVALID')]
    result = run_terminal(rows, call, terminal_rig=terminal_rig)
    assert len(prompts) == 1, result['terminal_funnel']['attempts']
    assert all(p.startswith('# 字幕材料穷尽终裁 v1') and 'needs_audio' not in p for p in prompts)
    assert result['findings'] == [], result['findings']
    assert result['release_gate'] == 'PASS'
    assert len(result['unresolved_findings_disclosed']) == len(rows)
    assert all(is_release_resolved_keep(r) for r in result['unresolved_findings_disclosed'])


def test_terminal_d_blocks_with_real_recorded_terminal_answer(monkeypatch, terminal_rig):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    row = {**unresolved(), 'suspect': '喜欢她啊', 'suggestion': '不喜欢她', 'proposed_full_cue': '我不喜欢她'}
    result = run_terminal([row], lambda p: json.dumps(answer('UNRESOLVABLE'), ensure_ascii=False),
                          terminal_rig=terminal_rig)
    assert len(result['findings']) == 1
    assert result['findings'][0]['exact_release_adjudication']['reason_code'] == 'TERMINAL_UNRESOLVABLE'
    assert result['release_gate'] == 'BLOCK'


def test_loop_stop_proof_rejects_changed_receipt_srt_reason_and_observation(monkeypatch, terminal_rig):
    from copy import deepcopy
    from src.autoslice.targeted_relisten import validate_no_gain
    from src.autoslice.exact_final_terminal_funnel import terminal_check
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    result = run_terminal([unresolved()], lambda p: json.dumps(answer(), ensure_ascii=False),
                          terminal_rig=terminal_rig)
    proof = result['unresolved_findings_disclosed'][0]['exact_release_adjudication'][
        'sub18_context_meaning_equivalent_keep']['no_gain_proof']
    # The proof carries the raw wide-range witness.  Rebind that exact
    # observation into the request before replaying the cold proof; a fresh
    # target check without its completed D material would correctly fail the
    # target-level contract.
    ranges = [entry['raw_observation'] for entry in proof['projection']['observations']]
    check = terminal_check(SRT, unresolved(), clip_context=terminal_rig.context,
                           offset_ms=0, ranges=ranges)
    assert validate_no_gain(proof, check) == proof
    for path in ('pass_receipts', 'stop_reason_code', 'projection', 'anchor'):
        broken = deepcopy(proof)
        broken[path] = [] if path == 'pass_receipts' else {}
        with pytest.raises(ValueError):
            validate_no_gain(broken, check)


def test_terminal_context_fit_uses_typed_mutation_and_real_apply(monkeypatch, terminal_rig):
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
    assert result['findings'][0]['exact_release_adjudication'].get('repaired'), result['findings']
    changed, repairs = _apply_exact_final_cpa_repairs(SRT, result, clip_context=terminal_rig.context)
    assert changed != SRT and len(repairs) == 1
    validate_repair(repairs[0])
    assert repairs[0]['timing_immutable'] is True


def test_argon_common_queue_wait_and_next_round_admission_retry(scene, monkeypatch):
    from src.autoslice import argon_escalation_runtime as runtime
    from src.autoslice.provider_slots import provider_slot
    waits = []
    def common_wait(timeout):
        waits.append(timeout)
        return 0.01  # Offline bound, substituted at the common policy seam.
    monkeypatch.setattr(runtime, 'provider_wait_for_call', common_wait)
    from tests.test_provider_slots_cross_process_admission import saturate
    with saturate(scene.root / 'runtime'):
        _, first = scene.run(scene.build())
    assert first['argon_escalation']['status'] == 'ADMISSION_PENDING'
    assert first['argon_escalation']['dispatches_this_advance'] == 0
    assert first['argon_escalation']['provider_slot_wait_s'] == 0.01
    _, second = scene.run(scene.build())
    assert second['argon_escalation']['status'] == 'PENDING'
    assert second['argon_escalation']['task_id'] == first['argon_escalation']['task_id']
    assert second['argon_escalation']['dispatches_this_advance'] == 1
    assert waits == [60, 60]


def test_argon_dispatched_batch_retrieval_caps_queue_and_preserves_original_job(scene):
    from src.autoslice.argon_escalation_runtime import bounded_retrieval
    from src.autoslice.provider_slots import provider_slot
    scene.run()
    from tests.test_provider_slots_cross_process_admission import saturate
    with saturate(scene.root / 'runtime'):
        with bounded_retrieval(0.01):
            _, pending = scene.run()
    assert pending['argon_escalation']['status'] == 'ADMISSION_PENDING'
    assert pending['argon_escalation']['provider_slot_wait_s'] <= 0.01
    _, second = scene.run()
    assert second['argon_escalation']['advance_operation'] == 'RETRIEVE_RETAINED_BATCH'
    assert second['argon_escalation']['dispatches_this_advance'] == 0
    assert len([c for c in scene.provider.calls if c['method'] == 'POST']) == 1


def test_inrun_retrieval_includes_retained_admission_pending_jobs():
    from src.autoslice.final_review_auditor import _argon_batch_in_flight
    row = {'exact_release_adjudication': {'reason_code': 'ARGON_ESCALATION_PENDING_OR_UNRESOLVED',
        'argon_escalation': {'status': 'ADMISSION_PENDING', 'job_name': 'retained-job',
            'dispatches_this_advance': 0}}}
    assert _argon_batch_in_flight(row, dispatched_now=False)
    assert not _argon_batch_in_flight(row, dispatched_now=True)


def test_argon_uncertain_has_distinct_reason_and_cannot_sustain_doubt(monkeypatch, terminal_rig):
    from src.autoslice.argon_escalation_integration import FindingSession
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    session = FindingSession.__new__(FindingSession)
    session.audit = {'status': 'UNCERTAIN', 'reason': '音节重叠，不能确定尾音',
        'hearing': {'uncertainties': ['尾音']}}
    heard = session.pending_audit({})
    assert heard['reason_code'] == 'ARGON_HEARD_UNCERTAIN'
    assert heard['argon_uncertainty_evidence']['can_sustain_question_alone'] is False
    session.audit = {'status': 'ADMISSION_PENDING'}
    assert session.pending_audit({})['reason_code'] != heard['reason_code']
    prompts = []
    def call(prompt):
        prompts.append(prompt)
        return json.dumps(answer(), ensure_ascii=False)
    row = unresolved()
    from src.autoslice.exact_final_terminal_funnel import terminal_check
    heard['request'] = terminal_check(SRT, row, clip_context=terminal_rig.context, offset_ms=0)
    row['exact_release_adjudication'] = heard
    result = run_terminal([row], call, terminal_rig=terminal_rig)
    assert not result['findings']
    assert '音节重叠，不能确定尾音' in prompts[0]
    assert 'ARGON_HEARD_UNCERTAIN' in prompts[0]




def test_recorded_current_supported_reaches_proposal_exit_without_k04_redispatch(rig):
    from copy import deepcopy
    from tests.test_targeted_relisten_public_contract import acquire_narrow
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    from src.autoslice.missing_proposal_bootstrap import (collect_proposal_audio,
        bootstrap_missing_proposal, prepare_missing_proposal_candidate, _proposal_audio_recorded_call)
    from src.autoslice.final_review_auditor import _derive_single_span_edit
    acquire_narrow(rig)
    finding = {**deepcopy(rig.finding), 'suggestion': '', 'proposed_full_cue': None,
        'content_risk': 'meaning_reversal'}
    proposal_audio = collect_proposal_audio(request=rig.check, finding=finding,
        entity_verifier=rig.verifier, transcript_provider=None, cached_only=True)
    first = SemanticAttemptRecorder(srt_text=rig.srt, finding=finding,
        clip_context=rig.context, offset_ms=rig.offset_ms)
    def supported(prompt):
        return json.dumps({'status': 'CURRENT_SUPPORTED', 'proposed_cue': '',
            'needs_audio': True, 'reason': '已提供观察支持现稿，不足以提出不同候选'})
    supported.cpa_cache_identity = {'model': 'offline-proposal'}
    call = _proposal_audio_recorded_call(first.wrap(supported), rig.check, proposal_audio)
    rebuilt, initial = bootstrap_missing_proposal(srt_text=rig.srt, finding=finding,
        structured_chat='', clip_context=rig.context, proposal_audio=proposal_audio,
        llm_call=call, derive_single_span_edit=_derive_single_span_edit)
    assert initial['status'] == 'CURRENT_SUPPORTED'
    later = SemanticAttemptRecorder(srt_text=rig.srt, finding=finding,
        clip_context=rig.context, offset_ms=rig.offset_ms)
    def no_call(prompt):
        raise AssertionError('completed supported proposal must be consumed before K04')
    no_call.cpa_cache_identity = supported.cpa_cache_identity
    actual, audit, terminal = prepare_missing_proposal_candidate(srt_text=rig.srt,
        finding=finding, structured_chat='', clip_context=rig.context,
        source_media_timeline_offset_ms=rig.offset_ms, llm_call=later.wrap(no_call),
        derive_single_span_edit=_derive_single_span_edit, check_request=rig.check,
        entity_verifier=rig.verifier)
    assert actual['proposed_full_cue'] == rig.check['current_cue']
    assert audit['status'] == 'CURRENT_SUPPORTED' and audit['new_provider_calls'] == 0
    assert terminal is None and audit['mutation_authorized'] is False


def test_neighbor_edit_and_candidate_span_delta_reuse_observed_argon(rig, monkeypatch):
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    _, check, finding, _ = argon_protocol.three_observations(rig)
    provider, integration = argon_protocol.attach_argon(rig, monkeypatch)
    calls = argon_protocol.install_native_agy(rig, monkeypatch)
    _, audit = argon_protocol.run_public(rig, finding, argon_protocol.argon_judge(rig))
    assert isinstance(audit.get('argon_escalation'), dict), (audit.get('reason_code'), audit.get('detail'))
    assert audit['argon_escalation']['status'] == 'OBSERVED', audit
    session = integration.session(check, finding)
    assert session.resume() and session.envelope
    changed = rig.srt.replace('豆沙问刚才是什么声音', '豆沙说刚才是什么声音')
    target = {**finding, 'suspect': check['current_cue'], 'suggestion': '哼唱',
        'proposed_full_cue': '哼唱', 'repair_class': 'spoken_unit',
        'span_start_codepoint': 0, 'span_end_codepoint': len(check['current_cue'])}
    current = build_context_adjudication_request(changed, target, clip_context=rig.context,
        source_media_timeline_offset_ms=rig.offset_ms, require_complete_utterance_support=True)
    current = relisten_protocol.seal_request({**current,
        'context_start_ms': current['context_start_ms'] + 50})
    reused = integration.session(current, target)
    assert reused.root == session.root
    assert reused.resume() and reused.envelope
    marked = reused.mark_request(current)
    observed = validate_consumer_evidence(marked, reused.envelope)
    assert observed['status'] == 'OBSERVED'
    deltas = observed['ordinary_observations'][-1]['question_deltas']
    assert 'evidence_id' in deltas and 'proposed_cue' in deltas and 'context_start_ms' in deltas
    assert len(calls) == 1 and not provider.calls
    altered_text = changed.replace(check['current_cue'], check['current_cue'] + '啊')
    altered = build_context_adjudication_request(altered_text,
        {**target, 'suspect': check['current_cue'] + '啊',
            'span_end_codepoint': len(check['current_cue']) + 1, 'base_text_sha256': None}, clip_context=rig.context,
        source_media_timeline_offset_ms=rig.offset_ms)
    with pytest.raises(ValueError, match='ARGON_TARGETED_CURRENT_CONTEXT_DRIFT'):
        session.mark_request(altered)
    assert integration.session(altered, target).spec.finding.finding_id != session.spec.finding.finding_id
    assert len(calls) == 1


def test_valid_prior_audio_gate_for_other_candidate_keeps_observation_and_current_judge(scene):
    from copy import deepcopy
    scene.after_needs_audio = True
    verifier = scene.build()
    scene.run(verifier)
    _, prior = scene.run(verifier)
    assert prior['argon_escalation']['status'] == 'OBSERVED'
    scene.after_needs_audio = False
    scene.after_choice = 'CURRENT'
    changed = {**deepcopy(scene.finding), 'suggestion': '个',
        'proposed_full_cue': '一百五十个', 'exact_release_adjudication': prior}
    _, current = scene.run(verifier, finding=changed)
    assert current.get('reason_code') != 'HISTORY_CONVERGENCE_AUDIO_GATE_INVALID', current
    assert 'prior_candidate_audio_obligation' in current['request'], current
    assert current['request']['prior_candidate_audio_obligation']['recovery']['needs_audio'] is True
    assert len([c for c in scene.provider.calls if c['method'] == 'POST']) == 1


@pytest.mark.parametrize('reason', ['JUDGE_REQUESTS_MORE_AUDIO', 'JUDGE_UNCERTAIN_KEEP_CURRENT'])
def test_selected_current_does_not_bypass_terminal_release_contract(reason):
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.semantic_answer import answer_selected
    row = unresolved(reason)
    row['exact_release_adjudication'].update(status='OBSERVED', timing_immutable=True,
        policy_branch=reason, witness_judge={'judge': {'choice': 'CURRENT', 'needs_audio': True}})
    assert answer_selected(row['exact_release_adjudication']['witness_judge']['judge'])
    assert not is_release_resolved_keep(row)


def test_producer_zero_edit_stop_uses_registered_ordinary_terminal_and_saves_keep(tmp_path, monkeypatch, terminal_rig):
    from copy import deepcopy
    from src.autoslice import producer_package_finalization as producer
    from src.autoslice.final_review_auditor import adjudicate_exact_release_findings
    from tests.test_producer_current_text_auto_resume import _completed_current_review
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    path = tmp_path / 'current.srt'
    path.write_text(SRT)
    authority_path = tmp_path / 'authority.json'
    authority_path.write_text('{}')
    audit, _ = _completed_current_review(SRT)
    audit.update(findings=[unresolved()], validated_finding_count=1,
        status='FLAGGED', release_gate='BLOCK', reason_codes=['FINAL_REVIEW_UNRESOLVED_FINDINGS'])
    prompts = []
    def call(prompt):
        prompts.append(prompt)
        return json.dumps(answer(), ensure_ascii=False)
    def review(text, authority, offset, end):
        # This is the ordinary exact consumer's real adapter registration,
        # even when its finding list is empty in a synthetic discovery fixture.
        adjudicate_exact_release_findings(text, [], entity_verifier=terminal_rig.verifier,
            clip_context=terminal_rig.context, judge_llm_call=call,
            source_media_timeline_offset_ms=offset)
        return deepcopy(audit)
    authority = {}
    result = producer._run_exact_final_review_gate(cid='offline', out_root=tmp_path,
        final_start=0, final_end=3000, recut=producer.CurrentTextReviewArtifacts(path),
        chat_authority_audit=authority, chat_authority_path=authority_path,
        run_exact_final_review=review, clip_context=terminal_rig.context)
    assert not result['findings'] and len(prompts) == 1
    assert result['cpa_adjudication_usage']['provider_adjudication_count'] == 1
    assert authority['cpa_adjudication_usage']['provider_adjudication_count'] == 1
    assert path.read_text() == SRT
    saved = json.loads((tmp_path / 'offline.review-flags.json').read_text())
    from src.autoslice.review_evidence_store import expand
    saved = expand(saved)
    assert saved['unresolved_findings_disclosed'][0]['exact_release_adjudication']['terminal_review']['choice'] == 'CURRENT'




def test_loop_stop_cold_keep_cannot_drop_the_actual_terminal_judgment(monkeypatch, terminal_rig):
    from src.autoslice.final_review_contract import is_release_resolved_keep
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    result = run_terminal([unresolved()], lambda p: json.dumps(answer(), ensure_ascii=False),
                          terminal_rig=terminal_rig)
    row = result['unresolved_findings_disclosed'][0]
    assert is_release_resolved_keep(row)
    row['exact_release_adjudication'].pop('terminal_review')
    assert not is_release_resolved_keep(row)


@pytest.mark.parametrize('reason', ['ARGON_RESPONSE_REJECTED', 'RetryNeedsPlan'])
def test_unseen_reason_classes_enter_same_terminal_without_observation_fabrication(reason, monkeypatch):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    prompts = []
    result = run_terminal([unresolved(reason)],
                          lambda p: prompts.append(p) or json.dumps(answer(), ensure_ascii=False))
    # These legacy zero-audio fixtures now exercise the real target-material
    # gate.  They must remain blocked before any terminal judge call rather
    # than fabricating a no-gain proof with an empty observation set.
    assert not result['unresolved_findings_disclosed']
    row = result['findings'][0]
    assert row['terminal_funnel_error']['reason_code'] == (
        'TARGET_MATERIAL_CHANNELS_REQUIRED:WIDE_CANDIDATE_BLIND_AUDIO')
    assert prompts == []




def test_unresolved_disclosed_row_also_enters_the_same_terminal(monkeypatch, terminal_rig):
    from src.autoslice.exact_final_terminal_funnel import review_pass_receipt, terminalize_review
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    audit = {'findings': [], 'validated_finding_count': 0,
        'unresolved_findings_disclosed': [unresolved('JUDGE_UNCERTAIN_KEEP_CURRENT')],
        'reason_codes': ['FINAL_REVIEW_UNRESOLVED_FINDINGS']}
    receipt = review_pass_receipt(pass_index=1, srt_text=SRT, audit=audit, repaired_text=SRT, repairs=[])
    result = terminalize_review(SRT, audit, pass_receipts=[receipt], adapters={
        'clip_context': terminal_rig.context, 'entity_verifier': terminal_rig.verifier,
        'source_media_timeline_offset_ms': terminal_rig.offset_ms,
        'judge_llm_call': lambda p: json.dumps(answer(), ensure_ascii=False)})
    assert len(result['terminal_funnel']['attempts']) == 1
    assert not result['findings'] and len(result['unresolved_findings_disclosed']) == 1


def test_terminal_evidence_summary_deduplicates_requests_but_retains_uncertainty():
    from src.autoslice.semantic_terminal_adjudication import stopped_evidence_summary
    evidence = {'status': 'UNCERTAIN', 'reason': '音节重叠，不能确定尾音',
        'hearing': {'uncertainties': ['尾音']}, 'request': {'whole_clip_current_srt': SRT * 1000}}
    finding = {'cue_index': 1, 'exact_release_adjudication': {'argon_escalation': evidence,
        'prior_audio_adjudication': [evidence] * 10}}
    summary = stopped_evidence_summary(finding)
    assert len(summary['recorded_evidence']) == 2
    assert '音节重叠，不能确定尾音' in json.dumps(summary, ensure_ascii=False)
    assert 'whole_clip_current_srt' not in json.dumps(summary)


def test_terminal_proposed_answer_cannot_retain_a_needs_audio_mutation_gate():
    from src.autoslice.semantic_terminal_adjudication import validate_terminal_answer
    from tests.terminal_request_support import terminal_request
    with pytest.raises(ValueError, match='TERMINAL_CHOICE_CONTRACT_INVALID'):
        validate_terminal_answer(terminal_request('喜欢', '不喜欢'),
            {**answer('PROPOSED'), 'needs_audio': True})


def test_producer_terminal_mutation_registers_saves_and_reenters_exact_review(tmp_path, monkeypatch, terminal_rig):
    from src.autoslice import producer_package_finalization as producer
    from src.autoslice.final_review_auditor import adjudicate_exact_release_findings
    from src.autoslice.jingting_chunker import parse_srt_cues
    from tests.test_producer_current_text_auto_resume import _completed_current_review
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    path = tmp_path / 'current.srt'
    path.write_text(SRT)
    authority_path = tmp_path / 'authority.json'
    authority_path.write_text('{}')
    selected = {**answer('CONTEXT_FIT'), 'status': 'RESOLVED', 'mode': 'faithful_paraphrase',
        'edit_start': 0, 'edit_end': 5, 'replacement': '我喜欢她', 'no_new_facts': True,
        'meaning_basis': '保留喜欢她的核心意思', 'literal_uncertainty': '尾音无法确定',
        'reliable_fragments': [{'text': '我喜欢她', 'basis': '两句语境一致'}],
        'evidence_refs': ['audio', 'cue:2']}
    reviews, prompts, prior_edit_reviews = [], [], []
    def call(prompt):
        prompts.append(prompt)
        return json.dumps(selected, ensure_ascii=False)
    def review(text, authority, offset, end):
        reviews.append(text)
        if text != SRT:
            prior = authority['final_review_audit']
            prior_edit_reviews.append(prior)
            assert prior['findings'][0]['lifecycle']['state'] == 'EDIT_REVIEW'
            assert prior['release_gate'] == 'BLOCK'
        adjudicate_exact_release_findings(text, [], entity_verifier=terminal_rig.verifier,
            clip_context=terminal_rig.context, judge_llm_call=call,
            source_media_timeline_offset_ms=offset)
        audit, _ = _completed_current_review(text)
        if text == SRT:
            audit.update(findings=[unresolved()], validated_finding_count=1,
                status='FLAGGED', release_gate='BLOCK', reason_codes=['FINAL_REVIEW_UNRESOLVED_FINDINGS'])
        return audit
    authority = {}
    result = producer._run_exact_final_review_gate(cid='offline', out_root=tmp_path,
        final_start=0, final_end=3000, recut=producer.CurrentTextReviewArtifacts(path),
        chat_authority_audit=authority, chat_authority_path=authority_path,
        run_exact_final_review=review, clip_context=terminal_rig.context)
    assert len(reviews) == 2 and len(prompts) == 1
    assert len(prior_edit_reviews) == 1
    assert result['findings'] == [] and result['release_gate'] == 'PASS'
    assert result['reviewed_srt_sha256'] != prior_edit_reviews[0]['reviewed_srt_sha256']
    assert reviews[0] == SRT and reviews[1] == path.read_text() != SRT
    assert result['exact_final_cpa_self_heal']['status'] == 'PASS'
    assert len(authority['entity_repairs']) == 1
    assert [(c.start_ms, c.end_ms) for c in parse_srt_cues(SRT)] == [
        (c.start_ms, c.end_ms) for c in parse_srt_cues(path.read_text())]
    saved = json.loads(authority_path.read_text())
    assert saved['final_output_srt_sha256']
    assert saved['cpa_adjudication_usage']['provider_adjudication_count'] == 1


def test_assisted_unknown_is_retained_per_exact_request_across_resume(rig, monkeypatch):
    """Count UNKNOWN transport identities, not every later phase as a resend."""
    import hashlib
    provider, _integration = argon_protocol.attach_argon(rig, monkeypatch)
    argon_protocol.install_native_agy(rig, monkeypatch)
    fallback = argon_protocol.public_judge(rig)
    unknown_inputs, all_inputs = [], []
    def judge(prompt):
        identity = hashlib.sha256(prompt.encode()).hexdigest()
        all_inputs.append(identity)
        if (len(rig.transport.calls) == 3
                and rig.transport.calls[-1]['class'] == 'CONTEXT_ASSISTED_AUDIO'
                and 'CONTEXT_ASSISTED_AUDIO' in prompt
                and not prompt.startswith('# CPA 补听范围选择')):
            unknown_inputs.append(identity)
            raise ConnectionError('synthetic CPA sent; terminal response unknown')
        return fallback(prompt)
    judge.cpa_cache_identity = {'transport': 'offline-protocol',
        'models': ['offline-assisted-unknown'], 'effort': 'high'}
    argon_protocol.run_public(rig, argon_protocol.FINDING, judge)
    assert unknown_inputs and len(unknown_inputs) == len(set(unknown_inputs))
    retained = {}
    for path in (rig.tmp / 'base/cache/semantic-cpa-attempts').glob('*/*.json'):
        row = json.loads(path.read_text())
        if row['status'] == 'OUTCOME_UNKNOWN':
            retained[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert retained
    before, previous_unknown = len(all_inputs), set(unknown_inputs)
    argon_protocol.run_public(rig, argon_protocol.FINDING, judge)
    assert not previous_unknown.intersection(all_inputs[before:])
    assert len(unknown_inputs) == len(set(unknown_inputs))
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == content for path, content in retained.items())
    assert len(rig.transport.calls) == 3
    assert not provider.calls


def test_selected_current_does_not_inflate_prior_unresolved_count_before_argon(scene):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    scene.run()
    recorder = SemanticAttemptRecorder(srt_text=scene.srt, finding=scene.finding,
        clip_context=scene.context, offset_ms=1000)
    initial = [json.loads(row['completion']) for row in recorder._entries() if row['status'] == 'COMPLETED']
    assert any(row.get('choice') == 'CURRENT' for row in initial)
    def unresolved_answer(prompt):
        return '{"choice":"NEITHER","needs_audio":true}'
    unresolved_answer.cpa_cache_identity = scene.judge.cpa_cache_identity
    wrapped = recorder.wrap(unresolved_answer)
    wrapped('prior actual question one')
    wrapped('prior actual question two')
    scene.after_choice = 'NEITHER'
    scene.run(scene.build())
    offered = [row for row in recorder._entries() if any(
        item.get('kind') == 'argon_acquisition'
        for item in row['request']['material'].get('bound_judge_evidence', []))]
    assert len(offered) == 1
    assert offered[0]['continuation_reason']['prior_unresolved_answers'] == 2
    assert 'bound_judge_evidence' in offered[0]['continuation_reason']['changed_material_fields']
    assert offered[0]['request']['continuation_plan']
    before = len(scene.prompts)
    scene.run(scene.build())
    assert len(scene.prompts) == before
