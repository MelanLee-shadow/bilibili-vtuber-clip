"""Hash-bound recovery after a source-boundary reviewer transport failure.

This module never approves transcript text or a boundary.  It validates that
all completed text-stage surfaces still agree byte-for-byte, permits exactly
one fresh source-full-window semantic review, and caches deterministic VAD
spans so a retry never has to repeat earlier ASR/LLM work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from src.autoslice.clip_context import validate_clip_context
from src.autoslice.producer_boundary_owner_contract import (
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.producer_chat_input import build_structured_chat_binding_audit
from src.autoslice.provider_failure import transport_unavailable_reason
from src.autoslice.subtitle_timing_qa import SpeechSpan

BOUNDARY_RESUME_SCHEMA = "producer-text-boundary-resume-input.v1"
VAD_CHECKPOINT_SCHEMA = "producer-vad-span-checkpoint.v1"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_SRT_BYTES = 8 * 1024 * 1024


class BoundaryResumeCheckpointError(RuntimeError):
    """A persisted recovery surface is missing, stale, or contradictory."""


@dataclass(frozen=True)
class BoundaryResumeCheckpoint:
    srt_text: str
    final_review_audit: dict[str, object]
    chat_authority_audit: dict[str, object]
    clip_context: dict[str, object]
    frozen_owner_contract: dict[str, object]
    boundary_target_ms: int
    boundary_search_scope: dict[str, object]
    receipt: dict[str, object]
    receipt_path: Path


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def seal_checkpoint(value: Mapping[str, object]) -> dict[str, object]:
    result = dict(value)
    result.pop("checkpoint_sha256", None)
    result["checkpoint_sha256"] = canonical_sha256(result)
    return result


def write_atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_regular(path: Path, *, limit: int, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise BoundaryResumeCheckpointError(f"{label.upper()}_NOT_REGULAR")
        size = path.stat().st_size
        if size <= 0 or size > limit:
            raise BoundaryResumeCheckpointError(f"{label.upper()}_SIZE_INVALID")
        return path.read_bytes()
    except OSError as exc:
        raise BoundaryResumeCheckpointError(
            f"{label.upper()}_READ_FAILED:{type(exc).__name__}"
        ) from exc


def read_json(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
    payload = read_regular(path, limit=_MAX_JSON_BYTES, label=label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoundaryResumeCheckpointError(f"{label.upper()}_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise BoundaryResumeCheckpointError(f"{label.upper()}_OBJECT_REQUIRED")
    return value, payload


def validate_provider_unavailable_boundary_review(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BoundaryResumeCheckpointError("BOUNDARY_RESUME_REVIEW_MISSING")
    review = dict(value)
    reasons = review.get("reason_codes")
    if (
        review.get("schema_version") != "talk-boundary-semantic-review.v1"
        or review.get("status") != "BLOCK"
        or review.get("candidate_id") is None
        or not isinstance(reasons, list)
        or len(reasons) != 1
        or not isinstance(reasons[0], str)
        or not reasons[0].startswith("BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:")
    ):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_NOT_PROVIDER_UNAVAILABLE"
        )
    evidence = review.get("unavailable_evidence")
    if transport_unavailable_reason(reasons) is None or (
        isinstance(evidence, Mapping)
        and (
            evidence.get("failure_class") != "provider_transport"
            or evidence.get("provider_class") == "rejected"
            or any(
                status in {401, 403}
                for status in evidence.get("provider_status_codes") or []
            )
        )
    ):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_NOT_PROVIDER_UNAVAILABLE"
        )
    if (
        isinstance(evidence, Mapping)
        and evidence.get("semantic_verdict_observed") is not False
    ):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_SEMANTIC_VERDICT_AMBIGUOUS"
        )
    forbidden_content_fields = (
        "needs_more_context",
        "story_closed",
        "syntax_complete",
        "content_anchor_covered",
        "same_topic_continues_after_target",
        "recommended_end_ms",
    )
    if any(review.get(key) is not None for key in forbidden_content_fields):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_CONTENT_VERDICT_PRESENT"
        )
    return review


def clip_context_piece_rows(
    spec: Mapping[str, object],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for piece in spec.get("pieces") or []:
        if not isinstance(piece, Mapping):
            raise BoundaryResumeCheckpointError(
                "BOUNDARY_RESUME_SPEC_PIECES_INVALID"
            )
        rows.append(
            {
                "start_ms": int(piece["start_ms"]),
                "end_ms": int(piece["end_ms"]),
                "source_media_sha256": piece.get("source_media_sha256"),
                "recording_basename": Path(
                    str(piece.get("remote_media") or "")
                ).name,
            }
        )
    if not rows:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_SPEC_PIECES_INVALID"
        )
    return rows


def load_boundary_resume_checkpoint(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    candidate_id: str,
    recording_date: str,
    out_root: Path,
    padded: Path,
    padded_duration_ms: int,
    authoritative_chat: Sequence[object],
    receipt_root: Path | None = None,
) -> BoundaryResumeCheckpoint:
    """Validate completed text surfaces before one boundary-only retry."""

    srt_path = out_root / "padded.fresh.srt"
    review_path = out_root / f"{candidate_id}.review-flags.json"
    chat_path = out_root / f"{candidate_id}.chat-authority.json"
    clip_path = out_root / f"{candidate_id}.clip-context.json"
    srt_payload = read_regular(
        srt_path,
        limit=_MAX_SRT_BYTES,
        label="boundary_resume_srt",
    )
    try:
        srt_text = srt_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_SRT_UTF8_INVALID"
        ) from exc
    review, review_payload = read_json(
        review_path,
        label="boundary_resume_review",
    )
    chat, chat_payload = read_json(
        chat_path,
        label="boundary_resume_chat",
    )
    clip, clip_payload = read_json(
        clip_path,
        label="boundary_resume_clip_context",
    )

    final_hash = hashlib.sha256(srt_payload).hexdigest()
    if chat.get("final_output_srt_sha256") != final_hash:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_FINAL_SRT_HASH_MISMATCH"
        )
    nested_review = chat.get("final_review_audit")
    if not isinstance(nested_review, Mapping) or dict(nested_review) != review:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_REVIEW_SURFACES_MISMATCH"
        )
    if (
        review.get("schema_version") != "final-review-audit.v1"
        or not isinstance(review.get("status"), str)
        or review.get("status") in {"AUDITOR_UNAVAILABLE", "FAILED"}
        or not isinstance(review.get("findings"), list)
        or isinstance(review.get("applied_count"), bool)
        or not isinstance(review.get("applied_count"), int)
        or review.get("infra_unresolved_count", 0) != 0
        or (review.get("infra_unresolved") or []) != []
    ):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_CORRECTION_AUDIT_INVALID"
        )
    unavailable_review = validate_provider_unavailable_boundary_review(
        review.get("boundary_semantic_review")
    )
    if unavailable_review.get("candidate_id") != candidate_id:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_REVIEW_CANDIDATE_MISMATCH"
        )

    frozen = validate_frozen_boundary_owner_contract(
        chat.get("frozen_boundary_owner_contract")
    )
    scope = frozen.get("boundary_search_scope")
    target = frozen.get("boundary_review_target_ms")
    duration_sum = sum(int(value) for value in durations)
    if (
        not isinstance(scope, Mapping)
        or isinstance(target, bool)
        or not isinstance(target, int)
        or scope.get("review_target_ms") != target
        or unavailable_review.get("boundary_search_scope") != dict(scope)
        or int(scope.get("repair_cap_ms", -1))
        != int(spec.get("boundary_repair_extend_cap_ms", -2))
        or int(scope.get("required_local_source_context_end_ms", -1))
        > duration_sum
    ):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_FROZEN_SCOPE_MISMATCH"
        )
    expected_retry = spec.get("boundary_retry_frozen_owner_contract")
    if expected_retry is not None:
        expected = validate_frozen_boundary_owner_contract(expected_retry)
        verification = frozen.get("boundary_retry_owner_contract_verification")
        if (
            not isinstance(verification, Mapping)
            or verification.get("status") != "PASS"
            or verification.get("expected_contract_sha256")
            != expected.get("contract_sha256")
        ):
            raise BoundaryResumeCheckpointError(
                "BOUNDARY_RESUME_RETRY_OWNER_MISMATCH"
            )

    source_hashes = [
        str(piece.get("source_media_sha256") or "")
        for piece in spec.get("pieces") or []
        if isinstance(piece, Mapping)
    ]
    validated_clip = validate_clip_context(
        clip,
        candidate_id=candidate_id,
        recording_date=recording_date,
        source_media_sha256s=source_hashes,
    )
    if (
        validated_clip.get("selection_hook")
        != str(spec.get("selection_hook") or "")
        or validated_clip.get("pieces") != clip_context_piece_rows(spec)
    ):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_CLIP_CONTEXT_SCOPE_MISMATCH"
        )
    expected_binding = build_structured_chat_binding_audit(
        spec,
        authoritative_chat,
    )
    if chat.get("structured_chat_binding_audit") != expected_binding:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_CHAT_BINDING_MISMATCH"
        )
    if padded_duration_ms != duration_sum:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_PADDED_DURATION_MISMATCH"
        )

    receipt = seal_checkpoint(
        {
            "schema_version": BOUNDARY_RESUME_SCHEMA,
            "status": "VALIDATED",
            "candidate_id": candidate_id,
            "recording_date": recording_date,
            "permitted_retry": (
                "SOURCE_FULL_WINDOW_BOUNDARY_SEMANTIC_REVIEW_ONLY"
            ),
            "prior_semantic_verdict_observed": False,
            "completed_provider_stages_replay_allowed": False,
            "input_artifacts": {
                "padded_media_sha256": sha256_file(padded),
                "padded_duration_ms": padded_duration_ms,
                "padded_fresh_srt_sha256": sha256_bytes(srt_payload),
                "review_flags_sha256": sha256_bytes(review_payload),
                "chat_authority_sha256": sha256_bytes(chat_payload),
                "clip_context_sha256": sha256_bytes(clip_payload),
                "frozen_owner_contract_sha256": frozen.get(
                    "contract_sha256"
                ),
                "boundary_search_scope_sha256": scope.get("scope_sha256"),
                "spec_canonical_sha256": canonical_sha256(spec),
            },
        }
    )
    receipt_digest = str(receipt["checkpoint_sha256"]).removeprefix("sha256:")
    receipt_directory = out_root if receipt_root is None else receipt_root
    receipt_path = receipt_directory / (
        f"{candidate_id}.text-boundary-resume-input.{receipt_digest[:16]}.json"
    )
    if receipt_path.exists():
        existing, _ = read_json(
            receipt_path,
            label="boundary_resume_receipt",
        )
        if existing != receipt:
            raise BoundaryResumeCheckpointError(
                "BOUNDARY_RESUME_RECEIPT_CONFLICT"
            )
    else:
        write_atomic_json(receipt_path, receipt)
    return BoundaryResumeCheckpoint(
        srt_text=srt_text,
        final_review_audit=review,
        chat_authority_audit=chat,
        clip_context=validated_clip,
        frozen_owner_contract=frozen,
        boundary_target_ms=target,
        boundary_search_scope=dict(scope),
        receipt=receipt,
        receipt_path=receipt_path,
    )


def validate_vad_rows(
    value: object,
    *,
    duration_ms: int,
) -> list[SpeechSpan]:
    if not isinstance(value, list):
        raise BoundaryResumeCheckpointError("VAD_CHECKPOINT_SPANS_INVALID")
    spans: list[SpeechSpan] = []
    prior_end = 0
    for row in value:
        if not isinstance(row, Mapping):
            raise BoundaryResumeCheckpointError(
                "VAD_CHECKPOINT_SPANS_INVALID"
            )
        start = row.get("start_ms")
        end = row.get("end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < prior_end
            or start < 0
            or end <= start
            or end > duration_ms
        ):
            raise BoundaryResumeCheckpointError(
                "VAD_CHECKPOINT_SPANS_INVALID"
            )
        spans.append(SpeechSpan(start_ms=start, end_ms=end))
        prior_end = end
    return spans


def load_or_compute_vad_spans(
    *,
    candidate_id: str,
    out_root: Path,
    padded: Path,
    padded_duration_ms: int,
    provider: Callable[[Path, int, int], list[SpeechSpan]],
    allow_recompute_if_missing: bool,
    recompute_reason: str,
    require_reusable_identity: bool = False,
) -> tuple[list[SpeechSpan], dict[str, object]]:
    """Reuse hash-bound VAD evidence, or explicitly create it once."""

    identity = getattr(provider, "vad_cache_identity", None)
    verified_identity = (
        dict(identity)
        if isinstance(identity, Mapping)
        and identity.get("status") == "VERIFIED"
        else None
    )
    media_sha256 = sha256_file(padded)
    path = out_root / f"{candidate_id}.padded-vad-spans.json"
    if path.exists():
        document, _payload = read_json(path, label="vad_checkpoint")
        declared_seal = document.get("checkpoint_sha256")
        expected_seal = canonical_sha256(
            {
                key: value
                for key, value in document.items()
                if key != "checkpoint_sha256"
            }
        )
        if (
            document.get("schema_version") != VAD_CHECKPOINT_SCHEMA
            or document.get("candidate_id") != candidate_id
            or document.get("source_media_sha256") != media_sha256
            or document.get("source_duration_ms") != padded_duration_ms
            or document.get("provider_identity") != verified_identity
            or declared_seal != expected_seal
        ):
            raise BoundaryResumeCheckpointError("VAD_CHECKPOINT_CONFLICT")
        spans = validate_vad_rows(
            document.get("spans"),
            duration_ms=padded_duration_ms,
        )
        return spans, {
            "schema_version": "producer-vad-span-consumption.v1",
            "status": "REUSED",
            "checkpoint_path": str(path),
            "checkpoint_sha256": declared_seal,
            "span_count": len(spans),
            "model_dispatch_count": 0,
        }
    if not allow_recompute_if_missing:
        raise BoundaryResumeCheckpointError("VAD_CHECKPOINT_REQUIRED")
    if verified_identity is None and require_reusable_identity:
        raise BoundaryResumeCheckpointError(
            "VAD_PROVIDER_IDENTITY_UNVERIFIED"
        )
    spans = list(provider(padded, 0, padded_duration_ms))
    rows = [
        {
            "start_ms": int(span.start_ms),
            "end_ms": int(span.end_ms),
        }
        for span in spans
    ]
    validated = validate_vad_rows(rows, duration_ms=padded_duration_ms)
    if verified_identity is None:
        return validated, {
            "schema_version": "producer-vad-span-consumption.v1",
            "status": "RECOMPUTED_UNCACHED_UNVERIFIED_PROVIDER",
            "checkpoint_path": None,
            "checkpoint_sha256": None,
            "span_count": len(validated),
            "model_dispatch_count": 1,
            "recompute_reason": recompute_reason,
        }
    document = seal_checkpoint(
        {
            "schema_version": VAD_CHECKPOINT_SCHEMA,
            "status": "OBSERVED",
            "candidate_id": candidate_id,
            "source_media_sha256": media_sha256,
            "source_duration_ms": padded_duration_ms,
            "provider_identity": verified_identity,
            "recompute_reason": recompute_reason,
            "spans": rows,
        }
    )
    write_atomic_json(path, document)
    return validated, {
        "schema_version": "producer-vad-span-consumption.v1",
        "status": "RECOMPUTED_NO_PRIOR_CHECKPOINT",
        "checkpoint_path": str(path),
        "checkpoint_sha256": document["checkpoint_sha256"],
        "span_count": len(validated),
        "model_dispatch_count": 1,
        "recompute_reason": recompute_reason,
    }
