from copy import deepcopy

import pytest

from src.autoslice.final_review_auditor import (
    build_context_adjudication_request, _candidate_blind_missing_proposal_scaffold,
)
from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
from src.autoslice.retained_range_context import persisted_native_range_artifacts
from tests.test_independent_context_adjudication import _native_range_artifacts


@pytest.mark.parametrize('provider', ['mai', 'moss'])
def test_saved_native_range_is_revalidated_for_a_different_current_target(tmp_path, provider):
    old, _finding, clip, request, raw, paths = _native_range_artifacts(tmp_path, provider)
    validated, reason = validate_range_acoustic_observation(old, request, raw)
    assert validated is not None, reason
    saved_request = build_context_adjudication_request(
        old, {**_finding, '_prior_range_acoustic_observations': [raw]},
        clip_context=clip, source_media_timeline_offset_ms=100)
    authority = {'exact_final_cpa_self_heal': {'passes': [{'repairs': [
        {'acoustic_witness_binding': {'check_request': saved_request}}]}]}}
    original = deepcopy(authority)
    before = {k: p.read_bytes() for k, p in paths.items()}
    references = persisted_native_range_artifacts(authority)
    assert len(references) == 1
    current = old.replace('当前句', '已修复邻句')
    target = {'cue_index': 1, 'suspect': '第一句', 'suggestion': '第一句',
              'proposed_full_cue': '第一句', 'candidate_blind_observation_scaffolding': True,
              '_prior_range_acoustic_observations': references}
    target = _candidate_blind_missing_proposal_scaffold(current, target)
    rebound = build_context_adjudication_request(
        current, target, clip_context=clip, source_media_timeline_offset_ms=100)
    observation, = rebound['context_only_range_acoustic_observations']
    assert observation['current_cue_index'] == 1
    # 41:1489-1492 (admin ruling): the text-free native range stays strict.
    assert observation['evidence_scope'] == 'RANGE_CONTEXT_ONLY'
    assert observation['text_revision'] == 'TEXT_ONLY_REVISION_AT_IDENTICAL_CUE_GRID'
    assert observation['mutation_authorized'] is False
    assert observation['artifact_bindings']['raw_response_sha256'] == raw['artifact_bindings']['raw_response_sha256']
    assert authority == original and {k: p.read_bytes() for k, p in paths.items()} == before
    # Drift in the new target's source/time domain is still rejected.
    wrong = build_context_adjudication_request(
        current, target, clip_context=clip, source_media_timeline_offset_ms=101)
    assert 'context_only_range_acoustic_observations' not in wrong
    paths['raw_response'].write_bytes(b'changed')
    assert persisted_native_range_artifacts(authority) == ()


def test_invalid_original_request_cannot_supply_an_artifact_reference(tmp_path):
    old, finding, clip, _request, raw, _paths = _native_range_artifacts(tmp_path, 'mai')
    request = build_context_adjudication_request(old, {**finding, '_prior_range_acoustic_observations': [raw]},
        clip_context=clip, source_media_timeline_offset_ms=100)
    request['current_cue'] = 'forged original target'
    assert persisted_native_range_artifacts({'request': request}) == ()


def test_new_bound_range_plans_one_check_but_cannot_reopen_after_other_text_changes(tmp_path, monkeypatch):
    import json
    from src.autoslice.semantic_retry_progress import (
        SemanticAttemptRecorder, RetryNeedsPlan, with_novel_range_plan,
    )
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    old, finding, clip, _request, raw, _paths = _native_range_artifacts(tmp_path, 'mai')
    request = build_context_adjudication_request(old, {**finding, '_prior_range_acoustic_observations': [raw]},
        clip_context=clip, source_media_timeline_offset_ms=100)
    references = persisted_native_range_artifacts({'request': request})
    current = old.replace('当前句', '已修复邻句')
    target = {'cue_index': 1, 'suspect': '第一句', 'suggestion': '第一句',
              'proposed_full_cue': '第一句', 'candidate_blind_observation_scaffolding': True}
    target = _candidate_blind_missing_proposal_scaffold(current, target)
    calls = []
    def llm(prompt):
        calls.append(prompt)
        return '{"decision":"UNRESOLVED","needs_audio":true}'
    llm.cpa_cache_identity = {'model': 'test'}
    for question in ('旧窄窗问题一', '旧窄窗问题二', '旧窄窗问题三'):
        SemanticAttemptRecorder(srt_text=current, finding=target, clip_context=clip,
            offset_ms=100).wrap(llm)(question)
    target_request = build_context_adjudication_request(current,
        {**target, '_prior_range_acoustic_observations': references},
        clip_context=clip, source_media_timeline_offset_ms=100)
    enriched = {**target, '_prior_range_acoustic_observations':
                target_request['context_only_range_acoustic_observations']}
    planned = with_novel_range_plan(srt_text=current, finding=enriched, clip_context=clip, offset_ms=100)
    assert planned['continuation_plan']['action'] == 'expand_context'
    evidence = planned['semantic_retry_evidence_basis']['evidence']
    prompt = 'New bound range question: ' + json.dumps(evidence)
    SemanticAttemptRecorder(srt_text=current, finding=planned, clip_context=clip,
        offset_ms=100).wrap(llm)(prompt)
    assert len(calls) == 4

    assert 'continuation_plan' not in with_novel_range_plan(
        srt_text=current, finding=enriched, clip_context=clip, offset_ms=100)
    changed_reply = deepcopy(enriched)
    changed_reply['_prior_range_acoustic_observations'][0]['artifact_bindings']['raw_response_sha256'] = 'e' * 64
    changed_reply['_prior_range_acoustic_observations'][0]['verdict']['response_sha256'] = 'f' * 64
    assert 'continuation_plan' not in with_novel_range_plan(
        srt_text=current, finding=changed_reply, clip_context=clip, offset_ms=100)
    # Changes elsewhere in the SRT or carryover view never make these same
    # source/audio/raw bytes new evidence for the already failed plan.
    altered = current.replace('第三句', '另一个合法修复')
    changed_view = json.loads(json.dumps(planned))
    changed_view['_prior_range_acoustic_observations'][0]['current_srt_sha256'] = 'f' * 64
    with pytest.raises(RetryNeedsPlan, match='CONTINUATION_PLAN_ALREADY_UNRESOLVED'):
        SemanticAttemptRecorder(srt_text=altered, finding=changed_view, clip_context=clip,
            offset_ms=100).wrap(llm)(prompt + 'same bytes')
    assert len(calls) == 4
    # A different, supplied secondary observation is not erased by canonical
    # range identity; the concrete existing plan can consume that new material.
    additional = {**planned, '_prior_secondary_audio_observations': [
        {'response_sha256': 'd' * 64, 'authority': 'EVIDENCE_ONLY'}]}
    SemanticAttemptRecorder(srt_text=current, finding=additional, clip_context=clip,
        offset_ms=100).wrap(llm)(prompt + ' independently supplied evidence')
    assert len(calls) == 5


def test_saved_range_reaches_normal_exact_consumer_before_retry_recorder(tmp_path, monkeypatch):
    import hashlib
    import json
    from src.autoslice.final_review_auditor import adjudicate_exact_release_findings
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    old, finding, clip, _request, raw, _paths = _native_range_artifacts(tmp_path, 'mai')
    original_request = build_context_adjudication_request(old,
        {**finding, '_prior_range_acoustic_observations': [raw]},
        clip_context=clip, source_media_timeline_offset_ms=100)
    references = persisted_native_range_artifacts({'request': original_request})
    current = old.replace('当前句', '邻句已修复')
    target = {'cue_index': 1, 'suspect': '第一句', 'proposed_full_cue': None,
              'suggestion': None, 'repair_class': 'phonetic', 'kind': 'context',
              'why': 'current target requires full utterance evidence',
              'base_text_sha256': hashlib.sha256('第一句'.encode()).hexdigest()}
    calls = []
    def judge(prompt):
        calls.append(prompt)
        return json.dumps({'status': 'UNRESOLVED', 'decision': 'UNRESOLVED',
                           'reason': 'unit evidence remains inconclusive', 'needs_audio': True,
                           'replacement_text': None})
    judge.cpa_cache_identity = {'model': 'test'}
    for question in ('old1', 'old2', 'old3'):
        SemanticAttemptRecorder(srt_text=current, finding=target, clip_context=clip,
            offset_ms=100).wrap(judge)(question)
    pending, resolved = adjudicate_exact_release_findings(
        current, [target], entity_verifier=None, clip_context=clip,
        source_media_timeline_offset_ms=100, judge_llm_call=judge,
        prior_range_acoustic_observations=references)
    assert len(calls) > 3 and not resolved
    assert '整句范围听写' in calls[3]
    assert pending[0]['continuation_plan']['action'] == 'expand_context'
    progress = pending[0]['exact_release_adjudication']['semantic_retry_progress']
    assert progress['events'][0]['status'] == 'COMPLETED'
    assert progress['events'][0]['changed_material_fields'] == ['audio_observations_sha256']
