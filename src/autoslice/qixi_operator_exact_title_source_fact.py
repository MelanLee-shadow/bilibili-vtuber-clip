"""Candidate-sealed operator title successor for Qixi's 女友感 package.

The recorded provider PASS remains historical evidence for the old title and
unchanged hook.  It is never relabelled as a review of Ivan's replacement
title.  This module is deliberately fixed to one candidate and one authority
asset; it is not a fallback for generic source-fact review.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
SCHEMA_VERSION = "qixi-operator-exact-title-source-fact-authority.v1"
CONSUMPTION_SCHEMA_VERSION = "qixi-operator-exact-title-source-fact-consumption.v1"
DECISION = "QIXI_OPERATOR_EXACT_TITLE_WITH_HISTORICAL_PROVIDER_PASS"
AUTHORITY_STATUS = "RESOLVED_QIXI_OPERATOR_EXACT_TITLE_HISTORICAL_PASS"
ASSET_PATH = Path(
    "assets/lidousha/authorities/auto_123655_771_844.qixi-operator-exact-title-source-fact-authority.v1.json"
)


class QixiOperatorExactTitleSourceFactError(ValueError):
    """The narrow title successor is absent, malformed, or stale."""


@dataclass(frozen=True, slots=True)
class Authority:
    document: dict[str, object]
    repo_path: Path
    file_sha256: str


def canonical_sha256(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def text_sha256(value: str) -> str:
    return bytes_sha256(value.encode())


def _mapping(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise QixiOperatorExactTitleSourceFactError(f"QIXI_OPERATOR_TITLE_{label}_SCHEMA_INVALID")
    return dict(value)


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise QixiOperatorExactTitleSourceFactError(f"QIXI_OPERATOR_TITLE_{label}_HASH_INVALID")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise QixiOperatorExactTitleSourceFactError(
            f"QIXI_OPERATOR_TITLE_{label}_HASH_INVALID"
        ) from exc
    return value


def _bare_sha(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise QixiOperatorExactTitleSourceFactError(f"QIXI_OPERATOR_TITLE_{label}_HASH_INVALID")
    return _sha(f"sha256:{value}", label)[7:]


def _regular_bytes(path: Path) -> bytes:
    cursor = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            cursor /= part
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                raise QixiOperatorExactTitleSourceFactError(
                    "QIXI_OPERATOR_TITLE_RUNTIME_PATH_CONTAINS_SYMLINK"
                )
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise QixiOperatorExactTitleSourceFactError(
                    "QIXI_OPERATOR_TITLE_RUNTIME_FILE_NOT_REGULAR"
                )
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except QixiOperatorExactTitleSourceFactError:
        raise
    except OSError as exc:
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_RUNTIME_FILE_UNREADABLE"
        ) from exc


def _descriptor(value: object, *, state: bool = False) -> dict[str, object]:
    keys = {"path", "sha256", "bytes", "mode"} if state else {"path", "sha256", "bytes"}
    row = _mapping(value, keys, "STATE_DESCRIPTOR" if state else "DESCRIPTOR")
    if not isinstance(row["path"], str) or not Path(str(row["path"])).is_absolute():
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_DESCRIPTOR_PATH_INVALID")
    _sha(row["sha256"], "DESCRIPTOR")
    if not isinstance(row["bytes"], int) or isinstance(row["bytes"], bool) or row["bytes"] < 1:
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_DESCRIPTOR_BYTES_INVALID")
    if state and (
        not isinstance(row["mode"], int)
        or isinstance(row["mode"], bool)
        or not 0 <= row["mode"] <= 0o777
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_DESCRIPTOR_MODE_INVALID")
    return row


def _check_file(descriptor: Mapping[str, object], *, label: str, mode: bool = False) -> bytes:
    payload = _regular_bytes(Path(str(descriptor["path"])))
    if len(payload) != descriptor["bytes"] or bytes_sha256(payload) != descriptor["sha256"]:
        raise QixiOperatorExactTitleSourceFactError(f"QIXI_OPERATOR_TITLE_{label}_DRIFT")
    if mode and stat.S_IMODE(os.lstat(str(descriptor["path"])).st_mode) != descriptor["mode"]:
        raise QixiOperatorExactTitleSourceFactError(f"QIXI_OPERATOR_TITLE_{label}_MODE_DRIFT")
    return payload


def _json_payload(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiOperatorExactTitleSourceFactError(
            f"QIXI_OPERATOR_TITLE_{label}_JSON_INVALID"
        ) from exc
    if not isinstance(value, Mapping):
        raise QixiOperatorExactTitleSourceFactError(f"QIXI_OPERATOR_TITLE_{label}_SCHEMA_INVALID")
    return dict(value)


def _source_piece_prompt(piece: Mapping[str, object]) -> str:
    return json.dumps(
        [
            {
                "end_ms": piece["end_ms"],
                "recording_basename": piece["basename"],
                "source_media_sha256": piece["sha256"],
                "start_ms": piece["start_ms"],
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _receipt_is_historical_pass(value: object, binding: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_HISTORICAL_RECEIPT_INVALID"
        )
    receipt = dict(value)
    body = dict(receipt)
    declared = body.pop("receipt_sha256", None)
    if (
        receipt.get("schema_version") != "lidousha-source-fact-review.v1"
        or receipt.get("status") != "PASS"
        or receipt.get("decision") != "KEEP"
        or not isinstance(declared, str)
        or canonical_sha256(body) != declared
        or declared != binding["receipt_sha256"]
        or receipt.get("original_title") != binding["historical_title"]
        or receipt.get("final_title") != binding["historical_title"]
        or receipt.get("original_selection_hook") != binding["selection_hook"]
        or receipt.get("final_selection_hook") != binding["selection_hook"]
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_HISTORICAL_RECEIPT_DRIFT")
    passes = receipt.get("passes")
    if not isinstance(passes, list) or len(passes) != 1 or not isinstance(passes[0], Mapping):
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_HISTORICAL_RECEIPT_INVALID"
        )
    row = passes[0]
    if (
        row.get("status") != "KEEP"
        or row.get("final_transcript_sha256") != binding["final_transcript_sha256"]
        or row.get("clip_context_prompt_sha256") != binding["clip_context_prompt_sha256"]
        or row.get("selection_scorecard_sha256") != binding["selection_scorecard_sha256"]
        or receipt.get("speaker_evidence") != binding["speaker_evidence"]
        or receipt.get("speaker_evidence_sha256") != binding["speaker_evidence_sha256"]
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_HISTORICAL_RECEIPT_DRIFT")
    return receipt


def validate_authority_document(value: object) -> dict[str, object]:
    top = _mapping(
        value,
        {
            "schema_version",
            "candidate_id",
            "recording_date",
            "scope",
            "operator_authorization",
            "approved_title",
            "source_binding",
            "historical_provider_pass",
            "sealed_before",
            "authority_sha256",
        },
        "AUTHORITY",
    )
    body = dict(top)
    declared = body.pop("authority_sha256")
    if declared != canonical_sha256(body):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_AUTHORITY_HASH_INVALID")
    if (
        top["schema_version"] != SCHEMA_VERSION
        or top["candidate_id"] != CANDIDATE_ID
        or top["recording_date"] != RECORDING_DATE
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_SCOPE_INVALID")
    if top["scope"] != {
        "candidate_scoped": True,
        "title_source_fact_projection_only": True,
        "subtitle_mutation": False,
        "media_mutation": False,
        "boundary_mutation": False,
        "cover_mutation": False,
        "upload_authorization": False,
        "provider_pass_claim": False,
    }:
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_SCOPE_INVALID")
    operator = _mapping(
        top["operator_authorization"],
        {"source", "conversation_id", "line", "timestamp", "quote", "quote_sha256"},
        "OPERATOR",
    )
    if (
        operator["source"] != "Claude JSONL"
        or operator["conversation_id"] != "0df2296b-500a-4681-ab5e-6fb46dc39579"
        or operator["line"] != 947
        or operator["timestamp"] != "2026-08-19T00:08:52.249Z"
        or not isinstance(operator["quote"], str)
        or operator["quote_sha256"] != text_sha256(operator["quote"])
    ):
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_OPERATOR_AUTHORIZATION_INVALID"
        )
    approved = _mapping(top["approved_title"], {"value", "sha256"}, "APPROVED_TITLE")
    if not isinstance(approved["value"], str) or approved["sha256"] != text_sha256(
        approved["value"]
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_APPROVED_TITLE_INVALID")
    binding = _mapping(
        top["source_binding"],
        {
            "selection_hook",
            "selection_hook_sha256",
            "selection_scorecard_sha256",
            "clip_context_prompt_sha256",
            "final_transcript_sha256",
            "preprovider_story_contract_sha256",
            "speaker_evidence",
            "speaker_evidence_sha256",
            "reviewed_srt",
            "correction",
            "clip_context",
            "chat_authority",
            "boundary_audit_sha256",
            "source_piece",
            "public_surface_authority",
        },
        "SOURCE_BINDING",
    )
    for key in (
        "selection_hook_sha256",
        "selection_scorecard_sha256",
        "clip_context_prompt_sha256",
        "final_transcript_sha256",
        "preprovider_story_contract_sha256",
        "speaker_evidence_sha256",
        "boundary_audit_sha256",
    ):
        _sha(binding[key], key)
    if not isinstance(binding["selection_hook"], str) or binding[
        "selection_hook_sha256"
    ] != text_sha256(binding["selection_hook"]):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_HOOK_INVALID")
    if canonical_sha256(binding["speaker_evidence"]) != binding["speaker_evidence_sha256"]:
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_SPEAKER_INVALID")
    for key in ("reviewed_srt", "correction", "clip_context", "chat_authority"):
        _descriptor(binding[key])
    public = _mapping(
        binding["public_surface_authority"],
        {"relative_path", "sha256", "bytes", "authority_sha256"},
        "PUBLIC_SURFACE_AUTHORITY",
    )
    if (
        not isinstance(public["relative_path"], str)
        or Path(public["relative_path"]).is_absolute()
        or ".." in Path(public["relative_path"]).parts
    ):
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_PUBLIC_AUTHORITY_PATH_INVALID"
        )
    _sha(public["sha256"], "PUBLIC_SURFACE_AUTHORITY")
    _sha(public["authority_sha256"], "PUBLIC_SURFACE_AUTHORITY")
    if (
        not isinstance(public["bytes"], int)
        or isinstance(public["bytes"], bool)
        or public["bytes"] < 1
    ):
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_PUBLIC_AUTHORITY_BYTES_INVALID"
        )
    piece = _mapping(
        binding["source_piece"], {"basename", "sha256", "start_ms", "end_ms"}, "SOURCE_PIECE"
    )
    if (
        not isinstance(piece["basename"], str)
        or not piece["basename"]
        or _sha(piece["sha256"], "SOURCE_MEDIA") != piece["sha256"]
        or not all(
            isinstance(piece[k], int) and not isinstance(piece[k], bool)
            for k in ("start_ms", "end_ms")
        )
        or piece["start_ms"] >= piece["end_ms"]
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_SOURCE_PIECE_INVALID")
    historical = _mapping(
        top["historical_provider_pass"],
        {
            "receipt",
            "receipt_sha256",
            "historical_title",
            "selection_hook",
            "final_transcript_sha256",
            "clip_context_prompt_sha256",
            "selection_scorecard_sha256",
            "speaker_evidence",
            "speaker_evidence_sha256",
        },
        "HISTORICAL_PASS",
    )
    for key in (
        "receipt_sha256",
        "final_transcript_sha256",
        "clip_context_prompt_sha256",
        "selection_scorecard_sha256",
        "speaker_evidence_sha256",
    ):
        _sha(historical[key], key)
    if (
        not all(
            isinstance(historical[key], str) and historical[key]
            for key in ("historical_title", "selection_hook")
        )
        or historical["selection_hook"] != binding["selection_hook"]
        or historical["speaker_evidence"] != binding["speaker_evidence"]
        or historical["speaker_evidence_sha256"] != binding["speaker_evidence_sha256"]
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_HISTORICAL_PASS_INVALID")
    _receipt_is_historical_pass(historical["receipt"], historical)
    sealed = _mapping(
        top["sealed_before"], {"record", "delivery_record", "publish", "state"}, "SEALED_BEFORE"
    )
    for key in ("record", "delivery_record", "publish"):
        _descriptor(sealed[key])
    _descriptor(sealed["state"], state=True)
    return top


def load_authority(candidate_id: str, *, repo_root: Path = ROOT) -> Authority | None:
    if candidate_id != CANDIDATE_ID:
        return None
    try:
        if not repository_authority_expects_asset(repo_root=repo_root, relative_path=ASSET_PATH):
            return None
        payload = _regular_bytes(repo_root / ASSET_PATH)
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=ASSET_PATH, observed_bytes=payload
        )
        document = validate_authority_document(json.loads(payload))
    except (
        OSError,
        ValueError,
        RepositoryAssetAuthorityError,
    ) as exc:
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_AUTHORITY_UNAVAILABLE"
        ) from exc
    return Authority(document, ASSET_PATH, bytes_sha256(payload))


def consume_authority(
    authority: Authority,
    *,
    candidate_id: str,
    title: str,
    selection_hook: str,
    final_transcript: str,
    final_reviewed_srt_path: Path,
    record: Mapping[str, object],
    speaker_evidence: object,
    projected_receipt: Mapping[str, object] | None = None,
    verify_sealed_before: bool = True,
    allow_preprovider_receipt_absent: bool = False,
) -> dict[str, object]:
    """Recheck the sealed preimage before any provider or cover can run."""

    document = validate_authority_document(authority.document)
    binding = dict(document["source_binding"])
    historical = dict(document["historical_provider_pass"])
    if (
        candidate_id != CANDIDATE_ID
        or title != document["approved_title"]["value"]
        or selection_hook != binding["selection_hook"]
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_RUNTIME_SURFACE_DRIFT")
    if (
        not isinstance(speaker_evidence, Mapping)
        or speaker_evidence != binding["speaker_evidence"]
        or canonical_sha256(speaker_evidence) != binding["speaker_evidence_sha256"]
        or text_sha256(final_transcript) != binding["final_transcript_sha256"]
    ):
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_RUNTIME_TRANSCRIPT_OR_SPEAKER_DRIFT"
        )
    if _regular_bytes(final_reviewed_srt_path) != _check_file(binding["reviewed_srt"], label="SRT"):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_RUNTIME_SRT_PATH_DRIFT")
    correction = _json_payload(
        _check_file(binding["correction"], label="CORRECTION"), label="CORRECTION"
    )
    context = _json_payload(_check_file(binding["clip_context"], label="CONTEXT"), label="CONTEXT")
    chat = _json_payload(_check_file(binding["chat_authority"], label="CHAT"), label="CHAT")
    reviewed_srt = binding["reviewed_srt"]
    correction_before_srt_sha256 = _bare_sha(
        correction.get("before_srt_sha256"), "CORRECTION_BEFORE_SRT"
    )
    if (
        correction.get("schema_version") != "human-subtitle-correction.v2"
        or correction.get("candidate_id") != candidate_id
        or correction.get("after_srt_sha256") != str(reviewed_srt["sha256"])[7:]
        or context.get("schema_version") != "lidousha-clip-context.v1"
        or context.get("candidate_id") != candidate_id
        or context.get("selection_hook") != selection_hook
        or chat.get("schema_version") != "chat-authority-audit.v2"
        or chat.get("status") != "APPLIED_AND_VERIFIED"
        or chat.get("final_text_srt_sha256") != correction_before_srt_sha256
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_RUNTIME_SIDECAR_DRIFT")
    historical_receipt = _receipt_is_historical_pass(historical["receipt"], historical)
    if verify_sealed_before:
        record_payload = _check_file(document["sealed_before"]["record"], label="RECORD")
        delivery_payload = _check_file(
            document["sealed_before"]["delivery_record"], label="DELIVERY_RECORD"
        )
        publish_payload = _check_file(document["sealed_before"]["publish"], label="PUBLISH")
        state_payload = _check_file(document["sealed_before"]["state"], label="STATE", mode=True)
        if record_payload != delivery_payload:
            raise QixiOperatorExactTitleSourceFactError(
                "QIXI_OPERATOR_TITLE_RUNTIME_RECORD_MIRROR_DRIFT"
            )
        sealed_record = _json_payload(record_payload, label="RECORD")
        sealed_publish = _json_payload(publish_payload, label="PUBLISH")
        state = _json_payload(state_payload, label="STATE")
        if (
            (sealed_record.get("story_contract") or {}).get("source_fact_review")
            != historical_receipt
            or sealed_publish.get("source_fact_review") != historical_receipt
            or (sealed_record.get("publish_staging") or {}).get("source_fact_review")
            != historical_receipt
        ):
            raise QixiOperatorExactTitleSourceFactError(
                "QIXI_OPERATOR_TITLE_RUNTIME_HISTORICAL_MIRROR_DRIFT"
            )
        rows = [
            row
            for row in state.get("picks", [])
            if isinstance(row, Mapping) and row.get("candidate_id") == candidate_id
        ]
        if (
            len(rows) != 1
            or rows[0].get("title") != historical["historical_title"]
            or rows[0].get("upload_enabled") is True
        ):
            raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_RUNTIME_STATE_DRIFT")
    expected_runtime_receipts: tuple[Mapping[str, object], ...] = (historical_receipt,)
    if projected_receipt is not None:
        expected_runtime_receipts += (projected_receipt,)
    contract = record.get("story_contract")
    preprovider_receipt_absent = (
        allow_preprovider_receipt_absent
        and verify_sealed_before
        and projected_receipt is None
        and isinstance(contract, Mapping)
        and contract.get("source_fact_review") is None
        and canonical_sha256(contract) == binding["preprovider_story_contract_sha256"]
    )
    if (
        not isinstance(contract, Mapping)
        or contract.get("candidate_id") != candidate_id
        or contract.get("selection_hook") != selection_hook
        or contract.get("selection_hook_sha256") != binding["selection_hook_sha256"]
        or canonical_sha256(contract.get("selection_scorecard"))
        != binding["selection_scorecard_sha256"]
        or text_sha256(str(contract.get("clip_context_prompt") or ""))
        != binding["clip_context_prompt_sha256"]
        or f"source_pieces: {_source_piece_prompt(binding['source_piece'])}"
        not in str(contract.get("clip_context_prompt") or "")
        or canonical_sha256(record.get("boundary_audit")) != binding["boundary_audit_sha256"]
        or (
            contract.get("source_fact_review") not in expected_runtime_receipts
            and not preprovider_receipt_absent
        )
    ):
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_RUNTIME_STORY_CONTRACT_DRIFT"
        )
    return {
        "schema_version": CONSUMPTION_SCHEMA_VERSION,
        "status": "CONSUMED",
        "decision": DECISION,
        "candidate_id": candidate_id,
        "authority_repo_path": authority.repo_path.as_posix(),
        "authority_file_sha256": authority.file_sha256,
        "authority_sha256": document["authority_sha256"],
        "approved_title": document["approved_title"]["value"],
        "approved_title_sha256": document["approved_title"]["sha256"],
        "selection_hook_sha256": binding["selection_hook_sha256"],
        "reviewed_srt_sha256": binding["reviewed_srt"]["sha256"],
        "historical_provider_pass": historical_receipt,
        "historical_provider_pass_sha256": historical["receipt_sha256"],
        "provider_pass_claim": False,
    }


def authorize(consumption: Mapping[str, object]) -> dict[str, object]:
    historical = consumption.get("historical_provider_pass")
    if (
        consumption.get("status") != "CONSUMED"
        or consumption.get("decision") != DECISION
        or consumption.get("provider_pass_claim") is not False
        or not isinstance(historical, Mapping)
        or historical.get("status") != "PASS"
        or historical.get("receipt_sha256") != consumption.get("historical_provider_pass_sha256")
    ):
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_CONSUMPTION_INVALID")
    body = {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": "PASS",
        "decision": DECISION,
        "reason_code": "OPERATOR_EXACT_TITLE_WITH_HISTORICAL_PROVIDER_PASS_RECORDED",
        "original_selection_hook": historical["original_selection_hook"],
        "original_title": historical["original_title"],
        "final_selection_hook": historical["final_selection_hook"],
        "final_title": consumption["approved_title"],
        "passes": [],
        "provider_retries": [],
        "historical_provider_pass": dict(historical),
        "operator_exact_title_source_fact_authority_consumption": dict(consumption),
    }
    return {**body, "receipt_sha256": canonical_sha256(body)}


def _validate_committed_public_successor(repo_root: Path, document: Mapping[str, object]) -> None:
    """Replay the sealed Qixi public-surface closure through its canonical gate."""

    binding = dict(document["source_binding"])
    sealed = dict(document["sealed_before"])
    public = dict(binding["public_surface_authority"])
    relative = Path(str(public["relative_path"]))
    payload = _regular_bytes(repo_root / relative)
    require_repository_asset_authority(
        repo_root=repo_root, relative_path=relative, observed_bytes=payload
    )
    if len(payload) != public["bytes"] or bytes_sha256(payload) != public["sha256"]:
        raise QixiOperatorExactTitleSourceFactError("QIXI_OPERATOR_TITLE_PUBLIC_AUTHORITY_DRIFT")
    try:
        from src.autoslice import qixi_post_correction_public_surface as public_surface

        public_document = public_surface.validate_authority(json.loads(payload))
        if public_document.get("authority_sha256") != public["authority_sha256"]:
            raise ValueError("bound public authority self hash drifted")
        public_predecessor = {
            role: {
                key: public_document["sealed_before"][role][key]
                for key in (
                    {"path", "bytes", "sha256", "mode"}
                    if role == "state"
                    else {"path", "bytes", "sha256"}
                )
            }
            for role in ("record", "delivery_record", "publish", "state")
        }
        if public_predecessor != sealed:
            raise ValueError("operator and public predecessor bindings differ")
        root = public_surface._journal_root_from_authority(public_document)
        journal = public_surface._load_journal(root, authority=public_document)
        if journal is None or journal.get("status") != "COMMITTED":
            raise ValueError("committed public-surface journal is missing")
        if not (root / "final-receipt.json").is_file():
            raise ValueError("committed public-surface final receipt is missing")
        public_surface._postcommit_replay(journal, authority=public_document)
    except (
        OSError,
        ValueError,
        RepositoryAssetAuthorityError,
        public_surface.QixiPostCorrectionPublicSurfaceError,
    ) as exc:
        raise QixiOperatorExactTitleSourceFactError(
            "QIXI_OPERATOR_TITLE_POSTCOMMIT_PROOF_INVALID"
        ) from exc


def validate_receipt(
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
    verify_successor: bool = True,
) -> bool:
    if candidate_id != CANDIDATE_ID or final_reviewed_srt_path is None or record is None:
        return False
    try:
        authority = load_authority(candidate_id, repo_root=repo_root)
        if authority is None:
            return False
        document = validate_authority_document(authority.document)
        consumption = review.get("operator_exact_title_source_fact_authority_consumption")
        if (
            not isinstance(consumption, Mapping)
            or title != document["approved_title"]["value"]
            or selection_hook != document["source_binding"]["selection_hook"]
        ):
            return False
        expected_consumption = consume_authority(
            authority,
            candidate_id=candidate_id,
            title=title,
            selection_hook=selection_hook,
            final_transcript=final_transcript,
            final_reviewed_srt_path=final_reviewed_srt_path,
            record=record,
            speaker_evidence=speaker_evidence,
            projected_receipt=review,
            verify_sealed_before=False,
        )
        if verify_successor:
            _validate_committed_public_successor(repo_root, document)
        return (
            dict(review) == authorize(expected_consumption) and consumption == expected_consumption
        )
    except (OSError, TypeError, ValueError):
        return False


def validate_public_surface_receipt(
    review: Mapping[str, object],
    *,
    generic_validator: Callable[..., bool],
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    final_reviewed_srt_path: Path,
    record: Mapping[str, object],
    speaker_evidence: object,
    repo_root: Path,
    verify_successor: bool = False,
) -> bool:
    """Select the sealed Qixi replay without widening the generic validator."""

    if review.get("decision") == DECISION:
        return validate_receipt(
            review,
            selection_hook=selection_hook,
            title=title,
            final_transcript=final_transcript,
            candidate_id=CANDIDATE_ID,
            final_reviewed_srt_path=final_reviewed_srt_path,
            record=record,
            speaker_evidence=speaker_evidence,
            repo_root=repo_root,
            verify_successor=verify_successor,
        )
    return generic_validator(
        review,
        selection_hook=selection_hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        candidate_id=CANDIDATE_ID,
        final_reviewed_srt_path=final_reviewed_srt_path,
        speaker_evidence=speaker_evidence,
        story_contract=record.get("story_contract"),
    )
