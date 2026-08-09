"""Cross-segment boundary-witness-reserve pieces must never be mistaken for
the semantic "last piece" by boundary-math consumers, while still counting
toward available local source context (the coverage gate they exist to
satisfy)."""

from __future__ import annotations

import pytest

from src.autoslice.boundary_source_context_coverage import (
    source_context_coverage_block,
)
from src.autoslice.piece_roles import (
    content_only,
    is_reserve_piece,
    last_content_piece_index,
    single_content_piece_index,
)
from src.autoslice.producer_boundary_owner_contract import (
    _redelivery_baseline_tail_rel_ms,
    freeze_required_boundary_owner_contract,
)
from src.autoslice.source_subtitle_truth import candidate_boundary_owner_scope


RESERVE_PIECE = {
    "remote_media": "/rec/next.mp4",
    "start_ms": 0,
    "end_ms": 88_000,
    "piece_role": "boundary_witness_reserve",
}


def test_is_reserve_piece_flag():
    assert is_reserve_piece(RESERVE_PIECE) is True
    assert is_reserve_piece({"start_ms": 0, "end_ms": 1_000}) is False


def test_last_content_piece_index_without_reserve():
    pieces = [{"start_ms": 0, "end_ms": 1_000}, {"start_ms": 2_000, "end_ms": 3_000}]
    assert last_content_piece_index(pieces) == 1


def test_last_content_piece_index_skips_trailing_reserve():
    pieces = [
        {"start_ms": 0, "end_ms": 1_000},
        {"start_ms": 2_000, "end_ms": 3_000},
        RESERVE_PIECE,
    ]
    assert last_content_piece_index(pieces) == 1


def test_last_content_piece_index_raises_when_all_reserve():
    with pytest.raises(ValueError):
        last_content_piece_index([RESERVE_PIECE])


def test_content_only_filters_reserve_from_parallel_sequence():
    pieces = [
        {"start_ms": 0, "end_ms": 1_000},
        {"start_ms": 2_000, "end_ms": 3_000},
        RESERVE_PIECE,
    ]
    durations = [1_000, 1_000, 88_000]
    assert content_only(pieces, durations) == [1_000, 1_000]


def test_content_only_rejects_length_mismatch():
    with pytest.raises(ValueError):
        content_only([{"start_ms": 0, "end_ms": 1_000}], [1_000, 2_000])


def test_single_content_piece_index_accepts_only_trailing_reserves():
    assert single_content_piece_index(
        [{"start_ms": 0, "end_ms": 1_000}, RESERVE_PIECE]
    ) == 0


@pytest.mark.parametrize(
    "pieces",
    [
        [],
        [RESERVE_PIECE],
        [
            {"start_ms": 0, "end_ms": 1_000},
            {"start_ms": 2_000, "end_ms": 3_000},
        ],
        [RESERVE_PIECE, {"start_ms": 0, "end_ms": 1_000}],
    ],
)
def test_single_content_piece_index_rejects_ambiguous_content_binding(pieces):
    with pytest.raises(ValueError):
        single_content_piece_index(pieces)


def test_redelivery_baseline_tail_requires_single_content_piece_binding():
    config = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "absolute_source_end_ms": 105_000,
    }
    content = {"start_ms": 100_000, "end_ms": 110_000}
    spec = {
        "pieces": [content, RESERVE_PIECE, RESERVE_PIECE],
        "subtitle_redelivery_baseline": config,
    }
    assert _redelivery_baseline_tail_rel_ms(
        spec,
        last_piece_start_ms=100_000,
        prior_piece_duration_ms=0,
    ) == 5_000

    spec["pieces"] = [content, {"start_ms": 110_000, "end_ms": 120_000}]
    assert (
        _redelivery_baseline_tail_rel_ms(
            spec,
            last_piece_start_ms=110_000,
            prior_piece_duration_ms=10_000,
        )
        is None
    )
    spec["pieces"] = [RESERVE_PIECE, content]
    assert (
        _redelivery_baseline_tail_rel_ms(
            spec,
            last_piece_start_ms=100_000,
            prior_piece_duration_ms=88_000,
        )
        is None
    )


def _base_spec(*, extra_pieces=None):
    pieces = [{"start_ms": 190_000, "end_ms": 200_000}]
    if extra_pieces:
        pieces.extend(extra_pieces)
    return {
        "candidate_id": "candidate-a",
        "semantic_start_ms": 190_000,
        "semantic_end_ms": 195_000,
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": pieces,
    }


def test_candidate_boundary_owner_scope_ignores_trailing_reserve_piece():
    without_reserve = candidate_boundary_owner_scope(
        spec=_base_spec(), durations=[10_000]
    )
    with_reserve = candidate_boundary_owner_scope(
        spec=_base_spec(extra_pieces=[RESERVE_PIECE]),
        durations=[10_000, 88_000],
    )
    # The reserve piece's duration is real additional source context, but it
    # must not shift where semantic_end_ms lands on the story's local axis.
    assert with_reserve["story_end_ms"] == without_reserve["story_end_ms"]
    assert with_reserve["story_start_ms"] == without_reserve["story_start_ms"]
    assert with_reserve["last_piece_source_start_ms"] == (
        without_reserve["last_piece_source_start_ms"]
    )
    assert with_reserve["prior_piece_duration_ms"] == (
        without_reserve["prior_piece_duration_ms"]
    )


def test_freeze_required_boundary_owner_contract_ignores_trailing_reserve_piece():
    without_reserve_audit: dict[str, object] = {}
    freeze_required_boundary_owner_contract(
        spec=_base_spec(),
        durations=[10_000],
        chat_authority_audit=without_reserve_audit,
        required_boundary_owners=[],
    )
    with_reserve_audit: dict[str, object] = {}
    freeze_required_boundary_owner_contract(
        spec=_base_spec(extra_pieces=[RESERVE_PIECE]),
        durations=[10_000, 88_000],
        chat_authority_audit=with_reserve_audit,
        required_boundary_owners=[],
    )
    without_reserve_target = without_reserve_audit[
        "frozen_boundary_owner_contract"
    ]["boundary_review_target_ms"]
    with_reserve_target = with_reserve_audit[
        "frozen_boundary_owner_contract"
    ]["boundary_review_target_ms"]
    assert with_reserve_target == without_reserve_target


def test_payoff_outside_story_yields_only_to_reachable_reviewed_tail():
    """Synthetic 1722 shape: the chat row stays outside the immutable story
    owner set, while its payoff hypothesis can yield to a reachable reviewed
    v2 tail cap.  The trailing reserve remains source witness only.
    """

    content = {"start_ms": 0, "end_ms": 90_000}
    spec = {
        "candidate_id": "candidate-payoff-tail",
        "semantic_start_ms": 0,
        "semantic_end_ms": 80_570,
        "boundary_repair_extend_cap_ms": 30_000,
        "semantic_tail_trim_cap_ms": 15_000,
        "pieces": [content, RESERVE_PIECE],
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "absolute_source_start_ms": 0,
            "absolute_source_end_ms": 67_760,
        },
    }
    payoff = {
        "kind": "danmaku",
        "finding_id": "later-payoff-hypothesis",
        "source_offset_ms": 80_000,
        "matched_start_ms": 82_000,
        "matched_end_ms": 84_240,
        "owner_eligible": True,
    }
    audit: dict[str, object] = {"applied": [payoff]}

    freeze_required_boundary_owner_contract(
        spec=spec,
        durations=[90_000, 88_000],
        chat_authority_audit=audit,
        required_boundary_owners=[],
    )

    assert payoff["boundary_required"] is False
    assert payoff["boundary_owner_rejection"] == (
        "OUTSIDE_IMMUTABLE_STORY_SCOPE"
    )
    frozen = audit["frozen_boundary_owner_contract"]
    assert frozen["required_owner_count"] == 0
    scope = frozen["boundary_search_scope"]
    assert scope["status"] == "PASS"
    assert scope["structured_payoff_ms"] == 84_240
    assert scope["structured_payoff_clamped_from_ms"] == 84_240
    assert scope["delivery_lower_bound_ms"] == 67_760
    assert scope["max_recommended_end_ms"] == 67_760


def test_available_local_source_context_end_ms_counts_the_reserve_piece():
    """The whole point of appending a reserve piece: sum(durations) — the
    coverage gate's available-context measurement — must include it."""

    scope = {
        "required_local_source_context_end_ms": 250_000,
        "scope_sha256": "sha256:test",
    }
    without_reserve = source_context_coverage_block(
        scope=scope,
        available_local_source_context_end_ms=200_000,
        candidate_id="candidate-a",
        boundary_max_forward_ms=30_000,
    )
    assert without_reserve is not None
    assert without_reserve["status"] == "BLOCK"
    assert without_reserve["source_context_coverage"]["deficit_ms"] == 50_000

    with_reserve = source_context_coverage_block(
        scope=scope,
        # 200_000 content + an 88_000ms reserve piece clears the deficit.
        available_local_source_context_end_ms=200_000 + 88_000,
        candidate_id="candidate-a",
        boundary_max_forward_ms=30_000,
    )
    assert with_reserve is None
