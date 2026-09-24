"""Structured chat payoff is frozen once, then consumed as the only SSoT."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters,
    _select_initial_boundary,
)
from src.autoslice.producer_boundary_owner_contract import (
    STRUCTURED_CHAT_PAYOFF_ASSESSMENT_KEY,
    freeze_required_boundary_owner_contract,
    frozen_boundary_owner_contract_sha256,
    structured_chat_payoff_scope_ms_from_frozen_contract,
)
from src.autoslice.structured_chat_payoff import freeze_assessment


def _cue(index: int, start_ms: int, end_ms: int, value: str) -> SrtCue:
    return SrtCue(index=index, start_ms=start_ms, end_ms=end_ms, text=value)


def _spec() -> dict[str, object]:
    return {
        "candidate_id": "scope-regression",
        "semantic_start_ms": 1_381_660,
        "semantic_end_ms": 1_506_940,
        "semantic_tail_trim_cap_ms": 15_000,
        "boundary_repair_extend_cap_ms": 60_000,
        "pieces": [{"start_ms": 1_371_660, "end_ms": 1_588_330}],
    }


def _audit(read_start_ms: int, *, owner_eligible: bool = True) -> dict[str, object]:
    return {
        "applied": [
            {
                "kind": "danmaku",
                "finding_id": "next-question-read",
                "source_offset_ms": 128_197,
                "matched_start_ms": read_start_ms,
                "matched_end_ms": 138_670,
                "owner_eligible": owner_eligible,
                "exact_text": "下一条问题",
            }
        ],
        "entity_repairs": [
            {
                "matched_start_ms": 130_030,
                "matched_end_ms": 130_190,
            }
        ],
    }


def _freeze(
    read_start_ms: int,
    *,
    owner_eligible: bool = True,
    retry_contract: dict[str, object] | None = None,
):
    spec = _spec()
    if retry_contract is not None:
        spec["boundary_retry_frozen_owner_contract"] = retry_contract
    audit = _audit(read_start_ms, owner_eligible=owner_eligible)
    target, scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=[216_683],
        chat_authority_audit=audit,
        required_boundary_owners=[],
    )
    return target, scope, audit


def _assessment(audit: dict[str, object]) -> dict[str, object]:
    frozen = audit["frozen_boundary_owner_contract"]
    assert isinstance(frozen, dict)
    assessment = frozen[STRUCTURED_CHAT_PAYOFF_ASSESSMENT_KEY]
    assert isinstance(assessment, dict)
    return assessment


def test_read_wholly_after_story_is_observed_but_not_effective():
    # Reproduces surgery: the row was posted before the target, but the entire
    # read belongs to the following tourism/kangaroo question.
    target, scope, audit = _freeze(137_350)
    row = audit["applied"][0]
    assert row["boundary_required"] is False
    assert row["boundary_owner_rejection"] == "OUTSIDE_IMMUTABLE_STORY_SCOPE"
    assert row["exact_text"] == "下一条问题"  # Context evidence remains retained.

    assessment = _assessment(audit)
    assert assessment["schema_version"] == "structured-chat-payoff-assessment.v1"
    assert assessment["mode"] == "effective_story"
    assert assessment["observed_ms"] == 138_670
    assert assessment["effective_story_ms"] is None
    assert assessment["scope_ms"] is None
    assert assessment["effective_row_refs"] == []
    assert assessment["excluded_row_refs"][0]["reason_code"] == (
        "OUTSIDE_IMMUTABLE_STORY_SCOPE"
    )

    assert scope["structured_payoff_ms"] is None
    assert target == 135_280
    assert scope["delivery_lower_bound_ms"] == 130_190
    assert structured_chat_payoff_scope_ms_from_frozen_contract(
        audit["frozen_boundary_owner_contract"]
    ) is None


def test_read_crossing_story_tail_keeps_existing_payoff_protection():
    target, scope, audit = _freeze(134_700)
    assert audit["applied"][0]["boundary_owner_rejection"] == (
        "STRADDLES_IMMUTABLE_STORY_SCOPE"
    )
    assessment = _assessment(audit)
    assert assessment["observed_ms"] == 138_670
    assert assessment["effective_story_ms"] == 138_670
    assert assessment["scope_ms"] == 138_670
    assert assessment["excluded_row_refs"] == []
    assert scope["structured_payoff_ms"] == 138_670
    assert scope["delivery_lower_bound_ms"] == 138_670
    assert target == 138_670


def test_unsupported_read_is_explicitly_excluded_from_effective_story():
    _target, scope, audit = _freeze(134_700, owner_eligible=False)
    assert audit["applied"][0]["boundary_owner_rejection"] == (
        "EXACT_READ_SUPPORT_NOT_OWNER_ELIGIBLE"
    )
    assessment = _assessment(audit)
    assert assessment["observed_ms"] == 138_670
    assert assessment["effective_story_ms"] is None
    assert assessment["scope_ms"] is None
    assert assessment["excluded_row_refs"][0]["reason_code"] == (
        "EXACT_READ_SUPPORT_NOT_OWNER_ELIGIBLE"
    )
    assert scope["structured_payoff_ms"] is None


def test_exact_pin_keeps_observed_hypothesis_for_existing_builder_clamp():
    spec = {
        "candidate_id": "exact-pin-payoff",
        "semantic_start_ms": 0,
        "semantic_end_ms": 113_010,
        "given_end_ms": 113_570,
        "given_end_mode": "exact_source_pin",
        "given_end_authority": "test exact-pin authority",
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": [{"start_ms": 0, "end_ms": 150_000}],
    }
    row = {
        "kind": "superchat",
        "finding_id": "post-pin-payoff-hypothesis",
        "source_offset_ms": 112_000,
        "matched_start_ms": 114_000,
        "matched_end_ms": 124_320,
        "owner_eligible": True,
    }
    audit: dict[str, object] = {"applied": [row]}

    target, scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=[150_000],
        chat_authority_audit=audit,
        required_boundary_owners=[],
    )

    assessment = _assessment(audit)
    assert row["boundary_owner_rejection"] == "OUTSIDE_IMMUTABLE_STORY_SCOPE"
    assert assessment["mode"] == "terminal_authority_clamp_hypothesis"
    assert assessment["observed_ms"] == 124_320
    assert assessment["effective_story_ms"] is None
    assert assessment["scope_ms"] == 124_320
    assert scope["structured_payoff_ms"] == 124_320
    assert scope["structured_payoff_clamped_from_ms"] == 124_320
    assert scope["semantic_search_origin_ms"] == 113_570
    assert scope["delivery_lower_bound_ms"] == 113_570
    assert scope["max_recommended_end_ms"] == 113_570
    assert target == 113_570


def test_reconciliation_row_is_audit_only_not_missing_owner_classification():
    audit = _audit(134_700)
    audit["applied"][0]["reconciliation"] = {"status": "historical-comparison"}
    spec = _spec()

    target, scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=[216_683],
        chat_authority_audit=audit,
        required_boundary_owners=[],
    )

    assessment = _assessment(audit)
    assert assessment["observed_row_refs"] == []
    assert assessment["effective_row_refs"] == []
    assert assessment["excluded_row_refs"] == []
    assert scope["structured_payoff_ms"] is None
    assert target == 135_280


def test_time_qualified_row_without_owner_classification_fails_closed():
    audit = _audit(134_700)
    with pytest.raises(
        RuntimeError,
        match="STRUCTURED_CHAT_PAYOFF_CLASSIFICATION_MISSING",
    ):
        freeze_assessment(
            audit,
            semantic_target_ms=135_280,
            terminal_authority_clamp=False,
        )


def test_downstream_consumer_does_not_rescan_mutated_chat_rows():
    _target, _scope, audit = _freeze(134_700)
    frozen = deepcopy(audit["frozen_boundary_owner_contract"])
    # Simulate a later mutable audit edit. The frozen assessment remains the
    # sole authority and the consumer cannot observe this row anymore.
    audit["applied"][0]["matched_end_ms"] = 149_999
    audit["applied"][0]["boundary_owner_rejection"] = (
        "OUTSIDE_IMMUTABLE_STORY_SCOPE"
    )
    assert structured_chat_payoff_scope_ms_from_frozen_contract(frozen) == 138_670


def test_freeze_to_producer_consumes_the_same_assessment_after_row_mutation(
    tmp_path,
):
    spec = _spec()
    audit = _audit(134_700)
    _target, scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=[216_683],
        chat_authority_audit=audit,
        required_boundary_owners=[],
    )
    frozen = deepcopy(audit["frozen_boundary_owner_contract"])
    frozen_scope_ms = structured_chat_payoff_scope_ms_from_frozen_contract(frozen)
    assert frozen_scope_ms == 138_670

    # Mutation after freeze must be invisible to the media-boundary consumer.
    audit["applied"][0]["matched_end_ms"] = 149_999
    audit["applied"][0]["boundary_required"] = False
    audit["applied"][0]["boundary_owner_rejection"] = (
        "OUTSIDE_IMMUTABLE_STORY_SCOPE"
    )
    cues = [
        _cue(1, 0, 130_190, "手术请求的前文。"),
        _cue(2, 130_190, 135_280, "原故事尾锚。"),
        _cue(3, 135_300, 138_670, "跨尾锚的同故事回收。"),
    ]
    spec["boundary_semantic_review"] = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "review_scope": "source_full_window",
        "status": "PASS",
        "recommended_end_ms": 138_670,
        "recommended_end_cue_index": 3,
        "cue_grid_sha256": cue_grid_sha256(cues),
        "boundary_search_scope": deepcopy(scope),
    }

    initial = _select_initial_boundary(
        spec=spec,
        durations=[216_683],
        padded=tmp_path / "unused.mp4",
        padded_dur=216_683,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        required_tail_end_ms=frozen_scope_ms,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
        boundary_repair_extend_cap_ms=60_000,
    )

    assert initial.target_rel == 138_670
    assert initial.snapped_end == 138_670
    assert initial.closure_cue.text == "跨尾锚的同故事回收。"


def test_retry_freezes_a_new_assessment_without_requiring_asr_equality():
    _target, _scope, first_audit = _freeze(137_350)
    first = first_audit["frozen_boundary_owner_contract"]
    _target, _scope, retry_audit = _freeze(134_700, retry_contract=first)
    retry = retry_audit["frozen_boundary_owner_contract"]
    receipt = retry["boundary_retry_owner_contract_verification"]

    assert receipt["status"] == "PASS"
    assert receipt["structured_chat_payoff_binding"] == "per_attempt"
    assert receipt["first_attempt_structured_chat_payoff_assessment_sha256"] == (
        first[STRUCTURED_CHAT_PAYOFF_ASSESSMENT_KEY]["assessment_sha256"]
    )
    assert receipt["retry_structured_chat_payoff_assessment_sha256"] == (
        retry[STRUCTURED_CHAT_PAYOFF_ASSESSMENT_KEY]["assessment_sha256"]
    )
    assert (
        receipt["first_attempt_structured_chat_payoff_assessment_sha256"]
        != receipt["retry_structured_chat_payoff_assessment_sha256"]
    )


def test_legacy_contract_consumes_saved_scope_without_reclassification():
    _target, _scope, audit = _freeze(134_700)
    legacy = deepcopy(audit["frozen_boundary_owner_contract"])
    legacy.pop(STRUCTURED_CHAT_PAYOFF_ASSESSMENT_KEY)
    legacy["contract_sha256"] = frozen_boundary_owner_contract_sha256(legacy)
    assert structured_chat_payoff_scope_ms_from_frozen_contract(legacy) == 138_670


def test_tampered_assessment_fails_closed_even_if_outer_hash_is_recomputed():
    _target, _scope, audit = _freeze(134_700)
    tampered = deepcopy(audit["frozen_boundary_owner_contract"])
    tampered[STRUCTURED_CHAT_PAYOFF_ASSESSMENT_KEY]["scope_ms"] = None
    tampered["contract_sha256"] = frozen_boundary_owner_contract_sha256(tampered)
    with pytest.raises(RuntimeError, match="BOUNDARY_RETRY_OWNER_SET_DRIFT"):
        structured_chat_payoff_scope_ms_from_frozen_contract(tampered)
