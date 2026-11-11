from tests.test_producer_current_text_continuation import (
    _boundary_pending_checkpoint,
    _discovery_pending_checkpoint,
    _srt,
)

import copy
import pytest

from src.autoslice import producer_text_pipeline as pipeline


def test_completed_successor_clock_preserves_historical_block_as_evidence_only(tmp_path):
    current, context, digest, continuation = _boundary_pending_checkpoint(tmp_path)
    clock, binding = pipeline._continuation_source_clock_reference(
        continuation,
        chat_authority=current,
        srt_text=_srt(),
        srt_sha256=digest,
        clip_context=context,
        candidate_id="relationship-candidate",
    )
    assert binding["role"] == "EVIDENCE_ONLY_SOURCE_CLOCK"
    assert binding["historical_release_gate"] == "BLOCK"
    assert clock["release_gate"] == "BLOCK"


@pytest.mark.parametrize('damage', [None, 'no_clock', 'endpoint', 'missing_witness',
                                     'source_grid', 'source_window', 'candidate', 'discovery', 'content'])
def test_finished_boundary_with_unavailable_discovery_is_only_pending_input(tmp_path, damage):
    current, context, digest, clock = _discovery_pending_checkpoint(tmp_path)
    review = current['final_review_audit']
    review['boundary_semantic_review'] = copy.deepcopy(clock['boundary_semantic_review'])
    boundary = review['boundary_semantic_review']
    if damage == 'no_clock': clock = None
    elif damage == 'endpoint': boundary['final_endpoint_binding']['closure_text_sha256'] = 'sha256:' + '0' * 64
    elif damage == 'missing_witness': boundary.pop('source_separation_witness')
    elif damage == 'source_grid': boundary['source_separation_witness']['source_cue_grid_sha256'] = 'sha256:' + '0' * 64
    elif damage == 'source_window': boundary['source_separation_witness']['source_final_end_ms'] += 1
    elif damage == 'candidate': boundary['candidate_id'] = 'other'
    elif damage == 'discovery': review['reason_codes'] = ['OTHER_FAILURE']
    elif damage == 'content': review.update(findings=[{'cue_index': 1}], validated_finding_count=1)
    snapshot = copy.deepcopy(current)
    kwargs = dict(srt_text=_srt(), srt_sha256=digest, clip_context=context,
                  candidate_id='relationship-candidate', source_clock_review=clock)
    if damage:
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._continuation_validate_existing_authority(current, **kwargs)
    else:
        accepted = pipeline._continuation_validate_existing_authority(current, **kwargs)
        assert accepted['status'] == 'AUDITOR_UNAVAILABLE' and accepted['release_gate'] == 'BLOCK'
        assert current == snapshot
        with pytest.raises(pipeline.FinalReviewContractError, match='DISCOVERY_INCOMPLETE'):
            pipeline.validate_final_review_release(accepted, expected_srt_sha256='sha256:' + digest)
