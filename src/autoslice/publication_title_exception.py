"""Candidate-scoped publication-title policy evaluation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.source_fact_review import (
    deterministic_text_narrowing_title_policy_exception_applies,
)
from src.autoslice.title_policy import (
    TitlePolicyError,
    publish_title_policy_violations,
    validate_candidate_title_surface,
)


def candidate_title_policy_violations(
    *,
    candidate_id: str,
    title: str,
    lane: str,
    source_fact_review: object,
    enforce_automatic_style: bool = False,
) -> list[str]:
    """Apply the common policy plus the one exact sealed title exception."""

    codes = publish_title_policy_violations(
        title,
        lane=lane,
        enforce_automatic_style=enforce_automatic_style,
    )
    try:
        validate_candidate_title_surface(candidate_id, title)
    except TitlePolicyError:
        codes.append("candidate_public_or_reviewed_title_surface_conflict")
    if deterministic_text_narrowing_title_policy_exception_applies(
        source_fact_review,
        candidate_id=candidate_id,
        title=title,
    ):
        return [code for code in codes if code != "publish_title_length_out_of_bounds"]
    return codes


def upload_manifest_title_policy_violations(
    manifest: Mapping[str, object],
    title: str,
    lane: str,
) -> list[str]:
    """Resolve the attested record surface before applying the exact exception."""

    attestation = manifest.get("package_attestation")
    record_entry = attestation.get("record") if isinstance(attestation, Mapping) else None
    record_path = (
        Path(str(record_entry.get("path") or "")) if isinstance(record_entry, Mapping) else Path()
    )
    record: Mapping[str, object] = {}
    if record_path.is_file():
        try:
            loaded = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, Mapping):
            record = loaded
    story_contract = record.get("story_contract")
    publish_staging = record.get("publish_staging")
    story_source_fact = (
        story_contract.get("source_fact_review") if isinstance(story_contract, Mapping) else None
    )
    staging_source_fact = (
        publish_staging.get("source_fact_review") if isinstance(publish_staging, Mapping) else None
    )
    candidate_id = (
        str(story_contract.get("candidate_id") or "") if isinstance(story_contract, Mapping) else ""
    )
    return candidate_title_policy_violations(
        candidate_id=candidate_id,
        title=title,
        lane=lane,
        source_fact_review=(
            story_source_fact if story_source_fact == staging_source_fact else None
        ),
    )
