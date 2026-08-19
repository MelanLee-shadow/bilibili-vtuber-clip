"""Candidate-scoped entity surface gates for title and visible cover text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .story_contract import audit_story_artifact
from .publication_title_exception import candidate_title_policy_violations
from .title_policy import (
    TitlePolicyError,
    validate_candidate_title_surface,
)


@dataclass(frozen=True)
class TitleGateOutcome:
    violations: tuple[str, ...]
    authority_error: str | None
    authority_status: str
    story_audit: dict[str, object] | None
    entity_projection_audit: dict[str, object] | None


def evaluate_candidate_title_gates(
    *,
    candidate_id: str,
    title: str,
    lane: str,
    enforce_automatic_style: bool,
    prior_violations: Sequence[str],
    prior_authority_error: str | None,
    prior_authority_status: str,
    story_contract: Mapping[str, object] | None,
    source_fact_review: Mapping[str, object] | None = None,
) -> TitleGateOutcome:
    """Apply candidate projection, shared title policy, and story policy in order."""

    violations = list(prior_violations)
    authority_error = prior_authority_error
    authority_status = prior_authority_status
    entity_audit = None
    try:
        entity_audit = validate_candidate_title_surface(candidate_id, title)
    except TitlePolicyError as exc:
        violations.append("candidate_entity_projection_title_conflict")
        authority_error = f"candidate_entity_projection_failed:{exc}"
        authority_status = "BLOCKED_ENTITY_SURFACE_PROJECTION"

    common = candidate_title_policy_violations(
        candidate_id=candidate_id,
        title=title,
        lane=lane,
        source_fact_review=source_fact_review,
        enforce_automatic_style=enforce_automatic_style,
    )
    violations.extend(code for code in common if code not in violations)
    source_fact_blocked = authority_status == "BLOCKED_SOURCE_FACT_REVIEW"
    if common and not source_fact_blocked:
        authority_error = "publish_title_policy_violation:" + ",".join(common)
        authority_status = "BLOCKED_PUBLISH_TITLE_POLICY"

    story_audit = None
    if isinstance(story_contract, dict):
        story_audit = audit_story_artifact(
            title,
            story_contract=story_contract,
            artifact_kind="title",
        )
        if story_audit["status"] != "PASS":
            story_codes = sorted(
                {
                    str(row.get("reason_code") or "STORY_CONTRACT_TITLE_FAILED")
                    for row in story_audit["violations"]
                    if isinstance(row, dict)
                }
            )
            violations.extend(code for code in story_codes if code not in violations)
            if not source_fact_blocked:
                authority_error = "story_contract_violation:" + ",".join(story_codes)
                authority_status = "BLOCKED_STORY_CONTRACT"
    return TitleGateOutcome(
        violations=tuple(violations),
        authority_error=authority_error,
        authority_status=authority_status,
        story_audit=story_audit,
        entity_projection_audit=entity_audit,
    )


def enforce_candidate_cover_projection(
    *,
    candidate_id: str,
    cover_result: dict[str, object],
    projection_required: bool,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Fail closed when rendered cover text violates a bound candidate surface."""

    if (
        str(cover_result.get("status")) not in {"AI_COVER_READY", "REUSED_COVER"}
        or not projection_required
    ):
        return cover_result, None
    generation = cover_result.get("cover_generation")
    rendered_lines = generation.get("rendered_lines") if isinstance(generation, Mapping) else None
    try:
        if not (
            isinstance(rendered_lines, list)
            and rendered_lines
            and all(isinstance(line, str) and line.strip() for line in rendered_lines)
        ):
            raise TitlePolicyError("candidate cover entity projection has no exact rendered text")
        audit = validate_candidate_title_surface(
            candidate_id,
            "\n".join(rendered_lines),
            artifact_kind="cover",
        )
    except TitlePolicyError as exc:
        blocked_generation = dict(generation) if isinstance(generation, Mapping) else {}
        blocked_generation["entity_projection_audit"] = {
            "schema_version": "candidate-entity-surface-audit.v1",
            "status": "FAIL",
            "candidate_id": candidate_id,
            "artifact_kind": "cover",
            "surface_type": "title_cover",
            "detail": str(exc),
        }
        return (
            {
                **cover_result,
                "status": "BLOCKED_ENTITY_SURFACE_PROJECTION",
                "cover_path": None,
                "cover_generation": blocked_generation,
                "reason_codes": [
                    *list(cover_result.get("reason_codes") or []),
                    "COVER_ENTITY_SURFACE_PROJECTION_FAILED",
                ],
            },
            None,
        )
    if isinstance(generation, dict) and audit is not None:
        generation["entity_projection_audit"] = audit
    return cover_result, audit
