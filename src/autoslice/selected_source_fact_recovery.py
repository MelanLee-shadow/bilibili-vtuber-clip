"""Strict v6 recovery seam for one selected source-fact rejection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from src.autoslice.published_topic_recovery_lineage import RECOVERY_REBOUND_FIELDS
from src.autoslice.runner_proxy import RunnerProxy


RECEIPT_SCHEMA = "selected-source-fact-recovery-receipt.v1"
RECOVERY_RECEIPT_FIELD = "selected_source_fact_recovery"
READY_TO_REQUEUE = "READY_TO_REQUEUE"
OUTSTANDING = "OUTSTANDING"
CONVERGED = "CONVERGED"
BLOCKED = "BLOCKED"

_ACTION = "REQUEUED_BY_OPERATOR_SOURCE_FACT_GRANT"
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REJECTION_REASON = "story_contract_unresolved_backfilled"
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "action",
        "candidate_id",
        "operator_scope_grant_id",
        "old_row",
        "old_row_sha256",
        "initial_queue_row",
        "initial_queue_row_sha256",
        "current_row",
        "current_row_sha256",
        "transitions",
        "recorded_failure_recovery_fingerprint",
        "current_failure_recovery_fingerprint",
        "receipt_sha256",
    }
)
_TRANSITION_FIELDS = frozenset(
    {"kind", "from_row_sha256", "to_row_sha256"}
)
_QUEUE_REBOUND_FIELDS = frozenset().union(*RECOVERY_REBOUND_FIELDS.values())
_INITIAL_TRANSITION = "REJECTION_TO_QUEUE"
_QUEUE_TO_PICK_TRANSITION = "QUEUE_TO_PICK"
_PICK_TO_QUEUE_TRANSITION = "PICK_TO_QUEUE"
QUEUE_TO_PICK_TRANSITION = _QUEUE_TO_PICK_TRANSITION
PICK_TO_QUEUE_TRANSITION = _PICK_TO_QUEUE_TRANSITION
_ACTIVE_TALK_COLLECTIONS = (
    "pending_talk",
    "talk_backlog",
    "picks",
    "talk_below_confidence_threshold",
)
_QUEUED_TALK_COLLECTIONS = ("pending_talk", "talk_backlog")
_SONG_COLLECTIONS = (
    "pending_song",
    "song_backlog",
    "song_selection_backlog",
    "songs",
    "song_superseded_attempts",
)
_SUCCESS_TERMINAL_STATUSES = frozenset({"ok", "review_ready", "quarantine", "published"})


@dataclass(frozen=True, slots=True)
class SelectedSourceFactRecoveryInspection:
    outcome: str
    reason_code: str


def _candidate_id(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("candidate_id") or row.get("cid") or "").strip()


def _canonical_copy(value: object) -> object:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RX.fullmatch(value) is not None


def is_selected_source_fact_rejection(row: Mapping[str, object]) -> bool:
    """Recognize only the selected 221-style source-fact rejection."""

    rc = row.get("rc")
    return bool(
        row.get("status") == "candidate_rejected"
        and row.get("rejected_status") == "failed"
        and isinstance(rc, int)
        and not isinstance(rc, bool)
        and rc == 1
        and row.get("selected_repair") is True
        and row.get("failure_kind") == "story_contract"
        and row.get("failure_stage") == "source_fact_repair"
        and row.get("failure_recoverable") is False
        and row.get("rejection_reason") == _REJECTION_REASON
    )


def _row_body(row: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in row.items()
        if key != RECOVERY_RECEIPT_FIELD
    }


def _changed_fields(
    before: Mapping[str, object], after: Mapping[str, object]
) -> set[str]:
    return {
        str(key)
        for key in set(before) | set(after)
        if key not in before or key not in after or before[key] != after[key]
    }


def _is_queue_row(row: Mapping[str, object], candidate_id: str) -> bool:
    body = _row_body(row)
    return bool(
        _candidate_id(body) == candidate_id
        and body.get("selected_repair") is True
        and "status" not in body
    )


def _is_pick_row(row: Mapping[str, object], candidate_id: str) -> bool:
    body = _row_body(row)
    return bool(
        _candidate_id(body) == candidate_id
        and body.get("selected_repair") is True
        and isinstance(body.get("status"), str)
        and str(body.get("status")).strip()
    )


def build_selected_source_fact_recovery_receipt(
    *,
    old_row: Mapping[str, object],
    queued_row: Mapping[str, object],
    candidate_id: str,
    grant_id: str,
    current_fingerprint: str,
) -> dict[str, object]:
    """Seal the exact old rejection and its one resulting queue row."""

    if not (
        isinstance(candidate_id, str)
        and candidate_id.strip() == candidate_id
        and candidate_id
        and isinstance(grant_id, str)
        and grant_id.strip() == grant_id
        and grant_id
        and _candidate_id(old_row) == candidate_id
        and is_selected_source_fact_rejection(old_row)
        and _is_queue_row(queued_row, candidate_id)
        and RECOVERY_RECEIPT_FIELD not in queued_row
    ):
        raise ValueError("selected source-fact recovery receipt input is invalid")
    recorded = old_row.get("failure_recovery_fingerprint")
    if not (
        _valid_sha256(recorded)
        and _valid_sha256(current_fingerprint)
        and recorded != current_fingerprint
    ):
        raise ValueError("selected source-fact recovery fingerprints are invalid")
    try:
        canonical_old = _canonical_copy(dict(old_row))
        queued_body = _canonical_copy(_row_body(queued_row))
    except (TypeError, ValueError) as exc:
        raise ValueError("selected source-fact recovery rows are not canonical JSON") from exc
    if not isinstance(canonical_old, dict) or not isinstance(queued_body, dict):
        raise ValueError("selected source-fact recovery rows are invalid")
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "action": _ACTION,
        "candidate_id": candidate_id,
        "operator_scope_grant_id": grant_id,
        "old_row": canonical_old,
        "old_row_sha256": _sha256_json(canonical_old),
        "initial_queue_row": queued_body,
        "initial_queue_row_sha256": _sha256_json(queued_body),
        "current_row": queued_body,
        "current_row_sha256": _sha256_json(queued_body),
        "transitions": [
            {
                "kind": _INITIAL_TRANSITION,
                "from_row_sha256": _sha256_json(canonical_old),
                "to_row_sha256": _sha256_json(queued_body),
            }
        ],
        "recorded_failure_recovery_fingerprint": recorded,
        "current_failure_recovery_fingerprint": current_fingerprint,
    }
    receipt["receipt_sha256"] = _sha256_json(receipt)
    return receipt


def _receipt_is_self_valid(
    receipt: object,
    *,
    candidate_id: str,
    grant_id: str,
    current_fingerprint: str,
) -> bool:
    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
        return False
    body = {
        str(key): value
        for key, value in receipt.items()
        if key != "receipt_sha256"
    }
    old_row = receipt.get("old_row")
    initial_queue = receipt.get("initial_queue_row")
    current_row = receipt.get("current_row")
    transitions = receipt.get("transitions")
    recorded = receipt.get("recorded_failure_recovery_fingerprint")
    try:
        old_row_sha256 = _sha256_json(old_row)
        initial_queue_sha256 = _sha256_json(initial_queue)
        current_row_sha256 = _sha256_json(current_row)
        receipt_sha256 = _sha256_json(body)
    except (TypeError, ValueError):
        return False
    if not (
        receipt.get("schema_version") == RECEIPT_SCHEMA
        and receipt.get("action") == _ACTION
        and receipt.get("candidate_id") == candidate_id
        and receipt.get("operator_scope_grant_id") == grant_id
        and isinstance(old_row, Mapping)
        and isinstance(initial_queue, Mapping)
        and isinstance(current_row, Mapping)
        and _candidate_id(old_row) == candidate_id
        and is_selected_source_fact_rejection(old_row)
        and _is_queue_row(initial_queue, candidate_id)
        and _candidate_id(current_row) == candidate_id
        and current_row.get("selected_repair") is True
        and receipt.get("old_row_sha256") == old_row_sha256
        and receipt.get("initial_queue_row_sha256") == initial_queue_sha256
        and receipt.get("current_row_sha256") == current_row_sha256
        and recorded == old_row.get("failure_recovery_fingerprint")
        and _valid_sha256(recorded)
        and receipt.get("current_failure_recovery_fingerprint") == current_fingerprint
        and _valid_sha256(current_fingerprint)
        and recorded != current_fingerprint
        and receipt.get("receipt_sha256") == receipt_sha256
        and isinstance(transitions, list)
        and transitions
    ):
        return False
    previous = old_row_sha256
    for index, transition in enumerate(transitions):
        expected_kind = (
            _INITIAL_TRANSITION
            if index == 0
            else _QUEUE_TO_PICK_TRANSITION
            if index % 2 == 1
            else _PICK_TO_QUEUE_TRANSITION
        )
        if not (
            isinstance(transition, Mapping)
            and set(transition) == _TRANSITION_FIELDS
            and transition.get("kind") == expected_kind
            and transition.get("from_row_sha256") == previous
            and _valid_sha256(transition.get("to_row_sha256"))
        ):
            return False
        previous = str(transition["to_row_sha256"])
    head_kind = transitions[-1].get("kind")
    head_shape_valid = (
        _is_pick_row(current_row, candidate_id)
        if head_kind == _QUEUE_TO_PICK_TRANSITION
        else _is_queue_row(current_row, candidate_id)
    )
    return bool(
        transitions[0].get("to_row_sha256") == initial_queue_sha256
        and previous == current_row_sha256
        and head_shape_valid
    )


def validate_selected_source_fact_recovery_receipt(
    receipt: object,
    *,
    queued_row: Mapping[str, object],
    candidate_id: str,
    grant_id: str,
    current_fingerprint: str,
    allow_queue_rebound: bool = False,
) -> bool:
    """Validate the current v6 row without accepting auxiliary truth."""

    if not _receipt_is_self_valid(
        receipt,
        candidate_id=candidate_id,
        grant_id=grant_id,
        current_fingerprint=current_fingerprint,
    ):
        return False
    assert isinstance(receipt, Mapping)
    current_row = receipt.get("current_row")
    transitions = receipt.get("transitions")
    try:
        actual = _canonical_copy(_row_body(queued_row))
    except (TypeError, ValueError):
        return False
    if not (
        isinstance(current_row, Mapping)
        and isinstance(actual, Mapping)
        and queued_row.get(RECOVERY_RECEIPT_FIELD) == receipt
    ):
        return False
    if current_row == actual:
        return True
    return bool(
        allow_queue_rebound
        and isinstance(transitions, list)
        and transitions
        and isinstance(transitions[-1], Mapping)
        and transitions[-1].get("kind") != _QUEUE_TO_PICK_TRANSITION
        and _changed_fields(current_row, actual).issubset(_QUEUE_REBOUND_FIELDS)
    )


def validate_consumed_source_fact_recovery_receipt(
    receipt: object,
    *,
    candidate_id: str,
    grant_id: str,
    consumed_row: Mapping[str, object] | None = None,
) -> bool:
    """Validate a pick-headed receipt without consulting mutable runtime code."""

    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
        return False
    current_row = receipt.get("current_row")
    transitions = receipt.get("transitions")
    if not (
        isinstance(current_row, Mapping)
        and isinstance(transitions, list)
        and transitions
        and isinstance(transitions[-1], Mapping)
        and transitions[-1].get("kind") == _QUEUE_TO_PICK_TRANSITION
    ):
        return False
    rebound = dict(current_row)
    rebound[RECOVERY_RECEIPT_FIELD] = receipt
    declared_current = receipt.get("current_failure_recovery_fingerprint")
    if not (
        _valid_sha256(declared_current)
        and validate_selected_source_fact_recovery_receipt(
            receipt,
            queued_row=rebound,
            candidate_id=candidate_id,
            grant_id=grant_id,
            current_fingerprint=str(declared_current),
        )
    ):
        return False
    if consumed_row is None:
        return True
    try:
        actual = _canonical_copy(_row_body(consumed_row))
    except (TypeError, ValueError):
        return False
    return bool(
        actual == current_row
        and consumed_row.get(RECOVERY_RECEIPT_FIELD) == receipt
    )


def advance_selected_source_fact_recovery_receipt(
    receipt: object,
    *,
    from_row: Mapping[str, object],
    to_row: Mapping[str, object],
    candidate_id: str,
    grant_id: str,
    transition_kind: str,
    allow_queue_rebound: bool = False,
) -> dict[str, object]:
    """Advance the exact queue/pick lineage and return a new self-seal."""

    if not isinstance(receipt, Mapping):
        raise ValueError("selected source-fact recovery receipt is missing")
    declared_current = receipt.get("current_failure_recovery_fingerprint")
    transitions = receipt.get("transitions")
    expected_kind = (
        _QUEUE_TO_PICK_TRANSITION
        if isinstance(transitions, list) and len(transitions) % 2 == 1
        else _PICK_TO_QUEUE_TRANSITION
    )
    from_shape_valid = (
        _is_queue_row(from_row, candidate_id)
        if expected_kind == _QUEUE_TO_PICK_TRANSITION
        else _is_pick_row(from_row, candidate_id)
    )
    to_shape_valid = (
        _is_pick_row(to_row, candidate_id)
        if expected_kind == _QUEUE_TO_PICK_TRANSITION
        else _is_queue_row(to_row, candidate_id)
    )
    if not (
        _valid_sha256(declared_current)
        and validate_selected_source_fact_recovery_receipt(
            receipt,
            queued_row=from_row,
            candidate_id=candidate_id,
            grant_id=grant_id,
            current_fingerprint=str(declared_current),
            allow_queue_rebound=(
                allow_queue_rebound
                and expected_kind == _QUEUE_TO_PICK_TRANSITION
            ),
        )
        and transition_kind == expected_kind
        and from_shape_valid
        and to_shape_valid
        and RECOVERY_RECEIPT_FIELD not in to_row
    ):
        raise ValueError("selected source-fact recovery transition is invalid")
    if not isinstance(transitions, list):
        raise ValueError("selected source-fact recovery transition is malformed")
    try:
        canonical_to = _canonical_copy(_row_body(to_row))
        next_receipt = _canonical_copy(dict(receipt))
    except (TypeError, ValueError) as exc:
        raise ValueError("selected source-fact recovery transition is not JSON") from exc
    if not isinstance(canonical_to, dict) or not isinstance(next_receipt, dict):
        raise ValueError("selected source-fact recovery transition is invalid")
    previous_sha = str(receipt.get("current_row_sha256") or "")
    next_sha = _sha256_json(canonical_to)
    next_receipt["current_row"] = canonical_to
    next_receipt["current_row_sha256"] = next_sha
    next_receipt["transitions"] = [
        *next_receipt["transitions"],
        {
            "kind": transition_kind,
            "from_row_sha256": previous_sha,
            "to_row_sha256": next_sha,
        },
    ]
    next_receipt["receipt_sha256"] = _sha256_json(
        {
            key: value
            for key, value in next_receipt.items()
            if key != "receipt_sha256"
        }
    )
    return next_receipt


def _target_rows(
    state: Mapping[str, object], collections: tuple[str, ...], candidate_id: str
) -> list[tuple[str, Mapping[str, object]]]:
    return [
        (collection, row)
        for collection in collections
        for row in (
            state.get(collection) if isinstance(state.get(collection), list) else []
        )
        if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
    ]


def _current_fingerprint(candidate_id: str) -> str | None:
    try:
        current = RunnerProxy().talk_failure_recovery_fingerprint(
            "story_contract", candidate_id
        )
    except Exception:  # noqa: BLE001 - recovery authority must fail closed
        return None
    return current if _valid_sha256(current) else None


def inspect_selected_source_fact_recovery(
    state: Mapping[str, object],
    *,
    candidate_id: str,
    grant_id: str,
) -> SelectedSourceFactRecoveryInspection:
    """Inspect initial or receipt-sealed queued v6 state without mutation."""

    blocked = SelectedSourceFactRecoveryInspection(
        BLOCKED, "SELECTED_SOURCE_FACT_RECOVERY_BLOCKED"
    )
    if not candidate_id or not grant_id:
        return blocked
    if _target_rows(state, _SONG_COLLECTIONS, candidate_id):
        return blocked
    target_rows = _target_rows(state, _ACTIVE_TALK_COLLECTIONS, candidate_id)
    if len(target_rows) != 1:
        return blocked
    collection, row = target_rows[0]
    if collection in _QUEUED_TALK_COLLECTIONS:
        receipt = row.get(RECOVERY_RECEIPT_FIELD)
        declared_current = (
            receipt.get("current_failure_recovery_fingerprint")
            if isinstance(receipt, Mapping)
            else None
        )
        if not _valid_sha256(
            declared_current
        ) or not validate_selected_source_fact_recovery_receipt(
            receipt,
            queued_row=row,
            candidate_id=candidate_id,
            grant_id=grant_id,
            current_fingerprint=declared_current,
            allow_queue_rebound=True,
        ):
            return blocked
        current = _current_fingerprint(candidate_id)
        if current is None or current != declared_current:
            return blocked
        return SelectedSourceFactRecoveryInspection(
            OUTSTANDING, "SELECTED_SOURCE_FACT_RECOVERY_QUEUE_OUTSTANDING"
        )
    if collection != "picks":
        return blocked
    receipt = row.get(RECOVERY_RECEIPT_FIELD)
    if receipt is not None:
        if not validate_consumed_source_fact_recovery_receipt(
            receipt,
            candidate_id=candidate_id,
            grant_id=grant_id,
            consumed_row=row,
        ):
            return blocked
        if row.get("status") in _SUCCESS_TERMINAL_STATUSES:
            return SelectedSourceFactRecoveryInspection(
                CONVERGED, "SELECTED_SOURCE_FACT_RECOVERY_TERMINAL"
            )
        if row.get("status") == "media_ready_cover_pending":
            return SelectedSourceFactRecoveryInspection(
                OUTSTANDING,
                "SELECTED_SOURCE_FACT_RECOVERY_COVER_PENDING",
            )
        if is_selected_source_fact_rejection(row):
            return SelectedSourceFactRecoveryInspection(
                CONVERGED, "SELECTED_SOURCE_FACT_RECOVERY_ATTEMPT_CONSUMED"
            )
        if (
            row.get("status")
            in {
                "failed",
                "boundary_unrepairable",
                "speaker_review_required",
                "speaker_evidence_insufficient",
            }
            and row.get("failure_recoverable") is True
        ):
            return SelectedSourceFactRecoveryInspection(
                OUTSTANDING,
                "SELECTED_SOURCE_FACT_RECOVERY_ATTEMPT_RETRY_PENDING",
            )
        if (
            row.get("status") == "candidate_rejected"
            or row.get("failure_recoverable") is False
        ):
            return SelectedSourceFactRecoveryInspection(
                CONVERGED, "SELECTED_SOURCE_FACT_RECOVERY_ATTEMPT_TERMINAL"
            )
        return blocked
    if not is_selected_source_fact_rejection(row):
        return blocked
    recorded = row.get("failure_recovery_fingerprint")
    if not _valid_sha256(recorded):
        return blocked
    current = _current_fingerprint(candidate_id)
    if current is None:
        return blocked
    if current == recorded:
        return SelectedSourceFactRecoveryInspection(
            CONVERGED, "SELECTED_SOURCE_FACT_RECOVERY_FINGERPRINT_UNCHANGED"
        )
    return SelectedSourceFactRecoveryInspection(
        READY_TO_REQUEUE, "SELECTED_SOURCE_FACT_RECOVERY_FINGERPRINT_CHANGED"
    )
