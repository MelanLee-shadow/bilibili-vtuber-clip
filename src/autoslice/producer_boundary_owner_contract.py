"""Freeze required subtitle/chat owners before talk boundary review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.autoslice.boundary_semantic_review import (
    build_boundary_search_scope,
)


def redelivery_baseline_boundary_owner(
    spec: Mapping[str, object],
    durations: Sequence[int],
) -> list[dict[str, object]]:
    """Project a hash-bound v2 reviewed baseline onto the padded timeline."""

    config = spec.get("subtitle_redelivery_baseline")
    if not isinstance(config, Mapping):
        return []
    if (
        config.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or config.get("exact_interval_replay") is not True
    ):
        return []
    start = config.get("absolute_source_start_ms")
    end = config.get("absolute_source_end_ms")
    expected_sha = str(config.get("source_sha256") or "").removeprefix(
        "sha256:"
    )
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or end <= start
        or len(expected_sha) != 64
    ):
        raise RuntimeError("REDELIVERY_BASELINE_BOUNDARY_OWNER_INVALID")
    pieces = [
        piece
        for piece in (spec.get("pieces") or [])
        if isinstance(piece, Mapping)
    ]
    if len(pieces) != len(durations):
        raise RuntimeError("REDELIVERY_BASELINE_BOUNDARY_MAPPING_INVALID")
    offset = 0
    windows: list[dict[str, int]] = []
    coverage = 0
    for piece, duration in zip(pieces, durations):
        piece_sha = str(
            piece.get("source_media_sha256") or ""
        ).removeprefix("sha256:")
        piece_start = int(piece["start_ms"])
        piece_end = int(piece["end_ms"])
        overlap_start = max(start, piece_start)
        overlap_end = min(end, piece_end)
        if piece_sha == expected_sha and overlap_start < overlap_end:
            windows.append(
                {
                    "start_ms": offset + overlap_start - piece_start,
                    "end_ms": offset + overlap_end - piece_start,
                }
            )
            coverage += overlap_end - overlap_start
        offset += int(duration)
    if coverage != end - start:
        raise RuntimeError("REDELIVERY_BASELINE_BOUNDARY_OWNER_PARTIAL")
    return [
        {
            "owner_kind": "reviewed_redelivery_baseline",
            "owner_id": "exact-reviewed-interval",
            "required": True,
            "source_start_ms": start,
            "source_end_ms": end,
            "local_windows": windows,
        }
    ]


def freeze_story_chat_boundary_owners(
    audit: dict,
    *,
    story_start_ms: int,
    story_end_ms: int,
) -> list[dict[str, object]]:
    """Mark already-applied story decisions as unable to escape by trimming."""

    groups = (
        ("exact_read", "applied"),
        ("sc_sender", "sender_repairs"),
        ("gift_name", "gift_repairs"),
        ("reply_coreference", "coreference_repairs"),
        ("entity_repair", "entity_repairs"),
    )
    contracts: list[dict[str, object]] = []
    for kind, key in groups:
        for ordinal, row in enumerate(audit.get(key) or [], start=1):
            if not isinstance(row, dict) or row.get("reconciliation"):
                continue
            # Whole-line read-aloud ownership requires the typed independent
            # support gate. Narrow slot repairs keep their own typed contracts.
            if kind == "exact_read" and row.get("owner_eligible") is not True:
                row["boundary_required"] = False
                row["boundary_owner_rejection"] = (
                    "EXACT_READ_SUPPORT_NOT_OWNER_ELIGIBLE"
                )
                continue
            start = row.get("matched_start_ms")
            end = row.get("matched_end_ms")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or end <= start
                or min(end, story_end_ms) - max(start, story_start_ms)
                <= 0
            ):
                continue
            owner_id = str(
                row.get("finding_id")
                or row.get("verdict_id")
                or f"{kind}:{ordinal}:{start}:{end}"
            )
            row["boundary_required"] = True
            row["boundary_owner_id"] = owner_id
            contracts.append(
                {
                    "owner_kind": kind,
                    "owner_id": owner_id,
                    "required": True,
                    "local_windows": [
                        {"start_ms": start, "end_ms": end}
                    ],
                }
            )
    return contracts


def freeze_required_boundary_owner_contract(
    *,
    spec: dict,
    durations: Sequence[int],
    chat_authority_audit: dict,
    required_boundary_owners: list[dict[str, object]],
) -> tuple[int, dict[str, object]]:
    """Freeze all owners and return the exact boundary-review target/scope."""

    last_piece = spec["pieces"][-1]
    prior_piece_duration_ms = sum(durations[:-1])
    last_piece_start_ms = int(last_piece["start_ms"])
    semantic_target_ms = prior_piece_duration_ms + (
        int(spec["semantic_end_ms"]) - last_piece_start_ms
    )
    manual_lower_bound_ms = (
        prior_piece_duration_ms
        + int(spec["given_end_ms"])
        - last_piece_start_ms
        if spec.get("given_end_ms") is not None
        else None
    )
    structured_payoff_ms = max(
        (
            int(row["matched_end_ms"])
            for row in chat_authority_audit.get("applied") or []
            if row.get("kind") in {"danmaku", "superchat"}
            and int(row.get("source_offset_ms") or 0)
            <= semantic_target_ms + 1_000
            and semantic_target_ms
            < int(row.get("matched_end_ms") or 0)
            <= semantic_target_ms + 15_000
            and int(row.get("matched_start_ms") or 0)
            <= semantic_target_ms + 5_000
        ),
        default=None,
    )
    initial_owner_tail_ms = max(
        (
            int(window["end_ms"])
            for owner in required_boundary_owners
            for window in owner.get("local_windows") or []
        ),
        default=None,
    )
    preliminary_story_end_ms = max(
        [
            semantic_target_ms,
            *(
                value
                for value in (
                    manual_lower_bound_ms,
                    structured_payoff_ms,
                    initial_owner_tail_ms,
                )
                if value is not None
            ),
        ]
    )
    story_start_ms = max(
        0,
        int(spec.get("semantic_start_ms", spec["pieces"][0]["start_ms"]))
        - int(spec["pieces"][0]["start_ms"]),
    )
    required_boundary_owners.extend(
        freeze_story_chat_boundary_owners(
            chat_authority_audit,
            story_start_ms=story_start_ms,
            story_end_ms=preliminary_story_end_ms,
        )
    )
    spec["required_boundary_owners"] = required_boundary_owners
    required_owner_tail_ms = max(
        (
            int(window["end_ms"])
            for owner in required_boundary_owners
            for window in owner.get("local_windows") or []
        ),
        default=None,
    )
    boundary_search_scope = build_boundary_search_scope(
        semantic_target_ms=semantic_target_ms,
        manual_lower_bound_ms=manual_lower_bound_ms,
        structured_payoff_ms=structured_payoff_ms,
        required_owner_end_ms=required_owner_tail_ms,
        repair_cap_ms=int(
            spec.get("boundary_repair_extend_cap_ms", 30_000)
        ),
        last_piece_start_ms=last_piece_start_ms,
        prior_piece_duration_ms=prior_piece_duration_ms,
    )
    spec["boundary_search_scope"] = boundary_search_scope
    boundary_target_ms = int(boundary_search_scope["review_target_ms"])
    chat_authority_audit["frozen_boundary_owner_contract"] = {
        "schema_version": "frozen-boundary-owner-contract.v1",
        "status": "FROZEN",
        "story_start_ms": story_start_ms,
        "story_end_ms": boundary_target_ms,
        "owner_discovery_end_ms": preliminary_story_end_ms,
        "required_owner_count": len(required_boundary_owners),
        "owners": required_boundary_owners,
        "boundary_search_scope": boundary_search_scope,
    }
    return boundary_target_ms, boundary_search_scope
