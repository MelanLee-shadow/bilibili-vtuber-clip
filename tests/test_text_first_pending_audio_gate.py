from copy import deepcopy
import json

import pytest

from src.autoslice.acoustic_witness_adjudication import judge_word_choice
from src.autoslice.exact_final_witness_authority import (
    history_audio_recovery_from_adjudication, valid_history_audio_recovery,
)
from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.semantic_retry_progress import RetryNeedsPlan
from tests.test_semantic_audio_recovery import SOURCE, missing_proposal_case


def _saved(tmp_path, request=None):
    if request is None:
        _, request = missing_proposal_case()
    first = judge_word_choice(
        llm_call=lambda _: '{"choice":"CURRENT","needs_audio":true,"reason":"需要连听完整话语","current_utterance_supported":false,"current_utterance_support_reason":"还需补听"}',
        check_request=request, witness={'status': 'UNAVAILABLE'},
    )
    def unavailable(_):
        raise RetryNeedsPlan('CONTINUATION_PLAN_ALREADY_UNRESOLVED')
    later = judge_word_choice(llm_call=unavailable, check_request=request,
                             witness={'status': 'UNAVAILABLE'})
    # admin round 2 (D): a local retry refusal is typed RETRY_BLOCKED,
    # not the provider-outage JUDGE_UNAVAILABLE; it still supplies no decision.
    assert first['needs_audio_requested'] is True and later['status'] == 'RETRY_BLOCKED'
    prior = {'schema_version': 'subtitle-span-adjudication.v1', 'status': 'UNCERTAIN',
             'repaired': False, 'timing_immutable': True, 'decision_authority': 'CPA_JUDGE',
             'witness_authority': 'EVIDENCE_ONLY', 'policy_branch': 'JUDGE_UNCERTAIN_KEEP_CURRENT',
             'request': request, 'text_first_judge': first, 'witness_judge': {'judge': later}}
    p = tmp_path / 'adjudication.json'
    p.write_text(json.dumps(prior, ensure_ascii=False))
    return json.loads(p.read_text()), request


def test_real_judge_writer_cold_replay_enters_existing_range_route(tmp_path, monkeypatch):
    import test_targeted_relisten_public_contract as protocol
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    finding = {'cue_index': 2, 'suspect': '了', 'proposed_full_cue': None,
               'repair_class': 'phonetic'}
    scaffold = _candidate_blind_missing_proposal_scaffold(SOURCE, finding)
    rig = protocol.build_offline_rig(tmp_path / "cold-range", monkeypatch,
        srt=SOURCE, finding=scaffold, offset_ms=30000, source_duration_ms=40000)
    prior, request = _saved(tmp_path, rig.check)
    original = deepcopy(prior)
    gate = history_audio_recovery_from_adjudication(prior)
    assert gate['status'] == 'BLOCK' and gate['gate_source'] == 'ordinary_text_first_judge'
    assert valid_history_audio_recovery(gate, request=request)
    # The real source crop and complete prompt reach normal transport. A
    # send-boundary failure must keep the cold-restored obligation, never
    # produce another unchanged text decision or claim a successful hearing.
    rig.transport.failure = RuntimeError("OFFLINE_COLD_RANGE_SEND_FAILURE")
    finding['exact_release_adjudication'] = prior
    calls = []
    def plan(prompt):
        calls.append(prompt)
        assert prompt.startswith("# CPA 补听范围选择")
        return '{"decision":"OBSERVE","first_cue":1,"last_cue":3,"provider":"gemini","purpose":"FULL_QA","reason":"连听完整话语"}'
    text, audit = adjudicate_context_finding(
        SOURCE, finding, entity_verifier=rig.verifier, clip_context=rig.context,
        source_media_timeline_offset_ms=30000, judge_llm_call=plan)
    assert text == SOURCE and audit['repaired'] is False and prior == original
    assert len(calls) == len(rig.transport.calls) == 1
    assert rig.transport.calls[0]['audio_sha256']
    assert 'Raw-audio dictation witness' not in rig.transport.calls[0]['prompt']
    recovered = audit['semantic_audio_recovery']
    assert recovered['mutation_authorized'] is False and recovered['status'] == 'BLOCK'
    assert recovered['provider_calls_this_attempt'] == 1
    assert recovered['new_effective_observations_this_attempt'] == 0
    assert audit['text_first_judge'] == original['text_first_judge']


@pytest.mark.parametrize('drift', ['request', 'prompt', 'completion', 'explicit_request',
                                   'needs_audio', 'later_resolved', 'repaired', 'mutation', 'policy'])
def test_old_text_preference_or_failed_binding_cannot_authorize_audio(tmp_path, drift):
    prior, request = _saved(tmp_path)
    if drift == 'request':
        prior['request']['request_sha256'] = 'f'*64
    elif drift in ('prompt', 'completion'):
        prior['text_first_judge'][drift+'_sha256'] = 'invalid'
    elif drift == 'explicit_request':
        prior['text_first_judge'].pop('needs_audio_requested')
    elif drift == 'needs_audio':
        prior['text_first_judge']['needs_audio'] = False
    elif drift == 'later_resolved':
        prior['witness_judge']['judge'] = {**prior['text_first_judge'], 'needs_audio': False,
                                         'needs_audio_requested': False}
    elif drift == 'repaired':
        prior['repaired'] = True
    elif drift == 'mutation':
        prior['mutation_authority'] = {'status': 'PASS'}
    else:
        prior['policy_branch'] = 'KEEP_EXISTING'
    gate = history_audio_recovery_from_adjudication(prior)
    assert gate is None or not valid_history_audio_recovery(gate, request=request)
