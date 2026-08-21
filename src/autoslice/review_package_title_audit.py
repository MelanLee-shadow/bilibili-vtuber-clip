"""Fail-closed audit for exact same-BV publication authorities.

Each recovery item carries one replayable registry authority across the
manifest, record, staging draft, and publish draft.  It binds both the final
title source and the existing BV identity.  Keep this narrow contract outside
the large package auditor so the closure remains independently testable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.autoslice.candidate_public_text_surface_authority import (
    CandidatePublicTextSurfaceAuthorityError,
    consume_candidate_public_text_surface_authority,
    load_candidate_public_text_surface_authority,
)
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)


@dataclass(frozen=True)
class ReviewPackageTitleIssue:
    """One title-authority violation surfaced by the package auditor."""

    code: str
    path: Path | None
    detail: str = ""


def recovery_publication_authority_contract(
    manifest: dict[str, Any],
    items: list[Any],
) -> tuple[
    dict[str, Any],
    bool,
    tuple[ReviewPackageTitleIssue, ...],
]:
    """Resolve the exact candidate map without trusting item self-report."""

    authorities = manifest.get("recovery_publication_authorities_by_candidate")
    selection_contract = manifest.get("selection_contract")
    required = bool(
        manifest.get("run_mode") == "RECOVERY_REVIEW"
        and isinstance(selection_contract, dict)
        and selection_contract.get("mode") == "EXACT_CANDIDATE_SET_NO_BACKFILL"
    )
    issues: list[ReviewPackageTitleIssue] = []
    if required and not isinstance(authorities, dict):
        issues.append(
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLICATION_AUTHORITY_MAP_MISSING",
                None,
            )
        )
    if not isinstance(authorities, dict):
        authorities = {}
    item_candidate_ids = {
        str(item.get("candidate_id") or "")
        for item in items
        if isinstance(item, dict) and str(item.get("candidate_id") or "")
    }
    if required and set(authorities) != item_candidate_ids:
        issues.append(
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLICATION_AUTHORITY_MAP_SET_MISMATCH",
                None,
                (f"authority={sorted(authorities)}; items={sorted(item_candidate_ids)}"),
            )
        )
    return authorities, required, tuple(issues)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_recovery_title_authority(
    *,
    item: dict[str, Any],
    item_candidate_id: str,
    item_title: str,
    publish_path: Path | None,
    record_path: Path | None,
    record: dict[str, Any],
    publish_staging: dict[str, Any],
    expected_authority: object,
) -> tuple[ReviewPackageTitleIssue, ...]:
    """Bind the required same-BV target/title contract across every surface."""

    record_authority = record.get("recovery_publication_authority")
    item_authority = item.get("recovery_publication_authority")
    staging_authority = publish_staging.get("recovery_publication_authority")
    publish = _load_json(publish_path) if publish_path else {}
    publish_authority = publish.get("recovery_publication_authority")
    present = [
        value is not None
        for value in (
            expected_authority,
            record_authority,
            item_authority,
            staging_authority,
            publish_authority,
        )
    ]
    issue_path = record_path or publish_path
    if not all(present):
        return (
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLICATION_AUTHORITY_SURFACE_MISSING",
                issue_path,
            ),
        )
    final_title = (
        str(publish.get("title") or "") or str(publish_staging.get("title") or "") or item_title
    )
    try:
        validated = validate_recovery_publication_authority(
            expected_authority,
            candidate_id=item_candidate_id,
            expected_final_title=final_title,
        )
    except RecoveryTitleAuthorityError as exc:
        return (
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLICATION_AUTHORITY_INVALID",
                issue_path,
                str(exc),
            ),
        )
    issues: list[ReviewPackageTitleIssue] = []
    if (
        record_authority != validated
        or item_authority != validated
        or staging_authority != validated
        or publish_authority != validated
        or item_title != final_title
        or publish_staging.get("title") != final_title
        or publish.get("title") != final_title
    ):
        issues.append(
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLICATION_AUTHORITY_BINDING_DRIFT",
                issue_path,
            )
        )
    artifact_hashes = record.get("artifact_hashes")
    if (
        publish_path is None
        or not publish_path.is_file()
        or not isinstance(artifact_hashes, dict)
        or artifact_hashes.get("publish_draft_sha256") != "sha256:" + _sha256_file(publish_path)
    ):
        issues.append(
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLICATION_PUBLISH_HASH_DRIFT",
                publish_path or record_path,
            )
        )
    return tuple(issues)


def audit_recovery_publication_surfaces(
    *,
    item: dict[str, Any],
    item_candidate_id: str,
    item_title: str,
    publish_path: Path | None,
    record_path: Path | None,
    record: dict[str, Any],
    publish_staging: dict[str, Any],
    expected_authority: object,
    required: bool,
) -> tuple[ReviewPackageTitleIssue, ...]:
    """Run the surface gate only for exact recovery or an introduced surface."""

    public_issues = audit_candidate_public_text_surfaces(
        candidate_id=item_candidate_id,
        item_title=item_title,
        publish_path=publish_path,
        record_path=record_path,
        record=record,
        publish_staging=publish_staging,
    )
    surface_present = any(
        value is not None
        for value in (
            item.get("recovery_publication_authority"),
            record.get("recovery_publication_authority"),
            publish_staging.get("recovery_publication_authority"),
        )
    )
    if not required and not surface_present:
        return public_issues
    recovery_issues = audit_recovery_title_authority(
        item=item,
        item_candidate_id=item_candidate_id,
        item_title=item_title,
        publish_path=publish_path,
        record_path=record_path,
        record=record,
        publish_staging=publish_staging,
        expected_authority=(expected_authority or item.get("recovery_publication_authority")),
    )
    return (*public_issues, *recovery_issues)


def audit_candidate_public_text_surfaces(
    *,
    candidate_id: str,
    item_title: str,
    publish_path: Path | None,
    record_path: Path | None,
    record: dict[str, Any],
    publish_staging: dict[str, Any],
) -> tuple[ReviewPackageTitleIssue, ...]:
    """Rebuild the exact public-text consumption across a current package."""

    issue_path = record_path or publish_path
    try:
        authority = load_candidate_public_text_surface_authority(candidate_id)
    except CandidatePublicTextSurfaceAuthorityError as exc:
        return (
            ReviewPackageTitleIssue(
                "CANDIDATE_PUBLIC_TEXT_AUTHORITY_INVALID", issue_path, str(exc)
            ),
        )
    if authority is None:
        return ()
    publish = _load_json(publish_path) if publish_path else {}
    story_contract = record.get("story_contract")
    try:
        if not isinstance(story_contract, dict):
            raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_STORY_CONTRACT_REQUIRED")
        expected = consume_candidate_public_text_surface_authority(
            authority,
            candidate_id=candidate_id,
            selection_hook=str(story_contract.get("selection_hook") or ""),
            story_contract=story_contract,
        )
        for value in (
            item_title,
            str(publish_staging.get("title") or ""),
            str(publish.get("title") or ""),
        ):
            authority.require_artifact_text(artifact_kind="publication", text=value)
        generations = (
            publish_staging.get("cover_generation"),
            publish.get("cover_generation"),
        )
        for generation in generations:
            lines = generation.get("rendered_lines") if isinstance(generation, dict) else None
            if not (
                isinstance(lines, list)
                and lines
                and all(isinstance(line, str) and line.strip() for line in lines)
            ):
                raise CandidatePublicTextSurfaceAuthorityError(
                    "PUBLIC_TEXT_RENDERED_COVER_TEXT_REQUIRED"
                )
            authority.require_artifact_text(artifact_kind="cover", text="\n".join(lines))
    except CandidatePublicTextSurfaceAuthorityError as exc:
        return (
            ReviewPackageTitleIssue("CANDIDATE_PUBLIC_TEXT_SURFACE_INVALID", issue_path, str(exc)),
        )
    staging_receipt = publish_staging.get("public_text_surface_authority_consumption")
    publish_receipt = publish.get("public_text_surface_authority_consumption")
    expected_source = authority.title_source
    if not (
        staging_receipt == publish_receipt == expected
        and publish_staging.get("title_source") == publish.get("title_source") == expected_source
        and publish_staging.get("upload_enabled") is False
        and publish.get("upload_enabled") is False
        and expected.get("upload_authorized") is False
        and expected.get("registry_hold_released") is False
    ):
        return (ReviewPackageTitleIssue("CANDIDATE_PUBLIC_TEXT_CONSUMPTION_DRIFT", issue_path),)
    return ()
