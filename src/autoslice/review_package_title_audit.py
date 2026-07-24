"""Fail-closed audit for recovery packages that retain a public title.

Recovery may preserve the title of an existing same-BV publication.  That is
not an ordinary generated title: the package has to carry one replayable
public-verification authority across the manifest, record, staging draft, and
publish draft.  Keep this narrow contract outside the large package auditor so
the title-specific closure remains independently testable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_title_authority,
)


@dataclass(frozen=True)
class ReviewPackageTitleIssue:
    """One title-authority violation surfaced by the package auditor."""

    code: str
    path: Path | None
    detail: str = ""


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
) -> tuple[ReviewPackageTitleIssue, ...]:
    """Bind a preserved public title across every packaged surface.

    A normal automatic or Ivan-manual title has no recovery authority and is
    deliberately outside this gate.  If any surface introduces the authority,
    all four surfaces and the packaged publish-byte hash are required.
    """

    record_authority = record.get("recovery_title_authority")
    item_authority = item.get("recovery_title_authority")
    staging_authority = publish_staging.get("recovery_title_authority")
    publish = _load_json(publish_path) if publish_path else {}
    publish_authority = publish.get("recovery_title_authority")
    present = [
        value is not None
        for value in (
            record_authority,
            item_authority,
            staging_authority,
            publish_authority,
        )
    ]
    issue_path = record_path or publish_path
    if not any(present):
        return ()
    if not all(present):
        return (
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLIC_TITLE_AUTHORITY_SURFACE_MISSING",
                issue_path,
            ),
        )
    final_title = (
        str(publish.get("title") or "")
        or str(publish_staging.get("title") or "")
        or item_title
    )
    try:
        validated = validate_recovery_title_authority(
            record_authority,
            candidate_id=item_candidate_id,
            expected_title=final_title,
        )
    except RecoveryTitleAuthorityError as exc:
        return (
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLIC_TITLE_AUTHORITY_INVALID",
                issue_path,
                str(exc),
            ),
        )
    issues: list[ReviewPackageTitleIssue] = []
    if (
        item_authority != validated
        or staging_authority != validated
        or publish_authority != validated
        or item_title != validated["title"]
        or publish_staging.get("title") != validated["title"]
        or publish.get("title") != validated["title"]
    ):
        issues.append(
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLIC_TITLE_AUTHORITY_BINDING_DRIFT",
                issue_path,
            )
        )
    artifact_hashes = record.get("artifact_hashes")
    if (
        publish_path is None
        or not publish_path.is_file()
        or not isinstance(artifact_hashes, dict)
        or artifact_hashes.get("publish_draft_sha256")
        != "sha256:" + _sha256_file(publish_path)
    ):
        issues.append(
            ReviewPackageTitleIssue(
                "RECOVERY_PUBLIC_TITLE_PUBLISH_HASH_DRIFT",
                publish_path or record_path,
            )
        )
    return tuple(issues)
