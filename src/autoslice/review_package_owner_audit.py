"""Package-level validation for final text and frozen boundary owners."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.autoslice.producer_boundary_owner_contract import (
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.source_subtitle_truth import (
    source_truth_owner_windows,
)


IssueAdder = Callable[..., None]
STORY_OWNER_GROUPS = (
    ("exact_read", "applied"),
    ("sc_sender", "sender_repairs"),
    ("gift_name", "gift_repairs"),
    ("reply_coreference", "coreference_repairs"),
    ("entity_repair", "entity_repairs"),
)
SOURCE_TRUTH_AUDIT_SCHEMA = "source-subtitle-truth-audit.v1"
SOURCE_TRUTH_SUCCESS_STATUSES = {
    "NO_RELEVANT_INTERVAL",
    "APPLIED",
    "ALREADY_SATISFIED",
}
SOURCE_TRUTH_LEDGER_PATH = (
    Path(__file__).resolve().parents[2]
    / "assets/lidousha/subtitle_truth_ledger.v1.json"
)


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strict_nonnegative_int(value: object) -> bool:
    return _strict_int(value) and int(value) >= 0


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value)
    )


def _normalized_windows(
    value: object,
) -> list[dict[str, int]] | None:
    if not isinstance(value, list) or not value:
        return None
    normalized: list[dict[str, int]] = []
    for window in value:
        if not isinstance(window, Mapping):
            return None
        start_ms = window.get("start_ms")
        end_ms = window.get("end_ms")
        if (
            not _strict_nonnegative_int(start_ms)
            or not _strict_int(end_ms)
            or int(end_ms) <= int(start_ms)
        ):
            return None
        normalized.append(
            {"start_ms": int(start_ms), "end_ms": int(end_ms)}
        )
    return sorted(
        normalized,
        key=lambda window: (window["start_ms"], window["end_ms"]),
    )


def _final_delivery_row_valid(
    row: Mapping[str, object],
    *,
    owner_windows: list[tuple[int, int]],
    delivery_start: int,
    delivery_end: int,
) -> tuple[bool, int]:
    row_windows = row.get("final_owner_windows")
    final_contract = row.get("final_owner_contract")
    if not isinstance(row_windows, list) or not row_windows:
        return False, 0
    expected_relative_windows = sorted(
        (
            start_ms - delivery_start,
            end_ms - delivery_start,
        )
        for start_ms, end_ms in owner_windows
    )
    actual_relative_windows = sorted(
        (
            int(window.get("start_ms", -1)),
            int(window.get("end_ms", -1)),
        )
        for window in row_windows
        if isinstance(window, Mapping)
        and _strict_nonnegative_int(window.get("start_ms"))
        and _strict_nonnegative_int(window.get("end_ms"))
    )
    valid = bool(
        row.get("final_owner_verified") is True
        and actual_relative_windows == expected_relative_windows
        and all(
            isinstance(window, Mapping)
            and window.get("text_ok") is True
            and window.get("speaker_ok") is True
            for window in row_windows
        )
        and isinstance(final_contract, Mapping)
        and final_contract.get("text_ok") is True
        and final_contract.get("speaker_ok") is True
        and all(
            delivery_start <= start_ms and end_ms <= delivery_end
            for start_ms, end_ms in owner_windows
        )
    )
    return valid, len(row_windows)


def _context_only_row_valid(
    row: Mapping[str, object],
    *,
    owner_windows: list[tuple[int, int]],
    delivery_start: int,
    delivery_end: int,
) -> bool:
    row_windows = row.get("final_owner_windows")
    actual_windows = (
        sorted(
            (
                int(window.get("start_ms", -1)),
                int(window.get("end_ms", -1)),
            )
            for window in row_windows
            if isinstance(window, Mapping)
            and _strict_nonnegative_int(window.get("start_ms"))
            and _strict_nonnegative_int(window.get("end_ms"))
        )
        if isinstance(row_windows, list)
        else []
    )
    return bool(
        row.get("final_owner_verified") is False
        and row.get("final_owner_scope_reason")
        == "ALL_EFFECTIVE_OWNER_WINDOWS_OUTSIDE_HALF_OPEN_FINAL_INTERVAL"
        and actual_windows == sorted(owner_windows)
        and isinstance(row_windows, list)
        and all(
            isinstance(window, Mapping)
            and str(window.get("delivery_relation") or "").startswith(
                "OUTSIDE_"
            )
            for window in row_windows
        )
        and all(
            end_ms <= delivery_start or start_ms >= delivery_end
            for start_ms, end_ms in owner_windows
        )
    )


def _source_truth_audit_valid(truth_audit: object) -> bool:
    if not isinstance(truth_audit, Mapping):
        return False
    applied = truth_audit.get("applied")
    satisfied = truth_audit.get("satisfied")
    failures = truth_audit.get("failures")
    status = truth_audit.get("status")
    try:
        ledger_sha256 = (
            "sha256:"
            + hashlib.sha256(SOURCE_TRUTH_LEDGER_PATH.read_bytes()).hexdigest()
        )
    except OSError:
        return False
    if not (
        truth_audit.get("schema_version") == SOURCE_TRUTH_AUDIT_SCHEMA
        and status in SOURCE_TRUTH_SUCCESS_STATUSES
        and isinstance(applied, list)
        and isinstance(satisfied, list)
        and failures == []
        and truth_audit.get("ledger_sha256") == ledger_sha256
        and isinstance(truth_audit.get("ledger_path"), str)
        and Path(str(truth_audit["ledger_path"])).name
        == SOURCE_TRUTH_LEDGER_PATH.name
    ):
        return False
    if not all(
        isinstance(row, Mapping)
        and isinstance(row.get("truth_id"), str)
        and bool(row.get("truth_id"))
        and isinstance(row.get("required"), bool)
        and row.get("boundary_role")
        in {"story_content", "next_topic_witness"}
        and _normalized_windows(row.get("local_windows")) is not None
        for row in [*applied, *satisfied]
    ):
        return False
    if status == "NO_RELEVANT_INTERVAL":
        return not applied and not satisfied
    if status == "APPLIED":
        return bool(applied)
    return not applied and bool(satisfied)


def _final_truth_owner_receipt_valid(
    *,
    truth_owner: object,
    truth_rows: list[dict[str, Any]],
    optional_truth_row_count: int,
) -> bool:
    if not truth_rows:
        return True
    if not isinstance(truth_owner, Mapping):
        return False
    delivery_interval = truth_owner.get("final_delivery_interval")
    if not isinstance(delivery_interval, Mapping):
        return False
    delivery_start = delivery_interval.get("start_ms")
    delivery_end = delivery_interval.get("end_ms")
    count_keys = (
        "required_truth_row_count",
        "context_only_truth_row_count",
        "optional_truth_row_count",
        "straddling_truth_row_count",
        "required_window_count",
    )
    if not (
        truth_owner.get("status") == "PASS"
        and truth_owner.get("failures") == []
        and delivery_interval.get("timeline") == "padded_source_local_ms"
        and delivery_interval.get("interval_semantics") == "half_open"
        and _strict_nonnegative_int(delivery_start)
        and _strict_nonnegative_int(delivery_end)
        and int(delivery_end) > int(delivery_start)
        and all(
            _strict_nonnegative_int(truth_owner.get(key))
            for key in count_keys
        )
    ):
        return False

    final_delivery_rows = 0
    context_only_rows = 0
    final_delivery_windows = 0
    required_truth_ids: list[str] = []
    context_only_truth_ids: list[str] = []
    context_only_truth_evidence: list[dict[str, object]] = []
    rows_valid = True
    for row in truth_rows:
        owner_windows = source_truth_owner_windows(row)
        if not owner_windows:
            rows_valid = False
            continue
        if row.get("final_owner_scope") == "FINAL_DELIVERY":
            final_delivery_rows += 1
            required_truth_ids.append(str(row.get("truth_id") or ""))
            valid, window_count = _final_delivery_row_valid(
                row,
                owner_windows=owner_windows,
                delivery_start=int(delivery_start),
                delivery_end=int(delivery_end),
            )
            rows_valid = rows_valid and valid
            final_delivery_windows += window_count
        elif (
            row.get("final_owner_scope")
            == "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
        ):
            context_only_rows += 1
            context_only_truth_ids.append(str(row.get("truth_id") or ""))
            context_only_truth_evidence.append(
                {
                    "truth_id": str(row.get("truth_id") or ""),
                    "boundary_role": str(
                        row.get("boundary_role") or ""
                    ),
                    "final_owner_scope": row.get("final_owner_scope"),
                    "final_owner_windows": row.get(
                        "final_owner_windows"
                    ),
                }
            )
            rows_valid = rows_valid and _context_only_row_valid(
                row,
                owner_windows=owner_windows,
                delivery_start=int(delivery_start),
                delivery_end=int(delivery_end),
            )
        else:
            rows_valid = False
    return bool(
        rows_valid
        and truth_owner.get("required_truth_row_count")
        == final_delivery_rows
        and truth_owner.get("required_truth_ids") == required_truth_ids
        and truth_owner.get("context_only_truth_row_count")
        == context_only_rows
        and truth_owner.get("context_only_truth_ids")
        == context_only_truth_ids
        and truth_owner.get("context_only_truth_evidence")
        == context_only_truth_evidence
        and truth_owner.get("optional_truth_row_count")
        == optional_truth_row_count
        and truth_owner.get("straddling_truth_row_count") == 0
        and truth_owner.get("required_window_count")
        == final_delivery_windows
        and final_delivery_rows + context_only_rows == len(truth_rows)
    )


def _candidate_owner_scope(
    *,
    frozen: object,
    record: Mapping[str, object],
) -> tuple[bool, int, int, Mapping[str, object] | None]:
    owner_scope = (
        frozen.get("owner_eligibility_scope")
        if isinstance(frozen, Mapping)
        else None
    )
    if not isinstance(owner_scope, Mapping):
        return False, -1, -1, None
    fields = {
        key: owner_scope.get(key)
        for key in (
            "story_start_ms",
            "story_end_ms",
            "semantic_source_start_ms",
            "semantic_source_end_ms",
            "given_source_end_ms",
            "first_piece_source_start_ms",
            "last_piece_source_start_ms",
            "prior_piece_duration_ms",
        )
    }
    required_values = [
        value
        for key, value in fields.items()
        if key != "given_source_end_ms"
    ]
    if not (
        owner_scope.get("schema_version")
        == "candidate-boundary-owner-scope.v1"
        and isinstance(owner_scope.get("candidate_id"), str)
        and bool(str(owner_scope.get("candidate_id") or ""))
        and all(_strict_int(value) for value in required_values)
        and (
            fields["given_source_end_ms"] is None
            or _strict_int(fields["given_source_end_ms"])
        )
    ):
        return False, -1, -1, owner_scope
    story_start = int(fields["story_start_ms"])
    story_end = int(fields["story_end_ms"])
    semantic_end = int(fields["semantic_source_end_ms"])
    given_end = fields["given_source_end_ms"]
    expected_start = int(fields["semantic_source_start_ms"]) - int(
        fields["first_piece_source_start_ms"]
    )
    expected_end = (
        int(fields["prior_piece_duration_ms"])
        + max(
            semantic_end,
            int(given_end) if given_end is not None else semantic_end,
        )
        - int(fields["last_piece_source_start_ms"])
    )
    valid = bool(
        0 <= story_start < story_end
        and int(fields["prior_piece_duration_ms"]) >= 0
        and story_start == expected_start
        and story_end == expected_end
        and isinstance(frozen, Mapping)
        and frozen.get("story_start_ms") == story_start
        and frozen.get("story_end_ms") == story_end
    )
    story_contract = record.get("story_contract")
    record_candidate_id = (
        story_contract.get("candidate_id")
        if isinstance(story_contract, Mapping)
        else record.get("candidate_id")
    )
    if (
        isinstance(record_candidate_id, str)
        and record_candidate_id
        and owner_scope.get("candidate_id") != record_candidate_id
    ):
        valid = False
    return valid, story_start, story_end, owner_scope


def _source_truth_owner_set_valid(
    *,
    truth_rows: list[dict[str, Any]],
    frozen_owner_by_key: Mapping[tuple[str, str], object],
    frozen_owner_keys: list[tuple[str, str]],
    scope_valid: bool,
    story_start: int,
    story_end: int,
    owner_scope: Mapping[str, object] | None,
) -> bool:
    valid = scope_valid
    expected_ids: set[str] = set()
    seen_ids: set[str] = set()
    for row in truth_rows:
        truth_id = str(row.get("truth_id") or "")
        windows = _normalized_windows(row.get("local_windows"))
        role = str(row.get("boundary_role") or "")
        if (
            row.get("required") is not True
            or not truth_id
            or truth_id in seen_ids
            or windows is None
            or role not in {"story_content", "next_topic_witness"}
            or not scope_valid
        ):
            valid = False
            continue
        seen_ids.add(truth_id)
        fully_inside = all(
            story_start <= window["start_ms"]
            and window["end_ms"] <= story_end
            for window in windows
        )
        overlaps = any(
            min(window["end_ms"], story_end)
            - max(window["start_ms"], story_start)
            > 0
            for window in windows
        )
        if role == "next_topic_witness":
            valid = valid and not overlaps
            continue
        if overlaps and not fully_inside:
            valid = False
            continue
        if not fully_inside:
            continue
        expected_ids.add(truth_id)
        owner = frozen_owner_by_key.get(
            ("source_subtitle_truth", truth_id)
        )
        if not (
            isinstance(owner, Mapping)
            and owner.get("required") is True
            and isinstance(owner_scope, Mapping)
            and owner.get("owner_scope_sha256")
            == owner_scope.get("scope_sha256")
            and owner.get("source_start_ms") == row.get("source_start_ms")
            and owner.get("source_end_ms") == row.get("source_end_ms")
            and _normalized_windows(owner.get("local_windows")) == windows
        ):
            valid = False
    frozen_ids = {
        owner_id
        for owner_kind, owner_id in frozen_owner_keys
        if owner_kind == "source_subtitle_truth"
    }
    return valid and frozen_ids == expected_ids


def _story_owner_set_valid(
    *,
    chat_authority: Mapping[str, object],
    frozen_owner_by_key: Mapping[tuple[str, str], object],
    frozen_owner_keys: list[tuple[str, str]],
    story_start: int,
    story_end: int,
) -> bool:
    expected: dict[tuple[str, str], list[dict[str, int]]] = {}
    valid = True
    for owner_kind, key in STORY_OWNER_GROUPS:
        rows = chat_authority.get(key) or []
        if not isinstance(rows, list):
            valid = False
            continue
        for ordinal, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping) or row.get("reconciliation"):
                continue
            if (
                owner_kind == "exact_read"
                and row.get("owner_eligible") is not True
            ):
                valid = valid and bool(
                    row.get("boundary_required") is False
                    and row.get("boundary_owner_rejection")
                    == "EXACT_READ_SUPPORT_NOT_OWNER_ELIGIBLE"
                )
                continue
            start = row.get("matched_start_ms")
            end = row.get("matched_end_ms")
            if (
                not _strict_nonnegative_int(start)
                or not _strict_int(end)
                or int(end) <= int(start)
            ):
                valid = valid and row.get("boundary_required") is not True
                continue
            overlap_ms = (
                min(int(end), story_end)
                - max(int(start), story_start)
            )
            if overlap_ms <= 0:
                valid = valid and bool(
                    row.get("boundary_required") is False
                    and row.get("boundary_owner_rejection")
                    == "OUTSIDE_IMMUTABLE_STORY_SCOPE"
                )
                continue
            if not story_start <= int(start) < int(end) <= story_end:
                valid = valid and bool(
                    row.get("boundary_required") is False
                    and row.get("boundary_owner_rejection")
                    == "STRADDLES_IMMUTABLE_STORY_SCOPE"
                )
                continue
            owner_id = str(
                row.get("finding_id")
                or row.get("verdict_id")
                or f"{owner_kind}:{ordinal}:{start}:{end}"
            )
            owner_key = (owner_kind, owner_id)
            expected_windows = [
                {"start_ms": int(start), "end_ms": int(end)}
            ]
            if owner_key in expected:
                valid = False
            expected[owner_key] = expected_windows
            owner = frozen_owner_by_key.get(owner_key)
            valid = valid and bool(
                row.get("boundary_required") is True
                and row.get("boundary_owner_id") == owner_id
                and isinstance(owner, Mapping)
                and owner.get("required") is True
                and _normalized_windows(owner.get("local_windows"))
                == expected_windows
            )
    story_kinds = {kind for kind, _ in STORY_OWNER_GROUPS}
    frozen = {
        (owner_kind, owner_id)
        for owner_kind, owner_id in frozen_owner_keys
        if owner_kind in story_kinds
    }
    return valid and set(expected) == frozen


def _retry_verification_valid(
    *,
    frozen: Mapping[str, object],
    owner_scope: Mapping[str, object] | None,
) -> bool:
    receipt = frozen.get("boundary_retry_owner_contract_verification")
    return bool(
        receipt is None
        or (
            isinstance(receipt, Mapping)
            and receipt.get("status") == "PASS"
            and _is_sha256(receipt.get("expected_contract_sha256"))
            and receipt.get("owner_set_sha256")
            == frozen.get("owner_set_sha256")
            and isinstance(owner_scope, Mapping)
            and receipt.get("owner_eligibility_scope_sha256")
            == owner_scope.get("scope_sha256")
        )
    )


def _frozen_owner_contract_valid(
    *,
    frozen: object,
    truth_rows: list[dict[str, Any]],
    chat_authority: Mapping[str, object],
    record: Mapping[str, object],
) -> tuple[bool, list[object] | None]:
    owners = (
        frozen.get("owners") if isinstance(frozen, Mapping) else None
    )
    owner_keys = [
        (
            str(owner.get("owner_kind") or ""),
            str(owner.get("owner_id") or ""),
        )
        for owner in (owners or [])
        if isinstance(owner, Mapping)
    ]
    owner_by_key = {
        (
            str(owner.get("owner_kind") or ""),
            str(owner.get("owner_id") or ""),
        ): owner
        for owner in (owners or [])
        if isinstance(owner, Mapping)
    }
    scope_valid, story_start, story_end, owner_scope = (
        _candidate_owner_scope(frozen=frozen, record=record)
    )
    try:
        validate_frozen_boundary_owner_contract(frozen)
        integrity_valid = True
    except (KeyError, RuntimeError, TypeError, ValueError):
        integrity_valid = False
    allowed_kinds = {
        "source_subtitle_truth",
        *(kind for kind, _ in STORY_OWNER_GROUPS),
    }
    owners_shape_valid = bool(
        isinstance(owners, list)
        and all(
            isinstance(owner, Mapping)
            and owner.get("required") is True
            and str(owner.get("owner_kind") or "")
            and str(owner.get("owner_id") or "")
            and _normalized_windows(owner.get("local_windows")) is not None
            for owner in owners
        )
        and len(owner_keys) == len(set(owner_keys))
        and all(kind in allowed_kinds for kind, _ in owner_keys)
    )
    source_valid = _source_truth_owner_set_valid(
        truth_rows=truth_rows,
        frozen_owner_by_key=owner_by_key,
        frozen_owner_keys=owner_keys,
        scope_valid=scope_valid,
        story_start=story_start,
        story_end=story_end,
        owner_scope=owner_scope,
    )
    return (
        bool(
            integrity_valid
            and scope_valid
            and owners_shape_valid
            and source_valid
            and _story_owner_set_valid(
                chat_authority=chat_authority,
                frozen_owner_by_key=owner_by_key,
                frozen_owner_keys=owner_keys,
                story_start=story_start,
                story_end=story_end,
            )
            and isinstance(frozen, Mapping)
            and _retry_verification_valid(
                frozen=frozen,
                owner_scope=owner_scope,
            )
        ),
        owners,
    )


def _boundary_attestation_valid(
    *,
    boundary_audit: object,
    frozen_owners: list[object] | None,
    chat_authority: Mapping[str, object],
) -> bool:
    if not isinstance(boundary_audit, Mapping):
        return False
    owner_verification = boundary_audit.get(
        "required_boundary_owner_verification"
    )
    coverage_verification = boundary_audit.get(
        "delivery_coverage_verification"
    )
    return bool(
        boundary_audit.get("frozen_required_boundary_owner_count")
        == len(frozen_owners or [])
        and boundary_audit.get("frozen_required_boundary_owners")
        == frozen_owners
        and isinstance(owner_verification, Mapping)
        and owner_verification.get("status") == "PASS"
        and owner_verification.get("failures") == []
        and isinstance(coverage_verification, Mapping)
        and coverage_verification.get("status") == "PASS"
        and coverage_verification.get("failure") is None
        and _strict_nonnegative_int(
            chat_authority.get(
                "final_boundary_required_exclusion_count", 0
            )
        )
        and chat_authority.get(
            "final_boundary_required_exclusion_count", 0
        )
        == 0
    )


def _final_decision_coverage_valid(
    *,
    chat_authority: Mapping[str, object],
    truth_rows: list[dict[str, Any]],
    baseline_audit: object,
    baseline_owner: object,
) -> bool:
    if not truth_rows and not isinstance(baseline_audit, Mapping):
        return True
    keys = (
        "final_required_legacy_decision_count",
        "final_required_source_truth_owner_count",
        "final_required_redelivery_baseline_owner_count",
        "final_required_decision_count",
    )
    if not all(
        _strict_nonnegative_int(chat_authority.get(key))
        for key in keys
    ):
        return False
    truth_owner = chat_authority.get(
        "final_source_truth_owner_verification"
    )
    source_count = (
        truth_owner.get("required_window_count")
        if isinstance(truth_owner, Mapping)
        else None
    )
    baseline_count = (
        baseline_owner.get("required_mapping_count")
        if isinstance(baseline_audit, Mapping)
        and baseline_audit.get("status")
        in {"APPLIED", "ALREADY_SATISFIED"}
        and isinstance(baseline_owner, Mapping)
        else 0
    )
    legacy_count = int(
        chat_authority["final_required_legacy_decision_count"]
    )
    return bool(
        _strict_nonnegative_int(source_count)
        and _strict_nonnegative_int(baseline_count)
        and chat_authority.get(
            "final_required_source_truth_owner_count"
        )
        == source_count
        and chat_authority.get(
            "final_required_redelivery_baseline_owner_count"
        )
        == baseline_count
        and chat_authority.get("final_required_decision_count")
        == legacy_count + int(source_count) + int(baseline_count)
    )


def audit_source_truth_owner_attestations(
    *,
    issue_adder: IssueAdder,
    issues: list[dict[str, Any]],
    stem: str,
    chat_authority_path: Path | None,
    chat_authority: dict[str, Any],
    record_path: Path | None,
    record: dict[str, Any],
) -> None:
    """Audit final text ownership separately from boundary ownership."""

    truth_audit_raw = chat_authority.get("source_subtitle_truth_audit")
    truth_audit = (
        truth_audit_raw if isinstance(truth_audit_raw, dict) else {}
    )
    if not _source_truth_audit_valid(truth_audit_raw):
        issue_adder(
            issues,
            "SOURCE_TRUTH_AUDIT_MISSING_OR_INVALID",
            stem=stem,
            path=chat_authority_path,
        )
    truth_rows = [
        row
        for key in ("applied", "satisfied")
        for row in truth_audit.get(key) or []
        if isinstance(row, dict) and row.get("required") is not False
    ]
    optional_rows = [
        row
        for key in ("applied", "satisfied")
        for row in truth_audit.get(key) or []
        if isinstance(row, dict) and row.get("required") is False
    ]
    if not _final_truth_owner_receipt_valid(
        truth_owner=chat_authority.get(
            "final_source_truth_owner_verification"
        ),
        truth_rows=truth_rows,
        optional_truth_row_count=len(optional_rows),
    ):
        issue_adder(
            issues,
            "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING",
            stem=stem,
            path=chat_authority_path,
        )

    baseline_audit = chat_authority.get(
        "redelivery_subtitle_baseline_audit"
    )
    baseline_owner = chat_authority.get(
        "final_redelivery_baseline_owner_verification"
    )
    if (
        isinstance(baseline_audit, dict)
        and baseline_audit.get("status")
        in {"APPLIED", "ALREADY_SATISFIED"}
        and (
            not isinstance(baseline_owner, dict)
            or baseline_owner.get("status") != "PASS"
            or int(baseline_owner.get("required_mapping_count") or 0)
            <= 0
        )
    ):
        issue_adder(
            issues,
            "REDELIVERY_BASELINE_FINAL_OWNER_ATTESTATION_MISSING",
            stem=stem,
            path=chat_authority_path,
        )
    if not _final_decision_coverage_valid(
        chat_authority=chat_authority,
        truth_rows=truth_rows,
        baseline_audit=baseline_audit,
        baseline_owner=baseline_owner,
    ):
        issue_adder(
            issues,
            "FINAL_AUTHORITY_DECISION_COVERAGE_EMPTY",
            stem=stem,
            path=chat_authority_path,
        )

    frozen = chat_authority.get("frozen_boundary_owner_contract")
    frozen_valid, frozen_owners = _frozen_owner_contract_valid(
        frozen=frozen,
        truth_rows=truth_rows,
        chat_authority=chat_authority,
        record=record,
    )
    if not frozen_valid:
        issue_adder(
            issues,
            "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID",
            stem=stem,
            path=chat_authority_path,
        )
    if not _boundary_attestation_valid(
        boundary_audit=record.get("boundary_audit"),
        frozen_owners=frozen_owners,
        chat_authority=chat_authority,
    ):
        issue_adder(
            issues,
            "REQUIRED_BOUNDARY_OWNER_ATTESTATION_MISSING",
            stem=stem,
            path=record_path or chat_authority_path,
        )
