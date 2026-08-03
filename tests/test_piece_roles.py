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
)
from src.autoslice.producer_boundary_owner_contract import (
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
