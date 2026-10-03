"""Candidate-bound user-selected endpoint authority for a disclosed same-topic tail.

This is deliberately not a generic "human said so" escape hatch.  A receipt is
accepted only when it is bound to the one active repository authority document,
the exact B2 v7 media/subtitle hashes, both complete cue grids, the approved
closure geometry, and the previously reviewed same-topic tail.  The authority
means that 维护者 selected the complete closing sentence as the editorial end; it
does not manufacture a next-topic transition or identify any speaker.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "talk-boundary-human-explicit-endpoint.v2"
AUTHORITY_SCHEMA = "lidousha-b2-human-explicit-endpoint-authority.v1"
AUTHORITY_KIND = "USER_EXPLICIT_ENDPOINT"
MANUAL_END_MODE = "human_explicit_endpoint_v1"
PACKAGE_BOUNDARY_AUTHORITY = (
    "human_explicit_endpoint_plus_semantic_closure_and_disclosed_same_topic_tail"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AUTHORITY_RELATIVE_BY_CANDIDATE = {
    "auto_120029_1409_1578": Path(
        "assets/lidousha/b2_caption_successor/"
        "auto_120029_1409_1578.human-explicit-endpoint-authority.v1.json"
    ),
}
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Mapping[str, object]) -> str:
    return _canonical_json_sha256(value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def payload_sha256(value: Mapping[str, object]) -> str:
    """Hash a receipt while excluding its self-hash field."""

    payload = dict(value)
    payload.pop("payload_sha256", None)
    return _canonical_sha256(payload)


def _authority_problem(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return "AUTHORITY_DOCUMENT_NOT_OBJECT"
    self_hash = str(value.get("authority_sha256") or "")
    payload = dict(value)
    payload.pop("authority_sha256", None)
    quotes = value.get("authority_quotes")
    closure = value.get("approved_closure")
    final_delivery = value.get("final_delivery")
    source_window = value.get("source_full_window")
    tail = value.get("excluded_same_topic_tail")
    limits = value.get("limits")
    if (
        value.get("schema_version") != AUTHORITY_SCHEMA
        or value.get("status") != "ACTIVE"
        or value.get("authority_kind")
        != "REVIEWER_OPERATOR_EXPLICIT_COMPLETE_ENDPOINT"
        or not isinstance(value.get("authority_id"), str)
        or not str(value.get("authority_id") or "").strip()
        or value.get("candidate_id") not in _AUTHORITY_RELATIVE_BY_CANDIDATE
        or not isinstance(value.get("target_bvid"), str)
        or not str(value.get("target_bvid") or "").strip()
        or value.get("not_a_semantic_transition_claim") is not True
        or _SHA256_RX.fullmatch(self_hash) is None
        or self_hash != _canonical_sha256(payload)
    ):
        return "AUTHORITY_DOCUMENT_HEADER_INVALID"
    if (
        not isinstance(quotes, list)
        or len(quotes) != 2
        or any(
            not isinstance(row, Mapping)
            or not isinstance(row.get("quote_id"), str)
            or not str(row.get("quote_id") or "").strip()
            or not isinstance(row.get("source"), str)
            or not str(row.get("source") or "").strip()
            or _SHA256_RX.fullmatch(str(row.get("text_sha256") or "")) is None
            or set(row) != {"quote_id", "source", "text_sha256"}
            for row in quotes
        )
        or value.get("authority_quotes_sha256")
        != _canonical_json_sha256(list(quotes))
    ):
        return "AUTHORITY_DOCUMENT_QUOTES_INVALID"
    if not isinstance(closure, Mapping) or not isinstance(final_delivery, Mapping):
        return "AUTHORITY_DOCUMENT_CLOSURE_INVALID"
    if not isinstance(source_window, Mapping) or not isinstance(tail, Mapping):
        return "AUTHORITY_DOCUMENT_SOURCE_WINDOW_INVALID"
    if (
        not isinstance(closure.get("text"), str)
        or not str(closure.get("text") or "").strip()
        or closure.get("text_sha256")
        != "sha256:"
        + hashlib.sha256(str(closure["text"]).encode("utf-8")).hexdigest()
        or any(
            not _is_int(closure.get(field))
            for field in (
                "final_delivery_cue_index",
                "final_delivery_end_ms",
                "final_media_end_ms",
                "source_cue_index",
                "source_end_ms",
                "source_media_end_ms",
                "source_timeline_offset_ms",
                "tail_pad_ms",
            )
        )
        or closure.get("final_delivery_cue_index") != closure.get("source_cue_index")
        or closure.get("source_end_ms")
        != closure.get("final_delivery_end_ms")
        + closure.get("source_timeline_offset_ms")
        or closure.get("source_media_end_ms")
        != closure.get("final_media_end_ms")
        + closure.get("source_timeline_offset_ms")
        or closure.get("tail_pad_ms")
        != closure.get("final_media_end_ms") - closure.get("final_delivery_end_ms")
        or closure.get("tail_pad_ms") != 400
    ):
        return "AUTHORITY_DOCUMENT_CLOSURE_INVALID"
    for artifact in (
        final_delivery.get("srt"),
        final_delivery.get("ass"),
        final_delivery.get("video"),
    ):
        if (
            not isinstance(artifact, Mapping)
            or set(artifact) != {"artifact_id", "sha256", "bytes"}
            or not isinstance(artifact.get("artifact_id"), str)
            or _SHA256_RX.fullmatch(str(artifact.get("sha256") or "")) is None
            or not _is_int(artifact.get("bytes"))
            or int(artifact["bytes"]) <= 0
        ):
            return "AUTHORITY_DOCUMENT_FINAL_ARTIFACT_INVALID"
    if (
        not _is_int(final_delivery.get("cue_count"))
        or final_delivery.get("cue_count") != closure.get("final_delivery_cue_index")
        or _SHA256_RX.fullmatch(str(final_delivery.get("cue_grid_sha256") or ""))
        is None
        or not _is_int(source_window.get("cue_count"))
        or source_window.get("reviewed_endpoint_cue_index")
        != closure.get("source_cue_index")
        or source_window.get("reviewed_endpoint_ms") != closure.get("source_end_ms")
        or source_window.get("final_start_ms")
        != closure.get("source_timeline_offset_ms")
        or source_window.get("final_end_ms") != closure.get("source_media_end_ms")
        or _SHA256_RX.fullmatch(str(source_window.get("cue_grid_sha256") or ""))
        is None
    ):
        return "AUTHORITY_DOCUMENT_GRID_INVALID"
    rows = tail.get("rows")
    if (
        tail.get("status") != "DISCLOSED_SAME_TOPIC_EXCLUSION"
        or tail.get("reason_code") != "USER_EXPLICIT_ENDPOINT_WITH_SAME_TOPIC_TAIL"
        or tail.get("same_topic_continues_after_target") is not True
        or tail.get("next_topic_separated") is not False
        or tail.get("excluded_from_delivery_by_user") is not True
        or not isinstance(rows, list)
        or not rows
        or tail.get("rows_sha256") != _canonical_json_sha256(rows)
        or tail.get("text_sha256")
        != "sha256:"
        + hashlib.sha256(
            "\n".join(str(row.get("text") or "") for row in rows).encode("utf-8")
        ).hexdigest()
        or source_window.get("excluded_tail_source_window_cue_indexes")
        != tail.get("source_window_cue_indexes")
        or any(
            not isinstance(row, Mapping)
            or not _is_int(row.get("old_v9_cue_index"))
            or not _is_int(row.get("source_window_cue_index"))
            or not _is_int(row.get("delivery_local_start_ms"))
            or not _is_int(row.get("delivery_local_end_ms"))
            or not _is_int(row.get("source_start_ms"))
            or not _is_int(row.get("source_end_ms"))
            or not isinstance(row.get("text"), str)
            or not str(row.get("text") or "").strip()
            or row.get("text_sha256")
            != "sha256:"
            + hashlib.sha256(str(row["text"]).encode("utf-8")).hexdigest()
            for row in rows
        )
    ):
        return "AUTHORITY_DOCUMENT_TAIL_INVALID"
    if (
        not isinstance(limits, Mapping)
        or limits.get("does_not_claim_next_topic_transition") is not True
        or limits.get("does_not_authorize_other_candidate_or_endpoint") is not True
        or limits.get("does_not_replace_formal_package_or_publication_gates") is not True
        or limits.get("same_bv_only") is not True
        or limits.get("new_bvid_allowed") is not False
    ):
        return "AUTHORITY_DOCUMENT_LIMITS_INVALID"
    return None


def load_human_explicit_endpoint_authority(
    *, candidate_id: str, target_bvid: str | None = None
) -> dict[str, Any]:
    """Load and validate the one candidate-specific repository authority."""

    relative = _AUTHORITY_RELATIVE_BY_CANDIDATE.get(candidate_id)
    if relative is None:
        raise ValueError("HUMAN_EXPLICIT_ENDPOINT_CANDIDATE_NOT_AUTHORIZED")
    path = _REPO_ROOT / relative
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("HUMAN_EXPLICIT_ENDPOINT_AUTHORITY_UNREADABLE") from exc
    problem = _authority_problem(value)
    if problem is not None:
        raise ValueError(problem)
    if value.get("candidate_id") != candidate_id:
        raise ValueError("HUMAN_EXPLICIT_ENDPOINT_AUTHORITY_CANDIDATE_MISMATCH")
    if target_bvid is not None and value.get("target_bvid") != target_bvid:
        raise ValueError("HUMAN_EXPLICIT_ENDPOINT_AUTHORITY_BVID_MISMATCH")
    value = dict(value)
    value["authority_document_binding"] = {
        "schema_version": AUTHORITY_SCHEMA,
        "authority_id": value["authority_id"],
        "relative_path": str(relative),
        "file_sha256": _file_sha256(path),
        "authority_sha256": value["authority_sha256"],
    }
    return value


def _expected_scope_geometry(
    authority: Mapping[str, object], review_scope: str
) -> dict[str, object] | None:
    closure = authority.get("approved_closure")
    final_delivery = authority.get("final_delivery")
    source_window = authority.get("source_full_window")
    if not all(isinstance(value, Mapping) for value in (closure, final_delivery, source_window)):
        return None
    if review_scope == "final_delivery":
        return {
            "cue_grid_sha256": final_delivery["cue_grid_sha256"],
            "recommended_end_cue_index": closure["final_delivery_cue_index"],
            "recommended_end_ms": closure["final_delivery_end_ms"],
            "final_start_ms": 0,
            "final_end_ms": closure["final_media_end_ms"],
        }
    if review_scope == "source_full_window":
        return {
            "cue_grid_sha256": source_window["cue_grid_sha256"],
            "recommended_end_cue_index": source_window[
                "reviewed_endpoint_cue_index"
            ],
            "recommended_end_ms": source_window["reviewed_endpoint_ms"],
            "final_start_ms": source_window["final_start_ms"],
            "final_end_ms": source_window["final_end_ms"],
        }
    return None


def _receipt_problem(receipt: object) -> str | None:
    if not isinstance(receipt, Mapping):
        return "HUMAN_EXPLICIT_ENDPOINT_RECEIPT_NOT_OBJECT"
    try:
        authority = load_human_explicit_endpoint_authority(
            candidate_id=str(receipt.get("candidate_id") or ""),
            target_bvid=str(receipt.get("target_bvid") or ""),
        )
    except ValueError as exc:
        return str(exc)
    scope = str(receipt.get("review_scope") or "")
    expected = _expected_scope_geometry(authority, scope)
    final_delivery = authority["final_delivery"]
    closure = authority["approved_closure"]
    source_window = authority["source_full_window"]
    tail = authority["excluded_same_topic_tail"]
    if expected is None:
        return "HUMAN_EXPLICIT_ENDPOINT_SCOPE_INVALID"
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA
        or receipt.get("status") != "PASS"
        or receipt.get("authority_kind") != AUTHORITY_KIND
        or receipt.get("candidate_id") != authority.get("candidate_id")
        or receipt.get("target_bvid") != authority.get("target_bvid")
        or receipt.get("authority_document")
        != authority.get("authority_document_binding")
        or receipt.get("authority_quote_bindings") != authority.get("authority_quotes")
        or receipt.get("authority_quotes_sha256")
        != authority.get("authority_quotes_sha256")
        or receipt.get("not_a_semantic_transition_claim") is not True
        or receipt.get("review_scope") != scope
        or receipt.get("cue_grid_sha256") != expected["cue_grid_sha256"]
        or receipt.get("source_cue_grid_sha256")
        != source_window["cue_grid_sha256"]
        or receipt.get("final_delivery_cue_grid_sha256")
        != final_delivery["cue_grid_sha256"]
        or receipt.get("recommended_end_cue_index")
        != expected["recommended_end_cue_index"]
        or receipt.get("recommended_end_ms") != expected["recommended_end_ms"]
        or receipt.get("final_start_ms") != expected["final_start_ms"]
        or receipt.get("final_end_ms") != expected["final_end_ms"]
        or receipt.get("closure_text_sha256") != closure["text_sha256"]
        or receipt.get("delivery_srt_sha256") != final_delivery["srt"]["sha256"]
        or receipt.get("delivery_ass_sha256") != final_delivery["ass"]["sha256"]
        or receipt.get("delivery_video_sha256")
        != final_delivery["video"]["sha256"]
        or receipt.get("excluded_tail") != tail
        or receipt.get("payload_sha256") != payload_sha256(receipt)
    ):
        return "HUMAN_EXPLICIT_ENDPOINT_RECEIPT_BINDING_INVALID"
    return None


def seal_human_explicit_endpoint(payload: Mapping[str, object]) -> dict[str, object]:
    """Create a self-hashed receipt only after repository-authority validation."""

    value = dict(payload)
    value.pop("payload_sha256", None)
    value["payload_sha256"] = payload_sha256(value)
    problem = _receipt_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


def valid_human_explicit_endpoint(
    receipt: object,
    *,
    review: Mapping[str, object],
) -> bool:
    """Validate the receipt and bind it to the exact semantic review fields."""

    if _receipt_problem(receipt) is not None or not isinstance(receipt, Mapping):
        return False
    endpoint = review.get("final_endpoint_binding")
    if not isinstance(endpoint, Mapping):
        return False
    return bool(
        review.get("candidate_id") == receipt.get("candidate_id")
        and review.get("review_scope") == receipt.get("review_scope")
        and review.get("cue_grid_sha256") == receipt.get("cue_grid_sha256")
        and review.get("recommended_end_cue_index")
        == receipt.get("recommended_end_cue_index")
        and review.get("recommended_end_ms") == receipt.get("recommended_end_ms")
        and endpoint.get("final_start_ms") == receipt.get("final_start_ms")
        and endpoint.get("final_end_ms") == receipt.get("final_end_ms")
        and endpoint.get("closure_text_sha256")
        == receipt.get("closure_text_sha256")
        and review.get("reviewed_srt_sha256")
        == receipt.get("delivery_srt_sha256")
        and review.get("reviewed_ass_sha256")
        == receipt.get("delivery_ass_sha256")
        and review.get("reviewed_video_sha256")
        == receipt.get("delivery_video_sha256")
        and review.get("next_topic_separated") is False
        and review.get("next_topic_witness_valid") is False
        and review.get("same_topic_continues_after_target") is True
        and review.get("not_a_semantic_transition_claim") is True
        and review.get("excluded_same_topic_tail") == receipt.get("excluded_tail")
        and review.get("human_explicit_endpoint_authority_sha256")
        == receipt.get("authority_document", {}).get("authority_sha256")
    )


def human_explicit_endpoint_authority_sha256(review: object) -> str | None:
    """Return the validated repository authority self-hash for package surfaces."""

    if not isinstance(review, Mapping):
        return None
    receipt = review.get("human_explicit_endpoint_override")
    if not valid_human_explicit_endpoint(receipt, review=review):
        return None
    document = receipt.get("authority_document") if isinstance(receipt, Mapping) else None
    return str(document.get("authority_sha256")) if isinstance(document, Mapping) else None
