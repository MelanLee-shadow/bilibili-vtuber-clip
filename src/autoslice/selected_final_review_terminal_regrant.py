"""One-shot v8 regrant for a consumed v7 terminal subtitle rejection.

The v7 receipt and its terminal v5 handoff remain immutable parent authority.
This module adds a parallel receipt for exactly one new producer dispatch; it
never renews, replaces, or advances the parent v7 lineage.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.autoslice.final_review_provider_budget_retry import (
    LEDGER_FIELD as PROVIDER_BUDGET_LEDGER_FIELD,
    LEDGER_STATE_ABSENT,
    resolve_provider_budget_retry_ledger_history,
)
from src.autoslice.published_topic_recovery_lineage import RECOVERY_REBOUND_FIELDS
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.selected_final_review_recovery import (
    RECOVERY_RECEIPT_FIELD as PARENT_RECOVERY_RECEIPT_FIELD,
    validate_consumed_final_review_recovery_receipt,
)


GRANT_SCHEMA = "operator-processing-scope-grant.v8"
GRANT_INTENT = "REGRANT_NAMED_SELECTED_FINAL_REVIEW_TERMINAL_REJECTION"
DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v8"
RECEIPT_SCHEMA = "selected-final-review-terminal-regrant-receipt.v1"
RECOVERY_RECEIPT_FIELD = "selected_final_review_terminal_regrant"
READY_TO_REQUEUE = "READY_TO_REQUEUE"
OUTSTANDING = "OUTSTANDING"
CONVERGED = "CONVERGED"
BLOCKED = "BLOCKED"
QUEUE_TO_PICK_TRANSITION = "V8_QUEUE_TO_PICK"

_INITIAL_TRANSITION = "V7_TERMINAL_PICK_TO_V8_QUEUE"
_ACTION = "REQUEUED_BY_OPERATOR_FINAL_REVIEW_TERMINAL_REGRANT"
_REJECTION_REASON = "subtitle_authority_unresolved_backfilled"
_MAX_TTL = timedelta(hours=6)
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_AUTHORIZATION_FIELDS = frozenset({"quote", "timestamp"})
_PREDECESSOR_FIELDS = frozenset(
    {
        "prior_operator_scope_grant_id",
        "parent_recovery_receipt_sha256",
        "terminal_row_sha256",
        "terminal_marker_sha256",
        "recorded_failure_recovery_fingerprint",
        "current_failure_recovery_fingerprint",
    }
)
_GRANT_FIELDS = frozenset(
    {
        "schema_version",
        "grant_id",
        "recording_date",
        "reason",
        "candidate_ids",
        "user_authorization",
        "expires_at",
        "intent",
        "upload_allowed",
        "attempt_limit",
        "predecessor",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "action",
        "candidate_id",
        "operator_scope_grant_id",
        "operator_scope_grant",
        "operator_scope_grant_sha256",
        "parent_final_review_recovery_receipt",
        "parent_final_review_recovery_receipt_sha256",
        "parent_terminal_row",
        "parent_terminal_row_sha256",
        "terminal_marker_sha256",
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
_TRANSITION_FIELDS = frozenset({"kind", "from_row_sha256", "to_row_sha256"})
_QUEUE_REBOUND_FIELDS = frozenset().union(*RECOVERY_REBOUND_FIELDS.values())
_ACTIVE_TALK_COLLECTIONS = (
    "pending_talk",
    "talk_backlog",
    "picks",
    "talk_below_confidence_threshold",
)
_QUEUED_TALK_COLLECTIONS = frozenset({"pending_talk", "talk_backlog"})
_SONG_COLLECTIONS = (
    "pending_song",
    "song_backlog",
    "song_selection_backlog",
    "songs",
    "song_superseded_attempts",
)
_KNOWN_PICK_STATUSES = frozenset(
    {
        "ok",
        "review_ready",
        "quarantine",
        "published",
        "failed",
        "candidate_rejected",
        "boundary_unrepairable",
        "speaker_review_required",
        "speaker_evidence_insufficient",
        "media_ready_cover_pending",
        "title_failed",
        "unreadable_cue_review_required",
    }
)


@dataclass(frozen=True, slots=True)
class SelectedFinalReviewTerminalRegrantInspection:
    outcome: str
    reason_code: str


def _canonical_copy(value: object) -> object:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RX.fullmatch(value) is not None


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _candidate_id(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("candidate_id") or row.get("cid") or "").strip()


def _row_body(row: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in row.items() if key != RECOVERY_RECEIPT_FIELD}


def _changed_fields(before: Mapping[str, object], after: Mapping[str, object]) -> set[str]:
    return {
        str(key)
        for key in set(before) | set(after)
        if key not in before or key not in after or before[key] != after[key]
    }


def _target_rows(
    state: Mapping[str, object], collections: Collection[str], candidate_id: str
) -> list[tuple[str, Mapping[str, object]]]:
    return [
        (collection, row)
        for collection in collections
        for row in (state.get(collection) if isinstance(state.get(collection), list) else [])
        if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
    ]


def _is_queue_row(row: Mapping[str, object], candidate_id: str) -> bool:
    body = _row_body(row)
    return bool(
        _candidate_id(body) == candidate_id
        and body.get("selected_repair") is True
        and "status" not in body
        and isinstance(body.get(PARENT_RECOVERY_RECEIPT_FIELD), Mapping)
        and PROVIDER_BUDGET_LEDGER_FIELD not in body
    )


def _is_pick_row(row: Mapping[str, object], candidate_id: str) -> bool:
    body = _row_body(row)
    return bool(
        _candidate_id(body) == candidate_id
        and body.get("selected_repair") is True
        and body.get("status") in _KNOWN_PICK_STATUSES
        and isinstance(body.get(PARENT_RECOVERY_RECEIPT_FIELD), Mapping)
    )


def is_selected_final_review_terminal_rejection(
    row: Mapping[str, object],
) -> bool:
    """Recognize only the consumed-v7 final-artifact rejection shape."""

    rc = row.get("rc")
    return bool(
        row.get("status") == "candidate_rejected"
        and row.get("rejected_status") == "failed"
        and isinstance(rc, int)
        and not isinstance(rc, bool)
        and rc == 1
        and row.get("selected_repair") is True
        and row.get("failure_kind") == "subtitle_authority"
        and row.get("failure_stage") == "chat_authority_final_artifact"
        and row.get("failure_recoverable") is False
        and row.get("rejection_reason") == _REJECTION_REASON
        and _valid_sha256(row.get("failure_recovery_fingerprint"))
        and isinstance(row.get(PARENT_RECOVERY_RECEIPT_FIELD), Mapping)
        and row.get(RECOVERY_RECEIPT_FIELD) is None
    )


def valid_terminal_regrant_grant(
    grant: object,
    *,
    candidate_id: str,
    recording_date: str | None = None,
) -> bool:
    """Validate the exact v8 grant envelope without doing runtime I/O."""

    if not isinstance(grant, Mapping) or set(grant) != _GRANT_FIELDS:
        return False
    authorization = grant.get("user_authorization")
    predecessor = grant.get("predecessor")
    authorized_at = (
        _aware_utc(authorization.get("timestamp")) if isinstance(authorization, Mapping) else None
    )
    expires_at = _aware_utc(grant.get("expires_at"))
    grant_date = grant.get("recording_date")
    return bool(
        grant.get("schema_version") == GRANT_SCHEMA
        and grant.get("intent") == GRANT_INTENT
        and grant.get("upload_allowed") is False
        and isinstance(grant.get("attempt_limit"), int)
        and not isinstance(grant.get("attempt_limit"), bool)
        and grant.get("attempt_limit") == 1
        and isinstance(grant.get("grant_id"), str)
        and str(grant.get("grant_id")).strip() == grant.get("grant_id")
        and grant.get("grant_id")
        and isinstance(grant_date, str)
        and _DATE_RX.fullmatch(grant_date)
        and (recording_date is None or grant_date == recording_date)
        and isinstance(grant.get("reason"), str)
        and len(str(grant.get("reason")).strip()) >= 8
        and grant.get("candidate_ids") == [candidate_id]
        and isinstance(authorization, Mapping)
        and set(authorization) == _AUTHORIZATION_FIELDS
        and isinstance(authorization.get("quote"), str)
        and len(str(authorization.get("quote")).strip()) >= 8
        and authorized_at is not None
        and expires_at is not None
        and authorized_at < expires_at <= authorized_at + _MAX_TTL
        and isinstance(predecessor, Mapping)
        and set(predecessor) == _PREDECESSOR_FIELDS
        and isinstance(predecessor.get("prior_operator_scope_grant_id"), str)
        and str(predecessor.get("prior_operator_scope_grant_id")).strip()
        == predecessor.get("prior_operator_scope_grant_id")
        and predecessor.get("prior_operator_scope_grant_id")
        and all(
            _valid_sha256(predecessor.get(field))
            for field in _PREDECESSOR_FIELDS
            if field != "prior_operator_scope_grant_id"
        )
    )


def _marker_for(state: Mapping[str, object], candidate_id: str) -> Mapping[str, object] | None:
    try:
        from src.autoslice.published_topic_collision import _recovery_ledger_entries

        probe = deepcopy(dict(state))
        marker = _recovery_ledger_entries(probe).get(candidate_id)
        return marker if probe == dict(state) and isinstance(marker, Mapping) else None
    except Exception:  # noqa: BLE001 - malformed marker authority fails closed
        return None


def _parent_authority_valid(
    state: Mapping[str, object],
    *,
    candidate_id: str,
    grant: Mapping[str, object],
    row: Mapping[str, object],
    check_runtime_fingerprint: bool,
) -> bool:
    predecessor = grant.get("predecessor")
    parent_receipt = row.get(PARENT_RECOVERY_RECEIPT_FIELD)
    marker = _marker_for(state, candidate_id)
    if not (
        valid_terminal_regrant_grant(grant, candidate_id=candidate_id)
        and isinstance(predecessor, Mapping)
        and is_selected_final_review_terminal_rejection(row)
        and isinstance(parent_receipt, Mapping)
        and isinstance(marker, Mapping)
    ):
        return False
    parent_grant_id = str(parent_receipt.get("operator_scope_grant_id") or "")
    try:
        from src.autoslice.published_topic_final_review_handoff import (
            terminal_handoff_transition,
            validate_terminal_handoff_state,
        )

        transition = terminal_handoff_transition(marker)
        parent_grant = (
            transition.get("operator_scope_grant") if isinstance(transition, Mapping) else None
        )
        parent_authorization = (
            parent_grant.get("user_authorization") if isinstance(parent_grant, Mapping) else None
        )
        parent_authorized_at = (
            _aware_utc(parent_authorization.get("timestamp"))
            if isinstance(parent_authorization, Mapping)
            else None
        )
        authorization = grant.get("user_authorization")
        authorized_at = (
            _aware_utc(authorization.get("timestamp"))
            if isinstance(authorization, Mapping)
            else None
        )
        marker_valid = validate_terminal_handoff_state(state, candidate_id, marker)
    except Exception:  # noqa: BLE001 - parent lineage must remain exact
        return False
    if not (
        marker_valid
        and parent_grant_id
        and isinstance(parent_grant, Mapping)
        and parent_grant.get("grant_id") == parent_grant_id
        and grant.get("grant_id") != parent_grant_id
        and parent_grant.get("recording_date") == grant.get("recording_date")
        and validate_consumed_final_review_recovery_receipt(
            parent_receipt,
            candidate_id=candidate_id,
            grant_id=parent_grant_id,
            consumed_row=row,
        )
        and parent_authorized_at is not None
        and authorized_at is not None
        and authorized_at > parent_authorized_at
        and predecessor.get("prior_operator_scope_grant_id") == parent_grant_id
        and predecessor.get("parent_recovery_receipt_sha256") == canonical_sha256(parent_receipt)
        and predecessor.get("terminal_row_sha256") == canonical_sha256(row)
        and predecessor.get("terminal_marker_sha256") == canonical_sha256(marker)
        and predecessor.get("recorded_failure_recovery_fingerprint")
        == row.get("failure_recovery_fingerprint")
    ):
        return False
    ledger_state, _ledger = resolve_provider_budget_retry_ledger_history(
        row,
        candidate_id=candidate_id,
        history_records=state.get("talk_superseded_attempts") or (),
    )
    if ledger_state != LEDGER_STATE_ABSENT:
        return False
    if not check_runtime_fingerprint:
        return True
    try:
        current = RunnerProxy().talk_failure_recovery_fingerprint(
            "subtitle_authority", candidate_id
        )
    except Exception:  # noqa: BLE001 - recovery fingerprint I/O fails closed
        return False
    return bool(
        _valid_sha256(current)
        and predecessor.get("current_failure_recovery_fingerprint") == current
    )


def build_selected_final_review_terminal_regrant_receipt(
    *,
    state: Mapping[str, object],
    old_row: Mapping[str, object],
    queued_row: Mapping[str, object],
    candidate_id: str,
    grant: Mapping[str, object],
) -> dict[str, object]:
    """Seal one exact terminal-v7 pick to its sole v8 queue row."""

    marker = _marker_for(state, candidate_id)
    predecessor = grant.get("predecessor")
    parent_receipt = old_row.get(PARENT_RECOVERY_RECEIPT_FIELD)
    if not (
        isinstance(marker, Mapping)
        and isinstance(predecessor, Mapping)
        and isinstance(parent_receipt, Mapping)
        and _parent_authority_valid(
            state,
            candidate_id=candidate_id,
            grant=grant,
            row=old_row,
            check_runtime_fingerprint=True,
        )
        and _is_queue_row(queued_row, candidate_id)
        and queued_row.get(PARENT_RECOVERY_RECEIPT_FIELD) == parent_receipt
        and RECOVERY_RECEIPT_FIELD not in queued_row
    ):
        raise ValueError("selected final-review terminal regrant input is invalid")
    canonical_parent_row = _canonical_copy(dict(old_row))
    canonical_parent_receipt = _canonical_copy(dict(parent_receipt))
    canonical_grant = _canonical_copy(dict(grant))
    queue_body = _canonical_copy(_row_body(queued_row))
    if not all(
        isinstance(value, dict)
        for value in (
            canonical_parent_row,
            canonical_parent_receipt,
            canonical_grant,
            queue_body,
        )
    ):
        raise ValueError("selected final-review terminal regrant is not canonical")
    old_sha = canonical_sha256(canonical_parent_row)
    queue_sha = canonical_sha256(queue_body)
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "action": _ACTION,
        "candidate_id": candidate_id,
        "operator_scope_grant_id": grant["grant_id"],
        "operator_scope_grant": canonical_grant,
        "operator_scope_grant_sha256": canonical_sha256(canonical_grant),
        "parent_final_review_recovery_receipt": canonical_parent_receipt,
        "parent_final_review_recovery_receipt_sha256": canonical_sha256(canonical_parent_receipt),
        "parent_terminal_row": canonical_parent_row,
        "parent_terminal_row_sha256": old_sha,
        "terminal_marker_sha256": canonical_sha256(marker),
        "initial_queue_row": queue_body,
        "initial_queue_row_sha256": queue_sha,
        "current_row": queue_body,
        "current_row_sha256": queue_sha,
        "transitions": [
            {
                "kind": _INITIAL_TRANSITION,
                "from_row_sha256": old_sha,
                "to_row_sha256": queue_sha,
            }
        ],
        "recorded_failure_recovery_fingerprint": predecessor[
            "recorded_failure_recovery_fingerprint"
        ],
        "current_failure_recovery_fingerprint": predecessor["current_failure_recovery_fingerprint"],
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def _receipt_is_self_valid(
    receipt: object,
    *,
    candidate_id: str,
    grant: Mapping[str, object],
) -> bool:
    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
        return False
    parent_row = receipt.get("parent_terminal_row")
    parent_receipt = receipt.get("parent_final_review_recovery_receipt")
    initial_queue = receipt.get("initial_queue_row")
    current_row = receipt.get("current_row")
    transitions = receipt.get("transitions")
    predecessor = grant.get("predecessor")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not (
        valid_terminal_regrant_grant(grant, candidate_id=candidate_id)
        and isinstance(predecessor, Mapping)
        and receipt.get("schema_version") == RECEIPT_SCHEMA
        and receipt.get("action") == _ACTION
        and receipt.get("candidate_id") == candidate_id
        and receipt.get("operator_scope_grant_id") == grant.get("grant_id")
        and receipt.get("operator_scope_grant") == grant
        and receipt.get("operator_scope_grant_sha256") == canonical_sha256(grant)
        and isinstance(parent_row, Mapping)
        and is_selected_final_review_terminal_rejection(parent_row)
        and isinstance(parent_receipt, Mapping)
        and parent_row.get(PARENT_RECOVERY_RECEIPT_FIELD) == parent_receipt
        and predecessor.get("prior_operator_scope_grant_id")
        == parent_receipt.get("operator_scope_grant_id")
        and grant.get("grant_id") != parent_receipt.get("operator_scope_grant_id")
        and predecessor.get("parent_recovery_receipt_sha256") == canonical_sha256(parent_receipt)
        and predecessor.get("terminal_row_sha256") == canonical_sha256(parent_row)
        and predecessor.get("recorded_failure_recovery_fingerprint")
        == parent_row.get("failure_recovery_fingerprint")
        and receipt.get("parent_final_review_recovery_receipt_sha256")
        == canonical_sha256(parent_receipt)
        and receipt.get("parent_terminal_row_sha256") == canonical_sha256(parent_row)
        and receipt.get("terminal_marker_sha256") == predecessor.get("terminal_marker_sha256")
        and receipt.get("recorded_failure_recovery_fingerprint")
        == predecessor.get("recorded_failure_recovery_fingerprint")
        and receipt.get("current_failure_recovery_fingerprint")
        == predecessor.get("current_failure_recovery_fingerprint")
        and isinstance(initial_queue, Mapping)
        and _is_queue_row(initial_queue, candidate_id)
        and initial_queue.get(PARENT_RECOVERY_RECEIPT_FIELD) == parent_receipt
        and receipt.get("initial_queue_row_sha256") == canonical_sha256(initial_queue)
        and isinstance(current_row, Mapping)
        and _candidate_id(current_row) == candidate_id
        and current_row.get(PARENT_RECOVERY_RECEIPT_FIELD) == parent_receipt
        and receipt.get("current_row_sha256") == canonical_sha256(current_row)
        and isinstance(transitions, list)
        and len(transitions) in {1, 2}
        and receipt.get("receipt_sha256") == canonical_sha256(body)
    ):
        return False
    parent_grant_id = str(parent_receipt.get("operator_scope_grant_id") or "")
    if not validate_consumed_final_review_recovery_receipt(
        parent_receipt,
        candidate_id=candidate_id,
        grant_id=parent_grant_id,
        consumed_row=parent_row,
    ):
        return False
    previous = canonical_sha256(parent_row)
    for index, transition in enumerate(transitions):
        expected_kind = _INITIAL_TRANSITION if index == 0 else QUEUE_TO_PICK_TRANSITION
        if not (
            isinstance(transition, Mapping)
            and set(transition) == _TRANSITION_FIELDS
            and transition.get("kind") == expected_kind
            and transition.get("from_row_sha256") == previous
            and _valid_sha256(transition.get("to_row_sha256"))
        ):
            return False
        previous = str(transition["to_row_sha256"])
    return bool(
        transitions[0].get("to_row_sha256") == receipt.get("initial_queue_row_sha256")
        and previous == receipt.get("current_row_sha256")
        and (
            _is_queue_row(current_row, candidate_id)
            if len(transitions) == 1
            else _is_pick_row(current_row, candidate_id)
        )
    )


def validate_selected_final_review_terminal_regrant_receipt(
    receipt: object,
    *,
    current_row: Mapping[str, object],
    candidate_id: str,
    grant: Mapping[str, object],
    allow_queue_rebound: bool = False,
) -> bool:
    """Validate one queue/pick head while permitting named queue enrichments."""

    if not _receipt_is_self_valid(receipt, candidate_id=candidate_id, grant=grant):
        return False
    assert isinstance(receipt, Mapping)
    declared = receipt.get("current_row")
    transitions = receipt.get("transitions")
    try:
        actual = _canonical_copy(_row_body(current_row))
    except (TypeError, ValueError):
        return False
    if not (
        isinstance(declared, Mapping)
        and isinstance(actual, Mapping)
        and current_row.get(RECOVERY_RECEIPT_FIELD) == receipt
    ):
        return False
    if declared == actual:
        return True
    return bool(
        allow_queue_rebound
        and isinstance(transitions, list)
        and len(transitions) == 1
        and _changed_fields(declared, actual).issubset(_QUEUE_REBOUND_FIELDS)
    )


def advance_selected_final_review_terminal_regrant_receipt(
    receipt: object,
    *,
    from_row: Mapping[str, object],
    to_row: Mapping[str, object],
    candidate_id: str,
    grant: Mapping[str, object],
    transition_kind: str,
    allow_queue_rebound: bool = False,
) -> dict[str, object]:
    """Consume the one-shot v8 authority at queue-to-pick persistence."""

    if not (
        isinstance(receipt, Mapping)
        and transition_kind == QUEUE_TO_PICK_TRANSITION
        and validate_selected_final_review_terminal_regrant_receipt(
            receipt,
            current_row=from_row,
            candidate_id=candidate_id,
            grant=grant,
            allow_queue_rebound=allow_queue_rebound,
        )
        and isinstance(receipt.get("transitions"), list)
        and len(receipt["transitions"]) == 1
        and _is_pick_row(to_row, candidate_id)
        and RECOVERY_RECEIPT_FIELD not in to_row
        and to_row.get(PARENT_RECOVERY_RECEIPT_FIELD)
        == receipt.get("parent_final_review_recovery_receipt")
    ):
        raise ValueError("selected final-review terminal regrant transition is invalid")
    canonical_to = _canonical_copy(_row_body(to_row))
    next_receipt = _canonical_copy(dict(receipt))
    if not isinstance(canonical_to, dict) or not isinstance(next_receipt, dict):
        raise ValueError("selected final-review terminal regrant transition is invalid")
    next_sha = canonical_sha256(canonical_to)
    next_receipt["current_row"] = canonical_to
    next_receipt["current_row_sha256"] = next_sha
    next_receipt["transitions"] = [
        *next_receipt["transitions"],
        {
            "kind": QUEUE_TO_PICK_TRANSITION,
            "from_row_sha256": receipt["current_row_sha256"],
            "to_row_sha256": next_sha,
        },
    ]
    next_receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in next_receipt.items() if key != "receipt_sha256"}
    )
    return next_receipt


def _same_receipt_epoch(receipt: Mapping[str, object], current: Mapping[str, object]) -> bool:
    immutable = _RECEIPT_FIELDS - {
        "current_row",
        "current_row_sha256",
        "transitions",
        "receipt_sha256",
    }
    transitions = receipt.get("transitions")
    head = current.get("transitions")
    return bool(
        all(receipt.get(key) == current.get(key) for key in immutable)
        and isinstance(transitions, list)
        and isinstance(head, list)
        and transitions == head[: len(transitions)]
    )


def _marker_regrant_relation_valid(
    marker: Mapping[str, object],
    receipt: Mapping[str, object],
    grant: Mapping[str, object],
) -> bool:
    """Recheck fresh authorization and parent-grant identity after persistence."""

    parent_receipt = receipt.get("parent_final_review_recovery_receipt")
    predecessor = grant.get("predecessor")
    try:
        from src.autoslice.published_topic_final_review_handoff import (
            terminal_handoff_transition,
        )

        transition = terminal_handoff_transition(marker)
        parent_grant = (
            transition.get("operator_scope_grant") if isinstance(transition, Mapping) else None
        )
        parent_authorization = (
            parent_grant.get("user_authorization") if isinstance(parent_grant, Mapping) else None
        )
        authorization = grant.get("user_authorization")
        parent_authorized_at = (
            _aware_utc(parent_authorization.get("timestamp"))
            if isinstance(parent_authorization, Mapping)
            else None
        )
        authorized_at = (
            _aware_utc(authorization.get("timestamp"))
            if isinstance(authorization, Mapping)
            else None
        )
    except Exception:  # noqa: BLE001 - marker relation fails closed
        return False
    return bool(
        isinstance(parent_receipt, Mapping)
        and isinstance(predecessor, Mapping)
        and isinstance(parent_grant, Mapping)
        and parent_grant.get("grant_id") == parent_receipt.get("operator_scope_grant_id")
        and predecessor.get("prior_operator_scope_grant_id") == parent_grant.get("grant_id")
        and grant.get("grant_id") != parent_grant.get("grant_id")
        and parent_grant.get("recording_date") == grant.get("recording_date")
        and parent_authorized_at is not None
        and authorized_at is not None
        and authorized_at > parent_authorized_at
        and receipt.get("terminal_marker_sha256") == canonical_sha256(marker)
    )


def validate_terminal_regrant_descendant_state(
    state: Mapping[str, object],
    candidate_id: str,
    marker: Mapping[str, object],
    *,
    parent_validator: object,
) -> bool:
    """Validate a marker-preserving v8 descendant through a synthetic parent."""

    if any(
        collection in state and not isinstance(state.get(collection), list)
        for collection in (*_ACTIVE_TALK_COLLECTIONS, *_SONG_COLLECTIONS)
    ):
        return False
    rows = _target_rows(state, _ACTIVE_TALK_COLLECTIONS, candidate_id)
    if len(rows) != 1 or _target_rows(state, _SONG_COLLECTIONS, candidate_id):
        return False
    collection, current_row = rows[0]
    receipt = current_row.get(RECOVERY_RECEIPT_FIELD)
    grant = receipt.get("operator_scope_grant") if isinstance(receipt, Mapping) else None
    if not (
        isinstance(receipt, Mapping)
        and isinstance(grant, Mapping)
        and receipt.get("terminal_marker_sha256") == canonical_sha256(marker)
        and _marker_regrant_relation_valid(marker, receipt, grant)
        and validate_selected_final_review_terminal_regrant_receipt(
            receipt,
            current_row=current_row,
            candidate_id=candidate_id,
            grant=grant,
            allow_queue_rebound=True,
        )
        and isinstance(receipt.get("transitions"), list)
        and (
            collection in _QUEUED_TALK_COLLECTIONS
            if len(receipt["transitions"]) == 1
            else collection == "picks" and len(receipt["transitions"]) == 2
        )
    ):
        return False
    parent_row = receipt.get("parent_terminal_row")
    if not isinstance(parent_row, Mapping):
        return False
    synthetic = deepcopy(dict(state))
    for active_collection in _ACTIVE_TALK_COLLECTIONS:
        synthetic[active_collection] = [
            row
            for row in (
                synthetic.get(active_collection)
                if isinstance(synthetic.get(active_collection), list)
                else []
            )
            if not (isinstance(row, Mapping) and _candidate_id(row) == candidate_id)
        ]
    synthetic["picks"].append(deepcopy(dict(parent_row)))
    try:
        if not parent_validator(synthetic, candidate_id, marker):
            return False
    except Exception:  # noqa: BLE001 - historical parent validation fails closed
        return False
    receipt_rows = [
        current_row,
        *(
            state.get("talk_superseded_attempts")
            if isinstance(state.get("talk_superseded_attempts"), list)
            else []
        ),
    ]
    for row in receipt_rows:
        if not isinstance(row, Mapping):
            continue
        candidate_receipt = row.get(RECOVERY_RECEIPT_FIELD)
        if candidate_receipt is None:
            continue
        if _candidate_id(row) != candidate_id:
            continue
        if not (
            _candidate_id(row) == candidate_id
            and isinstance(candidate_receipt, Mapping)
            and _receipt_is_self_valid(candidate_receipt, candidate_id=candidate_id, grant=grant)
            and _same_receipt_epoch(candidate_receipt, receipt)
        ):
            return False
    return True


def inspect_selected_final_review_terminal_regrant(
    state: Mapping[str, object],
    *,
    candidate_id: str,
    grant: Mapping[str, object],
) -> SelectedFinalReviewTerminalRegrantInspection:
    """Inspect initial, queued, or consumed v8 state without mutation."""

    blocked = SelectedFinalReviewTerminalRegrantInspection(
        BLOCKED, "SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_BLOCKED"
    )
    if not valid_terminal_regrant_grant(grant, candidate_id=candidate_id):
        return blocked
    if _target_rows(state, _SONG_COLLECTIONS, candidate_id):
        return blocked
    rows = _target_rows(state, _ACTIVE_TALK_COLLECTIONS, candidate_id)
    if len(rows) != 1:
        return blocked
    collection, row = rows[0]
    receipt = row.get(RECOVERY_RECEIPT_FIELD)
    if receipt is None:
        if collection != "picks" or any(
            isinstance(history, Mapping)
            and _candidate_id(history) == candidate_id
            and history.get(RECOVERY_RECEIPT_FIELD) is not None
            for history in (state.get("talk_superseded_attempts") or [])
        ):
            return blocked
        if not _parent_authority_valid(
            state,
            candidate_id=candidate_id,
            grant=grant,
            row=row,
            check_runtime_fingerprint=True,
        ):
            return blocked
        predecessor = grant["predecessor"]
        if (
            predecessor["current_failure_recovery_fingerprint"]
            == predecessor["recorded_failure_recovery_fingerprint"]
        ):
            return SelectedFinalReviewTerminalRegrantInspection(
                CONVERGED,
                "SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_FINGERPRINT_UNCHANGED",
            )
        return SelectedFinalReviewTerminalRegrantInspection(
            READY_TO_REQUEUE,
            "SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_FINGERPRINT_CHANGED",
        )
    marker = _marker_for(state, candidate_id)
    if not isinstance(marker, Mapping):
        return blocked
    try:
        from src.autoslice.published_topic_final_review_handoff import (
            validate_terminal_handoff_state,
        )

        terminal_valid = validate_terminal_handoff_state(state, candidate_id, marker)
    except Exception:  # noqa: BLE001 - marker validation fails closed
        terminal_valid = False
    if not (
        terminal_valid
        and validate_selected_final_review_terminal_regrant_receipt(
            receipt,
            current_row=row,
            candidate_id=candidate_id,
            grant=grant,
            allow_queue_rebound=True,
        )
    ):
        return blocked
    predecessor = grant["predecessor"]
    try:
        current = RunnerProxy().talk_failure_recovery_fingerprint(
            "subtitle_authority", candidate_id
        )
    except Exception:  # noqa: BLE001 - post-grant code drift blocks dispatch
        return blocked
    if current != predecessor["current_failure_recovery_fingerprint"]:
        return blocked
    if collection in _QUEUED_TALK_COLLECTIONS:
        return SelectedFinalReviewTerminalRegrantInspection(
            OUTSTANDING,
            "SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_QUEUE_OUTSTANDING",
        )
    if collection == "picks" and _is_pick_row(row, candidate_id):
        return SelectedFinalReviewTerminalRegrantInspection(
            CONVERGED,
            "SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_ATTEMPT_CONSUMED",
        )
    return blocked


def active_selected_final_review_terminal_regrant_scope(
    date: str,
    state: Mapping[str, object],
    candidate_ids: Collection[str] | None,
) -> tuple[str, str] | None:
    """Return the exact admitted v8 CID/grant without broad fallback."""

    if candidate_ids is None or isinstance(candidate_ids, (str, bytes)):
        return None
    values = tuple(candidate_ids)
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    try:
        from src.autoslice.operator_processing_scope import (
            STATE_KEY,
            operator_scope_admission,
            operator_talk_scope,
        )

        grant = state.get(STATE_KEY)
        admission = operator_scope_admission(state, date=date)
        talk_scope = operator_talk_scope(state, date=date)
    except Exception:  # noqa: BLE001 - typed authority fails closed
        return None
    if not (
        isinstance(grant, Mapping)
        and valid_terminal_regrant_grant(grant, candidate_id=values[0], recording_date=date)
        and admission.admitted
        and admission.candidate_ids == values
        and admission.outstanding_candidate_ids == values
        and talk_scope == values
        and isinstance(admission.grant_id, str)
        and admission.grant_id == grant.get("grant_id")
    ):
        return None
    return values[0], admission.grant_id


def validate_initial_terminal_regrant_transition(
    pre_state: Mapping[str, object],
    post_state: Mapping[str, object],
    *,
    candidate_id: str,
    grant: Mapping[str, object],
) -> bool:
    """Validate the sole pick-to-queue mutation without changing either state."""

    if set(_changed_fields(pre_state, post_state)) - {
        "picks",
        "pending_talk",
        "talk_backlog",
        "talk_superseded_attempts",
    }:
        return False
    pre_rows = _target_rows(pre_state, _ACTIVE_TALK_COLLECTIONS, candidate_id)
    post_rows = _target_rows(post_state, _ACTIVE_TALK_COLLECTIONS, candidate_id)
    if not (
        len(pre_rows) == len(post_rows) == 1
        and pre_rows[0][0] == "picks"
        and post_rows[0][0] in _QUEUED_TALK_COLLECTIONS
        and pre_state.get("operator_processing_scope") == grant
        and post_state.get("operator_processing_scope") == grant
        and pre_state.get("upload_allowed") is False
        and post_state.get("upload_allowed") is False
        and _marker_for(pre_state, candidate_id) == _marker_for(post_state, candidate_id)
        and all(pre_state.get(key) == post_state.get(key) for key in _SONG_COLLECTIONS)
    ):
        return False
    pre_row = pre_rows[0][1]
    post_row = post_rows[0][1]
    receipt = post_row.get(RECOVERY_RECEIPT_FIELD)
    if not (
        isinstance(receipt, Mapping)
        and receipt.get("parent_terminal_row") == pre_row
        and post_row.get(PARENT_RECOVERY_RECEIPT_FIELD)
        == pre_row.get(PARENT_RECOVERY_RECEIPT_FIELD)
        and validate_selected_final_review_terminal_regrant_receipt(
            receipt,
            current_row=post_row,
            candidate_id=candidate_id,
            grant=grant,
        )
    ):
        return False
    for collection in _ACTIVE_TALK_COLLECTIONS:
        pre_non_target = [
            row
            for row in (pre_state.get(collection) or [])
            if not (isinstance(row, Mapping) and _candidate_id(row) == candidate_id)
        ]
        post_non_target = [
            row
            for row in (post_state.get(collection) or [])
            if not (isinstance(row, Mapping) and _candidate_id(row) == candidate_id)
        ]
        if pre_non_target != post_non_target:
            return False
    before_history = pre_state.get("talk_superseded_attempts") or []
    after_history = post_state.get("talk_superseded_attempts") or []
    if not (
        isinstance(before_history, list)
        and isinstance(after_history, list)
        and after_history[:-1] == before_history
        and len(after_history) == len(before_history) + 1
        and isinstance(after_history[-1], Mapping)
        and _candidate_id(after_history[-1]) == candidate_id
        and after_history[-1].get(RECOVERY_RECEIPT_FIELD) == receipt
        and after_history[-1].get(PARENT_RECOVERY_RECEIPT_FIELD)
        == pre_row.get(PARENT_RECOVERY_RECEIPT_FIELD)
    ):
        return False
    return bool(
        inspect_selected_final_review_terminal_regrant(
            pre_state, candidate_id=candidate_id, grant=grant
        ).outcome
        == READY_TO_REQUEUE
        and inspect_selected_final_review_terminal_regrant(
            post_state, candidate_id=candidate_id, grant=grant
        ).outcome
        == OUTSTANDING
    )
