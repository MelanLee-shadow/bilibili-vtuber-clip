"""Canonical provider refresh for Qixi's corrected terminal evidence.

This is intentionally a *review* primitive, rather than a text-repair
primitive.  It is fixed to the 女友感 candidate and consumes the already
sealed human correction.  Callers must stage its returned JSON documents and
perform their own sealed transaction; this module never writes a runtime
target and never alters the SRT, ASS, or media bytes.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)
from src.autoslice.final_review_auditor import _final_review_structured_context
from src.autoslice.final_review_auditor import audit_correction_mutation_authority
from src.autoslice.clip_context import clip_context_prompt_text
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.chat_evidence import ChatEvidence
from src.autoslice.producer_boundary_review_stage import exact_delivery_correction_audit
from src.autoslice.producer_text_pipeline import (
    TextPipelineAdapters,
    _run_exact_final_release_review,
)
from src.autoslice.repository_asset_authority import require_repository_asset_authority
from src.autoslice.qixi_transaction_core import (
    FileSnapshot,
    InstallCallbacks,
    QixiTransactionCoreError,
    create_staged_inode,
    install_checkpointed_inode,
    journal_before_snapshot,
    restore_owned_inode,
    safe_parent,
    stable_regular_snapshot,
)


CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = Path(
    "assets/lidousha/qixi_terminal_evidence_refresh/auto_123655_771_844.v1.json"
)
HUMAN_TRUTH_AUTHORITY_PATH = Path(
    "assets/lidousha/sealed_subtitle_corrections/auto_123655_771_844.v1.json"
)
_SOURCE_FINAL_START_MS = 9_780
_SOURCE_FINAL_END_MS = 82_670
_JOURNAL_ROLES = ("chat", "record", "delivery_record", "publish", "state")
_ENTRY_PHASES = frozenset({"PREPARED", "INSTALLING", "INSTALLED"})
_JOURNAL_STATUSES = frozenset(
    {"PREPARED", "ROLLBACK_REQUIRED", "ROLLED_BACK", "COMMITTED"}
)
_LIVE_CORRECTION_KEYS = frozenset(
    {
        "schema_version", "stage_order", "corrected_at", "candidate_id",
        "before_srt_sha256", "after_srt_sha256", "replace_operations", "set_line_operations",
        "refresh_only", "text_source", "text_source_sha256", "text_override",
        "text_override_sha256", "text_override_manifest", "text_override_manifest_sha256",
        "text_override_decision_output", "text_override_decision_output_sha256",
        "text_override_output", "text_override_output_sha256", "timing_source",
        "timing_source_sha256", "speaker_mode", "speaker_manifest", "speaker_manifest_sha256",
        "burned_media", "burned_media_sha256", "delivery_branding_authority", "upload_enabled",
    }
)
_LIVE_DELIVERY_AUTHORITY_KEYS = frozenset(
    {
        "schema_version", "authority_path", "authority_sha256", "authority_repository_seal",
        "branding_intro", "record_sha256", "publish_sha256", "burned_video_sha256",
    }
)
_DIAGNOSTIC_REASON_RX = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
_DIAGNOSTIC_STATUS_RX = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DIAGNOSTIC_MAX_REASON_CODES = 32


class QixiTerminalEvidenceRefreshError(ValueError):
    """The sealed correction cannot safely receive fresh terminal reviews."""

    def __init__(
        self,
        reason_code: str,
        *,
        underlying_reason_code: str | None = None,
        diagnostic_result: Mapping[str, object] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.underlying_reason_code = underlying_reason_code
        # This is populated only by the local, allowlisted diagnostic builder
        # below.  In particular, no exception string or provider payload gets
        # attached to an error which the CLI can render.
        self.diagnostic_result = (
            copy.deepcopy(dict(diagnostic_result))
            if diagnostic_result is not None
            else None
        )
        super().__init__(reason_code)


def _diagnostic_token(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and _DIAGNOSTIC_STATUS_RX.fullmatch(value):
        return value
    return fallback


def _diagnostic_reason_codes(value: object) -> list[str]:
    """Return a bounded, text-free set of stable machine reason codes."""

    if not isinstance(value, list):
        return []
    codes = {
        code
        for code in value[:_DIAGNOSTIC_MAX_REASON_CODES]
        if isinstance(code, str) and _DIAGNOSTIC_REASON_RX.fullmatch(code)
    }
    return sorted(codes)[:_DIAGNOSTIC_MAX_REASON_CODES]


def _diagnostic_finding_reason_codes(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    codes = {
        str(row["reason_code"])
        for row in value[:_DIAGNOSTIC_MAX_REASON_CODES]
        if isinstance(row, Mapping)
        and isinstance(row.get("reason_code"), str)
        and _DIAGNOSTIC_REASON_RX.fullmatch(str(row["reason_code"]))
    }
    return sorted(codes)[:_DIAGNOSTIC_MAX_REASON_CODES]


def _diagnostic_cue_indices(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    indices = {
        index
        for row in value[:_DIAGNOSTIC_MAX_REASON_CODES]
        if isinstance(row, Mapping)
        for index in (row.get("cue_index", row.get("cue")),)
        if isinstance(index, int) and not isinstance(index, bool) and 0 < index <= 1_000_000
    }
    return sorted(indices)[:_DIAGNOSTIC_MAX_REASON_CODES]


def _diagnostic_sha256(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    return None


def _final_review_failure_diagnostic(
    *,
    final_audit: object,
    expected_srt_sha256: str,
    reason_code: str,
) -> dict[str, object]:
    """Produce a bounded observation, never an audit/provider transcript.

    The audit is provider-adjacent and may contain prompts, excerpt text,
    endpoints, or other private material.  This projection is deliberately
    allowlisted to the machine predicates an operator needs to distinguish a
    true unresolved finding from a closure-contract failure.
    """

    audit = dict(final_audit) if isinstance(final_audit, Mapping) else {}
    discovery = audit.get("discovery")
    discovery_map = dict(discovery) if isinstance(discovery, Mapping) else {}
    boundary = audit.get("boundary_semantic_review")
    boundary_map = dict(boundary) if isinstance(boundary, Mapping) else {}
    findings = audit.get("findings")
    finding_count = len(findings) if isinstance(findings, list) else None
    mutation_authority = audit.get("correction_mutation_authority")
    mutation_map = (
        dict(mutation_authority) if isinstance(mutation_authority, Mapping) else {}
    )
    mutation_failures = mutation_map.get("failures")
    mutation_failure_count = (
        len(mutation_failures) if isinstance(mutation_failures, list) else None
    )
    validated_count = audit.get("validated_finding_count")
    if isinstance(validated_count, bool) or not isinstance(validated_count, int):
        validated_count = None

    return {
        "schema_version": "qixi-terminal-evidence-refresh-full-dry-run-diagnostic.v1",
        "status": "FULL_DRY_RUN_BLOCKED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "upload_enabled": False,
        "predicate": {
            "name": "final_review_contract",
            "status": "FAIL",
            "reason_code": _diagnostic_token(
                reason_code, fallback="FINAL_REVIEW_REASON_CODE_INVALID"
            ),
        },
        "final_review_reason_code": _diagnostic_token(
            reason_code, fallback="FINAL_REVIEW_REASON_CODE_INVALID"
        ),
        # This is the hash calculated from the exact SRT passed to the final
        # reviewer, not an untrusted value copied from its response.
        "reviewed_srt_sha256": expected_srt_sha256,
        "reported_reviewed_srt_sha256": _diagnostic_sha256(
            audit.get("reviewed_srt_sha256")
        ),
        "final_review": {
            "status": _diagnostic_token(audit.get("status"), fallback="UNAVAILABLE"),
            "release_gate": _diagnostic_token(
                audit.get("release_gate"), fallback="UNAVAILABLE"
            ),
            "reason_codes": _diagnostic_reason_codes(audit.get("reason_codes")),
        },
        "discovery": {
            "status": _diagnostic_token(
                discovery_map.get("status"), fallback="UNAVAILABLE"
            ),
            "finding_count": finding_count,
            "reason_codes": _diagnostic_reason_codes(
                discovery_map.get("reason_codes")
            ),
        },
        "findings": {
            "count": finding_count,
            "validated_count": validated_count,
            "reason_codes": _diagnostic_finding_reason_codes(findings),
            "cue_indices": _diagnostic_cue_indices(findings),
        },
        "correction_mutation_authority": {
            "status": _diagnostic_token(
                mutation_map.get("status"), fallback="UNAVAILABLE"
            ),
            "failure_count": mutation_failure_count,
            "reason_codes": _diagnostic_finding_reason_codes(mutation_failures),
            "cue_indices": _diagnostic_cue_indices(mutation_failures),
        },
        "boundary": {
            "status": _diagnostic_token(
                boundary_map.get("status"), fallback="UNAVAILABLE"
            ),
            "reason_codes": _diagnostic_reason_codes(
                boundary_map.get("reason_codes")
            ),
        },
        "terminal_write_predicates": [
            {"name": "target_write", "status": "NOT_ATTEMPTED"},
            {"name": "state_write", "status": "NOT_ATTEMPTED"},
            {"name": "journal_write", "status": "NOT_ATTEMPTED"},
            {"name": "terminal_private_stage", "status": "NOT_CREATED"},
        ],
    }


def full_dry_run_failure_result(
    error: QixiTerminalEvidenceRefreshError,
) -> dict[str, object]:
    """Return the bounded CLI failure observation for a terminal dry run."""

    if error.diagnostic_result is not None:
        return copy.deepcopy(error.diagnostic_result)
    predicate_name = "terminal_refresh"
    if error.reason_code.endswith("_ALLOWLIST_DRIFT"):
        predicate_name = "allowed_mutations"
    return {
        "schema_version": "qixi-terminal-evidence-refresh-full-dry-run-diagnostic.v1",
        "status": "FULL_DRY_RUN_BLOCKED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "upload_enabled": False,
        "predicate": {
            "name": predicate_name,
            "status": "FAIL",
            "reason_code": _diagnostic_token(
                error.reason_code, fallback="QIXI_TERMINAL_REFRESH_FAILURE"
            ),
        },
        "terminal_write_predicates": [
            {"name": "target_write", "status": "NOT_ATTEMPTED"},
            {"name": "state_write", "status": "NOT_ATTEMPTED"},
            {"name": "journal_write", "status": "NOT_ATTEMPTED"},
            {"name": "terminal_private_stage", "status": "NOT_CREATED"},
        ],
    }


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_regular(path: Path) -> bytes:
    try:
        snapshot = stable_regular_snapshot(path, label="terminal refresh file")
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_FILE_UNREADABLE") from exc
    if snapshot is None:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_FILE_UNSAFE")
    return snapshot.payload


def load_authority(repo_root: Path = ROOT) -> dict[str, object]:
    """Load the one committed/deployed authority before any runtime read."""

    path = repo_root / AUTHORITY_PATH
    payload = _read_regular(path)
    try:
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=AUTHORITY_PATH, observed_bytes=payload
        )
        value = json.loads(payload)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiTerminalEvidenceRefreshError(
            "QIXI_TERMINAL_REFRESH_AUTHORITY_UNSEALED"
        ) from exc
    authority = _require_mapping(value, "AUTHORITY")
    claimed = authority.pop("authority_sha256", None)
    required = {
        "schema_version", "candidate_id", "recording_date", "runtime_root", "upload_enabled",
        "preimage", "repository_preimage", "predecessor_recovery", "correction", "allowed_mutations",
    }
    if (
        set(authority) != required
        or authority.get("schema_version") != "qixi-terminal-evidence-refresh-authority.v1"
        or authority.get("candidate_id") != CANDIDATE_ID
        or authority.get("recording_date") != RECORDING_DATE
        or authority.get("upload_enabled") is not False
        or not isinstance(claimed, str)
        or _canonical_sha(authority) != claimed
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_AUTHORITY_INVALID")
    authority["authority_sha256"] = claimed
    return authority


def validate_runtime(authority: Mapping[str, object]) -> dict[str, bytes]:
    """Snapshot every sealed runtime input; no target is opened for writing."""

    preimage = _require_mapping(authority.get("preimage"), "PREIMAGE")
    expected = {"record", "delivery_record", "publish", "state", "chat", "clip_context", "srt", "ass", "burn", "correction"}
    if set(preimage) != expected:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PREIMAGE_SCHEMA_INVALID")
    result: dict[str, bytes] = {}
    for role, row in preimage.items():
        descriptor = _require_mapping(row, "DESCRIPTOR")
        keys = {"path", "sha256", "bytes"} | ({"mode"} if role in {"record", "delivery_record", "publish", "state", "chat"} else set())
        if set(descriptor) != keys or not isinstance(descriptor.get("path"), str):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_DESCRIPTOR_INVALID")
        path = Path(str(descriptor["path"]))
        payload = _read_regular(path)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if digest != descriptor["sha256"] or len(payload) != descriptor["bytes"]:
            raise QixiTerminalEvidenceRefreshError(f"QIXI_TERMINAL_REFRESH_{role.upper()}_DRIFT")
        if "mode" in descriptor and stat.S_IMODE(os.lstat(path).st_mode) != descriptor["mode"]:
            raise QixiTerminalEvidenceRefreshError(f"QIXI_TERMINAL_REFRESH_{role.upper()}_MODE_DRIFT")
        result[role] = payload
    return result


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QixiTerminalEvidenceRefreshError(f"QIXI_TERMINAL_REFRESH_{label}_INVALID")
    return dict(value)


def _bare_sha256(value: object, *, label: str) -> str:
    """Accept the runtime correction's one documented SHA spelling only."""

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise QixiTerminalEvidenceRefreshError(f"QIXI_TERMINAL_REFRESH_{label}_INVALID")
    return "sha256:" + value


def _normalize_live_correction(
    correction: Mapping[str, object], *, authority_correction: Mapping[str, object],
    before_srt: str, final_srt: str, authority_preimage: Mapping[str, object],
) -> dict[str, object]:
    """Bind the sealed live v2 document before projecting its internal hashes.

    The deployed human-correction document deliberately writes its SRT and
    burned-media digests as bare lowercase hex.  This boundary is therefore
    intentionally one-way: it accepts that exact runtime schema and returns
    only the prefixed internal digest form used by the review primitives.
    """

    live = _require_mapping(correction, "LIVE_CORRECTION")
    if set(live) != _LIVE_CORRECTION_KEYS:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_SCHEMA_INVALID")
    if (
        live.get("schema_version") != "human-subtitle-correction.v2"
        or live.get("stage_order") != "human_text_then_speaker_then_burn"
        or live.get("candidate_id") != CANDIDATE_ID
        or live.get("upload_enabled") is not False
        or live.get("refresh_only") is not False
        or live.get("replace_operations") != []
        or live.get("speaker_mode") != "uniform_host"
        or not isinstance(live.get("corrected_at"), str)
        or not live["corrected_at"]
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_SCHEMA_INVALID")
    nullable = (
        "text_source", "text_source_sha256", "text_override", "text_override_sha256",
        "text_override_manifest", "text_override_manifest_sha256", "text_override_decision_output",
        "text_override_decision_output_sha256", "text_override_output", "text_override_output_sha256",
        "timing_source", "timing_source_sha256", "speaker_manifest", "speaker_manifest_sha256",
    )
    if any(live[key] is not None for key in nullable):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_SCHEMA_INVALID")
    delivery = _require_mapping(live.get("delivery_branding_authority"), "LIVE_DELIVERY_AUTHORITY")
    repository_seal = _require_mapping(
        delivery.get("authority_repository_seal"), "LIVE_DELIVERY_REPOSITORY_SEAL"
    )
    branding = _require_mapping(delivery.get("branding_intro"), "LIVE_BRANDING")
    if (
        set(delivery) != _LIVE_DELIVERY_AUTHORITY_KEYS
        or delivery.get("schema_version") != "sealed-subtitle-correction-delivery-authority.v1"
        or not isinstance(delivery.get("authority_path"), str)
        or not isinstance(delivery.get("authority_sha256"), str)
        or set(repository_seal) != {"mode", "deployed_commit", "relative_path", "sha256"}
        or repository_seal.get("mode") != "DEPLOYED_MANIFEST"
        or not isinstance(repository_seal.get("deployed_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(repository_seal.get("deployed_commit"))) is None
        or not isinstance(repository_seal.get("relative_path"), str)
        or not isinstance(repository_seal.get("sha256"), str)
        or set(branding) != {"intro_id", "intro_media_sha256", "intro_offset_ms", "status"}
        or not isinstance(branding.get("intro_id"), str)
        or not isinstance(branding.get("intro_media_sha256"), str)
        or isinstance(branding.get("intro_offset_ms"), bool)
        or not isinstance(branding.get("intro_offset_ms"), int)
        or branding.get("status") != "PREPENDED"
        or any(not isinstance(delivery.get(key), str) for key in ("record_sha256", "publish_sha256", "burned_video_sha256"))
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_SCHEMA_INVALID")
    expected_before, expected_after = _sha(before_srt), _sha(final_srt)
    authority = _require_mapping(authority_correction, "AUTHORITY_CORRECTION")
    if (
        authority.get("before_srt_sha256") != expected_before
        or authority.get("after_srt_sha256") != expected_after
        or authority.get("set_line_operations") != live.get("set_line_operations")
        or _bare_sha256(live.get("before_srt_sha256"), label="CORRECTION_BEFORE_SHA") != expected_before
        or _bare_sha256(live.get("after_srt_sha256"), label="CORRECTION_AFTER_SHA") != expected_after
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_HASH_DRIFT")
    burn = _require_mapping(authority_preimage.get("burn"), "BURN")
    if (
        live.get("burned_media") != burn.get("path")
        or _bare_sha256(live.get("burned_media_sha256"), label="CORRECTION_BURN_SHA") != burn.get("sha256")
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_BURN_DRIFT")
    return {
        "before_srt_sha256": expected_before,
        "after_srt_sha256": expected_after,
        "set_line_operations": list(live["set_line_operations"]),
    }


def _assert_text_and_grid_immutable(
    *, before_srt: str, final_srt: str, correction: Mapping[str, object]
) -> None:
    """Accept only the sealed four-line human correction and no timing drift."""

    before_cues = parse_srt_cues(before_srt)
    final_cues = parse_srt_cues(final_srt)
    operations = correction.get("set_line_operations")
    if not isinstance(operations, list) or len(before_cues) != len(final_cues):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_GRID_INVALID")
    expected: dict[int, str] = {}
    for operation in operations:
        if not isinstance(operation, str) or "=" not in operation:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_OP_INVALID")
        index_text, text = operation.split("=", 1)
        try:
            index = int(index_text)
        except ValueError as exc:
            raise QixiTerminalEvidenceRefreshError(
                "QIXI_TERMINAL_REFRESH_CORRECTION_OP_INVALID"
            ) from exc
        if index in expected or not text:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_OP_INVALID")
        expected[index] = text
    if set(expected) != {1, 3, 6, 27}:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_SCOPE_INVALID")
    for index, (before, final) in enumerate(zip(before_cues, final_cues), start=1):
        if before.start_ms != final.start_ms or before.end_ms != final.end_ms:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TIMING_DRIFT")
        if index in expected:
            if final.text != expected[index]:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_LISTED_TEXT_DRIFT")
        elif final.text != before.text:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_UNLISTED_TEXT_DRIFT")


def _srt_time_ms(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r"\d{2}:\d{2}:\d{2},\d{3}", value) is None:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_TIMING_INVALID")
    hours, minutes, seconds = (int(part) for part in value[:8].split(":"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1_000 + int(value[9:])


def _require_qixi_source_boundary(boundary_audit: Mapping[str, object]) -> dict[str, object]:
    """Admit only the current source-full-window proof for this candidate."""

    boundary = _require_mapping(boundary_audit, "BOUNDARY")
    source = _require_mapping(
        boundary.get("boundary_semantic_review"), "SOURCE_BOUNDARY"
    )
    endpoint = _require_mapping(
        source.get("final_endpoint_binding"), "SOURCE_ENDPOINT"
    )
    if (
        boundary.get("final_start_ms") != _SOURCE_FINAL_START_MS
        or boundary.get("final_end_ms") != _SOURCE_FINAL_END_MS
        or source.get("schema_version") != "talk-boundary-semantic-review.v1"
        or source.get("candidate_id") != CANDIDATE_ID
        or source.get("status") != "PASS"
        or source.get("review_scope") != "source_full_window"
        or source.get("next_topic_separated") is not True
        or source.get("next_topic_witness_valid") is not True
        or _diagnostic_sha256(source.get("request_sha256")) is None
        or _diagnostic_sha256(source.get("cue_grid_sha256")) is None
        or endpoint.get("schema_version")
        != "talk-boundary-final-endpoint-binding.v1"
        or endpoint.get("status") != "PASS"
        or endpoint.get("final_start_ms") != _SOURCE_FINAL_START_MS
        or endpoint.get("final_end_ms") != _SOURCE_FINAL_END_MS
        or endpoint.get("reason_codes") != []
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_SOURCE_BOUNDARY_INVALID")
    # Deliberately return only the source layer; an old delivery-local receipt
    # has a different cue grid and can never serve as source witness evidence.
    return copy.deepcopy(source)


def _correction_pass_for_terminal_review(
    old_audit: Mapping[str, object], *, source_boundary: Mapping[str, object]
) -> dict[str, object]:
    raw = _require_mapping(old_audit.get("correction_pass"), "CORRECTION_PASS")
    if audit_correction_mutation_authority(raw).get("status") != "PASS":
        raise QixiTerminalEvidenceRefreshError(
            "QIXI_TERMINAL_REFRESH_CORRECTION_PASS_MUTATION_AUTHORITY_INVALID"
        )
    result = copy.deepcopy(raw)
    result["boundary_semantic_review"] = copy.deepcopy(dict(source_boundary))
    return result


def _live_human_truth_seal(correction: Mapping[str, object]) -> str:
    delivery = _require_mapping(
        correction.get("delivery_branding_authority"), "LIVE_DELIVERY_AUTHORITY"
    )
    seal = _require_mapping(
        delivery.get("authority_repository_seal"), "LIVE_DELIVERY_REPOSITORY_SEAL"
    )
    digest = seal.get("sha256")
    if _diagnostic_sha256(digest) is None:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_SEAL_INVALID")
    return str(digest)


def _operator_truth_row(
    *, cue_index: int, before: object, final: object
) -> dict[str, object]:
    return {
        "truth_id": f"qixi-terminal-human-cue-{cue_index}",
        "required": True,
        "action": "replace_cue",
        "cue_indexes": [cue_index],
        "local_windows": [{"start_ms": final.start_ms, "end_ms": final.end_ms}],
        "declared_output_contract": {
            "schema_version": "source-truth-declared-output.v1",
            "action": "replace_cue",
            "canonical_texts": [final.text],
            "required_text": "",
        },
        "resolved_target_projection": {
            "schema_version": "source-truth-resolved-target-projection.v1",
            "selector": "half-open-overlap-gte-min-then-action-resolution",
            "min_overlap_ms": 80,
            "action": "replace_cue",
            "status": "RESOLVED",
            "cues": [{
                "cue_index": cue_index,
                "start_ms": final.start_ms,
                "end_ms": final.end_ms,
                "before_text": before.text,
                "after_text": final.text,
            }],
        },
    }


def _human_truth_from_authority(
    *, authority: Mapping[str, object], authority_bytes: bytes,
    runtime_authority_sha256: str, before_srt: str, final_srt: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate Ivan's sealed six-cue truth and make an in-memory review aid."""

    document = _require_mapping(authority, "HUMAN_TRUTH")
    claimed = document.pop("authority_sha256", None)
    correction = _require_mapping(document.get("correction"), "HUMAN_TRUTH_CORRECTION")
    before_cues, final_cues = parse_srt_cues(before_srt), parse_srt_cues(final_srt)
    if (
        document.get("schema_version") != "sealed-subtitle-correction-authority.v1"
        or document.get("candidate_id") != CANDIDATE_ID
        or document.get("recording_date") != RECORDING_DATE
        or document.get("upload") is not False
        or not isinstance(claimed, str)
        or _canonical_sha(document) != claimed
        or "sha256:" + hashlib.sha256(authority_bytes).hexdigest()
        != runtime_authority_sha256
        or correction.get("cue_count") != 33
        or len(before_cues) != 33
        or len(final_cues) != 33
        or correction.get("source_srt_sha256") != _sha(before_srt)
        or correction.get("output_srt_sha256") != _sha(final_srt)
        or correction.get("other_cues_immutable") is not True
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_DRIFT")
    replacements = correction.get("replacements")
    assertions = correction.get("assertions")
    if not isinstance(replacements, list) or not isinstance(assertions, list):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_SCHEMA_INVALID")
    replacement_indexes: set[int] = set()
    asserted_indexes: set[int] = set()
    rows: list[dict[str, object]] = []
    for row in replacements:
        if not isinstance(row, Mapping):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_SCHEMA_INVALID")
        index = row.get("cue_index")
        if (
            isinstance(index, bool) or not isinstance(index, int) or index in replacement_indexes
            or index not in {1, 3, 6, 27} or set(row) != {"cue_index", "start", "end", "before", "after"}
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_SCHEMA_INVALID")
        before, final = before_cues[index - 1], final_cues[index - 1]
        if (
            before.start_ms != _srt_time_ms(row["start"])
            or before.end_ms != _srt_time_ms(row["end"])
            or final.start_ms != before.start_ms or final.end_ms != before.end_ms
            or before.text != row["before"] or final.text != row["after"]
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_DRIFT")
        replacement_indexes.add(index)
        rows.append(_operator_truth_row(cue_index=index, before=before, final=final))
    for row in assertions:
        if not isinstance(row, Mapping):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_SCHEMA_INVALID")
        index = row.get("cue_index")
        if (
            isinstance(index, bool) or not isinstance(index, int) or index in asserted_indexes
            or index not in {5, 14} or set(row) != {"cue_index", "start", "end", "text"}
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_SCHEMA_INVALID")
        before, final = before_cues[index - 1], final_cues[index - 1]
        if (
            before.start_ms != _srt_time_ms(row["start"])
            or before.end_ms != _srt_time_ms(row["end"])
            or final.start_ms != before.start_ms or final.end_ms != before.end_ms
            or before.text != row["text"] or final.text != row["text"]
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_DRIFT")
        asserted_indexes.add(index)
        rows.append(_operator_truth_row(cue_index=index, before=before, final=final))
    if replacement_indexes != {1, 3, 6, 27} or asserted_indexes != {5, 14}:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_SCHEMA_INVALID")
    if any(
        before.text != final.text
        for index, (before, final) in enumerate(zip(before_cues, final_cues), start=1)
        if index not in replacement_indexes
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_DRIFT")
    rows.sort(key=lambda row: int(row["cue_indexes"][0]))
    provenance: dict[str, object] = {
        "schema_version": "qixi-terminal-operator-truth-provenance.v1",
        "status": "PASS",
        "authority_path": str(HUMAN_TRUTH_AUTHORITY_PATH),
        "authority_bytes": len(authority_bytes),
        "authority_bytes_sha256": runtime_authority_sha256,
        "authority_sha256": claimed,
        "covered_cues": [{
            "cue_index": int(row["cue_indexes"][0]),
            "text_sha256": _sha(str(row["declared_output_contract"]["canonical_texts"][0])),
        } for row in rows],
    }
    provenance["provenance_sha256"] = _canonical_sha(provenance)
    return (
        {
            "schema_version": "source-subtitle-truth-audit.v1",
            "status": "ALREADY_SATISFIED",
            "applied": [], "satisfied": rows, "failures": [],
        },
        provenance,
    )


def _load_qixi_human_truth(
    *, repo_root: Path, live_correction: Mapping[str, object],
    before_srt: str, final_srt: str,
) -> tuple[dict[str, object], dict[str, object]]:
    payload = _read_regular(repo_root / HUMAN_TRUTH_AUTHORITY_PATH)
    try:
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=HUMAN_TRUTH_AUTHORITY_PATH,
            observed_bytes=payload,
        )
        document = json.loads(payload)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiTerminalEvidenceRefreshError(
            "QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_UNSEALED"
        ) from exc
    return _human_truth_from_authority(
        authority=_require_mapping(document, "HUMAN_TRUTH"), authority_bytes=payload,
        runtime_authority_sha256=_live_human_truth_seal(live_correction),
        before_srt=before_srt, final_srt=final_srt,
    )


def _validate_operator_truth_provenance(
    audit: Mapping[str, object], *, expected: Mapping[str, object]
) -> None:
    provenance = _require_mapping(
        audit.get("qixi_operator_truth_provenance"), "HUMAN_TRUTH_PROVENANCE"
    )
    claimed = provenance.pop("provenance_sha256", None)
    if (
        not isinstance(claimed, str)
        or _canonical_sha(provenance) != claimed
        or audit.get("qixi_operator_truth_provenance") != expected
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_HUMAN_TRUTH_PROVENANCE_DRIFT")


def refresh_terminal_evidence(
    *,
    before_srt: str,
    final_srt: str,
    correction: Mapping[str, object],
    old_chat_authority: Mapping[str, object],
    selection_hook: str,
    selection_scorecard: object,
    structured_context: str,
    clip_context: Mapping[str, object],
    source_boundary: Mapping[str, object],
    verified_authority_audit: Mapping[str, object],
    operator_truth_provenance: Mapping[str, object],
    source_final_start_ms: int,
    source_final_end_ms: int,
    boundary_max_forward_ms: int,
    adapters: TextPipelineAdapters,
    authoritative_chat: Sequence[object],
    verify_confusable_entity: Callable | None = None,
    final_review_llm: Callable[[str], str] | None = None,
    boundary_review_llm: Callable[[str], str] | None = None,
    extract_json: Callable[[str], Any] | None = None,
) -> dict[str, object]:
    """Produce new provider-backed final-review and boundary receipts.

    Tests may inject the two provider seams.  In production, the fixed runner
    deliberately leaves them unset, causing the canonical pipeline's normal
    CPA transports to be constructed at the point of review.
    """

    _assert_text_and_grid_immutable(
        before_srt=before_srt, final_srt=final_srt, correction=correction
    )
    old_chat = _require_mapping(old_chat_authority, "CHAT")
    old_audit = _require_mapping(old_chat.get("final_review_audit"), "OLD_REVIEW")
    if old_chat.get("final_text_srt_sha256") != _sha(before_srt)[7:]:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_OLD_CHAT_TEXT_DRIFT")
    if correction.get("before_srt_sha256") != _sha(before_srt):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_BEFORE_DRIFT")
    if correction.get("after_srt_sha256") != _sha(final_srt) or not clip_context:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_AFTER_DRIFT")
    if (
        source_final_start_ms != _SOURCE_FINAL_START_MS
        or source_final_end_ms != _SOURCE_FINAL_END_MS
        or source_boundary.get("review_scope") != "source_full_window"
        or source_boundary.get("status") != "PASS"
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_SOURCE_BOUNDARY_INVALID")
    correction_pass = _correction_pass_for_terminal_review(
        old_audit, source_boundary=source_boundary
    )

    # The boundary reviewer is intentionally called first.  Its fresh receipt
    # becomes the exact boundary input bound by the new final-review receipt.
    from src.autoslice import producer_text_pipeline as text_pipeline

    from src.autoslice.llm_client import extract_json_object
    boundary_correction = exact_delivery_correction_audit(
        final_srt_text=final_srt,
        correction_audit=correction_pass,
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
        candidate_id=CANDIDATE_ID,
        selection_hook=selection_hook,
        selection_scorecard=selection_scorecard,
        structured_context=structured_context,
        candidate_context=clip_context_prompt_text(clip_context),
        boundary_max_forward_ms=boundary_max_forward_ms,
        llm_call=boundary_review_llm or text_pipeline._build_final_review_llm_call(),
        extract_json=extract_json or extract_json_object,
    )

    final_audit = _run_exact_final_release_review(
        srt_text=final_srt,
        correction_audit=boundary_correction,
        adapters=adapters,
        authoritative_chat=authoritative_chat,
        selection_hook=selection_hook,
        clip_context=clip_context,
        verified_authority_audit=verified_authority_audit,
        verify_confusable_entity=verify_confusable_entity,
        final_review_llm=final_review_llm,
        pronoun_audit_llm=final_review_llm,
    )
    final_audit = dict(final_audit)
    final_audit["qixi_operator_truth_provenance"] = copy.deepcopy(
        dict(operator_truth_provenance)
    )
    _validate_operator_truth_provenance(
        final_audit, expected=operator_truth_provenance
    )
    try:
        validate_final_review_release(final_audit, expected_srt_sha256=_sha(final_srt))
    except FinalReviewContractError as exc:
        raise QixiTerminalEvidenceRefreshError(
            "QIXI_TERMINAL_REFRESH_FINAL_REVIEW_BLOCKED",
            underlying_reason_code=exc.reason_code,
            diagnostic_result=_final_review_failure_diagnostic(
                final_audit=final_audit,
                expected_srt_sha256=_sha(final_srt),
                reason_code=exc.reason_code,
            ),
        ) from exc

    refreshed_chat = copy.deepcopy(old_chat)
    final_sha = _sha(final_srt)[7:]
    for key in ("final_text_srt_sha256", "final_speaker_srt_sha256", "final_output_srt_sha256"):
        refreshed_chat[key] = final_sha
    refreshed_chat["final_review_audit"] = final_audit
    refreshed_chat["final_status"] = "FINAL_ARTIFACTS_VERIFIED"
    return {
        "chat_authority": refreshed_chat,
        "final_review_audit": final_audit,
        "final_delivery_boundary_semantic_review": boundary_correction["boundary_semantic_review"],
    }


def project_evidence_mirrors(
    *,
    record: Mapping[str, object],
    delivery_record: Mapping[str, object],
    publish: Mapping[str, object],
    state: Mapping[str, object],
    refreshed_chat_bytes: bytes,
    final_delivery_boundary: Mapping[str, object],
    allowed_mutations: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Project only the new evidence bindings into the four public mirrors.

    This deliberately does not accept a caller supplied list of JSON pointers.
    The two record copies must be byte-identical semantic objects, and every
    title, cover, media, subtitle and ASS field remains inherited verbatim.
    """

    old_record = _require_mapping(record, "RECORD")
    old_delivery = _require_mapping(delivery_record, "DELIVERY_RECORD")
    if old_record != old_delivery:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECORD_MIRROR_DRIFT")
    old_publish = _require_mapping(publish, "PUBLISH")
    old_state = _require_mapping(state, "STATE")
    allowed = _require_mapping(allowed_mutations, "ALLOWLIST")
    boundary = _require_mapping(old_record.get("boundary_audit"), "BOUNDARY")
    story = _require_mapping(old_record.get("story_contract"), "STORY")
    artifact_hashes = _require_mapping(old_record.get("artifact_hashes"), "ARTIFACT_HASHES")
    chat_path = old_record.get("chat_authority_audit_path")
    if not isinstance(chat_path, str) or not chat_path.endswith(".chat-authority.json"):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_LOCATOR_DRIFT")
    new_boundary = dict(boundary)
    new_boundary["final_delivery_boundary_semantic_review"] = copy.deepcopy(
        dict(final_delivery_boundary)
    )
    new_story = dict(story)
    new_story["boundary_semantic_review"] = copy.deepcopy(dict(final_delivery_boundary))
    new_hashes = dict(artifact_hashes)
    new_hashes["chat_authority_audit_sha256"] = (
        "sha256:" + hashlib.sha256(refreshed_chat_bytes).hexdigest()
    )
    new_record = copy.deepcopy(old_record)
    new_record["boundary_audit"] = new_boundary
    new_record["story_contract"] = new_story
    new_record["artifact_hashes"] = new_hashes

    # The post-correction publish document carries the same StoryContract; it
    # is not allowed to grow a second, independently generated review object.
    new_publish = copy.deepcopy(old_publish)
    if (
        allowed.get("publish")
        and isinstance(new_publish.get("story_contract"), Mapping)
    ):
        new_publish["story_contract"] = copy.deepcopy(new_story)
    # State may contain more than one collection.  Locate exactly one current
    # candidate row and update only its nested public record projection when it
    # exists; a missing or duplicate candidate is a hard block.
    new_state = copy.deepcopy(old_state)
    if allowed.get("state"):
        rows: list[dict[str, object]] = []
        for collection in ("picks", "talk", "talks", "pending_talk"):
            value = new_state.get(collection)
            if isinstance(value, list):
                rows.extend(
                    row for row in value
                    if isinstance(row, dict) and row.get("candidate_id") == CANDIDATE_ID
                )
        if len(rows) != 1:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_STATE_CANDIDATE_DRIFT")
        if isinstance(rows[0].get("story_contract"), Mapping):
            rows[0]["story_contract"] = copy.deepcopy(new_story)
    projected = {
        "record": new_record,
        "delivery_record": copy.deepcopy(new_record),
        "publish": new_publish,
        "state": new_state,
    }
    for role, prior, current in (
        ("record", old_record, projected["record"]),
        ("delivery_record", old_delivery, projected["delivery_record"]),
        ("publish", old_publish, projected["publish"]),
        ("state", old_state, projected["state"]),
    ):
        changed = _changed_pointers(prior, current)
        if not _changed_within_allowlisted_subtrees(changed, allowed.get(role)):
            raise QixiTerminalEvidenceRefreshError(
                f"QIXI_TERMINAL_REFRESH_{role.upper()}_ALLOWLIST_DRIFT"
            )
    return projected


def _changed_pointers(before: object, after: object, prefix: str = "") -> set[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        keys = set(before) | set(after)
        return set().union(*(
            _changed_pointers(before.get(key), after.get(key), f"{prefix}/{key}")
            for key in keys
        )) if keys else set()
    if before == after:
        return set()
    return {prefix or "/"}


def _changed_within_allowlisted_subtrees(
    changed: set[str], roots: object,
) -> bool:
    """Require every listed JSON-pointer root to own a changed leaf exactly."""

    if not isinstance(roots, list) or any(
        not isinstance(root, str) or not root.startswith("/") or root == "/"
        for root in roots
    ) or len(roots) != len(set(roots)):
        return False
    if not roots:
        return not changed
    for leaf in changed:
        if not any(leaf == root or leaf.startswith(root + "/") for root in roots):
            return False
    return all(
        any(leaf == root or leaf.startswith(root + "/") for leaf in changed)
        for root in roots
    )


_PUBLISH_STORY_BOUNDARY_ROOT = "/story_contract/boundary_semantic_review"


def _candidate_state_story_contracts(state: Mapping[str, object]) -> list[object]:
    """Return only this candidate's state-story mirrors, never a loose row match."""

    contracts: list[object] = []
    for collection in ("picks", "talk", "talks", "pending_talk"):
        rows = state.get(collection)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, Mapping) and row.get("candidate_id") == CANDIDATE_ID:
                if "story_contract" in row:
                    contracts.append(row["story_contract"])
    return contracts


def _verify_committed_optional_mirrors(
    *,
    allowed_mutations: Mapping[str, object],
    before_publish: bytes,
    after_publish: bytes,
    before_state: bytes,
    after_state: bytes,
    current_publish: Mapping[str, object],
    current_state: Mapping[str, object],
    story: Mapping[str, object],
) -> None:
    """Apply the sealed mirror scope to a committed journal projection.

    A missing mirror is not an invitation to manufacture one.  In particular,
    the live Qixi publish/state inputs have no StoryContract mirror and their
    empty authority scopes require both byte and decoded-document immutability.
    """

    allowed = _require_mapping(allowed_mutations, "ALLOWLIST")
    publish_roots = allowed.get("publish")
    state_roots = allowed.get("state")
    if (
        not isinstance(publish_roots, list)
        or not isinstance(state_roots, list)
        or any(not isinstance(root, str) for root in [*publish_roots, *state_roots])
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ALLOWLIST_INVALID")
    before_publish_document = _json_document(before_publish, "COMMITTED_BEFORE_PUBLISH")
    before_state_document = _json_document(before_state, "COMMITTED_BEFORE_STATE")
    if not publish_roots:
        if (
            before_publish != after_publish
            or before_publish_document != current_publish
            or "story_contract" in before_publish_document
            or "story_contract" in current_publish
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CONTEXT_DRIFT")
    else:
        before_story = before_publish_document.get("story_contract")
        current_story = current_publish.get("story_contract")
        if (
            _PUBLISH_STORY_BOUNDARY_ROOT in publish_roots
            and isinstance(before_story, Mapping)
        ):
            if current_story != story:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CONTEXT_DRIFT")
        elif current_story != before_story or (
            not isinstance(before_story, Mapping) and isinstance(current_story, Mapping)
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CONTEXT_DRIFT")
    if not state_roots:
        if (
            before_state != after_state
            or before_state_document != current_state
            or _candidate_state_story_contracts(before_state_document)
            or _candidate_state_story_contracts(current_state)
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CONTEXT_DRIFT")


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _refresh_root(authority: Mapping[str, object]) -> Path:
    record = _require_mapping(_require_mapping(authority["preimage"], "PREIMAGE")["record"], "RECORD")
    return Path(str(record["path"])).parent / "qixi-terminal-evidence-refresh" / str(authority["authority_sha256"])[7:23]


def _create_refresh_root(root: Path) -> None:
    """Create only the two private journal directories below a sealed parent."""

    namespace = root.parent
    try:
        safe_parent(namespace)
        if not os.path.lexists(namespace):
            namespace.mkdir(mode=0o700)
        namespace_mode = os.lstat(namespace).st_mode
        if stat.S_ISLNK(namespace_mode) or not stat.S_ISDIR(namespace_mode) or stat.S_IMODE(namespace_mode) != 0o700:
            raise OSError("terminal refresh namespace is unsafe")
        root.mkdir(mode=0o700)
    except (OSError, QixiTransactionCoreError) as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_ROOT_UNSAFE") from exc


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    try:
        safe_parent(path)
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_UNSAFE") from exc
    if path.exists() or path.is_symlink():
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CREATE_ONLY_COLLISION")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_exact(path: Path, *, before: bytes, after: bytes, mode: int) -> None:
    """CAS replace one owned target, preserving its regular-file mode."""

    try:
        safe_parent(path)
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_UNSAFE") from exc
    observed = _sealed_snapshot(path, payload=before, mode=mode, label="terminal refresh replace")
    if observed.payload != before:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_DRIFT")
    fd, temporary = tempfile.mkstemp(prefix=".qixi-terminal-refresh-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        os.write(fd, after)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if _read_regular(path) != before:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_DRIFT")
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _sealed_snapshot(path, payload=after, mode=mode, label="terminal refresh replace")
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _sealed_snapshot(path: Path, *, payload: bytes, mode: int, label: str) -> FileSnapshot:
    """Freeze one runtime target's inode for the terminal lane's CAS journal."""

    try:
        snapshot = stable_regular_snapshot(path, label=label)
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_UNSAFE") from exc
    if snapshot is None or snapshot.payload != payload or snapshot.mode != mode:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_DRIFT")
    return snapshot


def _write_journal(path: Path, journal: dict[str, object]) -> None:
    """Replace the sole journal inode only after sealing its self-hash."""

    journal["journal_sha256"] = _canonical_sha({k: v for k, v in journal.items() if k != "journal_sha256"})
    _replace_exact(path, before=_read_regular(path), after=_json_bytes(journal), mode=0o600)


def _journal_sha256(journal: Mapping[str, object]) -> str:
    return _canonical_sha({key: value for key, value in journal.items() if key != "journal_sha256"})


def _receipt_for(journal: Mapping[str, object]) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": "qixi-terminal-evidence-refresh-receipt.v1",
        "status": "COMMITTED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "authority_sha256": journal["authority_sha256"],
        "journal_sha256": journal["journal_sha256"],
        "entries_sha256": _canonical_sha(journal["entries"]),
        "matrix_sha256": _canonical_sha(journal["matrix"]),
    }
    receipt["receipt_sha256"] = _canonical_sha(receipt)
    return receipt


def _terminal_matrix() -> dict[str, object]:
    return {
        "schema_version": "qixi-terminal-evidence-refresh-matrix.v1",
        "status": "PASS",
        "predicates": [
            {"id": "sealed_preimages", "status": "PASS"},
            {"id": "subtitle_grid", "status": "PASS"},
            {"id": "provider_final_review", "status": "PASS"},
            {"id": "provider_boundary_review", "status": "PASS"},
            {"id": "mirror_projection", "status": "PASS"},
        ],
    }


def _validate_journal(
    journal: Mapping[str, object], *, authority: Mapping[str, object], before: Mapping[str, bytes], after: Mapping[str, bytes]
) -> dict[str, object]:
    """Make a self-rehashed journal prove the fixed authority and after-image."""

    required = {
        "schema_version", "status", "candidate_id", "recording_date", "authority_sha256",
        "entries", "matrix", "journal_sha256",
    }
    if set(journal) != required or journal.get("schema_version") != "qixi-terminal-evidence-refresh-journal.v1":
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    if (
        journal.get("status") not in _JOURNAL_STATUSES
        or journal.get("candidate_id") != CANDIDATE_ID
        or journal.get("recording_date") != RECORDING_DATE
        or journal.get("authority_sha256") != authority.get("authority_sha256")
        or journal.get("journal_sha256") != _journal_sha256(journal)
        or journal.get("matrix") != _terminal_matrix()
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
    entries = journal.get("entries")
    if not isinstance(entries, list) or len(entries) != len(_JOURNAL_ROLES):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    preimage = _require_mapping(authority.get("preimage"), "PREIMAGE")
    seen: set[str] = set()
    entry_keys = {
        "role", "path", "before_sha256", "after_sha256", "before_bytes_b64", "after_bytes_b64",
        "before_mode", "before_device", "before_inode", "after_mode", "backup_name", "staged_name",
        "staged_device", "staged_inode", "installed_device", "installed_inode", "phase",
    }
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != entry_keys:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
        role = entry.get("role")
        if not isinstance(role, str) or role not in _JOURNAL_ROLES or role in seen:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
        seen.add(role)
        descriptor = _require_mapping(preimage.get(role), role)
        try:
            old = base64.b64decode(str(entry["before_bytes_b64"]), validate=True)
            new = base64.b64decode(str(entry["after_bytes_b64"]), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID") from exc
        if (
            entry.get("path") != descriptor.get("path")
            or old != before[role]
            or new != after[role]
            or entry.get("before_sha256") != "sha256:" + hashlib.sha256(old).hexdigest()
            or entry.get("after_sha256") != "sha256:" + hashlib.sha256(new).hexdigest()
            or entry.get("before_mode") != descriptor.get("mode")
            or entry.get("after_mode") != descriptor.get("mode")
            or entry.get("phase") not in _ENTRY_PHASES
            or not isinstance(entry.get("before_device"), int)
            or isinstance(entry.get("before_device"), bool)
            or not isinstance(entry.get("before_inode"), int)
            or isinstance(entry.get("before_inode"), bool)
            or not isinstance(entry.get("backup_name"), str)
            or Path(str(entry["backup_name"])).name != entry["backup_name"]
            or not isinstance(entry.get("staged_name"), str)
            or not str(entry["staged_name"]).startswith(".")
            or Path(str(entry["staged_name"])).name != entry["staged_name"]
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        phase = entry["phase"]
        for field in ("staged_device", "staged_inode"):
            value = entry[field]
            if phase == "PREPARED" and value is not None:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
            # INSTALLING has a deliberate checkpoint before stage creation;
            # a crash there is resumable without inventing inode evidence.
            if phase == "INSTALLED" and (isinstance(value, bool) or not isinstance(value, int)):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        if phase == "INSTALLING" and ((entry["staged_device"] is None) != (entry["staged_inode"] is None)):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        if phase == "INSTALLING" and entry["staged_device"] is not None and (
            isinstance(entry["staged_device"], bool) or not isinstance(entry["staged_device"], int)
            or isinstance(entry["staged_inode"], bool) or not isinstance(entry["staged_inode"], int)
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        for field in ("installed_device", "installed_inode"):
            value = entry[field]
            if phase != "INSTALLED" and value is not None:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
            if phase == "INSTALLED" and (isinstance(value, bool) or not isinstance(value, int)):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
    if seen != set(_JOURNAL_ROLES):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    return dict(journal)


def _verify_committed_projection(
    root: Path, journal: Mapping[str, object], *, authority: Mapping[str, object], before: Mapping[str, bytes], after: Mapping[str, bytes]
) -> None:
    _validate_journal(journal, authority=authority, before=before, after=after)
    if journal.get("status") != "COMMITTED":
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMIT_MISSING")
    entries = journal["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, Mapping)
        role = str(entry["role"])
        observed = _sealed_snapshot(
            Path(str(entry["path"])), payload=after[role], mode=int(entry["after_mode"]),
            label="terminal refresh committed target",
        )
        if (observed.device, observed.inode) != (entry["installed_device"], entry["installed_inode"]):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_TARGET_DRIFT")
    expected = _json_bytes(_receipt_for(journal))
    receipt = root / "receipt.json"
    if os.path.lexists(receipt):
        if _read_regular(receipt) != expected:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECEIPT_DRIFT")
    else:
        _write_new(receipt, expected)


def _checkpoint(root: Path, journal: dict[str, object], index: int, **changes: object) -> None:
    entries = journal["entries"]
    assert isinstance(entries, list) and isinstance(entries[index], Mapping)
    updated = dict(entries[index])
    updated.update(changes)
    entries[index] = updated
    _write_journal(root / "journal.json", journal)


def _backup_path(root: Path, entry: Mapping[str, object]) -> Path:
    name = entry.get("backup_name")
    if not isinstance(name, str) or Path(name).name != name:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    return root / "backups" / name


def _ensure_backup(root: Path, entry: Mapping[str, object]) -> None:
    path = _backup_path(root, entry)
    expected = base64.b64decode(str(entry["before_bytes_b64"]))
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir() or stat.S_IMODE(os.lstat(path.parent).st_mode) != 0o700:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_BACKUP_ROOT_UNSAFE")
    if path.exists():
        if _read_regular(path) != expected:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_BACKUP_DRIFT")
        return
    _write_new(path, expected)


def _entry_staged_path(entry: Mapping[str, object]) -> Path:
    target = Path(str(entry["path"]))
    name = entry.get("staged_name")
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("."):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    return target.parent / name


def _rollback_entries(root: Path, journal: dict[str, object]) -> None:
    """Reverse only transaction-owned after inodes; retain failures durably."""

    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    for index in reversed(range(len(entries))):
        entry = entries[index]
        if not isinstance(entry, Mapping) or entry.get("phase") == "PREPARED":
            continue
        target = Path(str(entry["path"]))
        after = base64.b64decode(str(entry["after_bytes_b64"]))
        current = stable_regular_snapshot(target, label="terminal refresh rollback")
        if current is not None and current.payload == after:
            if entry.get("phase") == "INSTALLED" and (
                current.device, current.inode
            ) != (entry.get("installed_device"), entry.get("installed_inode")):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT")
            try:
                restored = restore_owned_inode(
                    current,
                    before=journal_before_snapshot(entry, target=target),
                    label="terminal refresh rollback",
                )
            except QixiTransactionCoreError as exc:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT") from exc
            if restored is None or restored.payload != base64.b64decode(str(entry["before_bytes_b64"])):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT")
        elif entry.get("phase") == "INSTALLED":
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT")
        staged = _entry_staged_path(entry)
        if os.path.lexists(staged):
            owned = _adopt_staged(entry)
            try:
                os.unlink(owned.path)
            except OSError as exc:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_STAGE_FAILURE") from exc
        marker = _stage_marker(entry)
        if os.path.lexists(marker):
            _read_regular(marker)
            marker.unlink()
        _checkpoint(
            root, journal, index, phase="PREPARED", staged_device=None, staged_inode=None,
            installed_device=None, installed_inode=None,
        )
    journal["status"] = "ROLLED_BACK"
    _write_journal(root / "journal.json", journal)


def _stage_marker(entry: Mapping[str, object]) -> Path:
    path = _entry_staged_path(entry)
    return path.with_name(path.name + ".owner")


def _write_stage_marker(entry: Mapping[str, object], snapshot: FileSnapshot) -> None:
    payload = _json_bytes(
        {
            "schema_version": "qixi-terminal-evidence-refresh-stage-owner.v1",
            "path": snapshot.path.name,
            "device": snapshot.device,
            "inode": snapshot.inode,
            "sha256": snapshot.sha256,
            "mode": snapshot.mode,
        }
    )
    _write_new(_stage_marker(entry), payload)


def _adopt_staged(entry: Mapping[str, object]) -> FileSnapshot:
    staged = _entry_staged_path(entry)
    marker = _json_document(_read_regular(_stage_marker(entry)), "STAGE_OWNER")
    snapshot = _sealed_snapshot(
        staged,
        payload=base64.b64decode(str(entry["after_bytes_b64"])),
        mode=int(entry["after_mode"]),
        label="terminal refresh staged owner",
    )
    if marker != {
        "schema_version": "qixi-terminal-evidence-refresh-stage-owner.v1",
        "path": staged.name,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "sha256": snapshot.sha256,
        "mode": snapshot.mode,
    }:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_STAGED_OWNER_DRIFT")
    return snapshot


def apply_projection(
    *, authority: Mapping[str, object], before: Mapping[str, bytes], after: Mapping[str, bytes], apply: bool
) -> dict[str, object]:
    """Stage a bounded four-document/chat refresh under a resumable receipt.

    `apply=False` is a complete no-target-write preflight.  The function
    writes no provider evidence: callers must complete provider review before
    calling it and pass the exact staged bytes here.
    """

    roles = ("chat", "record", "delivery_record", "publish", "state")
    if set(after) != set(roles) or any(role not in before for role in roles):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PROJECTION_SCHEMA_INVALID")
    preimage = _require_mapping(authority["preimage"], "PREIMAGE")
    matrix = _terminal_matrix()
    if not apply:
        return {"status": "DRY_RUN_PASS", "matrix": matrix, "target_writes": 0}
    root = _refresh_root(authority)
    existing_journal: dict[str, object] | None = None
    if os.path.lexists(root):
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(os.lstat(root).st_mode) != 0o700:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RESUME_JOURNAL_INVALID")
        journal_path = root / "journal.json"
        if not journal_path.is_file() or journal_path.is_symlink():
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RESUME_JOURNAL_INVALID")
        journal = _validate_journal(
            _json_document(_read_regular(journal_path), "JOURNAL"), authority=authority,
            before=before, after=after,
        )
        if journal.get("status") == "COMMITTED":
            _verify_committed_projection(root, journal, authority=authority, before=before, after=after)
            return {"status": "ALREADY_COMMITTED", "matrix": matrix, "journal": str(journal_path)}
        if journal.get("status") == "ROLLBACK_REQUIRED":
            try:
                _rollback_entries(root, journal)
            except BaseException as exc:
                exc.add_note("terminal refresh rollback failed; ROLLBACK_REQUIRED journal is retained")
                raise
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLED_BACK")
        if journal.get("status") == "ROLLED_BACK":
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLED_BACK")
        if journal.get("status") != "PREPARED":
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RESUME_JOURNAL_INVALID")
        existing_journal = journal
    else:
        _create_refresh_root(root)
    frozen: dict[str, FileSnapshot] = {}
    if existing_journal is None:
        for role in roles:
            descriptor = _require_mapping(preimage[role], role)
            frozen[role] = _sealed_snapshot(
                Path(str(descriptor["path"])), payload=before[role], mode=int(descriptor["mode"]),
                label=f"terminal refresh {role}",
            )
    journal_body = existing_journal or {
        "schema_version": "qixi-terminal-evidence-refresh-journal.v1",
        "status": "PREPARED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "authority_sha256": authority["authority_sha256"],
        "entries": [
            {
                "role": role,
                "path": _require_mapping(preimage[role], role)["path"],
                "before_sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(),
                "after_sha256": "sha256:" + hashlib.sha256(after[role]).hexdigest(),
                "after_bytes_b64": base64.b64encode(after[role]).decode("ascii"),
                "before_bytes_b64": base64.b64encode(before[role]).decode("ascii"),
                "before_mode": frozen[role].mode,
                "before_device": frozen[role].device,
                "before_inode": frozen[role].inode,
                "after_mode": frozen[role].mode,
                "backup_name": f"{role}.before",
                "staged_name": ".{name}.terminal-refresh-{role}.tmp".format(
                    name=Path(str(_require_mapping(preimage[role], role)["path"])).name,
                    role=role,
                ),
                "staged_device": None,
                "staged_inode": None,
                "installed_device": None,
                "installed_inode": None,
                "phase": "PREPARED",
            }
            for role in roles
        ],
        "matrix": matrix,
    }
    journal_body["journal_sha256"] = _journal_sha256(journal_body)
    journal_path = root / "journal.json"
    if existing_journal is None:
        _write_new(journal_path, _json_bytes(journal_body))
    entries = journal_body["entries"]
    assert isinstance(entries, list)
    try:
        for index, entry in enumerate(entries):
            assert isinstance(entry, Mapping)
            target = Path(str(entry["path"]))
            if entry["phase"] == "PREPARED":
                _ensure_backup(root, entry)
                _checkpoint(root, journal_body, index, phase="INSTALLING")
                entry = entries[index]
            assert isinstance(entry, Mapping)
            if entry["phase"] == "INSTALLING":
                staged_path = _entry_staged_path(entry)
                payload = base64.b64decode(str(entry["after_bytes_b64"]))
                target_after = _sealed_snapshot(
                    target, payload=payload, mode=int(entry["after_mode"]),
                    label="terminal refresh crash-after-rename",
                ) if _read_regular(target) == payload else None
                if target_after is not None and os.path.lexists(_stage_marker(entry)):
                    marker = _json_document(_read_regular(_stage_marker(entry)), "STAGE_OWNER")
                    if marker.get("inode") != target_after.inode or marker.get("device") != target_after.device:
                        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_STAGED_OWNER_DRIFT")
                    _checkpoint(root, journal_body, index, phase="INSTALLED", installed_device=target_after.device, installed_inode=target_after.inode)
                    _stage_marker(entry).unlink()
                    continue
                if os.path.lexists(staged_path):
                    staged = _adopt_staged(entry)
                else:
                    staged = create_staged_inode(
                        staged_path,
                        payload=payload,
                        mode=int(entry["after_mode"]),
                        label="terminal refresh stage",
                    )
                    _write_stage_marker(entry, staged)
                _checkpoint(
                    root,
                    journal_body,
                    index,
                    staged_device=staged.device,
                    staged_inode=staged.inode,
                )
                entry = entries[index]
                assert isinstance(entry, Mapping)
                before_snapshot = (
                    frozen[str(entry["role"])] if existing_journal is None
                    else journal_before_snapshot(entry, target=target)
                )
                installed = install_checkpointed_inode(
                    staged, target=target, expected_before=before_snapshot,
                    callbacks=InstallCallbacks(
                        checkpoint_installed=lambda snap: _checkpoint(root, journal_body, index, phase="INSTALLED", installed_device=snap.device, installed_inode=snap.inode),
                        verify_installed=lambda snap: _sealed_snapshot(target, payload=payload, mode=snap.mode, label="terminal refresh install"),
                    ), label="terminal refresh install",
                )
                if installed.sha256 != entry["after_sha256"]:
                    raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_INSTALL_DRIFT")
                _stage_marker(entry).unlink()
    except Exception as failure:
        journal_body["status"] = "ROLLBACK_REQUIRED"
        try:
            _write_journal(journal_path, journal_body)
        except Exception as checkpoint_error:
            failure.add_note(
                "terminal refresh rollback state could not be checkpointed: "
                + type(checkpoint_error).__name__
            )
        try:
            _rollback_entries(root, journal_body)
        except BaseException as rollback_error:
            failure.add_note(
                "terminal refresh rollback failed; ROLLBACK_REQUIRED journal is retained: "
                + type(rollback_error).__name__
            )
        else:
            failure.add_note("terminal refresh rollback completed; ROLLED_BACK journal is retained")
        raise
    journal_body["status"] = "COMMITTED"
    _write_journal(journal_path, journal_body)
    _verify_committed_projection(root, journal_body, authority=authority, before=before, after=after)
    return {"status": "COMMITTED", "matrix": matrix, "journal": str(journal_path)}


def validate_committed_refresh(*, repo_root: Path = ROOT) -> None:
    """Replay the fixed terminal-refresh successor after basename recovery."""

    authority = load_authority(repo_root)
    predecessor = _require_mapping(authority["predecessor_recovery"], "PREDECESSOR")
    for role in ("journal", "receipt"):
        descriptor = _require_mapping(predecessor.get(role), "PREDECESSOR")
        payload = _read_regular(Path(str(descriptor.get("path") or "")))
        if len(payload) != descriptor.get("bytes") or "sha256:" + hashlib.sha256(payload).hexdigest() != descriptor.get("sha256"):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PREDECESSOR_DRIFT")
    try:
        from src.autoslice.qixi_operator_exact_title_source_fact import load_authority as load_title_authority
        from src.autoslice.qixi_post_correction_public_artifact_recovery import validate_committed_successor

        title_authority = load_title_authority(CANDIDATE_ID, repo_root=repo_root)
        if title_authority is None:
            raise ValueError("title authority missing")
        public = title_authority.document["source_binding"]["public_surface_authority"]
        if not isinstance(public, Mapping):
            raise ValueError("public authority binding missing")
        # Recovery owns its public authority parser and journal semantics.
        from src.autoslice import qixi_post_correction_public_surface as public_surface

        public_payload = _read_regular(repo_root / str(public["relative_path"]))
        public_document = public_surface.validate_authority(json.loads(public_payload))
        validate_committed_successor(public_document)
    except (OSError, ValueError, KeyError) as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PREDECESSOR_INVALID") from exc
    root = _refresh_root(authority)
    journal_path, receipt_path = root / "journal.json", root / "receipt.json"
    journal = _json_document(_read_regular(journal_path), "JOURNAL")
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    before: dict[str, bytes] = {}
    after: dict[str, bytes] = {}
    for row in entries:
        if not isinstance(row, Mapping) or not isinstance(row.get("role"), str):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
        role = str(row["role"])
        try:
            before[role] = base64.b64decode(str(row["before_bytes_b64"]), validate=True)
            after[role] = base64.b64decode(str(row["after_bytes_b64"]), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID") from exc
    _verify_committed_projection(root, journal, authority=authority, before=before, after=after)
    receipt = _json_document(_read_regular(receipt_path), "RECEIPT")
    if receipt != _receipt_for(journal):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECEIPT_DRIFT")
    current_chat = _json_document(after["chat"], "COMMITTED_CHAT")
    current_record = _json_document(after["record"], "COMMITTED_RECORD")
    current_delivery = _json_document(after["delivery_record"], "COMMITTED_DELIVERY")
    current_publish = _json_document(after["publish"], "COMMITTED_PUBLISH")
    current_state = _json_document(after["state"], "COMMITTED_STATE")
    if current_record != current_delivery:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECORD_MIRROR_DRIFT")
    final_srt = _read_regular(Path(str(_require_mapping(authority["preimage"], "PREIMAGE")["srt"]["path"]))).decode("utf-8")
    final_hash = _sha(final_srt)
    if any(current_chat.get(key) != final_hash[7:] for key in (
        "final_text_srt_sha256", "final_speaker_srt_sha256", "final_output_srt_sha256",
    )):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CHAT_DRIFT")
    final_audit = _require_mapping(current_chat.get("final_review_audit"), "COMMITTED_FINAL_REVIEW")
    try:
        validate_final_review_release(final_audit, expected_srt_sha256=final_hash)
    except FinalReviewContractError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_FINAL_REVIEW_DRIFT") from exc
    boundary = _require_mapping(final_audit.get("boundary_semantic_review"), "COMMITTED_BOUNDARY")
    story = _require_mapping(current_record.get("story_contract"), "COMMITTED_STORY")
    record_boundary = _require_mapping(current_record.get("boundary_audit"), "COMMITTED_RECORD_BOUNDARY")
    hashes = _require_mapping(current_record.get("artifact_hashes"), "COMMITTED_HASHES")
    if (
        story.get("boundary_semantic_review") != boundary
        or record_boundary.get("final_delivery_boundary_semantic_review") != boundary
        or hashes.get("chat_authority_audit_sha256") != "sha256:" + hashlib.sha256(after["chat"]).hexdigest()
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CONTEXT_DRIFT")
    _verify_committed_optional_mirrors(
        allowed_mutations=_require_mapping(authority["allowed_mutations"], "ALLOWLIST"),
        before_publish=before["publish"],
        after_publish=after["publish"],
        before_state=before["state"],
        after_state=after["state"],
        current_publish=current_publish,
        current_state=current_state,
        story=story,
    )


def build_staged_refresh(
    *,
    repo_root: Path,
    authority: Mapping[str, object],
    runtime: Mapping[str, bytes],
    adapters: TextPipelineAdapters,
    final_review_llm: Callable[[str], str] | None = None,
    boundary_review_llm: Callable[[str], str] | None = None,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Run both canonical provider reviews in memory before any target write."""

    repository = _require_mapping(authority["repository_preimage"], "REPOSITORY_PREIMAGE")
    old_path = repo_root / str(repository["path"])
    old_bytes = _read_regular(old_path)
    if (
        len(old_bytes) != repository["bytes"]
        or "sha256:" + hashlib.sha256(old_bytes).hexdigest() != repository["sha256"]
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_REPOSITORY_PREIMAGE_DRIFT")
    final_srt = runtime["srt"].decode("utf-8")
    before_srt = old_bytes.decode("utf-8")
    record = _json_document(runtime["record"], "RECORD")
    delivery = _json_document(runtime["delivery_record"], "DELIVERY_RECORD")
    publish = _json_document(runtime["publish"], "PUBLISH")
    state = _json_document(runtime["state"], "STATE")
    clip_context = _json_document(runtime["clip_context"], "CLIP_CONTEXT")
    chat = _json_document(runtime["chat"], "CHAT")
    story = _require_mapping(record.get("story_contract"), "STORY")
    boundary = _require_mapping(record.get("boundary_audit"), "BOUNDARY")
    start, end = boundary.get("final_start_ms"), boundary.get("final_end_ms")
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int) or not start < end
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_BOUNDARY_RANGE_INVALID")
    source_boundary = _require_qixi_source_boundary(boundary)
    live_correction = _json_document(runtime["correction"], "CORRECTION")
    correction = _normalize_live_correction(
        live_correction,
        authority_correction=_require_mapping(authority["correction"], "AUTHORITY_CORRECTION"),
        before_srt=before_srt,
        final_srt=final_srt,
        authority_preimage=_require_mapping(authority["preimage"], "PREIMAGE"),
    )
    operator_truth_audit, operator_truth_provenance = _load_qixi_human_truth(
        repo_root=repo_root, live_correction=live_correction,
        before_srt=before_srt, final_srt=final_srt,
    )
    result = refresh_terminal_evidence(
        before_srt=before_srt,
        final_srt=final_srt,
        correction=correction,
        old_chat_authority=chat,
        selection_hook=str(story.get("selection_hook") or ""),
        selection_scorecard=story.get("selection_scorecard"),
        structured_context=_final_review_structured_context(
            selection_hook=str(story.get("selection_hook") or ""),
            authoritative_chat=_canonical_chat_from_applied(chat),
        ),
        clip_context=clip_context,
        source_boundary=source_boundary,
        verified_authority_audit={
            "source_subtitle_truth_audit": operator_truth_audit,
        },
        operator_truth_provenance=operator_truth_provenance,
        source_final_start_ms=start,
        source_final_end_ms=end,
        boundary_max_forward_ms=int(boundary.get("boundary_repair_extend_cap_ms") or 30_000),
        adapters=adapters,
        authoritative_chat=_canonical_chat_from_applied(chat),
        final_review_llm=final_review_llm,
        boundary_review_llm=boundary_review_llm,
    )
    chat_bytes = _json_bytes(_require_mapping(result["chat_authority"], "REFRESHED_CHAT"))
    projected = project_evidence_mirrors(
        record=record,
        delivery_record=delivery,
        publish=publish,
        state=state,
        refreshed_chat_bytes=chat_bytes,
        final_delivery_boundary=_require_mapping(
            result["final_delivery_boundary_semantic_review"], "REFRESHED_BOUNDARY"
        ),
        allowed_mutations=_require_mapping(authority["allowed_mutations"], "ALLOWLIST"),
    )
    return (
        {
            "chat": chat_bytes,
            "record": _json_bytes(projected["record"]),
            "delivery_record": _json_bytes(projected["delivery_record"]),
            "publish": _json_bytes(projected["publish"]),
            "state": _json_bytes(projected["state"]),
        },
        result,
    )


def _json_document(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiTerminalEvidenceRefreshError(
            f"QIXI_TERMINAL_REFRESH_{label}_JSON_INVALID"
        ) from exc
    return _require_mapping(value, label)


def _canonical_chat_from_applied(chat: Mapping[str, object]) -> tuple[ChatEvidence, ...]:
    """Rehydrate only the retained, final-delivery chat evidence."""

    applied = chat.get("applied")
    if not isinstance(applied, list):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_EVIDENCE_MISSING")
    rows: list[ChatEvidence] = []
    for row in applied:
        if not isinstance(row, Mapping) or row.get("survived_final_text_srt") is not True:
            continue
        kind, text, offset = row.get("kind"), row.get("exact_text"), row.get("source_offset_ms")
        if not isinstance(kind, str) or not isinstance(text, str) or not isinstance(offset, int):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_EVIDENCE_INVALID")
        rows.append(ChatEvidence(kind=kind, text=text, offset_ms=offset, sender=str(row.get("sender") or ""), source=str(row.get("source") or ""), source_sha256=str(row.get("source_sha256") or ""), source_event_id=str(row.get("source_event_id") or "")))
    if not rows:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_EVIDENCE_MISSING")
    return tuple(rows)
