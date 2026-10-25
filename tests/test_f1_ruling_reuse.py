"""F1: original bytes, current consumers, semantic identity and answer provenance."""
import json
from pathlib import Path

import pytest

from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint
from src.autoslice.llm_client import CpaCompletion, LlmConfig, build_llm_call


def provider(raw='{"findings":[]}', model='top'):
    calls = []
    def call(prompt):
        calls.append(prompt)
        return raw
    call.cpa_cache_identity = {'transport': 'recorded', 'models': [model], 'effort': 'high'}
    call.calls = calls
    return call


def bound_transport(tmp_path, version, *, wrapper='bash', env_tag='one'):
    script = tmp_path / 'versions' / version / 'scripts/llm_via_cpa.sh'
    script.parent.mkdir(parents=True)
    script.write_text('# offline framing ' + version)
    call = build_llm_call(LlmConfig(transport='command',
        command_template=f'{wrapper} {script} {{prompt_file}} {{completion_file}} offline high 1',
        command_child_env={'CPA_BASE_URL': 'https://offline.invalid/v1', 'CPA_API_KEY': 'fictional',
                           'CPA_TEST_TAG': env_tag}))
    call.cpa_cache_identity = {'transport': 'cpa_command', 'models': ['offline'], 'effort': 'high'}
    call.provider_runtime_binding = {'provider_transport': 'runtime_cpa',
        'provider_credential_source': str(script.parent.parent / 'cpa.env')}
    return call


def test_deployment_command_env_and_parser_change_reuses_original(tmp_path):
    first = bound_transport(tmp_path, 'a' * 40)
    second = bound_transport(tmp_path, 'b' * 40, wrapper='/bin/bash', env_tag='two')
    args = dict(stage='ruling', prompt='same material', inputs={'audio': 'a' * 64},
                parser_path=Path(__file__), root=tmp_path / 'raw')
    old = CompletedReviewCheckpoint.for_call(llm_call=first, **args)
    new = CompletedReviewCheckpoint.for_call(llm_call=second, **args)
    assert old.directory == new.directory
    assert old.binding['provider']['bound_transport_sha256'] != new.binding['provider']['bound_transport_sha256']
    old.response(lambda _: 'original negative answer', preserve_raw=True)
    original = {p: p.read_bytes() for p in old.directory.glob('*.json')}
    new.binding['parser_sha256'] = 'c' * 64  # Current parser reconsumes old bytes.
    assert new.response(lambda _: pytest.fail('relocation redispatched')) == 'original negative answer'
    assert all(p.read_bytes() == raw for p, raw in original.items())


@pytest.mark.parametrize('change', ['candidate', 'audio', 'context', 'window', 'source', 'current',
                                   'prompt', 'model', 'effort', 'policy'])
def test_substantive_material_or_rule_change_is_a_new_key(tmp_path, change):
    call = provider()
    inputs = {'candidate': '甲', 'audio': 'a' * 64, 'context': '前文',
              'window': [10, 20], 'source': 'b' * 64, 'current': '乙'}
    def checkpoint():
        return CompletedReviewCheckpoint.for_ruling(stage='ruling', prompt=prompt,
            llm_call=call, inputs=inputs, parser_path=Path(__file__), root=tmp_path)
    prompt = 'same prompt'
    before = checkpoint()
    if change in inputs:
        inputs[change] = {'changed': True}
    elif change == 'prompt':
        prompt += ' changed'
    elif change == 'policy':
        call.semantic_policy_version = 'new-policy'
    else:
        call.cpa_cache_identity['models' if change == 'model' else 'effort'] = ['other'] if change == 'model' else 'low'
    assert before.directory != checkpoint().directory


def test_invalid_findings_raw_is_revalidated_without_format_retry(tmp_path, monkeypatch):
    from src.autoslice.final_review_auditor import _request_final_review_findings, FinalReviewAuditError
    from src.autoslice.llm_client import extract_json_object
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('not json')
    for _ in range(2):
        with pytest.raises(FinalReviewAuditError) as error:
            _request_final_review_findings('findings JSON', llm_call=call, extract_json=extract_json_object)
        assert error.value.failure_layer == 'CONTRACT_ERROR'
    assert len(call.calls) == 1
    # A repaired parser uses the SAME original bytes, without another vote.
    seen = []
    assert _request_final_review_findings('findings JSON', llm_call=call,
        extract_json=lambda raw: (seen.append(raw) or {'findings': []})) == []
    assert seen == ['not json'] and len(call.calls) == 1


def test_read_aloud_raw_roundtrip(tmp_path, monkeypatch):
    from src.autoslice.read_aloud_llm_verifier import _completed_response
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"choice":"甲"}')
    first, _ = _completed_response('read-aloud', 'exact chat and audio', call)
    second, _ = _completed_response('read-aloud', 'exact chat and audio', call)
    assert first == second and len(call.calls) == 1
    assert second.answering_model == 'top'


def test_concurrent_same_question_dispatches_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    call = provider('original answer')
    kwargs = dict(stage='ruling', prompt='same question', llm_call=call, inputs={},
                  parser_path=Path(__file__), root=tmp_path)
    start = threading.Barrier(2)
    def run():
        checkpoint = CompletedReviewCheckpoint.for_ruling(**kwargs)
        start.wait()
        return checkpoint.response(call, preserve_raw=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        assert [f.result() for f in futures] == ['original answer', 'original answer']
    assert len(call.calls) == 1


@pytest.mark.parametrize('stage', ['bootstrap', 'converge'])
@pytest.mark.parametrize('answer', ['not JSON', '{"status":"PROPOSED","proposed_cue":"甲\\n乙"}',
                                  '{"decision":"UNRESOLVED","replacement_text":"","reason":"缺音频"}'])
def test_proposal_negative_and_unresolved_raw_roundtrip(tmp_path, monkeypatch, stage, answer):
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal, converge_missing_proposal
    from src.autoslice.final_review_auditor import _derive_single_span_edit
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider(answer)
    kwargs = dict(srt_text='1\n00:00:00,000 --> 00:00:01,000\n当前句\n',
        finding={'cue_index': 1, 'suspect': '当前句'}, structured_chat='', llm_call=call,
        derive_single_span_edit=_derive_single_span_edit)
    if stage == 'converge':
        kwargs['proposal_audit'] = {'status': 'UNRESOLVED'}
    function = bootstrap_missing_proposal if stage == 'bootstrap' else converge_missing_proposal
    first = function(**kwargs)
    second = function(**kwargs)
    assert first[0] is None and second[0] is None
    assert first[1]['status'] == second[1]['status']
    assert first[1].get('reason_code') == second[1].get('reason_code')
    if first[1]['status'] == 'INVALID':
        assert first[1]['failure_layer'] == second[1]['failure_layer'] == 'CONTRACT_ERROR'
    assert len(call.calls) == 1


def test_range_plan_fallback_is_not_top_and_restores_model(tmp_path, monkeypatch):
    from src.autoslice.semantic_audio_recovery import _range_plan_completion
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider(CpaCompletion('{"decision":"NO_NEW_AUDIO","reason":"same material"}', 'fallback'))
    first, hit = _range_plan_completion('range question', call, {'source': 'a' * 64})
    assert not hit and first.answering_model == 'fallback'
    _, hit = _range_plan_completion('range question', call, {'source': 'a' * 64})
    assert not hit and len(call.calls) == 2
    call.cpa_cache_identity['models'] = ['fallback']
    replay, hit = _range_plan_completion('range question', call, {'source': 'a' * 64})
    assert hit and replay == first and replay.answering_model == 'fallback'
    assert len(call.calls) == 2


def test_range_plan_malformed_raw_does_not_redispatch(tmp_path, monkeypatch):
    from src.autoslice.semantic_audio_recovery import _range_plan_completion
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('malformed original')
    assert _range_plan_completion('range JSON', call, {}) == ('malformed original', False)
    assert _range_plan_completion('range JSON', call, {}) == ('malformed original', True)
    assert len(call.calls) == 1


def test_acoustic_pressure_and_actual_answer_cache_roundtrip(tmp_path, monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import judge_word_choice
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_request, _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider(CpaCompletion('{"choice":"NEITHER","needs_audio":true,"reason":"recorded"}', 'fallback'))
    args = dict(check_request=_semantic_cache_request(), witness=_semantic_cache_witness())
    call.cpa_resource_pressure = {'provider_call_count': 1}
    first = judge_word_choice(llm_call=call, **args)
    call.cpa_resource_pressure = {'provider_call_count': 99}
    judge_word_choice(llm_call=call, **args)
    assert len(call.calls) == 2  # A fallback cannot hit under top identity.
    assert call.calls[0] == call.calls[1] and '软资源压力' not in call.calls[0]
    call.cpa_cache_identity['models'] = ['fallback']
    replay = judge_word_choice(llm_call=call, **args)
    assert len(call.calls) == 2
    assert replay['span_review_raw_completion'] == first['span_review_raw_completion']
    assert replay['model_identity']['models'] == ['fallback']
    assert replay['telemetry']['cpa_resource_pressure']['provider_call_count'] == 99


def test_acoustic_invalid_fallback_raw_hits_only_actual_model(tmp_path, monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_request, _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider(CpaCompletion('{"choice":"OUTSIDE"}', 'fallback'))
    args = dict(check_request=_semantic_cache_request(), witness=_semantic_cache_witness())
    first = _judge_word_choice_body(llm_call=call, **args)
    call.cpa_cache_identity['models'] = ['fallback']
    replay = _judge_word_choice_body(llm_call=call, **args)
    assert first['reason_code'] == replay['reason_code'] == 'JUDGE_CHOICE_OUT_OF_SET'
    assert len(call.calls) == 1
    call.cpa_cache_identity['models'] = ['top']
    _judge_word_choice_body(llm_call=call, **args)
    assert len(call.calls) == 2


@pytest.mark.parametrize('change', ['source', 'audio', 'policy'])
def test_acoustic_exact_cache_binds_material_not_visible_in_prompt(tmp_path, monkeypatch, change):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_request, _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"choice":"NEITHER","needs_audio":true}')
    request, witness = _semantic_cache_request(), _semantic_cache_witness()
    _judge_word_choice_body(llm_call=call, check_request=request, witness=witness)
    if change == 'policy':
        call.semantic_policy_version = 'new-ruling-policy'
    else:
        witness['source_media_sha256' if change == 'source' else 'audio_clip_sha256'] = 'f' * 64
    _judge_word_choice_body(llm_call=call, check_request=request, witness=witness)
    assert len(call.calls) == 2
    assert call.calls[0] == call.calls[1]  # Neither audio identity nor policy is prompt prose.


def test_acoustic_invalid_answer_reworded_controller_reason_reuses_raw(tmp_path, monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_request, _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"choice":"OUTSIDE"}')
    results = [_judge_word_choice_body(llm_call=call,
        check_request=_semantic_cache_request(reason=reason), witness=_semantic_cache_witness())
        for reason in ('首次审查理由', '同一材料改写控制器理由')]
    assert len(call.calls) == 1
    assert {r['reason_code'] for r in results} == {'JUDGE_CHOICE_OUT_OF_SET'}


def test_acoustic_malformed_raw_is_contract_error_and_not_resampled(tmp_path, monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_request, _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('not JSON')
    results = [_judge_word_choice_body(llm_call=call, check_request=_semantic_cache_request(),
                                      witness=_semantic_cache_witness()) for _ in range(2)]
    assert len(call.calls) == 1
    assert {r['reason_code'] for r in results} == {'JUDGE_CALL_FAILED'}
    assert all(r['failure_layer'] == 'CONTRACT_ERROR' for r in results)


def test_pressure_wrapper_preserves_owner_policy_and_cache_root(tmp_path, monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    from src.autoslice.final_review_provider_budget import with_cpa_resource_pressure
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_request, _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"choice":"NEITHER","needs_audio":true}')
    call.semantic_policy_version = 'owner-ruling-policy'
    call.completed_review_root = tmp_path / 'owner-raw'
    args = dict(check_request=_semantic_cache_request(), witness=_semantic_cache_witness())
    _judge_word_choice_body(llm_call=with_cpa_resource_pressure(call, 1, 12), **args)
    replay = _judge_word_choice_body(llm_call=with_cpa_resource_pressure(call, 99, 12), **args)
    assert len(call.calls) == 1 and replay['served_from_cache']


def test_recorder_actual_answer_roundtrip_and_top_claim_generation(tmp_path, monkeypatch):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider(CpaCompletion('{"decision":"UNRESOLVED"}', 'fallback'))
    def recorder():
        return SemanticAttemptRecorder(srt_text='1\n00:00:00,000 --> 00:00:01,000\n当前句\n',
            finding={'cue_index': 1}, clip_context={'source_media_sha256': 'a' * 64}, offset_ms=0)
    first = recorder().wrap(call)('same question')
    call.cpa_cache_identity['models'] = ['fallback']
    replay = recorder().wrap(call)('same question')
    assert first == replay and replay.answering_model == 'fallback' and len(call.calls) == 1
    call.cpa_cache_identity['models'] = ['top']
    recorder().wrap(call)('same question')
    assert len(call.calls) == 2
    entries = [json.loads(p.read_text()) for p in (tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json')]
    assert len(entries) == 2
    assert all(e['request']['model']['models'] == ['top'] and
        e['answering_model_identity']['models'] == ['fallback'] for e in entries)


@pytest.mark.parametrize('damage', ['missing_model', 'model_drift'])
def test_recorder_legacy_answer_without_model_is_not_promoted_or_redispatched(tmp_path, monkeypatch, damage):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, RetryNeedsPlan
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider(CpaCompletion('{"decision":"UNRESOLVED"}', 'fallback'))
    recorder = SemanticAttemptRecorder(srt_text='1\n00:00:00,000 --> 00:00:01,000\n当前句\n',
        finding={'cue_index': 1}, clip_context={'source_media_sha256': 'a' * 64}, offset_ms=0)
    wrapped = recorder.wrap(call)
    wrapped('same question')
    path, = (tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json')
    old = json.loads(path.read_text())
    if damage == 'missing_model':
        old.pop('answering_model_identity')
        old.pop('answer_binding_sha256')
    else:
        old['answering_model_identity']['models'] = ['top']
    path.write_text(json.dumps(old))
    preserved = path.read_bytes()
    with pytest.raises(RetryNeedsPlan, match=(
            'COMPLETED_ANSWER_MODEL_UNKNOWN' if damage == 'missing_model' else 'SEMANTIC_ATTEMPT_RECORD_INVALID')):
        wrapped('same question')
    assert len(call.calls) == 1 and path.read_bytes() == preserved


@pytest.mark.parametrize('damage', ['missing_model', 'invalid_json'])
def test_acoustic_unattributed_or_damaged_raw_never_redispatches(tmp_path, monkeypatch, damage):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_request, _semantic_cache_witness
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"choice":"NEITHER","needs_audio":true}')
    args = dict(check_request=_semantic_cache_request(), witness=_semantic_cache_witness())
    _judge_word_choice_body(llm_call=call, **args)
    path, = (p for p in (tmp_path / 'cache/judge-verdicts').rglob('*.json') if 'semantic-no-new' not in p.parts)
    entry = json.loads(path.read_text())
    if damage == 'missing_model':
        entry['verdict'].pop('model_identity')
        path.write_text(json.dumps(entry))
    else:
        path.write_text('invalid cache JSON')
    original = path.read_bytes()
    with pytest.raises(ValueError, match=(
            'JUDGE_ANSWER_MODEL_UNKNOWN' if damage == 'missing_model' else 'JUDGE_CACHE_ENTRY_INVALID')):
        _judge_word_choice_body(llm_call=call, **args)
    assert len(call.calls) == 1 and path.read_bytes() == original


def test_producer_progress_requires_unconsumed_substantive_decision():
    from src.autoslice.producer_package_finalization import _new_repair_rulings
    repair = {'matched_start_ms': 10, 'matched_end_ms': 20, 'before': '甲', 'after': '乙',
        'judge': {'choice': 'PROPOSED', 'completion_sha256': 'a' * 64}}
    passes = [{'repairs': [repair]}]
    assert not _new_repair_rulings([{**repair, 'request_sha256': 'new-wrapper',
        'cpa_resource_pressure': {'calls': 100}}], passes)
    assert _new_repair_rulings([{**repair, 'acoustic_witness_binding': {'audio_sha256': 'b' * 64}}], passes)
    assert _new_repair_rulings([{**repair, 'judge': {**repair['judge'], 'completion_sha256': 'c' * 64}}], passes)


def test_terminal_invalid_raw_is_saved_before_current_validation(tmp_path, monkeypatch):
    from src.autoslice import semantic_terminal_adjudication as terminal
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setattr('src.autoslice.response_schemas.terminal_schema', lambda request: {})
    seen = []
    def reject(request, raw):
        seen.append(raw)
        raise ValueError('TERMINAL_KEEP_ROUTE_REQUIRED')
    monkeypatch.setattr(terminal, 'validate_terminal_answer', reject)
    call = provider('{"choice":"CURRENT"}')
    for _ in range(2):
        raw = terminal.request_terminal_answer(call, 'terminal JSON', {'material': 'same'})
        with pytest.raises(ValueError, match='TERMINAL_KEEP_ROUTE_REQUIRED'):
            terminal.validate_terminal_answer({'material': 'same'}, json.loads(raw))
    assert len(call.calls) == 1 and len(seen) == 2


def test_outer_raw_cache_replay_keeps_audit_and_unknown_dispatch_guard(tmp_path, monkeypatch):
    from src.autoslice.completed_review_checkpoint import ruling_response
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, RetryNeedsPlan
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    call = provider('{"decision":"UNRESOLVED"}')
    recorder = SemanticAttemptRecorder(srt_text='1\n00:00:00,000 --> 00:00:01,000\n当前句\n',
        finding={'cue_index': 1}, clip_context={'source_media_sha256': 'a' * 64}, offset_ms=0)
    wrapped = recorder.wrap(call)
    args = dict(stage='ruling', prompt='same original question', llm_call=wrapped,
                inputs={}, parser_path=Path(__file__))
    raw, hit = ruling_response(**args)
    assert not hit
    replay, hit = ruling_response(**args)
    assert hit and replay == raw and len(call.calls) == 1
    event = recorder.audit()['events'][-1]
    assert event['status'] == 'EXACT_RAW_REPLAY' and event['provider_calls'] == 0
    path, = (tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json')
    entry = json.loads(path.read_text())
    assert event['request_sha256'] == entry['request_sha256']
    entry['status'] = 'OUTCOME_UNKNOWN'
    entry.pop('completion')
    path.write_text(json.dumps(entry))
    preserved = path.read_bytes()
    with pytest.raises(RetryNeedsPlan, match='PRIOR_CALL_OUTCOME_UNKNOWN'):
        ruling_response(**args)
    assert len(call.calls) == 1 and path.read_bytes() == preserved
