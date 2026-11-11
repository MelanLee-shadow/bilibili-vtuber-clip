from dataclasses import replace
import json
import tracemalloc

import pytest

from src.autoslice import finding_closure_shell as shell
from src.autoslice.finding_closure_ports import RebaseRequest, RetainedObject
from src.autoslice.targeted_relisten import validate_no_gain
from tests.test_finding_closure_wiring import prepared
from tests.test_exact_final_funnel import terminal_rig, SRT, unresolved, answer  # noqa: F401
from tests.test_wpb_finding_context import WORDS, check, srt


def setup_finding():
    finding = prepared(check())
    proof = {'schema_version': 'retained-original-proof', 'proof_sha256': 'old-proof'}
    row = finding.finding.value()
    row.update(suspect='大', suggestion='微', span_start_codepoint=5,
               span_end_codepoint=6, repair_class='phonetic')
    row['_audio_gain_termination'] = proof
    retained = RetainedObject.from_value(row)
    finding = replace(finding, finding=retained, spec=replace(finding.spec, finding_ref=retained.sha256))
    owners = shell.ExistingFindingOwners(shell.OwnerInputs(
        'a'*64, 1000, RetainedObject.from_value({'candidate_id':'clip',
            'pieces':[{'source_media_sha256':'a'*64}]}),
        RetainedObject.from_value({'meaning':'recorded'}),
        RetainedObject.from_value(finding.materials.value()['model_identity'])))
    return owners, finding, proof


@pytest.mark.parametrize('index', [2, 1, 14])
def test_rebase_stops_using_old_proof_when_whole_text_changes(index):
    owners, original, proof = setup_finding()
    original_bytes = original.finding.json_text
    words = list(WORDS)
    words[index] += '变'
    result, = owners.rebase(RebaseRequest(srt(words), (original,)))
    assert '_audio_gain_termination' not in result.finding.value()
    assert result.finding.value()['previous_finding_ref'] == original.finding.sha256
    assert original.finding.json_text == original_bytes
    assert original.finding.value()['_audio_gain_termination'] == proof
    if index == 14:
        assert result.spec.key == original.spec.key


def test_rebase_unchanged_preserves_proof_and_original_identity():
    owners, original, _ = setup_finding()
    current, = owners.rebase(RebaseRequest(check()['whole_clip_current_srt'], (original,)))
    assert current.finding == original.finding
    assert current.spec.key == original.spec.key


def test_corrupt_proof_is_still_rejected():
    with pytest.raises(ValueError, match='RELISTEN_NO_GAIN_BINDING_DRIFT'):
        validate_no_gain({'schema_version': 'tampered', 'proof_sha256': 'x'}, check())


@pytest.mark.parametrize('index', [2, 14, None])
def test_rebase_shared_history_is_bounded_and_isolated(index, monkeypatch):
    owners, original, _ = setup_finding()
    history = {'leaf': 'original'}
    for _ in range(13):
        history = {'left': history, 'right': history}
    row = original.finding.read_view()
    row['retained_history'] = history
    retained = RetainedObject.from_value(row)
    original = replace(original, finding=retained,
        spec=replace(original.spec, finding_ref=retained.sha256))
    words = list(WORDS)
    if index is not None:
        words[index] += '变'
    entry_peaks = []
    original_prepare = type(owners).prepare_finding
    def track_prepare(self, *args, **kwargs):
        entry_peaks.append(tracemalloc.get_traced_memory()[1])
        return original_prepare(self, *args, **kwargs)
    monkeypatch.setattr(type(owners), 'prepare_finding', track_prepare)
    tracemalloc.start()
    try:
        result, = owners.rebase(RebaseRequest(srt(words), (original,)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Local changes enter a separate terminal-check consumer; measure this
    # rebase projection before that consumer's own allocations.
    measured_peak = entry_peaks[0] if entry_peaks else peak
    assert measured_peak < 2 * 1024 * 1024, measured_peak
    if index is None:
        assert result.finding == retained
        assert '_audio_gain_termination' in result.finding.read_view()
    else:
        assert '_audio_gain_termination' not in result.finding.read_view()
        assert result.finding.read_view()['previous_finding_ref'] == retained.sha256
    if index == 14:
        expected = retained.read_view()
        expected.pop('_audio_gain_termination')
        expected['previous_finding_ref'] = retained.sha256
        assert result.finding.sha256 == RetainedObject.from_value(expected).sha256
    view = result.finding.read_view()
    view['retained_history']['left'] = {'changed': True}
    assert result.finding.read_view()['retained_history']['left'] != {'changed': True}
    assert retained.read_view()['retained_history']['left'] != {'changed': True}
    public = result.finding.value()
    assert public['retained_history']['left'] is not public['retained_history']['right']
    public['retained_history']['left']['added'] = True
    assert 'added' not in public['retained_history']['right']
    assert 'added' not in result.finding.read_view()['retained_history']['left']


def test_current_proof_from_ordinary_terminal_owner_rejects_tampering(terminal_rig):  # noqa: F811
    from src.autoslice import exact_final_terminal_funnel as funnel, targeted_relisten as audio
    from src.autoslice.finding_context import materials_for_check

    def materials(request, finding):
        projection = audio.evidence_projection(request, include_target_text=True)
        return materials_for_check(request, finding=finding, contracts={'test':'offline'},
            model_identity={'transport':'recorded','models':['offline'],'effort':'high'},
            observation_ids=[item['observation_id'] for item in projection['observations']])
    result = funnel.terminalize_review(SRT, {'findings':[unresolved()],
        'validated_finding_count':1, 'status':'FLAGGED', 'release_gate':'BLOCK',
        'reason_codes':['FINAL_REVIEW_UNRESOLVED_FINDINGS']}, pass_receipts=[],
        adapters={'clip_context':terminal_rig.context,'entity_verifier':terminal_rig.verifier,
                  'judge_llm_call':lambda _:json.dumps(answer(),ensure_ascii=False),
                  'source_media_timeline_offset_ms':terminal_rig.offset_ms}, task_materials=materials)
    row, = result['unresolved_findings_disclosed']
    request = row['exact_release_adjudication']['terminal_review']['request']
    proof = request['audio_gain_termination']
    assert audio.validate_current_no_gain(proof, request['check_request']) == proof
    altered = dict(proof, raw_completion=proof['raw_completion']+' ')
    with pytest.raises(ValueError, match='RELISTEN_NO_GAIN_BINDING_DRIFT'):
        audio.validate_current_no_gain(altered, request['check_request'])
