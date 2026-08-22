"""Package-level validation for final text and frozen boundary owners."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.autoslice.acoustic_witness_adjudication import (
    valid_inaudible_drop_repair,
    valid_inaudible_override_repair,
)
from src.autoslice.boundary_semantic_review import (
    boundary_search_scope_is_valid,
)
from src.autoslice.channel_profile import (
    load_channel_profile as _load_channel_profile,
)
from src.autoslice.producer_boundary_owner_contract import (
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.qixi_terminal_reconciliation import (
    validate_terminal_baseline_replay_reconciliation,
)
from src.autoslice.redelivery_boundary_projection import (
    AUTHORITY_CONFIG_KEY,
    MATERIALIZATION_SCHEMA_VERSION,
    RedeliveryBoundaryProjectionError,
    projection_scope_binding,
    validate_terminal_projection_authority,
)
from src.autoslice.source_subtitle_truth import (
    BOUNDARY_OWNER_LEAD_TOLERANCE_MS,
    source_truth_owner_windows,
)
from src.autoslice.reviewed_exact_source_interval import (
    FROZEN_CONTRACT_KEY as EXACT_INTERVAL_FROZEN_CONTRACT_KEY,
    RUNTIME_CONFIG_KEY as EXACT_INTERVAL_RUNTIME_CONFIG_KEY,
    ReviewedExactSourceIntervalError,
    validate_runtime_authority as validate_exact_interval_authority,
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
SOURCE_TRUTH_LEDGER_PATH = _load_channel_profile(Path(__file__).resolve().parents[2]).asset_file(
    "subtitle_truth_ledger"
)


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strict_nonnegative_int(value: object) -> bool:
    return _strict_int(value) and int(value) >= 0


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value))


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
        normalized.append({"start_ms": int(start_ms), "end_ms": int(end_ms)})
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
            and str(window.get("delivery_relation") or "").startswith("OUTSIDE_")
            for window in row_windows
        )
        and all(
            end_ms <= delivery_start or start_ms >= delivery_end
            for start_ms, end_ms in owner_windows
        )
    )


def _package_source_pieces(provenance: object) -> list[tuple[str, int, int]]:
    """(recording_basename, source_start_ms, source_end_ms) per source piece.

    When the provenance carries the final recut's absolute source interval,
    that DELIVERED window is the truth-applicability surface: a ledger entry
    landing only in the padded context (2026-07-27 1475 case — 1573's chair
    truths at 1576-1582s inside 1475's pad) cannot make the delivered text
    stale. Padded pieces remain the conservative fallback.
    """

    if not isinstance(provenance, Mapping):
        return []
    final_recut = provenance.get("final_recut")
    raw = provenance.get("source_piece")
    if isinstance(final_recut, Mapping):
        start = final_recut.get("absolute_source_start_ms")
        end = final_recut.get("absolute_source_end_ms")
        rows = raw if isinstance(raw, list) else [raw]
        first = rows[0] if rows else None
        source_path = first.get("source_path") if isinstance(first, Mapping) else None
        if (
            _strict_int(start)
            and _strict_int(end)
            and int(end) > int(start)
            and isinstance(source_path, str)
            and source_path
        ):
            return [(Path(source_path).name, int(start), int(end))]
    rows = raw if isinstance(raw, list) else [raw]
    pieces: list[tuple[str, int, int]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return []
        source_path = row.get("source_path")
        start_ms = row.get("start_ms")
        end_ms = row.get("end_ms")
        if (
            not isinstance(source_path, str)
            or not source_path
            or not _strict_int(start_ms)
            or not _strict_int(end_ms)
            or int(end_ms) <= int(start_ms)
        ):
            return []
        pieces.append((Path(source_path).name, int(start_ms), int(end_ms)))
    return pieces


def _ledger_progression_equivalent(
    truth_audit: Mapping[str, Any],
    *,
    provenance: object,
) -> bool:
    """True when the current ledger adds no truth applicable to this package.

    The frozen audit pinned the ledger bytes at production time; the ledger
    legitimately keeps growing afterwards. Byte drift only matters if a new
    (or revised) active entry overlaps this package's source interval and the
    package has never seen its truth_id — that means the final text was
    finalized without knowledge the current ledger considers applicable, so
    the package must be reproduced. Anything short of that is equivalent.
    Any parse/shape uncertainty returns False (fail toward reproduction).
    """

    pieces = _package_source_pieces(provenance)
    if not pieces:
        return False
    known: set[str] = set()
    for key in ("applied", "satisfied"):
        for row in truth_audit.get(key) or []:
            if isinstance(row, Mapping) and isinstance(row.get("truth_id"), str):
                known.add(str(row["truth_id"]))
    try:
        import json as _json

        document = _json.loads(SOURCE_TRUTH_LEDGER_PATH.read_text(encoding="utf-8"))
        entries = document.get("entries")
        aliases_raw = document.get("source_aliases") or []
        if not isinstance(entries, list) or not isinstance(aliases_raw, list):
            return False
        # basename-level alias map; the sha-bound binding check lives in the
        # producer, and matching more names here only makes this stricter.
        alias_to_canonical = {
            str(alias.get("alias_recording_basename") or ""): str(
                alias.get("canonical_recording_basename") or ""
            )
            for alias in aliases_raw
            if isinstance(alias, Mapping)
        }
        for entry in entries:
            if not isinstance(entry, Mapping):
                return False
            state = entry.get("assertion_state")
            if state is not None and str(state) in {
                "PROPOSED",
                "REJECTED",
                "SUPERSEDED",
            }:
                continue
            entry_name = str(entry.get("recording_basename") or "")
            entry_start = entry.get("source_start_ms")
            entry_end = entry.get("source_end_ms")
            if not _strict_int(entry_start) or not _strict_int(entry_end):
                return False
            for piece_name, piece_start, piece_end in pieces:
                canonical_piece = alias_to_canonical.get(piece_name, piece_name)
                if entry_name not in (piece_name, canonical_piece):
                    continue
                if (
                    max(int(entry_start), piece_start) < min(int(entry_end), piece_end)
                    and str(entry.get("truth_id") or "") not in known
                ):
                    return False
    except (OSError, ValueError, RuntimeError):
        return False
    return True


def _source_truth_audit_valid(
    truth_audit: object,
    *,
    provenance: object = None,
) -> bool:
    if not isinstance(truth_audit, Mapping):
        return False
    applied = truth_audit.get("applied")
    satisfied = truth_audit.get("satisfied")
    failures = truth_audit.get("failures")
    status = truth_audit.get("status")
    try:
        ledger_sha256 = (
            "sha256:" + hashlib.sha256(SOURCE_TRUTH_LEDGER_PATH.read_bytes()).hexdigest()
        )
    except OSError:
        return False
    ledger_current = truth_audit.get("ledger_sha256") == ledger_sha256
    if not ledger_current:
        # The ledger moved on after this package was produced. That is only
        # disqualifying when the progression added truth applicable to this
        # package that the frozen audit never saw.
        recorded = truth_audit.get("ledger_sha256")
        if not (
            isinstance(recorded, str)
            and recorded.startswith("sha256:")
            and _ledger_progression_equivalent(truth_audit, provenance=provenance)
        ):
            return False
    if not (
        truth_audit.get("schema_version") == SOURCE_TRUTH_AUDIT_SCHEMA
        and status in SOURCE_TRUTH_SUCCESS_STATUSES
        and isinstance(applied, list)
        and isinstance(satisfied, list)
        and failures == []
        and isinstance(truth_audit.get("ledger_path"), str)
        and Path(str(truth_audit["ledger_path"])).name == SOURCE_TRUTH_LEDGER_PATH.name
    ):
        return False
    if not all(
        isinstance(row, Mapping)
        and isinstance(row.get("truth_id"), str)
        and bool(row.get("truth_id"))
        and isinstance(row.get("required"), bool)
        and row.get("boundary_role") in {"story_content", "next_topic_witness"}
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
        and all(_strict_nonnegative_int(truth_owner.get(key)) for key in count_keys)
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
        elif row.get("final_owner_scope") == "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY":
            context_only_rows += 1
            context_only_truth_ids.append(str(row.get("truth_id") or ""))
            context_only_truth_evidence.append(
                {
                    "truth_id": str(row.get("truth_id") or ""),
                    "boundary_role": str(row.get("boundary_role") or ""),
                    "final_owner_scope": row.get("final_owner_scope"),
                    "final_owner_windows": row.get("final_owner_windows"),
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
        and truth_owner.get("required_truth_row_count") == final_delivery_rows
        and truth_owner.get("required_truth_ids") == required_truth_ids
        and truth_owner.get("context_only_truth_row_count") == context_only_rows
        and truth_owner.get("context_only_truth_ids") == context_only_truth_ids
        and truth_owner.get("context_only_truth_evidence") == context_only_truth_evidence
        and truth_owner.get("optional_truth_row_count") == optional_truth_row_count
        and truth_owner.get("straddling_truth_row_count") == 0
        and truth_owner.get("required_window_count") == final_delivery_windows
        and final_delivery_rows + context_only_rows == len(truth_rows)
    )


def _candidate_owner_scope(
    *,
    frozen: object,
    record: Mapping[str, object],
) -> tuple[bool, int, int, Mapping[str, object] | None]:
    owner_scope = frozen.get("owner_eligibility_scope") if isinstance(frozen, Mapping) else None
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
    required_values = [value for key, value in fields.items() if key != "given_source_end_ms"]
    if not (
        owner_scope.get("schema_version") == "candidate-boundary-owner-scope.v1"
        and isinstance(owner_scope.get("candidate_id"), str)
        and bool(str(owner_scope.get("candidate_id") or ""))
        and all(_strict_int(value) for value in required_values)
        and (fields["given_source_end_ms"] is None or _strict_int(fields["given_source_end_ms"]))
    ):
        return False, -1, -1, owner_scope
    story_start = int(fields["story_start_ms"])
    story_end = int(fields["story_end_ms"])
    semantic_end = int(fields["semantic_source_end_ms"])
    given_end = fields["given_source_end_ms"]
    semantic_story_start = int(fields["semantic_source_start_ms"]) - int(
        fields["first_piece_source_start_ms"]
    )
    lead_tolerance = owner_scope.get("lead_tolerance_ms")
    if lead_tolerance is None:
        # legacy scopes predate the lead-tolerance fields
        expected_start = semantic_story_start
    elif (
        _strict_int(lead_tolerance)
        and 0 <= int(lead_tolerance) <= BOUNDARY_OWNER_LEAD_TOLERANCE_MS
        and owner_scope.get("semantic_story_start_ms") == semantic_story_start
    ):
        expected_start = max(0, semantic_story_start - int(lead_tolerance))
    else:
        return False, -1, -1, owner_scope
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
            story_start <= window["start_ms"] and window["end_ms"] <= story_end
            for window in windows
        )
        overlaps = any(
            min(window["end_ms"], story_end) - max(window["start_ms"], story_start) > 0
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
        owner = frozen_owner_by_key.get(("source_subtitle_truth", truth_id))
        if not (
            isinstance(owner, Mapping)
            and owner.get("required") is True
            and isinstance(owner_scope, Mapping)
            and owner.get("owner_scope_sha256") == owner_scope.get("scope_sha256")
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
    qixi_terminal_projection_authority: Mapping[str, object] | None = None,
    qixi_delivery_start_ms: int | None = None,
) -> bool:
    expected: dict[tuple[str, str], list[dict[str, int]]] = {}
    valid = True
    for owner_kind, key in STORY_OWNER_GROUPS:
        rows = chat_authority.get(key) or []
        if not isinstance(rows, list):
            valid = False
            continue
        for ordinal, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                continue
            reconciliation = row.get("reconciliation")
            if reconciliation:
                # Exact-final CPA runs after the boundary owner set has been
                # frozen.  Replacing one cue's text must retire the older
                # final-surface claim, but it must not retroactively erase the
                # already-reviewed same-window boundary owner.  Accept that
                # narrow split only through the hash-bound successor ledger;
                # every other reconciliation remains excluded as before.
                if (
                    owner_kind == "entity_repair"
                    and isinstance(reconciliation, Mapping)
                    and reconciliation.get("schema_version") == "exact-final-cpa-supersession.v1"
                ):
                    valid = valid and (
                        _exact_final_superseded_boundary_owner_valid(
                            row=row,
                            row_index=ordinal - 1,
                            chat_authority=chat_authority,
                        )
                    )
                elif (
                    owner_kind == "exact_read"
                    and qixi_terminal_projection_authority is not None
                    and isinstance(qixi_delivery_start_ms, int)
                    and not isinstance(qixi_delivery_start_ms, bool)
                    and validate_terminal_baseline_replay_reconciliation(
                        row,
                        baseline_audit=chat_authority.get(
                            "redelivery_subtitle_baseline_audit"
                        ),
                        terminal_projection_authority=qixi_terminal_projection_authority,
                        delivery_start_ms=qixi_delivery_start_ms,
                    )
                ):
                    # The typed terminal replay retires the text surface but
                    # expressly preserves its already-frozen boundary owner.
                    # Continue through the ordinary owner identity/window
                    # checks below instead of silently dropping this row.
                    pass
                elif (
                    owner_kind == "exact_read"
                    and isinstance(reconciliation, Mapping)
                    and reconciliation.get("schema_version")
                    == "qixi-terminal-baseline-replay-reconciliation.v1"
                ):
                    # A Qixi-shaped receipt cannot inherit the permissive
                    # historical reconciliation path.  Without the
                    # manifest-bound terminal authority and an exact replay,
                    # it must fail rather than erase a frozen owner.
                    return False
                else:
                    continue
            if owner_kind == "entity_repair" and row.get("mode") == "exact_final_cpa_self_heal":
                valid = valid and _post_boundary_freeze_surface_owner_valid(
                    row=row,
                    row_index=ordinal - 1,
                    chat_authority=chat_authority,
                )
                continue
            if owner_kind == "exact_read" and row.get("owner_eligible") is not True:
                valid = valid and bool(
                    row.get("boundary_required") is False
                    and row.get("boundary_owner_rejection")
                    == "EXACT_READ_SUPPORT_NOT_OWNER_ELIGIBLE"
                )
                continue
            start = row.get("matched_start_ms")
            end = row.get("matched_end_ms")
            if not _strict_nonnegative_int(start) or not _strict_int(end) or int(end) <= int(start):
                valid = valid and row.get("boundary_required") is not True
                continue
            overlap_ms = min(int(end), story_end) - max(int(start), story_start)
            if overlap_ms <= 0:
                valid = valid and bool(
                    row.get("boundary_required") is False
                    and row.get("boundary_owner_rejection") == "OUTSIDE_IMMUTABLE_STORY_SCOPE"
                )
                continue
            if not story_start <= int(start) < int(end) <= story_end:
                valid = valid and bool(
                    row.get("boundary_required") is False
                    and row.get("boundary_owner_rejection") == "STRADDLES_IMMUTABLE_STORY_SCOPE"
                )
                continue
            owner_id = str(
                row.get("finding_id")
                or row.get("verdict_id")
                or f"{owner_kind}:{ordinal}:{start}:{end}"
            )
            owner_key = (owner_kind, owner_id)
            expected_windows = [{"start_ms": int(start), "end_ms": int(end)}]
            if owner_key in expected:
                valid = False
            expected[owner_key] = expected_windows
            owner = frozen_owner_by_key.get(owner_key)
            valid = valid and bool(
                row.get("boundary_required") is True
                and row.get("boundary_owner_id") == owner_id
                and isinstance(owner, Mapping)
                and owner.get("required") is True
                and _normalized_windows(owner.get("local_windows")) == expected_windows
            )
    story_kinds = {kind for kind, _ in STORY_OWNER_GROUPS}
    frozen = {
        (owner_kind, owner_id)
        for owner_kind, owner_id in frozen_owner_keys
        if owner_kind in story_kinds
    }
    return valid and set(expected) == frozen


def _exact_final_superseded_boundary_owner_valid(
    *,
    row: Mapping[str, object],
    row_index: int,
    chat_authority: Mapping[str, object],
) -> bool:
    """Keep one frozen boundary owner after an exact same-window CPA edit."""

    reconciliation = row.get("reconciliation")
    if not isinstance(reconciliation, Mapping):
        return False
    repair_sha256 = reconciliation.get("exact_final_repair_sha256")
    if not (
        reconciliation.get("schema_version") == "exact-final-cpa-supersession.v1"
        and reconciliation.get("status") == "SUPERSEDED_BY_EXACT_FINAL_CPA"
        and reconciliation.get("timing_immutable") is True
        and _is_sha256(repair_sha256)
        and reconciliation.get("before_sha256")
        and reconciliation.get("after_sha256")
    ):
        return False

    registrations = chat_authority.get("exact_final_cpa_surface_registrations")
    entity_rows = chat_authority.get("entity_repairs")
    if not isinstance(registrations, list) or not isinstance(entity_rows, list):
        return False
    matching = [
        registration
        for registration in registrations
        if isinstance(registration, Mapping)
        and registration.get("schema_version") == "exact-final-cpa-surface-registration.v1"
        and registration.get("status") == "REGISTERED"
        and registration.get("exact_final_repair_sha256") == repair_sha256
        and isinstance(registration.get("superseded_entity_repair_indexes"), list)
        and row_index in registration["superseded_entity_repair_indexes"]
    ]
    if len(matching) != 1:
        return False
    successor_index = matching[0].get("owner_entity_repair_index")
    if (
        isinstance(successor_index, bool)
        or not isinstance(successor_index, int)
        or not 0 <= successor_index < len(entity_rows)
    ):
        return False
    successor = entity_rows[successor_index]
    predecessor_text = row.get("structured_exact_text")
    if not isinstance(predecessor_text, str) or not predecessor_text:
        predecessor_after = row.get("after")
        predecessor_text = (
            predecessor_after[0]
            if isinstance(predecessor_after, list)
            and len(predecessor_after) == 1
            and isinstance(predecessor_after[0], str)
            else predecessor_after
        )
    successor_text = (
        successor.get("structured_exact_text") if isinstance(successor, Mapping) else None
    )
    successor_is_typed_drop = bool(
        isinstance(successor, Mapping)
        and successor.get("mode") == "exact_final_cpa_self_heal"
        and valid_inaudible_drop_repair(successor)
    )
    return bool(
        isinstance(successor, Mapping)
        and isinstance(predecessor_text, str)
        and predecessor_text
        and isinstance(successor_text, str)
        and (successor_text or successor_is_typed_drop)
        and reconciliation.get("before_sha256")
        == "sha256:" + hashlib.sha256(predecessor_text.encode("utf-8")).hexdigest()
        and reconciliation.get("after_sha256")
        == "sha256:" + hashlib.sha256(successor_text.encode("utf-8")).hexdigest()
        and successor.get("matched_start_ms") == row.get("matched_start_ms")
        and successor.get("matched_end_ms") == row.get("matched_end_ms")
        and successor.get("exact_final_repair_sha256") == repair_sha256
        and _post_boundary_freeze_surface_owner_valid(
            row=successor,
            row_index=successor_index,
            chat_authority=chat_authority,
        )
    )


def _post_boundary_freeze_surface_owner_valid(
    *,
    row: Mapping[str, object],
    row_index: int,
    chat_authority: Mapping[str, object],
) -> bool:
    """Verify an exact-final surface owner without reopening boundary owners.

    Exact-final CPA self-heal runs after the boundary owner set is frozen.  Its
    repairs must own final text, but retroactively adding them to the frozen
    boundary contract would invalidate the already-reviewed boundary geometry.
    Accept the typed exclusion only when the registration ledger and immutable
    self-heal receipt independently bind the exact repair hash.
    """

    repair_sha256 = row.get("exact_final_repair_sha256")
    mutation = row.get("mutation_authority")
    witness = row.get("acoustic_witness")
    inaudible_nonempty = bool(
        row.get("structured_exact_text")
        and isinstance(witness, Mapping)
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is False
    )
    if not (
        row.get("decision_authority") == "CPA_JUDGE"
        and row.get("timing_immutable") is True
        and row.get("boundary_required") is False
        and row.get("boundary_owner_rejection") == "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"
        and _is_sha256(repair_sha256)
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version") == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and (not inaudible_nonempty or valid_inaudible_override_repair(row))
    ):
        return False

    registrations = chat_authority.get("exact_final_cpa_surface_registrations")
    if not isinstance(registrations, list):
        return False
    matching_registrations = [
        registration
        for registration in registrations
        if isinstance(registration, Mapping)
        and registration.get("schema_version") == "exact-final-cpa-surface-registration.v1"
        and registration.get("status") == "REGISTERED"
        and registration.get("owner_entity_repair_index") == row_index
        and registration.get("exact_final_repair_sha256") == repair_sha256
    ]
    if len(matching_registrations) != 1:
        return False

    self_heal = chat_authority.get("exact_final_cpa_self_heal")
    passes = self_heal.get("passes") if isinstance(self_heal, Mapping) else None
    if not (
        isinstance(self_heal, Mapping)
        and self_heal.get("schema_version") == "exact-final-cpa-self-heal-audit.v1"
        and self_heal.get("status") == "PASS"
        and isinstance(passes, list)
    ):
        return False
    matching_receipts = []
    for pass_row in passes:
        repairs = pass_row.get("repairs") if isinstance(pass_row, Mapping) else None
        if not isinstance(repairs, list):
            return False
        for repair in repairs:
            if not isinstance(repair, Mapping):
                return False
            digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        repair,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            if digest == repair_sha256:
                matching_receipts.append(repair)
    return len(matching_receipts) == 1


def _retry_verification_valid(
    *,
    frozen: Mapping[str, object],
    owner_scope: Mapping[str, object] | None,
) -> bool:
    receipt = frozen.get("boundary_retry_owner_contract_verification")
    if receipt is None:
        return True
    if not (
        isinstance(receipt, Mapping)
        and receipt.get("status") == "PASS"
        and _is_sha256(receipt.get("expected_contract_sha256"))
        and isinstance(owner_scope, Mapping)
        and receipt.get("owner_eligibility_scope_sha256") == owner_scope.get("scope_sha256")
    ):
        return False
    if frozen.get("deterministic_owner_set_sha256") is None:
        # Legacy receipt from before ASR-derived owners were unbound across
        # attempts: it asserted whole-set identity, which is strictly stronger
        # than today's rule.  Historical packages stay auditable as-is.
        return receipt.get("owner_set_sha256") == frozen.get("owner_set_sha256")
    return bool(
        receipt.get("deterministic_owner_set_sha256")
        == frozen.get("deterministic_owner_set_sha256")
        and receipt.get("retry_owner_set_sha256") == frozen.get("owner_set_sha256")
        and receipt.get("asr_derived_owner_binding") == "per_attempt"
    )


def _frozen_owner_contract_valid(
    *,
    frozen: object,
    truth_rows: list[dict[str, Any]],
    chat_authority: Mapping[str, object],
    record: Mapping[str, object],
    qixi_terminal_projection_authority: Mapping[str, object] | None = None,
) -> tuple[bool, list[object] | None]:
    owners = frozen.get("owners") if isinstance(frozen, Mapping) else None
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
    scope_valid, story_start, story_end, owner_scope = _candidate_owner_scope(
        frozen=frozen, record=record
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
    boundary_audit = record.get("boundary_audit")
    delivery_start = (
        boundary_audit.get("final_start_ms")
        if isinstance(boundary_audit, Mapping)
        else None
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
                qixi_terminal_projection_authority=qixi_terminal_projection_authority,
                qixi_delivery_start_ms=delivery_start,
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
    owner_verification = boundary_audit.get("required_boundary_owner_verification")
    coverage_verification = boundary_audit.get("delivery_coverage_verification")
    return bool(
        boundary_audit.get("frozen_required_boundary_owner_count") == len(frozen_owners or [])
        and boundary_audit.get("frozen_required_boundary_owners") == frozen_owners
        and isinstance(owner_verification, Mapping)
        and owner_verification.get("status") == "PASS"
        and owner_verification.get("failures") == []
        and isinstance(coverage_verification, Mapping)
        and coverage_verification.get("status") == "PASS"
        and coverage_verification.get("failure") is None
        and _strict_nonnegative_int(
            chat_authority.get("final_boundary_required_exclusion_count", 0)
        )
        and chat_authority.get("final_boundary_required_exclusion_count", 0) == 0
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
    if not all(_strict_nonnegative_int(chat_authority.get(key)) for key in keys):
        return False
    truth_owner = chat_authority.get("final_source_truth_owner_verification")
    source_count = (
        truth_owner.get("required_window_count") if isinstance(truth_owner, Mapping) else None
    )
    baseline_count = (
        baseline_owner.get("required_mapping_count")
        if isinstance(baseline_audit, Mapping)
        and baseline_audit.get("status") in {"APPLIED", "ALREADY_SATISFIED"}
        and isinstance(baseline_owner, Mapping)
        else 0
    )
    legacy_count = int(chat_authority["final_required_legacy_decision_count"])
    return bool(
        _strict_nonnegative_int(source_count)
        and _strict_nonnegative_int(baseline_count)
        and chat_authority.get("final_required_source_truth_owner_count") == source_count
        and chat_authority.get("final_required_redelivery_baseline_owner_count") == baseline_count
        and chat_authority.get("final_required_decision_count")
        == legacy_count + int(source_count) + int(baseline_count)
    )


def _terminal_projection_materialization_valid(
    *,
    frozen: object,
    baseline_audit: object,
    source_review: object,
) -> bool:
    exact_raw = (
        frozen.get(EXACT_INTERVAL_FROZEN_CONTRACT_KEY) if isinstance(frozen, Mapping) else None
    )
    if exact_raw is not None:
        # This is the mutually exclusive P0 authority branch.  Its package
        # materialization is checked directly; an ordinary terminal
        # projection appearing beside it is ambiguity and therefore invalid.
        if isinstance(frozen, Mapping) and frozen.get(AUTHORITY_CONFIG_KEY) is not None:
            return False
        try:
            exact = validate_exact_interval_authority(exact_raw)
        except ReviewedExactSourceIntervalError:
            return False
        try:
            from src.autoslice.reviewed_subtitle_baseline_registry import (
                ReviewedSubtitleBaselineRegistryError,
                load_candidate_reviewed_subtitle_baseline,
            )

            profile = _load_channel_profile(Path(__file__).resolve().parents[2])
            registered = load_candidate_reviewed_subtitle_baseline(
                profile.asset_directory("reviewed_subtitle_baselines"),
                str(exact.get("candidate_id") or ""),
                repo_root=Path(__file__).resolve().parents[2],
            )
        except ReviewedSubtitleBaselineRegistryError:
            return False
        if (
            registered is None
            or registered.exact_interval_authority is None
            or exact != registered.exact_interval_authority
        ):
            return False
        source = exact["source"]
        subtitle = exact["reviewed_subtitle"]
        terminal = exact["terminal"]
        exact_source_review = (
            source_review.get(EXACT_INTERVAL_RUNTIME_CONFIG_KEY)
            if isinstance(source_review, Mapping)
            else None
        )
        binding = (
            source_review.get("reviewed_exact_source_interval_binding")
            if isinstance(source_review, Mapping)
            else None
        )
        endpoint = (
            source_review.get("final_endpoint_binding")
            if isinstance(source_review, Mapping)
            else None
        )
        source_identity = (
            baseline_audit.get("source_recording_identity")
            if isinstance(baseline_audit, Mapping)
            else None
        )
        current_interval = (
            baseline_audit.get("current_source_interval")
            if isinstance(baseline_audit, Mapping)
            else None
        )
        reviewed_coverage = (
            baseline_audit.get("reviewed_coverage") if isinstance(baseline_audit, Mapping) else None
        )
        source_sha = str(source["source_sha256"]).removeprefix("sha256:")
        srt_sha = str(subtitle["srt_sha256"]).removeprefix("sha256:")
        return bool(
            exact_source_review == exact
            and isinstance(binding, Mapping)
            and binding.get("authority_sha256") == exact["authority_sha256"]
            and binding.get("fresh_asr_role") == "diagnostic_witness_only"
            and isinstance(baseline_audit, Mapping)
            and baseline_audit.get("status") in {"APPLIED", "ALREADY_SATISFIED"}
            and baseline_audit.get("application_strategy") == "exact_reviewed_interval_replay"
            and baseline_audit.get("baseline_sha256") == srt_sha
            and baseline_audit.get("expected_baseline_sha256") == srt_sha
            and baseline_audit.get("video_tail_extension_ms", 0) == 0
            and isinstance(source_identity, Mapping)
            and source_identity.get("expected_basename") == source["recording_basename"]
            and source_identity.get("current_basename") == source["recording_basename"]
            and source_identity.get("expected_sha256") == source_sha
            and source_identity.get("current_sha256") == source_sha
            and isinstance(current_interval, Mapping)
            and current_interval.get("absolute_source_start_ms")
            == source["absolute_source_start_ms"]
            and current_interval.get("absolute_source_end_ms") == source["absolute_source_end_ms"]
            and isinstance(reviewed_coverage, Mapping)
            and reviewed_coverage == current_interval
            and source_review.get("candidate_id") == exact["candidate_id"]
            and source_review.get("status") == "PASS"
            and isinstance(endpoint, Mapping)
            and _strict_nonnegative_int(endpoint.get("final_start_ms"))
            and _strict_nonnegative_int(endpoint.get("final_end_ms"))
            and int(endpoint["final_end_ms"]) - int(endpoint["final_start_ms"])
            == terminal["media_end_anchor_ms"]
        )
    authority_raw = frozen.get(AUTHORITY_CONFIG_KEY) if isinstance(frozen, Mapping) else None
    frozen_scope = frozen.get("boundary_search_scope") if isinstance(frozen, Mapping) else None
    projection_scope = (
        frozen_scope.get("reviewed_exact_interval_projection")
        if isinstance(frozen_scope, Mapping)
        else None
    )
    selected = (
        source_review.get("selected_terminal_projection_binding")
        if isinstance(source_review, Mapping)
        else None
    )
    source_scope = (
        source_review.get("boundary_search_scope") if isinstance(source_review, Mapping) else None
    )
    if authority_raw is None and projection_scope is None and selected is None:
        return True
    if authority_raw is None or projection_scope is None:
        return False
    try:
        authority = validate_terminal_projection_authority(authority_raw)
        expected_scope = projection_scope_binding(authority)
    except RedeliveryBoundaryProjectionError:
        return False
    owner_scope = frozen.get("owner_eligibility_scope") if isinstance(frozen, Mapping) else None
    source_identity = (
        baseline_audit.get("source_recording_identity")
        if isinstance(baseline_audit, Mapping)
        else None
    )
    reviewed_coverage = (
        baseline_audit.get("reviewed_coverage") if isinstance(baseline_audit, Mapping) else None
    )
    current_interval = (
        baseline_audit.get("current_source_interval")
        if isinstance(baseline_audit, Mapping)
        else None
    )
    source_sha256 = str(authority["source_sha256"]).removeprefix("sha256:")
    baseline_sha256 = str(authority["baseline_srt_sha256"]).removeprefix("sha256:")
    tail_ms = (
        baseline_audit.get("video_tail_extension_ms")
        if isinstance(baseline_audit, Mapping)
        else None
    )
    common_valid = bool(
        isinstance(baseline_audit, Mapping)
        and baseline_audit.get("status") in {"APPLIED", "ALREADY_SATISFIED"}
        and baseline_audit.get("application_strategy") == "exact_reviewed_interval_replay"
        and _strict_nonnegative_int(tail_ms)
        and tail_ms <= 400
        and baseline_audit.get("baseline_sha256") == baseline_sha256
        and baseline_audit.get("expected_baseline_sha256") == baseline_sha256
        and baseline_audit.get("authority") == authority["authority"]
        and isinstance(source_identity, Mapping)
        and source_identity.get("expected_basename") == authority["source_recording_basename"]
        and source_identity.get("current_basename") == authority["source_recording_basename"]
        and source_identity.get("expected_sha256") == source_sha256
        and source_identity.get("current_sha256") == source_sha256
        and isinstance(reviewed_coverage, Mapping)
        and reviewed_coverage.get("absolute_source_start_ms")
        == authority["absolute_source_start_ms"]
        and reviewed_coverage.get("absolute_source_end_ms") == authority["absolute_source_end_ms"]
        and isinstance(current_interval, Mapping)
        and current_interval.get("absolute_source_start_ms")
        == authority["absolute_source_start_ms"]
        and current_interval.get("absolute_source_end_ms")
        == authority["absolute_source_end_ms"] + tail_ms
        and isinstance(owner_scope, Mapping)
        and owner_scope.get("candidate_id") == authority["candidate_id"]
        and isinstance(source_review, Mapping)
        and source_review.get("candidate_id") == authority["candidate_id"]
        and boundary_search_scope_is_valid(frozen_scope)
        and boundary_search_scope_is_valid(source_scope)
        and source_scope == frozen_scope
    )
    if not common_valid:
        return False
    if selected is None:
        return bool(
            projection_scope == expected_scope
            and isinstance(source_scope, Mapping)
            and source_scope.get("reviewed_exact_interval_projection") == expected_scope
            and (baseline_audit.get("terminal_projection_materialization") is None)
        )
    if not isinstance(baseline_audit, Mapping):
        return False
    receipt = baseline_audit.get("terminal_projection_materialization")
    return bool(
        projection_scope == expected_scope
        and isinstance(source_scope, Mapping)
        and source_scope.get("reviewed_exact_interval_projection") == expected_scope
        and isinstance(receipt, Mapping)
        and receipt.get("schema_version") == MATERIALIZATION_SCHEMA_VERSION
        and receipt.get("status") == "PASS"
        and receipt.get("authority_sha256") == authority["authority_sha256"]
        and receipt.get("application_strategy") == "exact_reviewed_interval_replay"
        and receipt.get("absolute_source_start_ms") == authority["absolute_source_start_ms"]
        and receipt.get("absolute_source_end_ms") == authority["absolute_source_end_ms"]
        and receipt.get("video_tail_extension_ms") == 0
        and baseline_audit.get("video_tail_extension_ms") == 0
        and current_interval == reviewed_coverage
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
    provenance: dict[str, Any] | None = None,
    qixi_terminal_projection_authority: Mapping[str, object] | None = None,
) -> None:
    """Audit final text ownership separately from boundary ownership."""

    truth_audit_raw = chat_authority.get("source_subtitle_truth_audit")
    truth_audit = truth_audit_raw if isinstance(truth_audit_raw, dict) else {}
    if not _source_truth_audit_valid(truth_audit_raw, provenance=provenance):
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
        truth_owner=chat_authority.get("final_source_truth_owner_verification"),
        truth_rows=truth_rows,
        optional_truth_row_count=len(optional_rows),
    ):
        issue_adder(
            issues,
            "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING",
            stem=stem,
            path=chat_authority_path,
        )

    baseline_audit = chat_authority.get("redelivery_subtitle_baseline_audit")
    baseline_owner = chat_authority.get("final_redelivery_baseline_owner_verification")
    if (
        isinstance(baseline_audit, dict)
        and baseline_audit.get("status") in {"APPLIED", "ALREADY_SATISFIED"}
        and (
            not isinstance(baseline_owner, dict)
            or baseline_owner.get("status") != "PASS"
            or int(baseline_owner.get("required_mapping_count") or 0) <= 0
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
    boundary_audit = record.get("boundary_audit")
    source_review = (
        boundary_audit.get("boundary_semantic_review")
        if isinstance(boundary_audit, Mapping)
        else None
    )
    if not _terminal_projection_materialization_valid(
        frozen=frozen,
        baseline_audit=baseline_audit,
        source_review=source_review,
    ):
        issue_adder(
            issues,
            "REDELIVERY_TERMINAL_PROJECTION_MATERIALIZATION_INVALID",
            stem=stem,
            path=chat_authority_path,
        )
    frozen_valid, frozen_owners = _frozen_owner_contract_valid(
        frozen=frozen,
        truth_rows=truth_rows,
        chat_authority=chat_authority,
        record=record,
        qixi_terminal_projection_authority=qixi_terminal_projection_authority,
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
