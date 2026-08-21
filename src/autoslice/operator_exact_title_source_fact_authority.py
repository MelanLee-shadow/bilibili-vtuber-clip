"""Sealed one-candidate operator title authority with preserved CPA dissent.

This is deliberately not a reusable source-fact exception.  It only permits
the named Ivan title for one already-reviewed candidate, while retaining the
provider's FAILED source-fact receipt verbatim as non-authoritative dissent.
"""

from __future__ import annotations

import hashlib
import json
import os
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
TARGET_CANDIDATE_ID = "auto_123655_1613_1676"
SCHEMA_VERSION = "lidousha-operator-exact-title-source-fact-authority.v1"
CONSUMPTION_SCHEMA_VERSION = "operator-exact-title-source-fact-consumption.v1"
PASS_DECISION = "OPERATOR_EXACT_TITLE_WITH_RECORDED_SOURCE_FACT_DISSENT"
AUTHORITY_REPO_PATH = Path(
    "assets/lidousha/authorities/auto_123655_1613_1676.operator-exact-title-source-fact-authority.v1.json"
)
_SHA_PREFIX = "sha256:"
_TOP_FIELDS = {
    "schema_version",
    "candidate_id",
    "recording_date",
    "scope",
    "operator_authorization",
    "approved_title",
    "source_binding",
    "failed_source_fact_review",
    "authority_sha256",
}


class OperatorExactTitleSourceFactAuthorityError(ValueError):
    """The candidate-scoped authority is absent, malformed, or stale."""


@dataclass(frozen=True, slots=True)
class OperatorExactTitleSourceFactAuthority:
    document: dict[str, object]
    repo_path: Path
    file_sha256: str


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _SHA_PREFIX + hashlib.sha256(payload).hexdigest()


def text_sha256(value: str) -> str:
    return _SHA_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return _SHA_PREFIX + hashlib.sha256(value).hexdigest()


def _mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise OperatorExactTitleSourceFactAuthorityError(f"OPERATOR_TITLE_{label}_SCHEMA_INVALID")
    return dict(value)


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperatorExactTitleSourceFactAuthorityError(f"OPERATOR_TITLE_{label}_INVALID")
    return value


def _sha(value: object, label: str) -> str:
    text = _nonempty(value, label)
    if not text.startswith(_SHA_PREFIX) or len(text) != len(_SHA_PREFIX) + 64:
        raise OperatorExactTitleSourceFactAuthorityError(f"OPERATOR_TITLE_{label}_HASH_INVALID")
    try:
        int(text.removeprefix(_SHA_PREFIX), 16)
    except ValueError as exc:
        raise OperatorExactTitleSourceFactAuthorityError(
            f"OPERATOR_TITLE_{label}_HASH_INVALID"
        ) from exc
    return text


def read_regular_no_symlink(path: Path) -> bytes:
    """Read one regular file without following a link in its ancestry."""

    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            cursor = cursor / part
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                raise OperatorExactTitleSourceFactAuthorityError(
                    "OPERATOR_TITLE_RUNTIME_PATH_CONTAINS_SYMLINK"
                )
        fd = os.open(absolute, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OperatorExactTitleSourceFactAuthorityError(
                    "OPERATOR_TITLE_RUNTIME_FILE_NOT_REGULAR"
                )
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OperatorExactTitleSourceFactAuthorityError:
        raise
    except OSError as exc:
        raise OperatorExactTitleSourceFactAuthorityError(
            "OPERATOR_TITLE_RUNTIME_FILE_UNREADABLE"
        ) from exc


def _json_file(path: Path, expected_sha256: str, label: str) -> dict[str, object]:
    payload = read_regular_no_symlink(path)
    if bytes_sha256(payload) != expected_sha256:
        raise OperatorExactTitleSourceFactAuthorityError(f"OPERATOR_TITLE_{label}_HASH_MISMATCH")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorExactTitleSourceFactAuthorityError(
            f"OPERATOR_TITLE_{label}_JSON_INVALID"
        ) from exc
    if not isinstance(value, Mapping):
        raise OperatorExactTitleSourceFactAuthorityError(f"OPERATOR_TITLE_{label}_SCHEMA_INVALID")
    return dict(value)


def _receipt_sha256(receipt: Mapping[str, object]) -> str:
    body = dict(receipt)
    declared = body.pop("receipt_sha256", None)
    if not isinstance(declared, str) or canonical_sha256(body) != declared:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_FAILED_RECEIPT_HASH_INVALID")
    return declared


def _validate_failed_receipt(value: object, *, title: str, hook: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_FAILED_RECEIPT_SCHEMA_INVALID")
    receipt = dict(value)
    _receipt_sha256(receipt)
    passes = receipt.get("passes")
    if (
        receipt.get("schema_version") != "lidousha-source-fact-review.v1"
        or receipt.get("status") != "FAILED"
        or receipt.get("decision") != "REPAIR_REQUIRES_TITLE_AUTHORITY"
        or receipt.get("reason_code") != "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED"
        or receipt.get("original_title") != title
        or receipt.get("final_title") != title
        or receipt.get("original_selection_hook") != hook
        or receipt.get("final_selection_hook") != hook
        or not isinstance(passes, list)
        or len(passes) != 1
        or not isinstance(passes[0], Mapping)
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_FAILED_RECEIPT_SCOPE_INVALID")
    changed = passes[0].get("changed_surfaces")
    if (
        passes[0].get("status") != "REPAIR"
        or not isinstance(changed, list)
        or len(changed) != 2
        or {row.get("artifact") for row in changed if isinstance(row, Mapping)}
        != {"selection_hook", "title"}
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_FAILED_RECEIPT_DISSENT_INVALID")
    return receipt


def validate_authority_document(value: object) -> dict[str, object]:
    """Validate the sealed static document before any runtime file is read."""

    authority = _mapping(value, _TOP_FIELDS, "AUTHORITY")
    if authority["schema_version"] != SCHEMA_VERSION or authority["candidate_id"] != TARGET_CANDIDATE_ID:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_AUTHORITY_SCOPE_INVALID")
    if authority["recording_date"] != "2026-08-17":
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RECORDING_DATE_INVALID")
    if authority["scope"] != {
        "candidate_scoped": True,
        "title_source_fact_projection_only": True,
        "subtitle_mutation": False,
        "media_mutation": False,
        "boundary_mutation": False,
        "cover_mutation": False,
        "upload_authorization": False,
        "provider_pass_claim": False,
    }:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_SCOPE_INVALID")
    operator = _mapping(
        authority["operator_authorization"],
        {"source", "conversation_id", "line", "timestamp", "quote", "quote_sha256"},
        "OPERATOR_AUTHORIZATION",
    )
    quote = _nonempty(operator["quote"], "OPERATOR_QUOTE")
    if (
        operator["source"] != "Claude JSONL"
        or operator["conversation_id"] != "0df2296b-500a-4681-ab5e-6fb46dc39579"
        or operator["line"] != 947
        or operator["timestamp"] != "2026-08-19T00:08:52.249Z"
        or operator["quote_sha256"] != text_sha256(quote)
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_OPERATOR_AUTHORIZATION_INVALID")
    approved = _mapping(authority["approved_title"], {"value", "sha256"}, "APPROVED_TITLE")
    title = _nonempty(approved["value"], "APPROVED_TITLE")
    if approved["sha256"] != text_sha256(title):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_APPROVED_TITLE_HASH_INVALID")
    source = _mapping(
        authority["source_binding"],
        {
            "initial_selection_hook",
            "initial_selection_hook_sha256",
            "resolved_selection_hook",
            "resolved_selection_hook_sha256",
            "selection_scorecard_sha256",
            "clip_context_prompt_sha256",
            "clip_context_file_sha256",
            "source_media",
            "reviewed_srt",
            "final_review",
            "chat_authority",
            "boundary_audit_sha256",
        },
        "SOURCE_BINDING",
    )
    initial_hook = _nonempty(source["initial_selection_hook"], "INITIAL_SELECTION_HOOK")
    resolved_hook = _nonempty(source["resolved_selection_hook"], "RESOLVED_SELECTION_HOOK")
    if source["initial_selection_hook_sha256"] != text_sha256(initial_hook) or source[
        "resolved_selection_hook_sha256"
    ] != text_sha256(resolved_hook):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_SELECTION_HOOK_HASH_INVALID")
    for key in (
        "selection_scorecard_sha256",
        "clip_context_prompt_sha256",
        "clip_context_file_sha256",
        "boundary_audit_sha256",
    ):
        _sha(source[key], key)
    media = _mapping(source["source_media"], {"basename", "sha256", "start_ms", "end_ms"}, "SOURCE_MEDIA")
    _nonempty(media["basename"], "SOURCE_MEDIA_BASENAME")
    _sha(media["sha256"], "SOURCE_MEDIA")
    if not all(isinstance(media[key], int) and not isinstance(media[key], bool) for key in ("start_ms", "end_ms")) or media["start_ms"] >= media["end_ms"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_SOURCE_INTERVAL_INVALID")
    srt = _mapping(source["reviewed_srt"], {"sha256", "bytes"}, "REVIEWED_SRT")
    review = _mapping(source["final_review"], {"sha256", "bytes", "schema_version", "status"}, "FINAL_REVIEW")
    chat = _mapping(source["chat_authority"], {"sha256", "bytes", "schema_version", "status"}, "CHAT_AUTHORITY")
    for label, row in (("REVIEWED_SRT", srt), ("FINAL_REVIEW", review), ("CHAT_AUTHORITY", chat)):
        _sha(row["sha256"], label)
        if not isinstance(row["bytes"], int) or isinstance(row["bytes"], bool) or row["bytes"] <= 0:
            raise OperatorExactTitleSourceFactAuthorityError(f"OPERATOR_TITLE_{label}_BYTES_INVALID")
    if review["schema_version"] != "final-review-audit.v2" or review["status"] != "CLEAN":
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_FINAL_REVIEW_INVALID")
    if chat["schema_version"] != "chat-authority-audit.v2" or chat["status"] != "APPLIED_AND_VERIFIED":
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_CHAT_AUTHORITY_INVALID")
    failed = _validate_failed_receipt(
        authority["failed_source_fact_review"], title=title, hook=initial_hook
    )
    pass_row = failed["passes"][0]
    assert isinstance(pass_row, Mapping)
    if pass_row.get("final_selection_hook") != resolved_hook:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RESOLVED_HOOK_INVALID")
    if _sha(failed["receipt_sha256"], "FAILED_RECEIPT") != failed["receipt_sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_FAILED_RECEIPT_INVALID")
    body = dict(authority)
    declared = body.pop("authority_sha256", None)
    if declared != canonical_sha256(body):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_AUTHORITY_HASH_INVALID")
    return authority


def load_operator_exact_title_source_fact_authority(
    candidate_id: str,
    *,
    repo_root: Path = ROOT,
) -> OperatorExactTitleSourceFactAuthority | None:
    if candidate_id != TARGET_CANDIDATE_ID:
        return None
    path = repo_root / AUTHORITY_REPO_PATH
    try:
        expected = repository_authority_expects_asset(
            repo_root=repo_root, relative_path=AUTHORITY_REPO_PATH
        )
    except RepositoryAssetAuthorityError as exc:
        raise OperatorExactTitleSourceFactAuthorityError(
            "OPERATOR_TITLE_REPOSITORY_AUTHORITY_INVALID"
        ) from exc
    if not expected:
        return None
    try:
        payload = read_regular_no_symlink(path)
        require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=AUTHORITY_REPO_PATH,
            observed_bytes=payload,
        )
        document = validate_authority_document(json.loads(payload))
    except (OSError, ValueError, RepositoryAssetAuthorityError) as exc:
        raise OperatorExactTitleSourceFactAuthorityError(
            "OPERATOR_TITLE_AUTHORITY_UNAVAILABLE"
        ) from exc
    return OperatorExactTitleSourceFactAuthority(
        document=document,
        repo_path=AUTHORITY_REPO_PATH,
        file_sha256=bytes_sha256(payload),
    )


def _sidecar_path(srt_path: Path, candidate_id: str, suffix: str) -> Path:
    return srt_path.parent.parent / f"{candidate_id}{suffix}"


def consume_operator_exact_title_source_fact_authority(
    authority: OperatorExactTitleSourceFactAuthority,
    *,
    candidate_id: str,
    title: str,
    selection_hook: str,
    final_transcript: str,
    final_reviewed_srt_path: Path,
    record: Mapping[str, object],
    speaker_evidence: object,
) -> dict[str, object]:
    """Replay every bound source surface without calling a provider."""

    document = validate_authority_document(authority.document)
    if candidate_id != TARGET_CANDIDATE_ID or candidate_id != document["candidate_id"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_CANDIDATE_INVALID")
    approved = dict(document["approved_title"])
    source = dict(document["source_binding"])
    if title != approved["value"] or selection_hook != source["initial_selection_hook"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_SURFACE_DRIFT")
    failed = dict(document["failed_source_fact_review"])
    pass_row = dict(failed["passes"][0])
    if (
        not isinstance(speaker_evidence, Mapping)
        or speaker_evidence != failed.get("speaker_evidence")
        or canonical_sha256(speaker_evidence) != failed.get("speaker_evidence_sha256")
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_SPEAKER_EVIDENCE_DRIFT")
    if text_sha256(final_transcript) != pass_row["final_transcript_sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_TRANSCRIPT_DRIFT")
    srt_payload = read_regular_no_symlink(final_reviewed_srt_path)
    srt = dict(source["reviewed_srt"])
    if len(srt_payload) != srt["bytes"] or bytes_sha256(srt_payload) != srt["sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_SRT_DRIFT")
    contract = record.get("story_contract")
    if not isinstance(contract, Mapping):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_STORY_CONTRACT_INVALID")
    scorecard = contract.get("selection_scorecard")
    prompt = contract.get("clip_context_prompt")
    media = dict(source["source_media"])
    source_piece = json.dumps(
        [{"end_ms": media["end_ms"], "recording_basename": media["basename"], "source_media_sha256": media["sha256"], "start_ms": media["start_ms"]}],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if (
        contract.get("candidate_id") != candidate_id
        or contract.get("selection_hook") != selection_hook
        or contract.get("selection_hook_sha256") != source["initial_selection_hook_sha256"]
        or canonical_sha256(scorecard) != source["selection_scorecard_sha256"]
        or not isinstance(prompt, str)
        or text_sha256(prompt) != source["clip_context_prompt_sha256"]
        or f"source_pieces: {source_piece}" not in prompt
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_STORY_CONTRACT_DRIFT")
    context_path = record.get("clip_context_path")
    chat_path = record.get("chat_authority_audit_path")
    if not isinstance(context_path, str) or not isinstance(chat_path, str):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_SIDECAR_PATH_INVALID")
    context_payload = read_regular_no_symlink(Path(context_path))
    if bytes_sha256(context_payload) != source["clip_context_file_sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_CONTEXT_DRIFT")
    review = dict(source["final_review"])
    review_path = _sidecar_path(final_reviewed_srt_path, candidate_id, ".review-flags.json")
    review_value = _json_file(review_path, str(review["sha256"]), "FINAL_REVIEW")
    if len(read_regular_no_symlink(review_path)) != review["bytes"] or review_value.get("schema_version") != review["schema_version"] or review_value.get("status") != review["status"] or review_value.get("reviewed_srt_sha256") != srt["sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_FINAL_REVIEW_DRIFT")
    chat = dict(source["chat_authority"])
    chat_value = _json_file(Path(chat_path), str(chat["sha256"]), "CHAT_AUTHORITY")
    if len(read_regular_no_symlink(Path(chat_path))) != chat["bytes"] or chat_value.get("schema_version") != chat["schema_version"] or chat_value.get("status") != chat["status"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_CHAT_DRIFT")
    boundary = record.get("boundary_audit")
    if not isinstance(boundary, Mapping) or canonical_sha256(boundary) != source["boundary_audit_sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_BOUNDARY_DRIFT")
    final_boundary = boundary.get("final_delivery_boundary_semantic_review")
    if (
        boundary.get("schema_version") != "boundary-audit.v1"
        or boundary.get("verdict") != "ok_sentence_boundary_cut"
        or not isinstance(final_boundary, Mapping)
        or final_boundary.get("status") != "PASS"
        or not isinstance(final_boundary.get("final_endpoint_binding"), Mapping)
        or final_boundary["final_endpoint_binding"].get("status") != "PASS"
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_BOUNDARY_INVALID")
    return {
        "schema_version": CONSUMPTION_SCHEMA_VERSION,
        "status": "CONSUMED",
        "decision": PASS_DECISION,
        "candidate_id": candidate_id,
        "authority_repo_path": authority.repo_path.as_posix(),
        "authority_file_sha256": authority.file_sha256,
        "authority_sha256": document["authority_sha256"],
        "approved_title_sha256": approved["sha256"],
        "initial_selection_hook_sha256": source["initial_selection_hook_sha256"],
        "resolved_selection_hook_sha256": source["resolved_selection_hook_sha256"],
        "reviewed_srt_sha256": srt["sha256"],
        "failed_source_fact_receipt_sha256": failed["receipt_sha256"],
        "recorded_source_fact_dissent": failed,
        "speaker_evidence_sha256": failed["speaker_evidence_sha256"],
        "provider_pass_claim": False,
    }


def operator_exact_title_source_fact_receipt_body(
    consumption: Mapping[str, object],
) -> dict[str, object]:
    """Build a receipt body; the common source-fact module seals its hash."""

    blocked = consumption.get("recorded_source_fact_dissent")
    if (
        consumption.get("status") != "CONSUMED"
        or consumption.get("decision") != PASS_DECISION
        or consumption.get("provider_pass_claim") is not False
        or not isinstance(blocked, Mapping)
        or blocked.get("status") != "FAILED"
        or blocked.get("receipt_sha256")
        != consumption.get("failed_source_fact_receipt_sha256")
    ):
        raise OperatorExactTitleSourceFactAuthorityError(
            "OPERATOR_TITLE_SOURCE_FACT_CONSUMPTION_INVALID"
        )
    return {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": "PASS",
        "decision": PASS_DECISION,
        "reason_code": "OPERATOR_EXACT_TITLE_WITH_PROVIDER_DISSENT_RECORDED",
        "original_selection_hook": blocked["original_selection_hook"],
        "original_title": blocked["original_title"],
        "final_selection_hook": blocked["passes"][0]["final_selection_hook"],
        "final_title": blocked["original_title"],
        "passes": list(blocked["passes"]),
        "provider_retries": list(blocked["provider_retries"]),
        "recorded_source_fact_dissent": dict(blocked),
        "operator_exact_title_source_fact_authority_consumption": dict(consumption),
    }


def authorize_operator_exact_title_source_fact(
    consumption: Mapping[str, object],
) -> dict[str, object]:
    """Seal this operator-only PASS receipt with the common receipt identity."""

    body = operator_exact_title_source_fact_receipt_body(consumption)
    return {**body, "receipt_sha256": canonical_sha256(body)}


def validate_operator_exact_title_source_fact_receipt(
    review: Mapping[str, object],
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    candidate_id: str | None,
    final_reviewed_srt_path: Path | None,
    record: Mapping[str, object] | None,
    speaker_evidence: object,
    repo_root: Path,
) -> bool:
    """Replay the candidate authority and compare a freshly sealed receipt."""

    if candidate_id is None or final_reviewed_srt_path is None or record is None:
        return False
    try:
        authority = load_operator_exact_title_source_fact_authority(
            candidate_id, repo_root=repo_root
        )
        if authority is None:
            return False
        document = validate_authority_document(authority.document)
        source = dict(document["source_binding"])
        failed = dict(document["failed_source_fact_review"])
        consumption = review.get("operator_exact_title_source_fact_authority_consumption")
        if (
            title != document["approved_title"]["value"]
            or selection_hook != source["resolved_selection_hook"]
            or not isinstance(speaker_evidence, Mapping)
            or speaker_evidence != failed["speaker_evidence"]
            or canonical_sha256(speaker_evidence) != failed["speaker_evidence_sha256"]
            or text_sha256(final_transcript) != failed["passes"][0]["final_transcript_sha256"]
            or not isinstance(consumption, Mapping)
            or consumption.get("authority_repo_path") != authority.repo_path.as_posix()
            or consumption.get("authority_file_sha256") != authority.file_sha256
            or consumption.get("authority_sha256") != document["authority_sha256"]
            or consumption.get("approved_title_sha256") != document["approved_title"]["sha256"]
            or consumption.get("initial_selection_hook_sha256")
            != source["initial_selection_hook_sha256"]
            or consumption.get("resolved_selection_hook_sha256")
            != source["resolved_selection_hook_sha256"]
            or consumption.get("reviewed_srt_sha256") != source["reviewed_srt"]["sha256"]
            or consumption.get("failed_source_fact_receipt_sha256") != failed["receipt_sha256"]
            or consumption.get("speaker_evidence_sha256") != failed["speaker_evidence_sha256"]
            or consumption.get("recorded_source_fact_dissent") != failed
            or consumption.get("provider_pass_claim") is not False
        ):
            return False
        _validate_final_runtime(
            source,
            candidate_id=candidate_id,
            selection_hook=selection_hook,
            final_reviewed_srt_path=final_reviewed_srt_path,
            record=record,
        )
        expected = authorize_operator_exact_title_source_fact(consumption)
    except (OperatorExactTitleSourceFactAuthorityError, OSError, ValueError):
        return False
    return dict(review) == expected


def _validate_final_runtime(
    source: Mapping[str, object],
    *,
    candidate_id: str,
    selection_hook: str,
    final_reviewed_srt_path: Path,
    record: Mapping[str, object],
) -> None:
    """Validate the rebuilt StoryContract and unchanged final sidecars."""

    srt = dict(source["reviewed_srt"])
    srt_payload = read_regular_no_symlink(final_reviewed_srt_path)
    if len(srt_payload) != srt["bytes"] or bytes_sha256(srt_payload) != srt["sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_SRT_DRIFT")
    contract = record.get("story_contract")
    if not isinstance(contract, Mapping):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_STORY_CONTRACT_INVALID")
    media = dict(source["source_media"])
    source_piece = json.dumps(
        [{"end_ms": media["end_ms"], "recording_basename": media["basename"], "source_media_sha256": media["sha256"], "start_ms": media["start_ms"]}],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt = contract.get("clip_context_prompt")
    if (
        contract.get("candidate_id") != candidate_id
        or contract.get("selection_hook") != selection_hook
        or contract.get("selection_hook_sha256") != source["resolved_selection_hook_sha256"]
        or canonical_sha256(contract.get("selection_scorecard"))
        != source["selection_scorecard_sha256"]
        or not isinstance(prompt, str)
        or selection_hook not in prompt
        or f"source_pieces: {source_piece}" not in prompt
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_STORY_CONTRACT_DRIFT")
    context_path = record.get("clip_context_path")
    chat_path = record.get("chat_authority_audit_path")
    if not isinstance(context_path, str) or not isinstance(chat_path, str):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_SIDECAR_PATH_INVALID")
    context_value = _json_file(Path(context_path), bytes_sha256(read_regular_no_symlink(Path(context_path))), "CONTEXT")
    if context_value.get("candidate_id") != candidate_id:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_CONTEXT_DRIFT")
    review = dict(source["final_review"])
    review_path = _sidecar_path(final_reviewed_srt_path, candidate_id, ".review-flags.json")
    review_value = _json_file(review_path, str(review["sha256"]), "FINAL_REVIEW")
    if len(read_regular_no_symlink(review_path)) != review["bytes"] or review_value.get("schema_version") != review["schema_version"] or review_value.get("status") != review["status"] or review_value.get("reviewed_srt_sha256") != srt["sha256"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_FINAL_REVIEW_DRIFT")
    chat = dict(source["chat_authority"])
    chat_value = _json_file(Path(chat_path), str(chat["sha256"]), "CHAT_AUTHORITY")
    if len(read_regular_no_symlink(Path(chat_path))) != chat["bytes"] or chat_value.get("schema_version") != chat["schema_version"] or chat_value.get("status") != chat["status"]:
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_CHAT_DRIFT")
    boundary = record.get("boundary_audit")
    final_boundary = boundary.get("final_delivery_boundary_semantic_review") if isinstance(boundary, Mapping) else None
    if (
        not isinstance(boundary, Mapping)
        or canonical_sha256(boundary) != source["boundary_audit_sha256"]
        or boundary.get("schema_version") != "boundary-audit.v1"
        or boundary.get("verdict") != "ok_sentence_boundary_cut"
        or not isinstance(final_boundary, Mapping)
        or final_boundary.get("status") != "PASS"
        or not isinstance(final_boundary.get("final_endpoint_binding"), Mapping)
        or final_boundary["final_endpoint_binding"].get("status") != "PASS"
    ):
        raise OperatorExactTitleSourceFactAuthorityError("OPERATOR_TITLE_RUNTIME_BOUNDARY_INVALID")
