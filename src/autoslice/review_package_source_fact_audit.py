from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cover_only_audit_scope import (
    CoverOnlyAuditScopeError,
    validate_scope as validate_cover_only_audit_scope,
)
from .review_package_portable_evidence import (
    contained_package_artifact,
    rebuild_package_speaker_evidence,
)
from .source_fact_review import validate_source_fact_review


@dataclass(frozen=True)
class SourceFactAuditIssue:
    code: str
    path: Path | None
    detail: str | None = None


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def audit_story_source_fact_receipt(
    *,
    root: Path,
    manifest: dict[str, Any],
    item: dict[str, Any],
    subtitle_path: Path | None,
    publish_path: Path | None,
    record_path: Path | None,
    record: dict[str, Any],
    story_contract: dict[str, Any],
    artifact_title: str,
    final_transcript: str,
) -> tuple[SourceFactAuditIssue, ...]:
    issues: list[SourceFactAuditIssue] = []
    publish_staging = (
        record.get("publish_staging") if isinstance(record.get("publish_staging"), dict) else {}
    )
    publish_document = _load_json(publish_path)
    source_fact_receipts = [
        story_contract.get("source_fact_review"),
        publish_staging.get("source_fact_review"),
        publish_document.get("source_fact_review"),
    ]
    scope_reused = False
    if all(value is None for value in source_fact_receipts):
        raw_scope = item.get("cover_only_audit_scope")
        try:
            scope_path = (
                contained_package_artifact(
                    root,
                    raw_scope,
                    label="cover-only audit scope",
                )
                if raw_scope is not None
                else None
            )
        except ValueError:
            scope_path = None
        scope_payload = _load_json(scope_path)
        if raw_scope is not None:
            try:
                if not scope_payload:
                    raise CoverOnlyAuditScopeError(
                        "cover-only audit scope is missing or unreadable"
                    )
                validate_cover_only_audit_scope(
                    scope_payload,
                    package_root=root,
                    item=item,
                )
            except CoverOnlyAuditScopeError as exc:
                issues.append(
                    SourceFactAuditIssue(
                        "COVER_ONLY_AUDIT_SCOPE_INVALID",
                        scope_path or record_path,
                        str(exc),
                    )
                )
            else:
                scope_reused = True

    rebuilt_speaker_evidence: dict[str, object] | None = None
    speaker_evidence_error: str | None = None
    if (
        not scope_reused
        and all(isinstance(value, dict) for value in source_fact_receipts)
        and source_fact_receipts[0] == source_fact_receipts[1] == source_fact_receipts[2]
    ):
        try:
            rebuilt_speaker_evidence = rebuild_package_speaker_evidence(
                root=root,
                item=item,
                record=record,
                subtitle_path=subtitle_path,
            )
        except (OSError, ValueError) as exc:
            speaker_evidence_error = str(exc)
            issues.append(
                SourceFactAuditIssue(
                    "SOURCE_FACT_SPEAKER_EVIDENCE_REJECTED",
                    record_path,
                    speaker_evidence_error,
                )
            )

    if not scope_reused and any(not isinstance(value, dict) for value in source_fact_receipts):
        issues.append(
            SourceFactAuditIssue("SOURCE_FACT_REVIEW_MISSING", publish_path or record_path)
        )
    elif not scope_reused and not (
        source_fact_receipts[0] == source_fact_receipts[1] == source_fact_receipts[2]
    ):
        issues.append(
            SourceFactAuditIssue("SOURCE_FACT_REVIEW_BINDING_DRIFT", publish_path or record_path)
        )
    elif not scope_reused and speaker_evidence_error is not None:
        issues.append(
            SourceFactAuditIssue(
                "SOURCE_FACT_REVIEW_INVALID",
                publish_path or record_path,
                "fresh speaker evidence could not validate the source-fact receipt",
            )
        )
    elif not scope_reused and not validate_source_fact_review(
        source_fact_receipts[0],
        selection_hook=str(story_contract.get("selection_hook") or ""),
        title=artifact_title,
        final_transcript=final_transcript,
        clip_context_prompt=str(story_contract.get("clip_context_prompt") or ""),
        selection_scorecard=story_contract.get("selection_scorecard"),
        candidate_id=str(story_contract.get("candidate_id") or ""),
        final_reviewed_srt_path=subtitle_path,
        speaker_evidence=rebuilt_speaker_evidence,
    ):
        issues.append(
            SourceFactAuditIssue("SOURCE_FACT_REVIEW_INVALID", publish_path or record_path)
        )
    elif not scope_reused:
        declared_receipt_sha256 = manifest.get("source_fact_review_sha256")
        if declared_receipt_sha256 is not None and declared_receipt_sha256 != source_fact_receipts[
            0
        ].get("receipt_sha256"):
            issues.append(
                SourceFactAuditIssue(
                    "SOURCE_FACT_REVIEW_MANIFEST_BINDING_DRIFT",
                    publish_path or record_path,
                )
            )
    return tuple(issues)
