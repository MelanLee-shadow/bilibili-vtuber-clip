from src.autoslice.review_package_boundary_validators import (
    semantic_boundary_review_is_valid,
    semantic_endpoint_snapped_is_valid,
    semantic_recommendation_is_materialized,
)


def _pin_crossing_review() -> dict[str, object]:
    pin_ms = 95_670
    snapped_end_ms = 95_680
    digest = "sha256:" + "a" * 64
    request = "sha256:" + "b" * 64
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "syntax_complete": True,
        "story_closed": True,
        "next_topic_separated": True,
        "content_anchor_covered": True,
        "next_topic_witness_valid": True,
        "recommended_end_ms": pin_ms,
        "recommended_end_cue_index": 32,
        "evidence_cue_indexes": [31, 32, 33, 34],
        "selector_story_witness": {"status": "PASS"},
        "request_sha256": request,
        "cue_grid_sha256": digest,
        "boundary_search_scope": {
            "boundary_end_mode": "exact_source_pin",
        },
        "recommendation_relaxations": [
            {
                "kind": "pin_crossing_closure_cue",
                "cue_index": 32,
                "cue_end_ms": snapped_end_ms,
                "pin_ms": pin_ms,
                "overrun_ms": 10,
                "tolerance_ms": 600,
            }
        ],
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "reason_codes": [],
            "semantic_request_sha256": request,
            "semantic_cue_grid_sha256": digest,
            "final_cue_grid_sha256": digest,
            "recommended_end_cue_index": 32,
            "recommended_end_ms": pin_ms,
            "final_closure_cue_index": 32,
            "final_snapped_end_ms": snapped_end_ms,
            "final_start_ms": 9_770,
            "final_end_ms": pin_ms,
            "closure_text_sha256": "sha256:" + "c" * 64,
        },
    }


def test_exact_pin_crossing_endpoint_is_valid_and_materialized():
    review = _pin_crossing_review()

    assert semantic_endpoint_snapped_is_valid(review)
    assert semantic_boundary_review_is_valid(
        review,
        expected_scope="source_full_window",
    )
    assert semantic_recommendation_is_materialized(
        review,
        snapped_sentence_end_ms=95_680,
        final_end_ms=95_670,
    )


def test_exact_pin_crossing_rejects_unbound_or_excessive_relaxation():
    for field, value in (
        ("cue_index", 31),
        ("cue_end_ms", 95_681),
        ("pin_ms", 95_669),
        ("overrun_ms", 11),
        ("tolerance_ms", 601),
    ):
        review = _pin_crossing_review()
        review["recommendation_relaxations"][0][field] = value
        assert not semantic_endpoint_snapped_is_valid(review)

    excessive = _pin_crossing_review()
    excessive["final_endpoint_binding"]["final_snapped_end_ms"] = 96_271
    excessive["recommendation_relaxations"][0].update(
        {
            "cue_end_ms": 96_271,
            "overrun_ms": 601,
        }
    )
    assert not semantic_endpoint_snapped_is_valid(excessive)


def test_exact_pin_crossing_rejects_delivery_past_frozen_pin():
    review = _pin_crossing_review()

    assert not semantic_recommendation_is_materialized(
        review,
        snapped_sentence_end_ms=95_680,
        final_end_ms=95_680,
    )
