from dataclasses import replace

import pytest

from src.autoslice import finding_closure as core, finding_closure_ports as ports
from src.autoslice.finding_closure_ports import RetainedObject, ReviewRequest, ReviewResult
from tests.test_current_audio_proof_projection import setup_finding


def recovery_case():
    owners, finding, _ = setup_finding()
    history = {'raw_completion': 'original answer', 'evidence': ['unchanged']}
    for generation in range(8):
        history = {'generation': generation, 'left': history, 'right': history}
    row = finding.finding.read_view()
    # This fixture's old-proof placeholder is unrelated to recovery and is
    # not a valid audio proof. Leave proof validation to the real proof tests.
    row.pop('_audio_gain_termination', None)
    row['recovery_history'] = history
    row['terminal_funnel_error'] = {'reason': 'previous owner failure'}
    retained = RetainedObject.from_value(row)
    finding = replace(finding, finding=retained,
                      spec=replace(finding.spec, finding_ref=retained.sha256))
    text = finding.check.read_view()['whole_clip_current_srt']
    work = core.Work('recovery-history', 'revalidate', core.sha(text), task=finding.spec)
    return owners, ReviewRequest(work, text, finding)


def forbid_full_history_expansion(monkeypatch):
    tree = ports.detached_json_tree

    def bounded_copy(value):
        if isinstance(value, dict) and 'recovery_history' in value:
            pytest.fail('recovery expanded all immutable history occurrences')
        return tree(value)

    monkeypatch.setattr(ports, 'detached_json_tree', bounded_copy)


def test_cache_only_recovery_preserves_history_and_original_owner_outcome(monkeypatch):
    owners, request = recovery_case()
    original = request.finding.finding
    before = original.sha256
    expected = original.value()
    expected.pop('terminal_funnel_error')
    expected['revalidated_original_ref'] = before
    effects = []

    def consume(self, current, **kwargs):
        assert kwargs['provider_calls_allowed'] is False
        assert current.work is request.work
        assert current.finding.origin_check == request.finding.origin_check
        view = current.finding.finding.read_view()
        assert view == expected
        assert view['recovery_history']['left'] is view['recovery_history']['right']
        assert current.finding.spec.finding_ref == current.finding.finding.sha256
        result = ReviewResult(current.finding, core.State.AWAITING_TERMINAL,
                              RetainedObject.from_value({'owner': 'incomplete'}))
        effects.append(result)
        return result

    # Keep real current-proof normalization, classification and capture.
    # Replace only the external owner effect; main recovery stays cache-only.
    monkeypatch.setattr(type(owners), '_adjudicate_captured', consume)
    forbid_full_history_expansion(monkeypatch)
    result = owners.recover(request)
    assert len(effects) == 1
    assert result.state is core.State.CALL_FAILURE
    receipt = result.receipt.read_view()
    assert receipt['owner_error'] == 'FINDING_CLOSURE_ORIGINAL_CALL_OWNER_RECOVERY_REQUIRED'
    assert receipt['finding'] == expected
    assert receipt['finding']['recovery_history']['left'] is receipt['finding']['recovery_history']['right']

    assert original.sha256 == before
    assert original.read_view()['terminal_funnel_error'] == {'reason': 'previous owner failure'}
    assert original.read_view()['recovery_history'] == expected['recovery_history']
    # A reader owns its snapshot: mutation of it cannot reach the retained
    # original or another reader. Recovery itself only mutates the top level.
    first, second = original.read_view(), original.read_view()
    first['recovery_history']['left']['reader_note'] = 'local only'
    assert 'reader_note' not in second['recovery_history']['left']
    assert 'reader_note' not in original.read_view()['recovery_history']['left']


def test_recovery_propagates_owner_contract_rejection_without_editing_history(monkeypatch):
    owners, request = recovery_case()
    original = request.finding.finding
    before = original.sha256
    rejection = core.ClosureContractError('FINDING_CLOSURE_OWNER_MATERIAL_DRIFT')

    def reject(self, current, **kwargs):
        assert kwargs['provider_calls_allowed'] is False
        assert 'terminal_funnel_error' not in current.finding.finding.read_view()
        raise rejection

    monkeypatch.setattr(type(owners), '_adjudicate_captured', reject)
    forbid_full_history_expansion(monkeypatch)
    with pytest.raises(core.ClosureContractError) as caught:
        owners.recover(request)
    assert caught.value is rejection
    assert original.sha256 == before
    assert 'terminal_funnel_error' in original.read_view()
