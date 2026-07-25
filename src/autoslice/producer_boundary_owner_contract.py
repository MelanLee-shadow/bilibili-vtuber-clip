"""Freeze required subtitle/chat owners before talk boundary review."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from src.autoslice.boundary_semantic_review import (
    build_boundary_search_scope,
)
from src.autoslice.source_subtitle_truth import (
    candidate_boundary_owner_scope,
)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalized_owner_set(
    owners: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for owner in owners:
        if not isinstance(owner, Mapping):
            raise RuntimeError("BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID")
        windows = owner.get("local_windows")
        if not isinstance(windows, list):
            raise RuntimeError("BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID")
        normalized_windows: list[dict[str, int]] = []
        for window in windows:
            if not isinstance(window, Mapping):
                raise RuntimeError(
                    "BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID"
                )
            start = window.get("start_ms")
            end = window.get("end_ms")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or start >= end
            ):
                raise RuntimeError(
                    "BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID"
                )
            normalized_windows.append(
                {"start_ms": start, "end_ms": end}
            )
        normalized.append(
            {
                "owner_kind": str(owner.get("owner_kind") or ""),
                "owner_id": str(owner.get("owner_id") or ""),
                "required": owner.get("required") is True,
                "source_start_ms": owner.get("source_start_ms"),
                "source_end_ms": owner.get("source_end_ms"),
                "owner_scope_sha256": owner.get("owner_scope_sha256"),
                "local_windows": sorted(
                    normalized_windows,
                    key=lambda window: (
                        window["start_ms"],
                        window["end_ms"],
                    ),
                ),
            }
        )
    return sorted(
        normalized,
        key=lambda owner: (
            owner["owner_kind"],
            owner["owner_id"],
            json.dumps(owner["local_windows"], sort_keys=True),
        ),
    )


def frozen_boundary_owner_contract_sha256(
    contract: Mapping[str, object],
) -> str:
    """Hash one frozen contract without its self-referential digest."""

    return _canonical_sha256(
        {
            str(key): value
            for key, value in contract.items()
            if key != "contract_sha256"
        }
    )


# Owner kinds whose identity and windows derive only from committed inputs
# (ledger truth intervals + immutable candidate scope), never from a fresh
# ASR pass.  Only these bind across a widened-context retry: story-chat
# owners are re-derived from a fresh transcript whose matched geometry (and
# even row membership) legitimately jitters between attempts, so each attempt
# freezes and enforces its own full owner set instead.
_DETERMINISTIC_OWNER_KINDS = frozenset({"source_subtitle_truth"})


def _deterministic_owner_subset(
    owners: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    return _normalized_owner_set(
        [
            owner
            for owner in owners
            if isinstance(owner, Mapping)
            and str(owner.get("owner_kind") or "")
            in _DETERMINISTIC_OWNER_KINDS
        ]
    )


def deterministic_owner_set_sha256(
    owners: Sequence[Mapping[str, object]],
) -> str:
    return _canonical_sha256(_deterministic_owner_subset(owners))


def validate_frozen_boundary_owner_contract(
    contract: object,
) -> dict[str, object]:
    """Validate the retry token before it can authorize a widened rerun."""

    if not isinstance(contract, Mapping):
        raise RuntimeError("BOUNDARY_RETRY_OWNER_SET_DRIFT")
    owners = contract.get("owners")
    owner_scope = contract.get("owner_eligibility_scope")
    try:
        normalized_owners = (
            _normalized_owner_set(owners)
            if isinstance(owners, list)
            else None
        )
    except RuntimeError as exc:
        raise RuntimeError("BOUNDARY_RETRY_OWNER_SET_DRIFT") from exc
    if (
        contract.get("schema_version")
        != "frozen-boundary-owner-contract.v1"
        or contract.get("status") != "FROZEN"
        or not isinstance(owners, list)
        or not isinstance(owner_scope, Mapping)
        or contract.get("required_owner_count") != len(owners)
        or any(
            not owner["owner_kind"]
            or not owner["owner_id"]
            or owner["required"] is not True
            or not owner["local_windows"]
            for owner in (normalized_owners or [])
        )
        or contract.get("owner_set_sha256")
        != _canonical_sha256(normalized_owners)
        # Contracts frozen before the deterministic subset existed carry no
        # such digest.  They are immutable evidence validated under the older
        # (strictly narrower) whole-set rule and must stay auditable; only a
        # present digest is checked, and a wrong one still fails closed.
        or (
            contract.get("deterministic_owner_set_sha256") is not None
            and contract.get("deterministic_owner_set_sha256")
            != _canonical_sha256(_deterministic_owner_subset(owners))
        )
        or owner_scope.get("scope_sha256")
        != _canonical_sha256(
            {
                str(key): value
                for key, value in owner_scope.items()
                if key != "scope_sha256"
            }
        )
        or contract.get("contract_sha256")
        != frozen_boundary_owner_contract_sha256(contract)
    ):
        raise RuntimeError("BOUNDARY_RETRY_OWNER_SET_DRIFT")
    return dict(contract)


def redelivery_baseline_boundary_owner(
    spec: Mapping[str, object],
    durations: Sequence[int],
) -> list[dict[str, object]]:
    """A reviewed text baseline never acquires content-selection authority.

    Baseline replay and its final per-mapping verification remain mandatory in
    the text pipeline.  Registering the old reviewed interval as a boundary
    owner would instead force a new cut to preserve the old cut's lead/tail,
    allowing stale delivery geometry to override the current candidate.
    """

    _ = spec, durations
    return []


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
            ):
                continue
            overlap_ms = (
                min(end, story_end_ms) - max(start, story_start_ms)
            )
            if overlap_ms <= 0:
                row["boundary_required"] = False
                row["boundary_owner_rejection"] = (
                    "OUTSIDE_IMMUTABLE_STORY_SCOPE"
                )
                continue
            if not story_start_ms <= start < end <= story_end_ms:
                row["boundary_required"] = False
                row["boundary_owner_rejection"] = (
                    "STRADDLES_IMMUTABLE_STORY_SCOPE"
                )
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


def _redelivery_baseline_tail_rel_ms(
    spec: Mapping[str, object],
    *,
    last_piece_start_ms: int,
    prior_piece_duration_ms: int,
) -> int | None:
    """v2 baseline 覆盖终点换算到跨 piece 交付轴（头部恒等锚的对偶）。"""

    config = spec.get("subtitle_redelivery_baseline")
    if not isinstance(config, Mapping):
        return None
    if config.get("schema_version") != "subtitle-redelivery-baseline.v2":
        return None
    end = config.get("absolute_source_end_ms")
    if isinstance(end, bool) or not isinstance(end, int):
        return None
    return prior_piece_duration_ms + (int(end) - last_piece_start_ms)


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
    owner_eligibility_scope = candidate_boundary_owner_scope(
        spec=spec,
        durations=durations,
    )
    story_start_ms = int(owner_eligibility_scope["story_start_ms"])
    immutable_story_end_ms = int(
        owner_eligibility_scope["story_end_ms"]
    )
    required_boundary_owners.extend(
        freeze_story_chat_boundary_owners(
            chat_authority_audit,
            story_start_ms=story_start_ms,
            story_end_ms=immutable_story_end_ms,
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
        boundary_end_mode=str(
            spec.get("given_end_mode") or "semantic_lower_bound"
        ),
        baseline_tail_cap_ms=_redelivery_baseline_tail_rel_ms(
            spec,
            last_piece_start_ms=last_piece_start_ms,
            prior_piece_duration_ms=prior_piece_duration_ms,
        ),
    )
    spec["boundary_search_scope"] = boundary_search_scope
    boundary_target_ms = int(boundary_search_scope["review_target_ms"])
    frozen_contract: dict[str, object] = {
        "schema_version": "frozen-boundary-owner-contract.v1",
        "status": "FROZEN",
        "story_start_ms": story_start_ms,
        "story_end_ms": immutable_story_end_ms,
        "boundary_review_target_ms": boundary_target_ms,
        "owner_discovery_end_ms": immutable_story_end_ms,
        "required_owner_count": len(required_boundary_owners),
        "owners": required_boundary_owners,
        "owner_eligibility_scope": owner_eligibility_scope,
        "owner_set_sha256": _canonical_sha256(
            _normalized_owner_set(required_boundary_owners)
        ),
        "deterministic_owner_set_sha256": deterministic_owner_set_sha256(
            required_boundary_owners
        ),
        "boundary_search_scope": boundary_search_scope,
    }
    frozen_contract["contract_sha256"] = (
        frozen_boundary_owner_contract_sha256(frozen_contract)
    )
    expected_retry_contract = spec.get(
        "boundary_retry_frozen_owner_contract"
    )
    if expected_retry_contract is not None:
        expected = validate_frozen_boundary_owner_contract(
            expected_retry_contract
        )
        # A widened-context retry re-derives the transcript, so ASR-matched
        # story-chat owner geometry may not reproduce bit-for-bit.  What must
        # not drift: the immutable candidate scope and every committed-truth
        # owner.  The retry's own full owner set is frozen and enforced for
        # its own boundary resolution above.
        if (
            expected.get("deterministic_owner_set_sha256")
            != frozen_contract["deterministic_owner_set_sha256"]
            or (
                expected.get("owner_eligibility_scope") or {}
            ).get("scope_sha256")
            != owner_eligibility_scope["scope_sha256"]
        ):
            raise RuntimeError("BOUNDARY_RETRY_OWNER_SET_DRIFT")
        frozen_contract["boundary_retry_owner_contract_verification"] = {
            "status": "PASS",
            "expected_contract_sha256": expected[
                "contract_sha256"
            ],
            "deterministic_owner_set_sha256": frozen_contract[
                "deterministic_owner_set_sha256"
            ],
            "first_attempt_owner_set_sha256": expected.get(
                "owner_set_sha256"
            ),
            "retry_owner_set_sha256": frozen_contract[
                "owner_set_sha256"
            ],
            "owner_eligibility_scope_sha256": (
                owner_eligibility_scope["scope_sha256"]
            ),
            "asr_derived_owner_binding": "per_attempt",
        }
        # The verification receipt is part of the new attempt's contract.
        frozen_contract["contract_sha256"] = (
            frozen_boundary_owner_contract_sha256(frozen_contract)
        )
    chat_authority_audit["frozen_boundary_owner_contract"] = (
        frozen_contract
    )
    return boundary_target_ms, boundary_search_scope
