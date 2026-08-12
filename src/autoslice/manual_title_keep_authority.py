"""Candidate-scoped KEEP authority for one blocked manual-title finding.

This is deliberately not a general manual-title bypass.  One repository-sealed
document binds the exact operator-approved title, the exact CPA dissent, the
immutable review inputs, and the complete FAILED receipt that produced it.
Consumers may replay that receipt without another provider call only while all
bindings still match.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)


ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_REPO_DIRECTORY = Path("assets/lidousha/authorities")
SCHEMA_VERSION = "lidousha-manual-title-keep-authority.v1"
CONSUMPTION_SCHEMA_VERSION = "manual-title-keep-authority-consumption.v1"
FINDING_CLASS = "SUPPORTED_COMPRESSION_HEDGE"
ACTION = "KEEP_EXACT_OPERATOR_APPROVED_VALUE"
PASS_DECISION = "PASS_WITH_RECORDED_DISSENT"
TARGET_CANDIDATE_ID = "auto_223750_578_734"
ADJUDICATION_CLIP_CONTEXT_PROMPT_SHA256 = (
    "sha256:572c977fec852e8ca863f065e191c156e65a7906d2a683cf5cd3d90de9dd5e17"
)
_CANDIDATE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")
_SHA_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOP_FIELDS = {
    "schema_version",
    "candidate_id",
    "field",
    "scope",
    "approved_title",
    "displayed_machine_title_snapshot",
    "ivan_authority",
    "entity_transform_authority",
    "source_binding",
    "blocked_source_fact",
    "replay",
    "dissent",
    "authority_sha256",
}


class ManualTitleKeepAuthorityError(ValueError):
    """The one-candidate KEEP authority is absent, malformed, or stale."""


@dataclass(frozen=True, slots=True)
class ManualTitleKeepAuthorityV1:
    document: dict[str, object]
    repo_path: Path
    file_sha256: str


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def read_regular_no_symlink(path: Path) -> bytes:
    """Read once through a no-follow fd after rejecting every symlink component."""

    absolute = path.absolute()
    if not absolute.is_absolute():
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_FILE_PATH_INVALID")
    cursor = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            cursor = cursor / part
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_FILE_PATH_CONTAINS_SYMLINK")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(absolute, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_FILE_NOT_REGULAR")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except ManualTitleKeepAuthorityError:
        raise
    except OSError as exc:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_FILE_UNREADABLE") from exc


def authority_repo_path(candidate_id: str) -> Path:
    if not _CANDIDATE_RE.fullmatch(candidate_id) or Path(candidate_id).name != candidate_id:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_CANDIDATE_INVALID")
    return AUTHORITY_REPO_DIRECTORY / (f"{candidate_id}.manual-title-keep-authority.v1.json")


def _mapping(value: object, *, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ManualTitleKeepAuthorityError(f"MANUAL_TITLE_KEEP_{label.upper()}_SCHEMA_INVALID")
    return dict(value)


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ManualTitleKeepAuthorityError(f"MANUAL_TITLE_KEEP_{label.upper()}_HASH_INVALID")
    return value


def _nonempty(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManualTitleKeepAuthorityError(f"MANUAL_TITLE_KEEP_{label.upper()}_INVALID")
    return value


def _regular_bound_file(root: Path, *, repo_path: object, expected_sha256: object) -> None:
    relative = Path(_nonempty(repo_path, label="repo_path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_REPO_PATH_INVALID")
    try:
        resolved_root = root.resolve(strict=True)
        cursor = resolved_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SUPPORT_ASSET_SYMLINK")
        path = cursor.resolve(strict=True)
        path.relative_to(resolved_root)
        observed = bytes_sha256(read_regular_no_symlink(path))
    except (OSError, ValueError) as exc:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SUPPORT_ASSET_UNREADABLE") from exc
    if observed != _sha(expected_sha256, label="support_asset"):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SUPPORT_ASSET_HASH_MISMATCH")


def _validate_quote(value: object, *, label: str) -> dict[str, object]:
    row = _mapping(value, fields={"quote", "sha256"}, label=label)
    quote = _nonempty(row["quote"], label=label)
    if row["sha256"] != text_sha256(quote):
        raise ManualTitleKeepAuthorityError(f"MANUAL_TITLE_KEEP_{label.upper()}_HASH_MISMATCH")
    return row


def _receipt_sha256(review: Mapping[str, object]) -> str:
    body = dict(review)
    declared = body.pop("receipt_sha256", None)
    expected = canonical_sha256(body)
    if declared != expected:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_BLOCKED_RECEIPT_HASH_MISMATCH")
    return expected


def _validate_blocked_receipt(
    authority: Mapping[str, object],
    *,
    approved_title: str,
    candidate_id: str,
) -> None:
    blocked_meta = _mapping(
        authority["blocked_source_fact"],
        fields={
            "receipt_sha256",
            "finding_class",
            "field",
            "span",
            "action",
            "finding_fingerprint_sha256",
            "proposed_title",
            "proposed_title_sha256",
            "expected_changed_surface_count",
            "expected_unmatched_blocking_finding_count",
        },
        label="blocked_source_fact",
    )
    if (
        blocked_meta["finding_class"] != FINDING_CLASS
        or blocked_meta["field"] != "title"
        or blocked_meta["span"] != "结伴后"
        or blocked_meta["action"] != ACTION
        or blocked_meta["expected_changed_surface_count"] != 1
        or blocked_meta["expected_unmatched_blocking_finding_count"] != 0
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_FINDING_SCOPE_INVALID")
    proposed_title = _nonempty(blocked_meta["proposed_title"], label="proposed_title")
    if blocked_meta["proposed_title_sha256"] != text_sha256(proposed_title):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_PROPOSED_TITLE_HASH_MISMATCH")
    # The dissent is permitted only for this literal one-span hedge.  Actor,
    # addressee, entity, event, or other temporal edits cannot fit this shape.
    if approved_title.count("结伴后") != 1 or proposed_title != approved_title.replace(
        "结伴后", "答应一起走后", 1
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_PROPOSAL_NOT_COMPRESSION_HEDGE")

    replay = _mapping(
        authority["replay"],
        fields={
            "final_transcript",
            "final_transcript_sha256",
            "clip_context_prompt",
            "clip_context_prompt_sha256",
            "selection_scorecard",
            "selection_scorecard_sha256",
            "blocked_source_fact_review",
        },
        label="replay",
    )
    transcript = _nonempty(replay["final_transcript"], label="final_transcript")
    context = _nonempty(replay["clip_context_prompt"], label="clip_context_prompt")
    if replay["final_transcript_sha256"] != text_sha256(transcript):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_FINAL_TRANSCRIPT_HASH_MISMATCH")
    if replay["clip_context_prompt_sha256"] != text_sha256(context):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_CLIP_CONTEXT_HASH_MISMATCH")
    if replay["clip_context_prompt_sha256"] != ADJUDICATION_CLIP_CONTEXT_PROMPT_SHA256:
        raise ManualTitleKeepAuthorityError(
            "MANUAL_TITLE_KEEP_SEALED_ADJUDICATION_CONTEXT_MISMATCH"
        )
    if replay["selection_scorecard_sha256"] != canonical_sha256(replay["selection_scorecard"]):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SCORECARD_HASH_MISMATCH")
    review = replay["blocked_source_fact_review"]
    if not isinstance(review, Mapping):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_BLOCKED_RECEIPT_SCHEMA_INVALID")
    receipt_sha256 = _receipt_sha256(review)
    if receipt_sha256 != blocked_meta["receipt_sha256"]:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_BLOCKED_RECEIPT_BINDING_MISMATCH")
    if (
        review.get("schema_version") != "lidousha-source-fact-review.v1"
        or review.get("status") != "FAILED"
        or review.get("decision") != "REPAIR_REQUIRES_TITLE_AUTHORITY"
        or review.get("reason_code") != "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED"
        or review.get("original_title") != approved_title
        or review.get("final_title") != approved_title
        or review.get("original_selection_hook") != review.get("final_selection_hook")
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_BLOCKED_RECEIPT_SHAPE_INVALID")
    entity_context = review.get("entity_context")
    source_binding = authority["source_binding"]
    assert isinstance(source_binding, Mapping)
    source_media = source_binding.get("source_media")
    interval = source_binding.get("exact_interval")
    plain = source_binding.get("reviewed_plain_srt")
    transform = authority["entity_transform_authority"]
    assert isinstance(transform, Mapping)
    candidate_binding = (
        entity_context.get("candidate_binding") if isinstance(entity_context, Mapping) else None
    )
    if (
        not isinstance(entity_context, Mapping)
        or entity_context.get("candidate_id") != candidate_id
        or entity_context.get("context_sha256") != source_binding.get("entity_context_sha256")
        or entity_context.get("projection_sha256")
        != str(transform.get("projection_sha256") or "").removeprefix("sha256:")
        or not isinstance(plain, Mapping)
        or entity_context.get("final_reviewed_srt_sha256")
        != str(plain.get("sha256") or "").removeprefix("sha256:")
        or not isinstance(candidate_binding, Mapping)
        or not isinstance(source_media, Mapping)
        or candidate_binding.get("source_recording_basename") != source_media.get("basename")
        or candidate_binding.get("source_sha256")
        != str(source_media.get("sha256") or "").removeprefix("sha256:")
        or not isinstance(interval, Mapping)
        or candidate_binding.get("absolute_source_start_ms") != interval.get("absolute_start_ms")
        or candidate_binding.get("absolute_source_end_ms") != interval.get("absolute_end_ms")
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SOURCE_ENTITY_BINDING_MISMATCH")
    passes = review.get("passes")
    if not isinstance(passes, list) or len(passes) != 1 or not isinstance(passes[0], Mapping):
        raise ManualTitleKeepAuthorityError(
            "MANUAL_TITLE_KEEP_BLOCKED_RECEIPT_FINDING_COUNT_INVALID"
        )
    review_pass = passes[0]
    changed = review_pass.get("changed_surfaces")
    addressee = review_pass.get("addressee_attribution")
    scorecard_review = review_pass.get("selection_scorecard_review")
    if (
        review_pass.get("status") != "REPAIR"
        or review_pass.get("clip_context_prompt_sha256") != ADJUDICATION_CLIP_CONTEXT_PROMPT_SHA256
        or review_pass.get("final_selection_hook") != review.get("original_selection_hook")
        or review_pass.get("final_title") != proposed_title
        or not isinstance(changed, list)
        or len(changed) != 1
        or not isinstance(changed[0], Mapping)
        or not isinstance(addressee, list)
        or not addressee
        or any(
            not isinstance(row, Mapping) or row.get("verdict") != "SUPPORTED" for row in addressee
        )
        or review_pass.get("title_policy_violations") != []
        or not isinstance(scorecard_review, Mapping)
        or scorecard_review.get("status") != "NOT_NEEDED"
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_UNMATCHED_BLOCKING_FINDING")
    finding = dict(changed[0])
    if (
        set(finding) != {"artifact", "before", "after", "reason", "evidence"}
        or finding.get("artifact") != "title"
        or finding.get("before") != approved_title
        or finding.get("after") != proposed_title
        or canonical_sha256(finding) != blocked_meta["finding_fingerprint_sha256"]
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_FINDING_FINGERPRINT_MISMATCH")
    dissent = _mapping(
        authority["dissent"],
        fields={"record", "disposition", "reason", "evidence", "proposed_title"},
        label="dissent",
    )
    if (
        dissent["record"] is not True
        or dissent["disposition"] != "NON_BLOCKING_DISSENT_RECORDED"
        or dissent["reason"] != finding["reason"]
        or dissent["evidence"] != finding["evidence"]
        or dissent["proposed_title"] != proposed_title
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_DISSENT_BINDING_MISMATCH")


def validate_manual_title_keep_authority_document(
    value: object,
) -> dict[str, object]:
    """Pure validation for tests and pre-commit construction."""

    authority = _mapping(value, fields=_TOP_FIELDS, label="authority")
    if authority["schema_version"] != SCHEMA_VERSION:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_AUTHORITY_SCHEMA_INVALID")
    candidate_id = _nonempty(authority["candidate_id"], label="candidate")
    authority_repo_path(candidate_id)
    if authority["field"] != "title" or authority["scope"] != {
        "candidate_scoped": True,
        "finding_scoped": True,
        "wildcard": False,
    }:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SCOPE_INVALID")
    approved = _mapping(
        authority["approved_title"], fields={"value", "sha256"}, label="approved_title"
    )
    approved_title = _nonempty(approved["value"], label="approved_title")
    if approved["sha256"] != text_sha256(approved_title):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_APPROVED_TITLE_HASH_MISMATCH")
    machine = _mapping(
        authority["displayed_machine_title_snapshot"],
        fields={"value", "sha256", "source"},
        label="machine_title_snapshot",
    )
    machine_title = _nonempty(machine["value"], label="machine_title_snapshot")
    if machine["sha256"] != text_sha256(machine_title):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_MACHINE_TITLE_HASH_MISMATCH")
    source = _mapping(
        machine["source"],
        fields={
            "artifact_id",
            "artifact_sha256",
            "json_pointer",
            "source_fact_receipt_sha256",
        },
        label="machine_title_source",
    )
    # Logical provenance only: neither the authority hash nor runtime replay
    # depends on a host-specific /home path.  Raw artifact bytes, JSON pointer,
    # receipt, and extracted title remain independently hash-bound.
    if source["artifact_id"] != (
        "vtuber-reproduce/run/20260811-e4249d4/auto_223750_578_734.recut.publish.json"
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_MACHINE_TITLE_ARTIFACT_ID_INVALID")
    _sha(source["artifact_sha256"], label="machine_title_artifact")
    if source["json_pointer"] != "/source_fact_review/passes/0/changed_surfaces/1/after":
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_MACHINE_TITLE_POINTER_INVALID")
    _sha(source["source_fact_receipt_sha256"], label="machine_source_receipt")

    transform = _mapping(
        authority["entity_transform_authority"],
        fields={
            "entity_id",
            "from_surface",
            "to_surface",
            "field",
            "allowed_replacement_count",
            "projection_repo_path",
            "projection_file_sha256",
            "projection_sha256",
        },
        label="entity_transform",
    )
    if (
        transform["entity_id"] != "ent_3c301b25eceb4b4eb0119a83034c1eb0"
        or transform["from_surface"] != "莉亚"
        or transform["to_surface"] != "莉娅"
        or transform["field"] != "title"
        or transform["allowed_replacement_count"] != 1
        or machine_title.count("莉亚") != 1
        or approved_title != machine_title.replace("莉亚", "莉娅", 1)
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_ENTITY_TRANSFORM_SCOPE_INVALID")
    _sha(transform["projection_file_sha256"], label="projection_file")
    _sha(transform["projection_sha256"], label="projection")

    ivan = _mapping(
        authority["ivan_authority"],
        fields={"title_delta_approval", "publication_release"},
        label="ivan_authority",
    )
    _validate_quote(ivan["title_delta_approval"], label="title_delta_approval")
    _validate_quote(ivan["publication_release"], label="publication_release")

    source_binding = _mapping(
        authority["source_binding"],
        fields={
            "source_media",
            "exact_interval",
            "reviewed_plain_srt",
            "reviewed_speaker_srt",
            "speaker_authority_assets",
            "entity_context_sha256",
        },
        label="source_binding",
    )
    source_media = _mapping(
        source_binding["source_media"], fields={"basename", "sha256"}, label="source_media"
    )
    _nonempty(source_media["basename"], label="source_media")
    _sha(source_media["sha256"], label="source_media")
    interval = _mapping(
        source_binding["exact_interval"],
        fields={"absolute_start_ms", "absolute_end_ms"},
        label="exact_interval",
    )
    if (
        isinstance(interval["absolute_start_ms"], bool)
        or not isinstance(interval["absolute_start_ms"], int)
        or isinstance(interval["absolute_end_ms"], bool)
        or not isinstance(interval["absolute_end_ms"], int)
        or interval["absolute_start_ms"] >= interval["absolute_end_ms"]
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_EXACT_INTERVAL_INVALID")
    plain = _mapping(
        source_binding["reviewed_plain_srt"],
        fields={"repo_path", "sha256"},
        label="reviewed_plain_srt",
    )
    _sha(plain["sha256"], label="reviewed_plain_srt")
    speaker = _mapping(
        source_binding["reviewed_speaker_srt"],
        fields={"sha256", "cue_count"},
        label="reviewed_speaker_srt",
    )
    _sha(speaker["sha256"], label="reviewed_speaker_srt")
    if not isinstance(speaker["cue_count"], int) or isinstance(speaker["cue_count"], bool):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_REVIEWED_SPEAKER_SRT_INVALID")
    speaker_assets = _mapping(
        source_binding["speaker_authority_assets"],
        fields={"override", "truth", "automatic_baseline"},
        label="speaker_authority_assets",
    )
    for key, row in speaker_assets.items():
        _mapping(row, fields={"repo_path", "sha256"}, label=f"speaker_{key}")
        _sha(row["sha256"], label=f"speaker_{key}")
    _sha(source_binding["entity_context_sha256"], label="entity_context")
    _validate_blocked_receipt(
        authority,
        approved_title=approved_title,
        candidate_id=candidate_id,
    )

    body = dict(authority)
    declared = body.pop("authority_sha256")
    if declared != canonical_sha256(body):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_AUTHORITY_HASH_MISMATCH")
    return authority


def load_manual_title_keep_authority(
    candidate_id: str,
    *,
    root: Path = ROOT,
) -> ManualTitleKeepAuthorityV1 | None:
    """Load the deterministic candidate path from active HEAD/deployment."""

    if candidate_id != TARGET_CANDIDATE_ID:
        return None
    relative = authority_repo_path(candidate_id)
    resolved_root = root.resolve(strict=True)
    path = resolved_root / relative
    try:
        expected = repository_authority_expects_asset(
            repo_root=resolved_root, relative_path=relative
        )
    except RepositoryAssetAuthorityError as exc:
        raise ManualTitleKeepAuthorityError(
            "MANUAL_TITLE_KEEP_REPOSITORY_AUTHORITY_INVALID"
        ) from exc
    if not expected:
        # Worktree-only bytes are inert: active HEAD/deployment does not grant
        # them authority.  A deleted/drifted *active* asset still has
        # ``expected=True`` and fails in the sealed-byte read below.
        return None
    try:
        raw = path.read_bytes()
        require_repository_asset_authority(
            repo_root=resolved_root,
            relative_path=relative,
            observed_bytes=raw,
        )
        value = json.loads(raw.decode("utf-8"))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RepositoryAssetAuthorityError,
    ) as exc:
        raise ManualTitleKeepAuthorityError(
            "MANUAL_TITLE_KEEP_AUTHORITY_UNREADABLE_OR_UNSEALED"
        ) from exc
    document = validate_manual_title_keep_authority_document(value)
    if document["candidate_id"] != candidate_id:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_CANDIDATE_BINDING_MISMATCH")
    transform = document["entity_transform_authority"]
    source_binding = document["source_binding"]
    assert isinstance(transform, Mapping) and isinstance(source_binding, Mapping)
    plain = source_binding["reviewed_plain_srt"]
    speaker_assets = source_binding["speaker_authority_assets"]
    assert isinstance(plain, Mapping) and isinstance(speaker_assets, Mapping)
    _regular_bound_file(
        resolved_root,
        repo_path=transform["projection_repo_path"],
        expected_sha256=transform["projection_file_sha256"],
    )
    _regular_bound_file(
        resolved_root,
        repo_path=plain["repo_path"],
        expected_sha256=plain["sha256"],
    )
    for row in speaker_assets.values():
        assert isinstance(row, Mapping)
        _regular_bound_file(
            resolved_root,
            repo_path=row["repo_path"],
            expected_sha256=row["sha256"],
        )
    return ManualTitleKeepAuthorityV1(
        document=document,
        repo_path=relative,
        file_sha256=bytes_sha256(raw),
    )


def validate_manual_title_keep_authority(
    authority: ManualTitleKeepAuthorityV1,
    *,
    candidate_id: str,
    title: str,
    selection_hook: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    final_reviewed_srt_path: Path,
    speaker_evidence: object,
    entity_context: object,
) -> dict[str, object]:
    """Return consumption for exact truth plus diagnostic fresh context.

    The repository-sealed replay prompt is the adjudication context that
    produced the blocked receipt.  A newly generated clip-context prompt may
    drift with the fresh ASR grid, so it is diagnostic-only: its hash and
    equality-to-adjudication bit are preserved in the consumption/receipt, but
    its bytes cannot move or invalidate the already reviewed KEEP decision.
    """

    document = validate_manual_title_keep_authority_document(authority.document)
    approved = document["approved_title"]
    replay = document["replay"]
    blocked_meta = document["blocked_source_fact"]
    source_binding = document["source_binding"]
    machine = document["displayed_machine_title_snapshot"]
    assert all(
        isinstance(value, Mapping)
        for value in (approved, replay, blocked_meta, source_binding, machine)
    )
    if (
        document["candidate_id"] != candidate_id
        or approved["value"] != title
        or replay["final_transcript"] != final_transcript
        or replay["selection_scorecard"] != selection_scorecard
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_RUNTIME_BINDING_MISMATCH")
    if not isinstance(clip_context_prompt, str):
        raise ManualTitleKeepAuthorityError(
            "MANUAL_TITLE_KEEP_RUNTIME_DIAGNOSTIC_CLIP_CONTEXT_PROMPT_INVALID"
        )
    blocked_review = replay["blocked_source_fact_review"]
    assert isinstance(blocked_review, Mapping)
    if blocked_review.get("original_selection_hook") != selection_hook:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SELECTION_HOOK_MISMATCH")
    plain = source_binding["reviewed_plain_srt"]
    speaker_binding = source_binding["reviewed_speaker_srt"]
    assert isinstance(plain, Mapping) and isinstance(speaker_binding, Mapping)
    if bytes_sha256(read_regular_no_symlink(final_reviewed_srt_path)) != plain["sha256"]:
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_REVIEWED_PLAIN_SRT_MISMATCH")
    if not isinstance(speaker_evidence, Mapping):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SPEAKER_EVIDENCE_MISSING")
    blocked_speaker_evidence = blocked_review.get("speaker_evidence")
    if (
        speaker_evidence != blocked_speaker_evidence
        or speaker_evidence.get("state") != "PresentValid"
        or speaker_evidence.get("speaker_final_srt_sha256") != speaker_binding["sha256"]
        or speaker_evidence.get("plain_srt_sha256") != plain["sha256"]
        or blocked_review.get("speaker_evidence_sha256") != canonical_sha256(speaker_evidence)
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_REVIEWED_SPEAKER_SRT_MISMATCH")
    blocked_entity_context = blocked_review.get("entity_context")
    if (
        not isinstance(entity_context, Mapping)
        or not isinstance(blocked_entity_context, Mapping)
        or entity_context != blocked_entity_context
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_CURRENT_ENTITY_CONTEXT_MISMATCH")
    source_media = source_binding["source_media"]
    interval = source_binding["exact_interval"]
    assert isinstance(source_media, Mapping) and isinstance(interval, Mapping)
    candidate_binding = entity_context.get("candidate_binding")
    if (
        entity_context.get("candidate_id") != candidate_id
        or entity_context.get("context_sha256") != source_binding["entity_context_sha256"]
        or entity_context.get("final_reviewed_srt_sha256")
        != str(plain["sha256"]).removeprefix("sha256:")
        or not isinstance(candidate_binding, Mapping)
        or candidate_binding.get("source_recording_basename") != source_media["basename"]
        or candidate_binding.get("source_sha256")
        != str(source_media["sha256"]).removeprefix("sha256:")
        or candidate_binding.get("absolute_source_start_ms") != interval["absolute_start_ms"]
        or candidate_binding.get("absolute_source_end_ms") != interval["absolute_end_ms"]
    ):
        raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_SOURCE_OR_ENTITY_BINDING_MISMATCH")
    return {
        "schema_version": CONSUMPTION_SCHEMA_VERSION,
        "status": "CONSUMED",
        "candidate_id": candidate_id,
        "field": "title",
        "action": ACTION,
        "finding_class": FINDING_CLASS,
        "disputed_span": "结伴后",
        "record_dissent": True,
        "authority_repo_path": authority.repo_path.as_posix(),
        "authority_file_sha256": authority.file_sha256,
        "authority_sha256": document["authority_sha256"],
        "approved_title_sha256": approved["sha256"],
        "displayed_machine_title_sha256": machine["sha256"],
        "blocked_source_fact_receipt_sha256": blocked_meta["receipt_sha256"],
        "finding_fingerprint_sha256": blocked_meta["finding_fingerprint_sha256"],
        "final_transcript_sha256": replay["final_transcript_sha256"],
        # This existing field remains the sealed adjudication input because
        # source_fact_review binds it to the original blocked receipt.
        "clip_context_prompt_sha256": replay["clip_context_prompt_sha256"],
        "diagnostic_clip_context_prompt_sha256": text_sha256(clip_context_prompt),
        "diagnostic_clip_context_matches_adjudication": (
            clip_context_prompt == replay["clip_context_prompt"]
        ),
        "selection_scorecard_sha256": replay["selection_scorecard_sha256"],
        "reviewed_plain_srt_sha256": plain["sha256"],
        "reviewed_speaker_srt_sha256": speaker_binding["sha256"],
        "entity_context_sha256": source_binding["entity_context_sha256"],
    }


def blocked_source_fact_review(
    authority: ManualTitleKeepAuthorityV1,
) -> dict[str, object]:
    replay = authority.document["replay"]
    assert isinstance(replay, Mapping)
    review = replay["blocked_source_fact_review"]
    assert isinstance(review, Mapping)
    return json.loads(json.dumps(review, ensure_ascii=False))
