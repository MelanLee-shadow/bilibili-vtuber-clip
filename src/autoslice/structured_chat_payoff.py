"""Freeze one canonical structured-chat payoff assessment per boundary attempt."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence


ASSESSMENT_KEY = "structured_chat_payoff_assessment"
ASSESSMENT_SCHEMA_VERSION = "structured-chat-payoff-assessment.v1"
EFFECTIVE_STORY_MODE = "effective_story"
TERMINAL_AUTHORITY_MODE = "terminal_authority_clamp_hypothesis"


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def assessment_sha256(assessment: Mapping[str, object]) -> str:
    """Hash an assessment without its self-referential digest."""

    return _canonical_sha256(
        {
            str(key): value
            for key, value in assessment.items()
            if key != "assessment_sha256"
        }
    )


def _row_ref(
    row: Mapping[str, object],
    *,
    ordinal: int,
    semantic_target_ms: int,
) -> dict[str, object] | None:
    if row.get("reconciliation"):
        # Reconciliation rows are historical comparison evidence. The owner
        # classifier intentionally does not classify them, so they cannot
        # become a fresh payoff hypothesis or trigger a false missing-classification block.
        return None
    kind = str(row.get("kind") or "")
    source_offset_ms = row.get("source_offset_ms")
    matched_start_ms = row.get("matched_start_ms")
    matched_end_ms = row.get("matched_end_ms")
    if (
        kind not in {"danmaku", "superchat"}
        or isinstance(source_offset_ms, bool)
        or not isinstance(source_offset_ms, int)
        or isinstance(matched_start_ms, bool)
        or not isinstance(matched_start_ms, int)
        or isinstance(matched_end_ms, bool)
        or not isinstance(matched_end_ms, int)
        or matched_end_ms <= matched_start_ms
        or source_offset_ms > semantic_target_ms + 1_000
        or not semantic_target_ms < matched_end_ms <= semantic_target_ms + 15_000
        or matched_start_ms > semantic_target_ms + 5_000
    ):
        return None
    row_id = str(
        row.get("finding_id")
        or row.get("verdict_id")
        or row.get("event_id")
        or f"applied:{ordinal}:{kind}:{source_offset_ms}:{matched_start_ms}:{matched_end_ms}"
    )
    return {
        "row_id": row_id,
        "kind": kind,
        "source_offset_ms": source_offset_ms,
        "matched_start_ms": matched_start_ms,
        "matched_end_ms": matched_end_ms,
    }


def _time_qualified_ref(
    ref: Mapping[str, object],
    *,
    semantic_target_ms: int,
) -> bool:
    source_offset_ms = ref.get("source_offset_ms")
    matched_start_ms = ref.get("matched_start_ms")
    matched_end_ms = ref.get("matched_end_ms")
    return bool(
        isinstance(source_offset_ms, int)
        and not isinstance(source_offset_ms, bool)
        and isinstance(matched_start_ms, int)
        and not isinstance(matched_start_ms, bool)
        and isinstance(matched_end_ms, int)
        and not isinstance(matched_end_ms, bool)
        and source_offset_ms <= semantic_target_ms + 1_000
        and semantic_target_ms < matched_end_ms <= semantic_target_ms + 15_000
        and matched_start_ms <= semantic_target_ms + 5_000
    )


def _ref_key(ref: Mapping[str, object]) -> tuple[object, ...]:
    return (
        ref.get("row_id"),
        ref.get("kind"),
        ref.get("source_offset_ms"),
        ref.get("matched_start_ms"),
        ref.get("matched_end_ms"),
    )


def _normalized_refs(
    refs: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for ref in refs:
        row_id = str(ref.get("row_id") or "")
        kind = str(ref.get("kind") or "")
        source_offset_ms = ref.get("source_offset_ms")
        matched_start_ms = ref.get("matched_start_ms")
        matched_end_ms = ref.get("matched_end_ms")
        if (
            not row_id
            or kind not in {"danmaku", "superchat"}
            or isinstance(source_offset_ms, bool)
            or not isinstance(source_offset_ms, int)
            or isinstance(matched_start_ms, bool)
            or not isinstance(matched_start_ms, int)
            or isinstance(matched_end_ms, bool)
            or not isinstance(matched_end_ms, int)
            or matched_end_ms <= matched_start_ms
        ):
            raise RuntimeError("STRUCTURED_CHAT_PAYOFF_ASSESSMENT_INVALID")
        normalized.append(
            {
                "row_id": row_id,
                "kind": kind,
                "source_offset_ms": source_offset_ms,
                "matched_start_ms": matched_start_ms,
                "matched_end_ms": matched_end_ms,
            }
        )
    return sorted(
        normalized,
        key=lambda ref: (
            int(ref["matched_end_ms"]),
            int(ref["matched_start_ms"]),
            str(ref["kind"]),
            str(ref["row_id"]),
        ),
    )


def freeze_assessment(
    audit: Mapping[str, object],
    *,
    semantic_target_ms: int,
    terminal_authority_clamp: bool,
) -> dict[str, object]:
    """Freeze observed/effective payoff identity after owner classification.

    ``observed_ms`` records every time-qualified hypothesis independent of the
    owner decision. ``effective_story_ms`` removes rows proved wholly outside
    (or otherwise ineligible for) the immutable story, while preserving a read
    that actually straddles the selected tail. A reviewed baseline/exact pin
    retains ``observed_ms`` only as a hypothesis for the existing terminal
    clamp; ordinary story geometry consumes ``effective_story_ms``.
    """

    if isinstance(semantic_target_ms, bool) or not isinstance(semantic_target_ms, int):
        raise RuntimeError("STRUCTURED_CHAT_PAYOFF_ASSESSMENT_INVALID")
    observed_refs: list[dict[str, object]] = []
    effective_refs: list[dict[str, object]] = []
    excluded_refs: list[dict[str, object]] = []
    for ordinal, row in enumerate(audit.get("applied") or [], start=1):
        if not isinstance(row, Mapping):
            continue
        ref = _row_ref(
            row,
            ordinal=ordinal,
            semantic_target_ms=semantic_target_ms,
        )
        if ref is None:
            continue
        observed_refs.append(ref)
        boundary_required = row.get("boundary_required")
        rejection = str(row.get("boundary_owner_rejection") or "")
        if boundary_required is True or rejection == "STRADDLES_IMMUTABLE_STORY_SCOPE":
            effective_refs.append(ref)
            continue
        if boundary_required is not False or not rejection:
            raise RuntimeError("STRUCTURED_CHAT_PAYOFF_CLASSIFICATION_MISSING")
        excluded_refs.append({**ref, "reason_code": rejection})

    observed_refs = _normalized_refs(observed_refs)
    effective_refs = _normalized_refs(effective_refs)
    excluded_refs.sort(
        key=lambda ref: (
            int(ref["matched_end_ms"]),
            int(ref["matched_start_ms"]),
            str(ref["kind"]),
            str(ref["row_id"]),
            str(ref["reason_code"]),
        )
    )
    observed_ms = max(
        (int(ref["matched_end_ms"]) for ref in observed_refs),
        default=None,
    )
    effective_story_ms = max(
        (int(ref["matched_end_ms"]) for ref in effective_refs),
        default=None,
    )
    mode = TERMINAL_AUTHORITY_MODE if terminal_authority_clamp else EFFECTIVE_STORY_MODE
    scope_ms = observed_ms if terminal_authority_clamp else effective_story_ms
    assessment: dict[str, object] = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "semantic_target_ms": semantic_target_ms,
        "mode": mode,
        "scope_basis": "observed_ms" if terminal_authority_clamp else "effective_story_ms",
        "observed_ms": observed_ms,
        "effective_story_ms": effective_story_ms,
        "scope_ms": scope_ms,
        "observed_row_refs": observed_refs,
        "effective_row_refs": effective_refs,
        "excluded_row_refs": excluded_refs,
    }
    assessment["assessment_sha256"] = assessment_sha256(assessment)
    return assessment


def validate_assessment(assessment: object) -> dict[str, object]:
    if not isinstance(assessment, Mapping):
        raise RuntimeError("STRUCTURED_CHAT_PAYOFF_ASSESSMENT_INVALID")
    semantic_target_ms = assessment.get("semantic_target_ms")
    mode = assessment.get("mode")
    observed_raw = assessment.get("observed_row_refs")
    effective_raw = assessment.get("effective_row_refs")
    excluded_raw = assessment.get("excluded_row_refs")
    if (
        assessment.get("schema_version") != ASSESSMENT_SCHEMA_VERSION
        or isinstance(semantic_target_ms, bool)
        or not isinstance(semantic_target_ms, int)
        or mode not in {EFFECTIVE_STORY_MODE, TERMINAL_AUTHORITY_MODE}
        or not isinstance(observed_raw, list)
        or not isinstance(effective_raw, list)
        or not isinstance(excluded_raw, list)
    ):
        raise RuntimeError("STRUCTURED_CHAT_PAYOFF_ASSESSMENT_INVALID")

    observed = _normalized_refs(observed_raw)
    effective = _normalized_refs(effective_raw)
    excluded: list[dict[str, object]] = []
    for ref in excluded_raw:
        if not isinstance(ref, Mapping) or not str(ref.get("reason_code") or ""):
            raise RuntimeError("STRUCTURED_CHAT_PAYOFF_ASSESSMENT_INVALID")
        normalized = _normalized_refs([ref])[0]
        excluded.append({**normalized, "reason_code": str(ref["reason_code"])})
    excluded.sort(
        key=lambda ref: (
            int(ref["matched_end_ms"]),
            int(ref["matched_start_ms"]),
            str(ref["kind"]),
            str(ref["row_id"]),
            str(ref["reason_code"]),
        )
    )
    if (
        observed_raw != observed
        or effective_raw != effective
        or excluded_raw != excluded
    ):
        raise RuntimeError("STRUCTURED_CHAT_PAYOFF_ASSESSMENT_INVALID")

    observed_keys = {_ref_key(ref) for ref in observed}
    effective_keys = {_ref_key(ref) for ref in effective}
    excluded_keys = {_ref_key(ref) for ref in excluded}
    observed_row_ids = {str(ref["row_id"]) for ref in observed}
    effective_row_ids = {str(ref["row_id"]) for ref in effective}
    excluded_row_ids = {str(ref["row_id"]) for ref in excluded}
    observed_ms = max(
        (int(ref["matched_end_ms"]) for ref in observed),
        default=None,
    )
    effective_story_ms = max(
        (int(ref["matched_end_ms"]) for ref in effective),
        default=None,
    )
    expected_scope_ms = observed_ms if mode == TERMINAL_AUTHORITY_MODE else effective_story_ms
    expected_scope_basis = "observed_ms" if mode == TERMINAL_AUTHORITY_MODE else "effective_story_ms"
    if (
        len(observed_keys) != len(observed)
        or len(effective_keys) != len(effective)
        or len(excluded_keys) != len(excluded)
        or len(observed_row_ids) != len(observed)
        or len(effective_row_ids) != len(effective)
        or len(excluded_row_ids) != len(excluded)
        or any(
            not _time_qualified_ref(ref, semantic_target_ms=semantic_target_ms)
            for ref in observed
        )
        or effective_keys & excluded_keys
        or effective_keys | excluded_keys != observed_keys
        or assessment.get("observed_ms") != observed_ms
        or assessment.get("effective_story_ms") != effective_story_ms
        or assessment.get("scope_ms") != expected_scope_ms
        or assessment.get("scope_basis") != expected_scope_basis
        or assessment.get("assessment_sha256") != assessment_sha256(assessment)
    ):
        raise RuntimeError("STRUCTURED_CHAT_PAYOFF_ASSESSMENT_INVALID")
    return dict(assessment)
