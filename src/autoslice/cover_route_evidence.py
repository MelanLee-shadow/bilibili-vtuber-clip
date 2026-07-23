"""Auditable cover-route decisions shared by staging and delivery gates.

Version 1 route documents only recorded the selected treatment and one reason.
Version 2 records the decision inputs, every considered treatment, and the
actual execution outcome so a screenshot failure cannot be mistaken for
authorization to generate a different cover.
"""

from __future__ import annotations

import re
from typing import Mapping


ROUTE_SCHEMA_V1 = "lidousha-cover-route-decision.v1"
ROUTE_SCHEMA_V2 = "lidousha-cover-route-decision.v2"
ROUTE_TREATMENTS = (
    "screenshot_direct",
    "screenshot_polish",
    "cpa_redraw",
)
FINAL_PARTICIPANT_VERIFICATION_SCHEMA = (
    "lidousha-cover-final-participant-verification.v1"
)
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")
_RELATION_VISUAL_RX = re.compile(
    r"联动|连麦|连线|搭档|当面对质|当面追问|追问|互相|两人|双方|"
    r"对方|她们|他们|左边的人|右边的人|霸凌|让给|"
    r"请[^，。！？]{0,12}(?:吃|喝)|脑瓜崩|收集"
)


def story_participant_ids(story_contract: object) -> list[str]:
    """Return stable participant ids without inventing identity evidence."""

    if not isinstance(story_contract, Mapping):
        return []
    values: list[str] = []
    for row in story_contract.get("participants") or []:
        if isinstance(row, Mapping):
            value = str(row.get("canonical_id") or row.get("id") or "").strip()
        elif isinstance(row, str):
            value = row.strip()
        else:
            value = ""
        if value and value not in values:
            values.append(value)
    return values


def is_hash_bound_reference_authority(reference_authority: object) -> bool:
    """Do not upgrade a bare participant list into source-frame authority."""

    return bool(
        isinstance(reference_authority, Mapping)
        and str(reference_authority.get("candidate_id") or "").strip()
        and _SHA256_RX.fullmatch(
            str(reference_authority.get("source_sha256") or "")
        )
        is not None
        and _SHA256_RX.fullmatch(
            str(reference_authority.get("reference_png_sha256") or "")
        )
        is not None
    )


def source_visible_participant_ids(reference_authority: object) -> list[str]:
    """Return only identities attested by the hash-bound frame authority."""

    if not is_hash_bound_reference_authority(reference_authority):
        return []
    assert isinstance(reference_authority, Mapping)
    values: list[str] = []
    for raw in reference_authority.get("visible_participant_ids") or []:
        value = str(raw or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def relationship_semantic_evidence(
    story_contract: object,
    *,
    title: str = "",
    cover_text: str = "",
) -> list[str]:
    """Explain why this cover must visually preserve a multi-person relation.

    Geometry and motion are intentionally absent here.  They can rank a
    composition only after source-bound identity evidence establishes who is
    actually present.
    """

    if (
        not isinstance(story_contract, Mapping)
        or story_contract.get("relation_state") != "CONFIRMED"
        or len(story_participant_ids(story_contract)) < 2
    ):
        return []
    participant_surfaces: list[list[str]] = []
    for row in story_contract.get("participants") or []:
        if not isinstance(row, Mapping):
            continue
        raw_aliases = row.get("surfaces")
        aliases = (
            raw_aliases
            if isinstance(raw_aliases, (list, tuple, set))
            else []
        )
        surfaces: list[str] = []
        for raw in (
            row.get("canonical_id"),
            row.get("display_name"),
            *aliases,
        ):
            value = str(raw or "").strip()
            if value and value not in surfaces:
                surfaces.append(value)
        if surfaces:
            participant_surfaces.append(surfaces)
    texts = (
        ("selection_hook", str(story_contract.get("selection_hook") or "")),
        ("title", str(title or "")),
        ("cover_text", str(cover_text or "")),
    )
    evidence: list[str] = []
    for label, text in texts:
        if not text:
            continue
        if _RELATION_VISUAL_RX.search(text):
            evidence.append(f"{label}:EXPLICIT_RELATION_LANGUAGE")
        participant_mentions = sum(
            any(surface in text for surface in surfaces)
            for surfaces in participant_surfaces
        )
        if participant_mentions >= 2:
            evidence.append(f"{label}:MULTI_PARTICIPANT_CO_MENTION")
    return list(dict.fromkeys(evidence))


def relationship_source_participants_verified(
    route_decision: object,
) -> bool:
    """Return whether every relationship participant has source authority."""

    if not isinstance(route_decision, Mapping):
        return False
    required = route_decision.get("required_participant_ids")
    visible = route_decision.get("source_visible_participant_ids")
    return bool(
        isinstance(required, list)
        and len(required) >= 2
        and isinstance(visible, list)
        and set(required) == set(visible)
        and route_decision.get("source_visibility_authority")
        == "HASH_BOUND_COVER_REFERENCE"
    )


def validate_final_participant_verification(
    cover_generation: Mapping[str, object],
) -> bool:
    """Validate an independent, final-cover-hash-bound identity verdict."""

    verification = cover_generation.get("final_participant_verification")
    if not isinstance(verification, Mapping):
        return False
    visible = verification.get("visible_participant_ids")
    required = (
        cover_generation.get("route_decision", {}).get(
            "required_participant_ids"
        )
        if isinstance(cover_generation.get("route_decision"), Mapping)
        else None
    )
    return bool(
        verification.get("schema_version")
        == FINAL_PARTICIPANT_VERIFICATION_SCHEMA
        and verification.get("status") == "PASS"
        and str(verification.get("authority") or "").strip()
        and isinstance(visible, list)
        and len(visible) == len(set(visible))
        and all(isinstance(value, str) and value.strip() for value in visible)
        and isinstance(required, list)
        and set(required) == set(visible)
        and _SHA256_RX.fullmatch(
            str(verification.get("final_cover_sha256") or "")
        )
        is not None
        and verification.get("final_cover_sha256")
        == cover_generation.get("final_cover_sha256")
    )


def _alternatives(
    selected_treatment: str, selected_rationale: str
) -> list[dict[str, object]]:
    selected_explanations = {
        "screenshot_direct": (
            "the selected source frame is strong enough to preserve as the final "
            "visual without image generation"
        ),
        "screenshot_polish": (
            "the real source moment is usable but benefits from a bounded "
            "identity-preserving cleanup"
        ),
        "cpa_redraw": (
            "the available source frame is not a sufficiently strong or verified "
            "cover composition"
        ),
    }
    rejected_explanations = {
        "screenshot_direct": {
            "screenshot_polish": (
                "the verified source frame already carries the hook; image cleanup "
                "would add generation risk without a demonstrated need"
            ),
            "cpa_redraw": (
                "the verified source frame already carries the hook and participant "
                "relationship; a redraw would discard grounded story evidence"
            ),
        },
        "screenshot_polish": {
            "screenshot_direct": (
                "the source moment should be preserved, but the route evidence says "
                "bounded cleanup is needed before it is cover-ready"
            ),
            "cpa_redraw": (
                "the usable real moment should be preserved; full redraw is broader "
                "than the demonstrated cleanup need"
            ),
        },
        "cpa_redraw": {
            "screenshot_direct": (
                "the source-frame evidence is not strong or verified enough to ship "
                "without image generation"
            ),
            "screenshot_polish": (
                "bounded cleanup cannot repair the missing subject confidence, weak "
                "composition, or inability to express the hook"
            ),
        },
    }
    rows: list[dict[str, object]] = []
    for treatment in ROUTE_TREATMENTS:
        if treatment == selected_treatment:
            rows.append(
                {
                    "treatment": treatment,
                    "status": "SELECTED",
                    "rationale": selected_rationale,
                    "rejected_reason": None,
                }
            )
            continue
        rows.append(
            {
                "treatment": treatment,
                "status": "REJECTED",
                "rationale": selected_explanations[treatment],
                "rejected_reason": (
                    rejected_explanations[selected_treatment][treatment]
                    + f"; selected evidence: {selected_rationale}"
                ),
            }
        )
    return rows


def build_cover_route_decision(
    *,
    selected_treatment: str,
    selected_rationale: str,
    story_contract: object,
    reference_authority: object,
    decision_inputs: Mapping[str, object],
    title: str = "",
    cover_text: str = "",
) -> dict[str, object]:
    if selected_treatment not in ROUTE_TREATMENTS:
        raise ValueError(f"unsupported cover treatment: {selected_treatment}")
    rationale = str(selected_rationale or "").strip()
    if not rationale:
        raise ValueError("cover route rationale must not be empty")
    alternatives = _alternatives(selected_treatment, rationale)
    generation_planned = selected_treatment in {
        "screenshot_polish",
        "cpa_redraw",
    }
    semantic_evidence = relationship_semantic_evidence(
        story_contract,
        title=title,
        cover_text=cover_text,
    )
    reference_is_hash_bound = is_hash_bound_reference_authority(
        reference_authority
    )
    return {
        "schema_version": ROUTE_SCHEMA_V2,
        "selection_policy": "lidousha-cover-treatment-router.v2",
        "selected_treatment": selected_treatment,
        # ``reason`` remains for legacy report readers; v2 makes its authority
        # explicit with selected_rationale.
        "reason": rationale,
        "selected_rationale": rationale,
        **dict(decision_inputs),
        "required_participant_ids": story_participant_ids(story_contract),
        "source_visible_participant_ids": source_visible_participant_ids(
            reference_authority
        ),
        "source_visibility_authority": (
            "HASH_BOUND_COVER_REFERENCE"
            if reference_is_hash_bound
            else "NO_IDENTITY_AUTHORITY"
        ),
        "relationship_visual_required": bool(semantic_evidence),
        "relationship_semantic_evidence": semantic_evidence,
        "final_visible_participant_ids": [],
        "final_visibility_authority": (
            "PENDING_RELATION_VISUAL_VERIFICATION"
            if semantic_evidence
            else "NOT_REQUIRED"
        ),
        "image_generation_planned": generation_planned,
        "image_generation_attempted": False,
        "image_generation_used": False,
        "execution_status": "PENDING",
        "actual_treatment": None,
        "alternatives_considered": list(ROUTE_TREATMENTS),
        "alternatives": alternatives,
        "rejected_alternatives": [
            {
                "treatment": row["treatment"],
                "rejected_reason": row["rejected_reason"],
            }
            for row in alternatives
            if row["status"] == "REJECTED"
        ],
    }


def record_cover_route_execution(
    cover_generation: dict[str, object],
    *,
    actual_treatment: str | None,
    execution_status: str,
    image_generation_attempted: bool,
    image_generation_used: bool,
    detail: str | None = None,
    final_participant_verification: Mapping[str, object] | None = None,
) -> None:
    """Record the actual lane without changing the selected authorization."""

    route = cover_generation.get("route_decision")
    if isinstance(route, dict) and route.get("schema_version") == ROUTE_SCHEMA_V2:
        route.update(
            {
                "actual_treatment": actual_treatment,
                "execution_status": execution_status,
                "image_generation_attempted": bool(image_generation_attempted),
                "image_generation_used": bool(image_generation_used),
            }
        )
        if detail:
            route["execution_detail"] = str(detail)
        if (
            actual_treatment in {"screenshot_direct", "screenshot_polish"}
            and execution_status in {"READY", "READY_DEGRADED"}
        ):
            route["final_visible_participant_ids"] = list(
                route.get("source_visible_participant_ids") or []
            )
            route["final_visibility_authority"] = (
                "SOURCE_PRESERVING_SCREENSHOT_PIPELINE"
            )
        elif isinstance(final_participant_verification, Mapping):
            cover_generation["final_participant_verification"] = dict(
                final_participant_verification
            )
            route["final_visible_participant_ids"] = list(
                final_participant_verification.get(
                    "visible_participant_ids"
                )
                or []
            )
            route["final_visibility_authority"] = str(
                final_participant_verification.get("authority") or ""
            )
    planned = bool(
        isinstance(route, Mapping) and route.get("image_generation_planned") is True
    )
    cover_generation.update(
        {
            "image_generation_planned": planned,
            "image_generation_attempted": bool(image_generation_attempted),
            "image_generation_used": bool(image_generation_used),
        }
    )


def _valid_alternatives(route: Mapping[str, object]) -> bool:
    alternatives = route.get("alternatives")
    if not isinstance(alternatives, list) or len(alternatives) != len(
        ROUTE_TREATMENTS
    ):
        return False
    by_treatment: dict[str, Mapping[str, object]] = {}
    for row in alternatives:
        if not isinstance(row, Mapping):
            return False
        treatment = str(row.get("treatment") or "")
        if treatment in by_treatment:
            return False
        by_treatment[treatment] = row
    if set(by_treatment) != set(ROUTE_TREATMENTS):
        return False
    selected = str(route.get("selected_treatment") or "")
    for treatment, row in by_treatment.items():
        if treatment == selected:
            if row.get("status") != "SELECTED" or row.get(
                "rejected_reason"
            ) is not None:
                return False
        elif row.get("status") != "REJECTED" or not str(
            row.get("rejected_reason") or ""
        ).strip():
            return False
    rejected = route.get("rejected_alternatives")
    if not isinstance(rejected, list) or len(rejected) != 2:
        return False
    rejected_treatments = {
        str(row.get("treatment") or "")
        for row in rejected
        if isinstance(row, Mapping)
        and str(row.get("rejected_reason") or "").strip()
    }
    return rejected_treatments == set(ROUTE_TREATMENTS) - {selected}


def validate_cover_route_decision(
    cover_generation: Mapping[str, object], *, allow_legacy_v1: bool = True
) -> bool:
    """Validate v2 rigorously while retaining read compatibility for v1."""

    route = cover_generation.get("route_decision")
    if not isinstance(route, Mapping):
        return False
    schema = route.get("schema_version")
    selected = str(route.get("selected_treatment") or "")
    reason = str(route.get("reason") or "").strip()
    if schema == ROUTE_SCHEMA_V1:
        return bool(
            allow_legacy_v1 and selected in ROUTE_TREATMENTS and reason
        )
    if schema != ROUTE_SCHEMA_V2:
        return False
    if (
        selected not in ROUTE_TREATMENTS
        or not reason
        or str(route.get("selected_rationale") or "").strip() != reason
        or route.get("selection_policy") != "lidousha-cover-treatment-router.v2"
        or not _valid_alternatives(route)
    ):
        return False
    for key in (
        "image_generation_planned",
        "image_generation_attempted",
        "image_generation_used",
    ):
        if not isinstance(route.get(key), bool):
            return False
    expected_planned = selected in {"screenshot_polish", "cpa_redraw"}
    if route.get("image_generation_planned") is not expected_planned:
        return False
    if (
        cover_generation.get("image_generation_planned") is not expected_planned
        or cover_generation.get("image_generation_attempted")
        is not route.get("image_generation_attempted")
        or cover_generation.get("image_generation_used")
        is not route.get("image_generation_used")
    ):
        return False
    for key in ("required_participant_ids", "source_visible_participant_ids"):
        values = route.get(key)
        if (
            not isinstance(values, list)
            or len(values) != len(set(values))
            or not all(isinstance(value, str) and value.strip() for value in values)
        ):
            return False
    final_visible = route.get("final_visible_participant_ids")
    semantic_evidence = route.get("relationship_semantic_evidence")
    if (
        not isinstance(route.get("relationship_visual_required"), bool)
        or not isinstance(semantic_evidence, list)
        or len(semantic_evidence) != len(set(semantic_evidence))
        or not all(
            isinstance(value, str) and value.strip()
            for value in semantic_evidence
        )
        or not isinstance(final_visible, list)
        or len(final_visible) != len(set(final_visible))
        or not all(
            isinstance(value, str) and value.strip()
            for value in final_visible
        )
        or not str(route.get("final_visibility_authority") or "").strip()
    ):
        return False
    story_contract = cover_generation.get("story_contract")
    if (
        route.get("required_participant_ids")
        or route.get("relationship_visual_required") is True
    ) and not isinstance(story_contract, Mapping):
        return False
    if isinstance(story_contract, Mapping):
        if route.get("required_participant_ids") != story_participant_ids(
            story_contract
        ):
            return False
        authority = story_contract.get("cover_reference_authority")
        if route.get(
            "source_visible_participant_ids"
        ) != source_visible_participant_ids(authority):
            return False
        expected_semantic_evidence = relationship_semantic_evidence(
            story_contract,
            title=str(cover_generation.get("title") or ""),
            cover_text=str(cover_generation.get("cover_text") or ""),
        )
        if (
            route.get("relationship_semantic_evidence")
            != expected_semantic_evidence
            or route.get("relationship_visual_required")
            is not bool(expected_semantic_evidence)
        ):
            return False
    relationship_required = (
        route.get("relationship_visual_required") is True
    )
    if relationship_required and not relationship_source_participants_verified(
        route
    ):
        return False
    actual = route.get("actual_treatment")
    execution_status = str(route.get("execution_status") or "")
    method = str(cover_generation.get("method") or "")
    origin = str(cover_generation.get("cover_origin") or "")
    used = route.get("image_generation_used") is True
    attempted = route.get("image_generation_attempted") is True
    if used and not attempted:
        return False
    if actual == "cpa_redraw":
        if relationship_required and (
            not validate_final_participant_verification(cover_generation)
            or not set(route["required_participant_ids"])
            <= set(route["final_visible_participant_ids"])
            or route.get("final_visibility_authority")
            != str(
                (
                    cover_generation.get("final_participant_verification")
                    or {}
                ).get("authority")
                or ""
            )
        ):
            return False
        return bool(
            selected == "cpa_redraw"
            and execution_status == "READY"
            and method == "images.edit"
            and origin == "AI_REDRAW"
            and used
        )
    if actual == "screenshot_polish":
        if relationship_required and not set(
            route["required_participant_ids"]
        ) <= set(route["final_visible_participant_ids"]):
            return False
        return bool(
            selected == "screenshot_polish"
            and execution_status == "READY"
            and method == "screenshot_polish"
            and origin == "SOURCE_SCREENSHOT_AI_POLISH"
            and used
        )
    if actual == "screenshot_direct":
        if relationship_required and not set(
            route["required_participant_ids"]
        ) <= set(route["final_visible_participant_ids"]):
            return False
        if (
            method != "screenshot_direct"
            or origin != "SOURCE_SCREENSHOT"
            or used
        ):
            return False
        if selected == "screenshot_direct":
            return execution_status == "READY" and not attempted
        return bool(
            selected == "screenshot_polish"
            and execution_status == "READY_DEGRADED"
            and str(route.get("execution_detail") or "").strip()
            and isinstance(cover_generation.get("screenshot_polish"), Mapping)
            and cover_generation["screenshot_polish"].get("status")
            == "DEGRADED_TO_DIRECT"
        )
    return False
