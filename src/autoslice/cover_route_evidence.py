"""Auditable cover-route decisions shared by staging and delivery gates.

Version 1 route documents only recorded the selected treatment and one reason.
Version 2 records the decision inputs, every considered treatment, and the
actual execution outcome so a screenshot failure cannot be mistaken for
authorization to generate a different cover.
"""

from __future__ import annotations

from typing import Mapping


ROUTE_SCHEMA_V1 = "lidousha-cover-route-decision.v1"
ROUTE_SCHEMA_V2 = "lidousha-cover-route-decision.v2"
ROUTE_TREATMENTS = (
    "screenshot_direct",
    "screenshot_polish",
    "cpa_redraw",
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


def source_visible_participant_ids(reference_authority: object) -> list[str]:
    """Return only identities attested by the hash-bound frame authority."""

    if not isinstance(reference_authority, Mapping):
        return []
    values: list[str] = []
    for raw in reference_authority.get("visible_participant_ids") or []:
        value = str(raw or "").strip()
        if value and value not in values:
            values.append(value)
    return values


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
            if isinstance(reference_authority, Mapping)
            else "NO_IDENTITY_AUTHORITY"
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
    story_contract = cover_generation.get("story_contract")
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
    actual = route.get("actual_treatment")
    execution_status = str(route.get("execution_status") or "")
    method = str(cover_generation.get("method") or "")
    origin = str(cover_generation.get("cover_origin") or "")
    used = route.get("image_generation_used") is True
    attempted = route.get("image_generation_attempted") is True
    if used and not attempted:
        return False
    if actual == "cpa_redraw":
        return bool(
            selected == "cpa_redraw"
            and execution_status == "READY"
            and method == "images.edit"
            and origin == "AI_REDRAW"
            and used
        )
    if actual == "screenshot_polish":
        return bool(
            selected == "screenshot_polish"
            and execution_status == "READY"
            and method == "screenshot_polish"
            and origin == "SOURCE_SCREENSHOT_AI_POLISH"
            and used
        )
    if actual == "screenshot_direct":
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
