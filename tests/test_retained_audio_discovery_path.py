"""Exercise retained audio through real discovery normalization and admission."""
from copy import deepcopy
import json

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from tests.test_producer_text_pipeline_final_review import (
    _adapters, _correction_pass, _literal_review_llm,
)
from tests.test_retained_pending_audio_gate import (
    OFFSET, SOURCE, _clip_context, _old_finding, _persist_authority,
)


@pytest.mark.parametrize('padded', [False, True])
@pytest.mark.parametrize('old_uncertain_answers', [0, 3])
def test_cold_retained_gate_survives_normal_discovery_and_reaches_range(
    tmp_path, monkeypatch, padded, old_uncertain_answers,
):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    old = _old_finding(padded=padded)
    authority, parent = _persist_authority(tmp_path, old)
    before, parent_bytes = deepcopy(authority), parent.read_bytes()
    monkeypatch.setattr(pipeline, 'clip_context_prompt_text', lambda *_: '')
    monkeypatch.setattr(pipeline, 'discover_candidate_pronoun_findings', lambda *a, **k: ([], {}))
    observed = []

    def verifier(_):
        pytest.fail('the pending range obligation must precede any narrow witness')

    verifier.observe_range_context = lambda **kw: observed.append(kw) or [
        {'status': 'UNCERTAIN', 'reason_code': 'FIXTURE_RANGE_FAILED'}]

    def reply(prompt):
        if '# CPA 补听范围选择' in prompt:
            return json.dumps({'decision': 'OBSERVE', 'first_cue': 1, 'last_cue': 3,
                               'reason': '仍需连听完整互动'})
        return '{"findings": []}'

    call = _literal_review_llm(reply)
    call.cpa_cache_identity = {'transport': 'fixture', 'models': ['judge'], 'effort': 'medium'}
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    def old_call(_):
        return '{"decision":"UNRESOLVED","needs_audio":true}'
    old_call.cpa_cache_identity = call.cpa_cache_identity
    for index in range(old_uncertain_answers):
        SemanticAttemptRecorder(srt_text=SOURCE, finding={'cue_index': 2},
            clip_context=_clip_context(), offset_ms=OFFSET).wrap(old_call)(f'old question {index}')
    receipt = pipeline._run_exact_final_release_review(
        srt_text=SOURCE, correction_audit=_correction_pass(), adapters=_adapters(),
        authoritative_chat=(), selection_hook='', clip_context=_clip_context(),
        timeline_offset_ms=OFFSET, verified_authority_audit=authority,
        verify_confusable_entity=verifier, final_review_llm=call,
        pronoun_audit_llm=reply,
    )
    assert len(observed) == 1, {
        'reasons': receipt.get('reason_codes'), 'discovery': receipt.get('discovery'),
        'findings': [{k: f.get('exact_release_adjudication', {}).get(k)
                      for k in ('reason_code', 'policy_branch', 'semantic_audio_recovery')}
                     for f in receipt.get('findings', [])],
    }
    assert observed[0]['range_witness_request']['cue_indexes'] == [1, 2, 3]
    assert receipt['release_gate'] == 'BLOCK'
    finding = next(f for f in receipt['findings'] if f['cue_index'] == 2)
    adjudication = finding['exact_release_adjudication']
    assert adjudication['request'] == old['exact_release_adjudication']['request']
    assert adjudication['semantic_audio_recovery']['mutation_authorized'] is False
    assert authority == before and parent.read_bytes() == parent_bytes


def test_range_routing_plan_preserves_unknown_transport_outcome(tmp_path, monkeypatch):
    from src.autoslice.final_review_auditor import (
        _candidate_blind_missing_proposal_scaffold, build_context_adjudication_request,
    )
    from src.autoslice.semantic_audio_recovery import recover_semantic_audio_range
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    authority, _ = _persist_authority(tmp_path, _old_finding(padded=True))
    finding = pipeline._retained_pending_audio_findings(
        SOURCE, authority, clip_context=_clip_context(), timeline_offset_ms=OFFSET)[0]
    request = build_context_adjudication_request(
        SOURCE, _candidate_blind_missing_proposal_scaffold(SOURCE, finding),
        clip_context=_clip_context(), source_media_timeline_offset_ms=OFFSET,
    )
    calls = []
    def raw_call(prompt):
        calls.append(prompt)
        raise RuntimeError('transport outcome unknown')
    raw_call.cpa_cache_identity = {'transport': 'fixture', 'models': ['judge'], 'effort': 'medium'}
    def verifier(_):
        pytest.fail('no narrow witness')
    verifier.observe_range_context = lambda **_: pytest.fail('unknown route cannot dispatch audio')
    def run():
        recorder = SemanticAttemptRecorder(srt_text=SOURCE, finding=finding,
            clip_context=_clip_context(), offset_ms=OFFSET)
        return recover_semantic_audio_range(srt_text=SOURCE, finding=finding,
            check_request=request, entity_verifier=verifier, llm_call=recorder.wrap(raw_call))[1]
    assert run()['error_type'] == 'RuntimeError'
    paths = list((tmp_path / 'runtime/cache/semantic-cpa-attempts').rglob('*.json'))
    saved = {p: p.read_bytes() for p in paths}
    assert len(paths) == 1 and json.loads(paths[0].read_bytes())['status'] == 'OUTCOME_UNKNOWN'
    second = run()
    assert second['error_type'] == 'RetryNeedsPlan' and len(calls) == 1
    assert {p: p.read_bytes() for p in paths} == saved


@pytest.mark.parametrize('drift', ['source', 'offset'])
def test_attempt_material_update_cannot_move_to_another_physical_issue(tmp_path, monkeypatch, drift):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    context = _clip_context()
    recorder = SemanticAttemptRecorder(srt_text=SOURCE, finding={'cue_index': 2},
        clip_context=context, offset_ms=OFFSET)
    original = deepcopy(recorder.material), recorder.directory
    offset = OFFSET
    if drift == 'source':
        context['pieces'][0]['source_media_sha256'] = 'sha256:' + 'f' * 64
    else:
        offset += 1
    with pytest.raises(ValueError, match='PHYSICAL_TARGET_DRIFT'):
        recorder.update_material(srt_text=SOURCE, finding={'cue_index': 2},
            clip_context=context, offset_ms=offset)
    assert (recorder.material, recorder.directory) == original
