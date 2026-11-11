"""Ordinary retained finding preparation consumes only its context dependencies."""
import gc
import tracemalloc

import pytest

from src.autoslice.finding_closure_ports import RetainedObject
from tests.test_retained_read_view import owners
from tests.test_terminal_funnel_local_cost import SRT, FINDING


def test_preparation_keeps_large_unrelated_history_out_of_context_copies():
    owner = owners()
    expected = owner.prepare_finding(SRT, RetainedObject.from_value(FINDING))
    original = RetainedObject.from_value({**FINDING, 'history': [
        {'round': index, 'detail': [index, 'original recorded response']}
        for index in range(40000)]})
    original_hash = original.sha256
    gc.collect()
    tracemalloc.start()
    try:
        actual = owner.prepare_finding(SRT, original)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual.check.value()['finding_context'] == expected.check.value()['finding_context']
    assert actual.materials == expected.materials
    assert actual.spec.key == expected.spec.key
    assert actual.finding is original and original.sha256 == original_hash
    assert peak < 20 * 1024**2, peak


@pytest.mark.parametrize('evidence', [[999], ['cue:999'], [True]])
def test_retained_projection_keeps_context_index_rejection(evidence):
    original = RetainedObject.from_value({**FINDING, 'evidence_cue_ids': evidence})
    with pytest.raises(ValueError, match='CONTEXT_CUE_INDEX_INVALID'):
        owners().prepare_finding(SRT, original)


def test_retained_boundary_rejects_invalid_unused_fields():
    with pytest.raises(ValueError):
        RetainedObject.from_value({**FINDING, 'unrelated': object()})


def test_projection_retains_explicit_evidence_and_original_history():
    owner = owners()
    original = RetainedObject.from_value({**FINDING, 'evidence_cue_ids': ['cue:1'],
        'history': {'raw': ['keep', 'verbatim']}})
    actual = owner.prepare_finding(SRT, original)
    assert 1 in actual.check.value()['finding_context']['scope_cue_indexes']
    assert actual.finding.value() == original.value()
    copy = actual.finding.value()
    copy['history']['raw'].append('caller mutation')
    assert original.value()['history']['raw'] == ['keep', 'verbatim']
