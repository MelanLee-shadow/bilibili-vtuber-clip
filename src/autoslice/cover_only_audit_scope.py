"""Narrow audit bridge for an already-reviewed, cover-only same-BV repair.

The current Talk package policy requires a source-fact receipt.  Historical
same-BV repairs can predate that receipt while still having a completed,
hash-bound final human review of the exact video, subtitle, and public title.
This module permits reuse of *only* that non-cover review, and only when the
current package is byte-identical on the reviewed non-cover surfaces.

It is deliberately not a general source-fact or title bypass.  A scope can be
created only from a terminal ``same-bv-repair-completed.v1`` predecessor, its
immutable plan/journal chain, and the prior final-human-review receipt.  The
current cover and cover evidence are merely bound here; the canonical package
auditor and title+cover joint-QC still decide whether those new bytes pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.autoslice import same_bv_repair as repair
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)


SCHEMA_VERSION = "lidousha-cover-only-audit-scope.v1"
PURPOSE = "COVER_ONLY_SAME_BV"
REUSED_GATE = "SOURCE_FACT_REVIEW_MISSING_ONLY"
_SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")


class CoverOnlyAuditScopeError(ValueError):
    """The requested cover-only reuse scope cannot be proven."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _scope_digest(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("scope_sha256", None)
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def _sha256(path: Path, *, prefixed: bool = False) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    return "sha256:" + value if prefixed else value


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoverOnlyAuditScopeError(
            f"{label} unreadable: {path} ({exc})"
        ) from exc
    if not isinstance(value, dict):
        raise CoverOnlyAuditScopeError(f"{label} must be a JSON object: {path}")
    return value


def _regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise CoverOnlyAuditScopeError(f"{label} may not be a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_absolute() or resolved.is_symlink() or not resolved.is_file():
        raise CoverOnlyAuditScopeError(f"{label} missing or unsafe: {path}")
    return resolved


def _binding(path: Path, *, package_root: Path | None = None) -> dict[str, Any]:
    resolved = _regular_file(path, label="bound file")
    bound_path: str
    if package_root is None:
        bound_path = str(resolved)
    else:
        try:
            bound_path = resolved.relative_to(package_root.resolve()).as_posix()
        except ValueError as exc:
            raise CoverOnlyAuditScopeError(
                f"bound package file escapes package root: {resolved}"
            ) from exc
    return {
        "path": bound_path,
        "sha256": _sha256(resolved),
        "bytes": resolved.stat().st_size,
    }


def _unprefixed(value: object, *, label: str) -> str:
    match = _SHA256_RE.fullmatch(str(value or ""))
    if match is None:
        raise CoverOnlyAuditScopeError(f"{label} SHA-256 is invalid")
    return match.group(1)


def _canonical_tags(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CoverOnlyAuditScopeError(f"{label} must be a non-empty tag list")
    tags = [str(tag).strip() for tag in value]
    if any(not tag for tag in tags) or len(tags) != len(set(tags)):
        raise CoverOnlyAuditScopeError(
            f"{label} contains an empty or duplicate tag"
        )
    # Bilibili/live readback does not preserve submitted tag order.  Freeze the
    # semantic set while still refusing additions, removals, or duplicates.
    return tuple(sorted(tags))


def _record_for_candidate(
    package_root: Path,
    candidate_id: str,
) -> tuple[str, Path, dict[str, Any]]:
    matches: list[tuple[str, Path, dict[str, Any]]] = []
    for path in sorted(package_root.glob("*.record.json")):
        payload = _load_json(path, label="record")
        story = payload.get("story_contract")
        if (
            isinstance(story, Mapping)
            and story.get("candidate_id") == candidate_id
        ):
            matches.append((path.name[: -len(".record.json")], path, payload))
    if len(matches) != 1:
        raise CoverOnlyAuditScopeError(
            "cover-only scope requires exactly one current record for "
            f"{candidate_id!r}; found {len(matches)}"
        )
    return matches[0]


def _current_package(
    package_root: Path,
    candidate_id: str,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    stem, record_path, record = _record_for_candidate(package_root, candidate_id)
    paths = {
        "video": package_root / f"{stem}.mp4",
        "subtitle": package_root / f"{stem}.srt",
        "cover": package_root / f"{stem}.cover.png",
        "record": record_path,
        "publish": package_root / f"{stem}.publish.json",
    }
    bindings = {
        key: _binding(path, package_root=package_root)
        for key, path in paths.items()
    }
    publish = _load_json(paths["publish"], label="publish document")
    story = record.get("story_contract")
    staging = record.get("publish_staging")
    receipts = (
        story.get("source_fact_review") if isinstance(story, Mapping) else None,
        staging.get("source_fact_review") if isinstance(staging, Mapping) else None,
        publish.get("source_fact_review"),
    )
    if any(value is not None for value in receipts):
        raise CoverOnlyAuditScopeError(
            "cover-only scope is only valid when all source-fact receipt "
            "surfaces are absent; a present receipt must pass the normal gate"
        )
    return stem, record, publish, bindings


def _replay_predecessor(
    completed_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    completed_path = _regular_file(
        completed_path, label="predecessor completed sidecar"
    )
    completed = _load_json(
        completed_path, label="predecessor completed sidecar"
    )
    raw_plan = completed.get("plan")
    if not isinstance(raw_plan, Mapping):
        raise CoverOnlyAuditScopeError("predecessor completed plan binding missing")
    plan_path = _regular_file(
        Path(str(raw_plan.get("path") or "")), label="predecessor plan"
    )
    if _sha256(plan_path) != _unprefixed(
        raw_plan.get("sha256"), label="predecessor plan"
    ):
        raise CoverOnlyAuditScopeError("predecessor plan hash drift")
    try:
        plan = repair.load_plan(plan_path)
        repair._validate_completed_plan_snapshot(plan, plan_path=plan_path)
    except repair.PlanInvalid as exc:
        raise CoverOnlyAuditScopeError(
            f"predecessor completed plan rejected: {exc}"
        ) from exc
    authority = plan.get("recovery_publication_authority")
    snapshot = completed.get("live_snapshot")
    if not isinstance(authority, Mapping) or not isinstance(snapshot, Mapping):
        raise CoverOnlyAuditScopeError(
            "predecessor authority or terminal live snapshot missing"
        )
    try:
        attestation = repair._predecessor_completion_attestation(
            completed_path,
            authority=authority,
            bvid=str(plan.get("bvid") or ""),
            snapshot=snapshot,
        )
    except repair.PlanInvalid as exc:
        raise CoverOnlyAuditScopeError(
            f"predecessor completion rejected: {exc}"
        ) from exc
    return completed, plan, attestation


def _prior_review(
    plan: Mapping[str, Any],
    *,
    candidate_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    package_attestation = plan.get("package_attestation")
    entry = (
        package_attestation.get("final_human_review")
        if isinstance(package_attestation, Mapping)
        else None
    )
    if not isinstance(entry, Mapping):
        raise CoverOnlyAuditScopeError(
            "predecessor plan has no final human review attestation"
        )
    receipt_path = _regular_file(
        Path(str(entry.get("path") or "")), label="prior final human review"
    )
    expected_sha = _unprefixed(
        entry.get("sha256"), label="prior final human review"
    )
    expected_bytes = entry.get("bytes")
    if _sha256(receipt_path) != expected_sha:
        raise CoverOnlyAuditScopeError("prior final human review hash drift")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes <= 0
        or receipt_path.stat().st_size != expected_bytes
    ):
        raise CoverOnlyAuditScopeError("prior final human review size drift")
    receipt = _load_json(receipt_path, label="prior final human review")
    if (
        receipt.get("schema_version") != "lidousha-final-human-review.v2"
        or receipt.get("scope") != "same_bv_repair"
        or receipt.get("status") != "ACCEPTED_FOR_SAME_BV"
    ):
        raise CoverOnlyAuditScopeError(
            "prior final human review is not an accepted same-BV review"
        )
    items = receipt.get("items")
    matched = [
        row
        for row in items or []
        if isinstance(row, dict) and row.get("candidate_id") == candidate_id
    ]
    if len(matched) != 1:
        raise CoverOnlyAuditScopeError(
            "prior final human review candidate binding is missing or ambiguous"
        )
    return receipt, matched[0]


def _artifact_sha(review_item: Mapping[str, Any], kind: str) -> str:
    artifacts = review_item.get("artifacts")
    entry = artifacts.get(kind) if isinstance(artifacts, Mapping) else None
    if not isinstance(entry, Mapping):
        raise CoverOnlyAuditScopeError(
            f"prior final human review lacks {kind} binding"
        )
    return _unprefixed(entry.get("sha256"), label=f"prior reviewed {kind}")


def _canonical_authority(
    raw: object,
    *,
    candidate_id: str,
    title: str,
) -> dict[str, Any]:
    try:
        authority = validate_recovery_publication_authority(
            raw,
            candidate_id=candidate_id,
            expected_final_title=title,
        )
    except RecoveryTitleAuthorityError as exc:
        raise CoverOnlyAuditScopeError(
            f"current recovery publication authority invalid: {exc}"
        ) from exc
    if raw != authority:
        raise CoverOnlyAuditScopeError(
            "current recovery publication authority is not canonical"
        )
    return authority


def create_scope(
    *,
    package_root: Path,
    candidate_id: str,
    predecessor_completed_path: Path,
    authorized_by: str,
    authorization_quote: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the exact scope; performs no writes."""

    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise CoverOnlyAuditScopeError(f"package root missing: {package_root}")
    if _CANDIDATE_RE.fullmatch(candidate_id) is None:
        raise CoverOnlyAuditScopeError("candidate_id is invalid")
    if authorized_by.strip() != "Ivan" or not authorization_quote.strip():
        raise CoverOnlyAuditScopeError(
            "cover-only source-fact reuse requires explicit Ivan authorization"
        )
    stem, record, publish, current = _current_package(
        package_root, candidate_id
    )
    completed_path = _regular_file(
        predecessor_completed_path,
        label="predecessor completed sidecar",
    )
    completed, plan, predecessor = _replay_predecessor(completed_path)
    plan_authority = plan.get("recovery_publication_authority")
    target = plan.get("target_metadata")
    if not isinstance(plan_authority, Mapping) or not isinstance(target, Mapping):
        raise CoverOnlyAuditScopeError(
            "predecessor plan lacks publication authority or target metadata"
        )
    title = str(target.get("title") or "")
    if not title:
        raise CoverOnlyAuditScopeError("predecessor title is empty")
    authority = _canonical_authority(
        record.get("recovery_publication_authority"),
        candidate_id=candidate_id,
        title=title,
    )
    staging = record.get("publish_staging")
    if (
        plan.get("bvid") != authority.get("bvid")
        or plan_authority != authority
        or completed.get("candidate_id") != candidate_id
        or completed.get("bvid") != authority.get("bvid")
        or completed.get("aid") != authority.get("aid")
        or not isinstance(completed.get("new_cid"), int)
        or isinstance(completed.get("new_cid"), bool)
    ):
        raise CoverOnlyAuditScopeError(
            "predecessor and current publication target differ"
        )
    if (
        not isinstance(staging, Mapping)
        or staging.get("title") != title
        or publish.get("title") != title
    ):
        raise CoverOnlyAuditScopeError(
            "current package title differs from the frozen predecessor title"
        )

    receipt, review_item = _prior_review(plan, candidate_id=candidate_id)
    publication_target = review_item.get("publication_target")
    if (
        review_item.get("reviewed_title") != title
        or not isinstance(publication_target, Mapping)
        or publication_target.get("candidate_id") != candidate_id
        or publication_target.get("bvid") != authority.get("bvid")
        or publication_target.get("aid") != authority.get("aid")
        or publication_target.get("cid") != authority.get("cid")
        or publication_target.get("final_title") != title
        or publication_target.get("authority_sha256")
        != authority.get("authority_sha256")
    ):
        raise CoverOnlyAuditScopeError(
            "prior final human review does not bind the frozen publication target"
        )
    predecessor_video = (plan.get("replacement") or {}).get("video")
    if not isinstance(predecessor_video, Mapping):
        raise CoverOnlyAuditScopeError("predecessor replacement video missing")
    if (
        current["video"]["sha256"]
        != _unprefixed(
            predecessor_video.get("sha256"), label="predecessor replacement video"
        )
        or current["video"]["bytes"] != predecessor_video.get("bytes")
        or current["video"]["sha256"] != _artifact_sha(review_item, "video")
        or current["subtitle"]["sha256"]
        != _artifact_sha(review_item, "subtitle")
    ):
        raise CoverOnlyAuditScopeError(
            "current video/subtitle differs from the prior reviewed non-cover bytes"
        )
    tags = (record.get("upload_tags") or {}).get("final_tags")
    if _canonical_tags(
        tags, label="current reviewed tags"
    ) != _canonical_tags(
        target.get("tags"), label="predecessor target tags"
    ):
        raise CoverOnlyAuditScopeError(
            "current reviewed tags differ from predecessor target metadata"
        )
    artifact_hashes = record.get("artifact_hashes")
    if not isinstance(artifact_hashes, Mapping):
        raise CoverOnlyAuditScopeError("current record artifact hashes missing")
    accepted_video = {
        _unprefixed(value, label="record video")
        for value in (
            artifact_hashes.get("burned_video_sha256"),
            artifact_hashes.get("video_sha256"),
        )
        if value is not None
    }
    accepted_subtitle = {
        _unprefixed(value, label="record subtitle")
        for value in (
            artifact_hashes.get("delivery_subtitle_sha256"),
            artifact_hashes.get("subtitle_sha256"),
        )
        if value is not None
    }
    if (
        current["video"]["sha256"] not in accepted_video
        or current["subtitle"]["sha256"] not in accepted_subtitle
        or current["cover"]["sha256"]
        != _unprefixed(
            artifact_hashes.get("cover_sha256"), label="record cover"
        )
    ):
        raise CoverOnlyAuditScopeError(
            "current record does not bind the exact package artifacts"
        )

    plan_path = Path(str((completed.get("plan") or {}).get("path") or ""))
    final_review_entry = (plan.get("package_attestation") or {}).get(
        "final_human_review"
    )
    assert isinstance(final_review_entry, Mapping)
    final_review_path = Path(str(final_review_entry.get("path") or ""))
    frozen_noncover = {
        "title": title,
        "description": target.get("desc"),
        "tags": target.get("tags"),
        "publish_policy": {
            "tid": target.get("tid"),
            "copyright": target.get("copyright"),
            "source": target.get("source"),
        },
        "video": current["video"],
        "subtitle": current["subtitle"],
    }
    scope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "reused_gate": REUSED_GATE,
        "candidate_id": candidate_id,
        "stem": stem,
        "created_at": created_at
        or datetime.now(timezone.utc).isoformat(),
        "authorization": {
            "by": "Ivan",
            "quote": authorization_quote.strip(),
        },
        "publication_target": {
            "bvid": authority.get("bvid"),
            "aid": authority.get("aid"),
            "original_cid": authority.get("cid"),
            "current_cid": completed.get("new_cid"),
            "authority_sha256": authority.get("authority_sha256"),
        },
        "frozen_noncover": frozen_noncover,
        "current_package": {
            "record": current["record"],
            "publish": current["publish"],
            "cover": current["cover"],
        },
        "predecessor": {
            "completed": _binding(completed_path),
            "plan": {
                **_binding(plan_path),
                "plan_id": plan.get("plan_id"),
            },
            "verified_journal_row": predecessor.get(
                "verified_journal_row"
            ),
            "final_human_review": {
                **_binding(final_review_path),
                "reviewed_at": receipt.get("reviewed_at"),
                "reviewed_by": receipt.get("reviewed_by"),
                "status": receipt.get("status"),
                "scope": receipt.get("scope"),
            },
        },
    }
    scope["scope_sha256"] = _scope_digest(scope)
    return scope


def validate_scope(
    scope: object,
    *,
    package_root: Path,
    item: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay a scope against current bytes and one exact manifest item."""

    if not isinstance(scope, Mapping):
        raise CoverOnlyAuditScopeError("cover-only audit scope is not an object")
    expected_fields = {
        "schema_version",
        "purpose",
        "reused_gate",
        "candidate_id",
        "stem",
        "created_at",
        "authorization",
        "publication_target",
        "frozen_noncover",
        "current_package",
        "predecessor",
        "scope_sha256",
    }
    if set(scope) != expected_fields:
        raise CoverOnlyAuditScopeError("cover-only audit scope fields are invalid")
    if (
        scope.get("schema_version") != SCHEMA_VERSION
        or scope.get("purpose") != PURPOSE
        or scope.get("reused_gate") != REUSED_GATE
        or scope.get("scope_sha256") != _scope_digest(scope)
    ):
        raise CoverOnlyAuditScopeError(
            "cover-only audit scope schema, purpose, or digest is invalid"
        )
    candidate_id = str(scope.get("candidate_id") or "")
    stem = str(scope.get("stem") or "")
    if item.get("candidate_id") != candidate_id or item.get("stem") != stem:
        raise CoverOnlyAuditScopeError(
            "cover-only audit scope does not bind this manifest item"
        )
    current = scope.get("current_package")
    frozen = scope.get("frozen_noncover")
    predecessor = scope.get("predecessor")
    authorization = scope.get("authorization")
    if not all(
        isinstance(value, Mapping)
        for value in (current, frozen, predecessor, authorization)
    ):
        raise CoverOnlyAuditScopeError("cover-only audit scope closure is invalid")
    completed = predecessor.get("completed")
    if not isinstance(completed, Mapping):
        raise CoverOnlyAuditScopeError("cover-only predecessor binding missing")
    rebuilt = create_scope(
        package_root=package_root,
        candidate_id=candidate_id,
        predecessor_completed_path=Path(str(completed.get("path") or "")),
        authorized_by=str(authorization.get("by") or ""),
        authorization_quote=str(authorization.get("quote") or ""),
        created_at=str(scope.get("created_at") or ""),
    )
    if dict(scope) != rebuilt:
        raise CoverOnlyAuditScopeError(
            "cover-only audit scope differs from current proven closure"
        )
    expected_item_paths = {
        "video": item.get("video") or item.get("mp4"),
        "subtitle": item.get("subtitle_srt") or item.get("subtitle"),
        "cover": item.get("cover"),
        "record": item.get("record") or item.get("record_json"),
        "publish": item.get("publish_json") or item.get("publish"),
    }
    bound_paths = {
        "video": frozen.get("video", {}).get("path"),
        "subtitle": frozen.get("subtitle", {}).get("path"),
        "cover": current.get("cover", {}).get("path"),
        "record": current.get("record", {}).get("path"),
        "publish": current.get("publish", {}).get("path"),
    }
    if expected_item_paths != bound_paths:
        raise CoverOnlyAuditScopeError(
            "cover-only audit scope artifact paths differ from manifest item"
        )
    if item.get("title") != frozen.get("title"):
        raise CoverOnlyAuditScopeError(
            "manifest title differs from cover-only frozen title"
        )
    return rebuilt


def write_scope_create_only(path: Path, scope: Mapping[str, Any]) -> None:
    """Persist a scope once; never overwrite an existing authority file."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(scope, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CoverOnlyAuditScopeError(
            f"create-only cover audit scope already exists: {path}"
        ) from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
