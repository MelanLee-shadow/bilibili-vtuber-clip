from src.autoslice.review_package_boundary_validators import (
    expected_boundary_authority,
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


def _projected_endpoint_review() -> dict[str, object]:
    authority_sha256 = "sha256:" + "d" * 64
    cue_grid_sha256 = "sha256:" + "e" * 64
    closure_sha256 = "sha256:" + "f" * 64
    binding = {
        "kind": "reviewed_exact_interval_terminal_projection",
        "cue_index": 35,
        "cue_start_ms": 64_440,
        "cue_end_ms": 67_670,
        "cue_text_sha256": closure_sha256,
        "reviewed_endpoint_ms": 67_760,
        "terminal_drift_ms": 90,
        "max_terminal_drift_ms": 250,
        "crossing_witness_cue_index": 36,
        "crossing_witness_start_ms": 67_670,
        "crossing_witness_end_ms": 69_210,
        "crossing_witness_text_sha256": "sha256:" + "a" * 64,
        "authority_sha256": authority_sha256,
        "cue_grid_sha256": cue_grid_sha256,
    }
    return {
        "cue_grid_sha256": cue_grid_sha256,
        "recommended_end_cue_index": 35,
        "recommended_end_ms": 67_760,
        "evidence_cue_indexes": [35, 36],
        "boundary_search_scope": {
            "reviewed_exact_interval_projection": {
                "schema_version": (
                    "reviewed-exact-interval-terminal-projection-scope.v1"
                ),
                "authority_sha256": authority_sha256,
                "reviewed_endpoint_ms": 67_760,
                "max_terminal_drift_ms": 250,
            }
        },
        "recommendation_relaxations": [binding],
        "selected_terminal_projection_binding": binding,
        "final_endpoint_binding": {
            "final_snapped_end_ms": 67_670,
            "final_end_ms": 67_760,
            "closure_text_sha256": closure_sha256,
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


def test_reviewed_projection_endpoint_is_valid_only_with_crossing_evidence():
    review = _projected_endpoint_review()

    assert semantic_endpoint_snapped_is_valid(review)
    assert semantic_recommendation_is_materialized(
        review,
        snapped_sentence_end_ms=67_670,
        final_end_ms=67_760,
    )

    review["evidence_cue_indexes"] = [35]
    assert not semantic_endpoint_snapped_is_valid(review)


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


def test_published_recall_anchor_has_distinct_package_authority():
    record = {
        "recovery_publication_authority": {
            "boundary_end_mode": "published_recall_anchor"
        }
    }
    audit = {"manual_end_mode": "published_recall_anchor"}

    assert expected_boundary_authority(
        record,
        audit,
        human_authority="Ivan full-rerun authority",
    ) == (
        "published_source_recall_anchor_plus_semantic_review",
        True,
    )


def test_exact_pin_crossing_rejects_delivery_past_frozen_pin():
    review = _pin_crossing_review()

    assert not semantic_recommendation_is_materialized(
        review,
        snapped_sentence_end_ms=95_680,
        final_end_ms=95_680,
    )
