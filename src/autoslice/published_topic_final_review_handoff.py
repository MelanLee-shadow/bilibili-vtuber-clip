"""One-way terminal handoff from a sealed v5 topic marker to v7 recovery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path


HANDOFF_TRANSITION_KIND = "HANDOFF_TO_SELECTED_FINAL_REVIEW_RECOVERY"
HANDOFF_READY = "READY"
HANDOFF_ABSENT = "ABSENT"
HANDOFF_BLOCKED = "BLOCKED"

_TRANSITION_SCHEMA = "published-topic-resolution-recovery-transition.v2"
_MARKER_SCHEMA = "published-topic-resolution-recovery-marker.v2"
_GRANT_SCHEMA = "operator-processing-scope-grant.v7"
_GRANT_INTENT = "RECOVER_NAMED_SELECTED_FINAL_REVIEW_REJECTION"
_RECEIPT_FIELD = "selected_final_review_recovery"
_RECEIPT_SCHEMA = "selected-final-review-recovery-receipt.v1"
_QUEUE_COLLECTIONS = frozenset({"pending_talk", "talk_backlog"})
_ACTIVE_TALK_COLLECTIONS = (
    "pending_talk",
    "talk_backlog",
    "picks",
    "talk_below_confidence_threshold",
)
_SONG_COLLECTIONS = (
    "pending_song",
    "song_backlog",
    "song_selection_backlog",
    "songs",
    "song_superseded_attempts",
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
    }
)
_HANDOFF_FIELDS = frozenset(
    {
        "schema_version",
        "transition_index",
        "transition_kind",
        "previous_row_binding",
        "next_row_binding",
        "candidate_id",
        "recording_date",
        "operator_scope_grant_id",
        "operator_scope_intent",
        "upload_authorized",
        "operator_scope_grant",
        "operator_scope_grant_sha256",
        "initial_recovery_receipt",
        "initial_recovery_receipt_sha256",
        "old_row_sha256",
        "initial_queue_row_sha256",
        "recorded_failure_recovery_fingerprint",
        "current_failure_recovery_fingerprint",
        "transition_sha256",
    }
)
_MARKER_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "queue_origin",
        "upload_authorized",
        "original_hold",
        "original_hold_sha256",
        "restored_candidate_sha256",
        "resolution",
        "original_authority",
        "scorecard_refresh",
        "current_row_binding",
        "transitions",
        "marker_sha256",
    }
)
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _canonical_copy(value: object) -> object:
    return json.loads(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _sha256(value: object) -> str:
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


def _candidate_id(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("candidate_id") or row.get("cid") or "").strip()


def _binding_is_valid(binding: object) -> bool:
    return bool(
        isinstance(binding, Mapping)
        and set(binding) == {"collection", "row_sha256"}
        and binding.get("collection")
        in {*_QUEUE_COLLECTIONS, "picks", "talk_below_confidence_threshold"}
        and _valid_sha256(binding.get("row_sha256"))
    )


def _aware_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _grant_is_valid(
    grant: object, *, candidate_id: str, recording_date: str, grant_id: str
) -> bool:
    if not isinstance(grant, Mapping) or set(grant) != _GRANT_FIELDS:
        return False
    authorization = grant.get("user_authorization")
    return bool(
        grant.get("schema_version") == _GRANT_SCHEMA
        and grant.get("grant_id") == grant_id
        and isinstance(grant_id, str)
        and grant_id.strip() == grant_id
        and grant_id
        and grant.get("recording_date") == recording_date
        and isinstance(recording_date, str)
        and _DATE_RX.fullmatch(recording_date)
        and isinstance(grant.get("reason"), str)
        and len(str(grant.get("reason")).strip()) >= 8
        and grant.get("candidate_ids") == [candidate_id]
        and isinstance(authorization, Mapping)
        and set(authorization) == {"quote", "timestamp"}
        and isinstance(authorization.get("quote"), str)
        and len(str(authorization.get("quote")).strip()) >= 8
        and _aware_timestamp(authorization.get("timestamp"))
        and _aware_timestamp(grant.get("expires_at"))
        and grant.get("intent") == _GRANT_INTENT
        and grant.get("upload_allowed") is False
    )


def reconstruct_initial_final_review_receipt(
    receipt: object,
) -> dict[str, object] | None:
    """Rebuild the immutable initial receipt from any valid evolved v7 receipt."""

    if not isinstance(receipt, Mapping):
        return None
    candidate_id = receipt.get("candidate_id")
    grant_id = receipt.get("operator_scope_grant_id")
    current_fingerprint = receipt.get("current_failure_recovery_fingerprint")
    try:
        from src.autoslice.selected_final_review_recovery import (
            _receipt_is_self_valid,
        )

        if not (
            isinstance(candidate_id, str)
            and isinstance(grant_id, str)
            and isinstance(current_fingerprint, str)
            and _receipt_is_self_valid(
                receipt,
                candidate_id=candidate_id,
                grant_id=grant_id,
                current_fingerprint=current_fingerprint,
            )
        ):
            return None
        transitions = receipt.get("transitions")
        if not isinstance(transitions, list) or not transitions:
            return None
        initial = _canonical_copy(dict(receipt))
        if not isinstance(initial, dict):
            return None
        initial["current_row"] = deepcopy(initial["initial_queue_row"])
        initial["current_row_sha256"] = initial["initial_queue_row_sha256"]
        initial["transitions"] = [deepcopy(transitions[0])]
        initial["receipt_sha256"] = _sha256(
            {key: value for key, value in initial.items() if key != "receipt_sha256"}
        )
        return initial if _receipt_is_self_valid(
            initial,
            candidate_id=candidate_id,
            grant_id=grant_id,
            current_fingerprint=current_fingerprint,
        ) else None
    except (KeyError, TypeError, ValueError):
        return None


def validate_handoff_transition_structure(
    transition: object, *, index: int, expected_previous: Mapping[str, object]
) -> dict[str, object] | None:
    """Validate the exact terminal transition and return its queue binding."""

    if not isinstance(transition, Mapping) or set(transition) != _HANDOFF_FIELDS:
        return None
    previous = transition.get("previous_row_binding")
    next_binding = transition.get("next_row_binding")
    candidate_id = transition.get("candidate_id")
    recording_date = transition.get("recording_date")
    grant_id = transition.get("operator_scope_grant_id")
    grant = transition.get("operator_scope_grant")
    initial = transition.get("initial_recovery_receipt")
    body = {
        key: value for key, value in transition.items() if key != "transition_sha256"
    }
    if not (
        transition.get("schema_version") == _TRANSITION_SCHEMA
        and transition.get("transition_index") == index
        and transition.get("transition_kind") == HANDOFF_TRANSITION_KIND
        and previous == expected_previous
        and _binding_is_valid(previous)
        and previous.get("collection") == "picks"
        and _binding_is_valid(next_binding)
        and next_binding.get("collection") in _QUEUE_COLLECTIONS
        and previous != next_binding
        and isinstance(candidate_id, str)
        and candidate_id
        and isinstance(recording_date, str)
        and isinstance(grant_id, str)
        and _grant_is_valid(
            grant,
            candidate_id=candidate_id,
            recording_date=recording_date,
            grant_id=grant_id,
        )
        and transition.get("operator_scope_intent") == _GRANT_INTENT
        and transition.get("upload_authorized") is False
        and transition.get("operator_scope_grant_sha256") == _sha256(grant)
        and isinstance(initial, Mapping)
        and reconstruct_initial_final_review_receipt(initial) == dict(initial)
        and transition.get("initial_recovery_receipt_sha256") == _sha256(initial)
        and initial.get("schema_version") == _RECEIPT_SCHEMA
        and initial.get("candidate_id") == candidate_id
        and initial.get("operator_scope_grant_id") == grant_id
        and transition.get("old_row_sha256") == previous.get("row_sha256")
        and transition.get("old_row_sha256") == initial.get("old_row_sha256")
        and transition.get("initial_queue_row_sha256")
        == initial.get("initial_queue_row_sha256")
        and transition.get("recorded_failure_recovery_fingerprint")
        == initial.get("recorded_failure_recovery_fingerprint")
        and transition.get("current_failure_recovery_fingerprint")
        == initial.get("current_failure_recovery_fingerprint")
        and all(
            _valid_sha256(transition.get(key))
            for key in (
                "old_row_sha256",
                "initial_queue_row_sha256",
                "recorded_failure_recovery_fingerprint",
                "current_failure_recovery_fingerprint",
            )
        )
        and transition.get("transition_sha256") == _sha256(body)
    ):
        return None
    queue_row = deepcopy(dict(initial["initial_queue_row"]))
    queue_row[_RECEIPT_FIELD] = deepcopy(dict(initial))
    if next_binding.get("row_sha256") != _sha256(queue_row):
        return None
    return dict(next_binding)


def terminal_handoff_transition(marker: object) -> Mapping[str, object] | None:
    if not isinstance(marker, Mapping):
        return None
    transitions = marker.get("transitions")
    if not isinstance(transitions, list) or not transitions:
        return None
    matches = [
        transition
        for transition in transitions
        if isinstance(transition, Mapping)
        and transition.get("transition_kind") == HANDOFF_TRANSITION_KIND
    ]
    return matches[0] if len(matches) == 1 and matches[0] is transitions[-1] else None


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


def _target_hold_exists(state: Mapping[str, object], candidate_id: str) -> bool:
    review = state.get("published_topic_dedup_review")
    holds = review.get("holds") if isinstance(review, Mapping) else []
    if not isinstance(holds, list):
        return True
    return any(
        isinstance(hold, Mapping)
        and (
            _candidate_id(hold) == candidate_id
            or _candidate_id(hold.get("candidate")) == candidate_id
        )
        for hold in holds
    )


def _receipt_transitions_are_prefix(
    receipt: Mapping[str, object], current_receipt: Mapping[str, object]
) -> bool:
    transitions = receipt.get("transitions")
    current = current_receipt.get("transitions")
    return bool(
        isinstance(transitions, list)
        and isinstance(current, list)
        and len(transitions) <= len(current)
        and transitions == current[: len(transitions)]
    )


def _validate_terminal_handoff_state_v7(
    state: Mapping[str, object], candidate_id: str, marker: Mapping[str, object]
) -> bool:
    """Validate the historical handoff solely from its embedded v7 authority."""

    transition = terminal_handoff_transition(marker)
    if transition is None:
        return False
    transitions = marker.get("transitions")
    marker_body = {key: value for key, value in marker.items() if key != "marker_sha256"}
    previous = transition.get("previous_row_binding")
    next_binding = validate_handoff_transition_structure(
        transition,
        index=len(transitions) - 1 if isinstance(transitions, list) else -1,
        expected_previous=previous if isinstance(previous, Mapping) else {},
    )
    if not (
        set(marker) == _MARKER_FIELDS
        and marker.get("schema_version") == _MARKER_SCHEMA
        and marker.get("candidate_id") == candidate_id
        and marker.get("upload_authorized") is False
        and state.get("upload_allowed") is False
        and marker.get("marker_sha256") == _sha256(marker_body)
        and next_binding is not None
        and marker.get("current_row_binding") == next_binding
        and transition.get("candidate_id") == candidate_id
        and not _target_rows(state, _SONG_COLLECTIONS, candidate_id)
        and not _target_hold_exists(state, candidate_id)
    ):
        return False
    current_rows = _target_rows(state, _ACTIVE_TALK_COLLECTIONS, candidate_id)
    if len(current_rows) != 1:
        return False
    collection, current_row = current_rows[0]
    current_receipt = current_row.get(_RECEIPT_FIELD)
    embedded = transition.get("initial_recovery_receipt")
    if not isinstance(current_receipt, Mapping) or not isinstance(embedded, Mapping):
        return False
    initial = reconstruct_initial_final_review_receipt(current_receipt)
    grant_id = str(transition.get("operator_scope_grant_id") or "")
    current_fingerprint = transition.get("current_failure_recovery_fingerprint")
    try:
        from src.autoslice.selected_final_review_recovery import (
            validate_consumed_final_review_recovery_receipt,
            validate_selected_final_review_recovery_receipt,
        )

        current_valid = (
            validate_selected_final_review_recovery_receipt(
                current_receipt,
                queued_row=current_row,
                candidate_id=candidate_id,
                grant_id=grant_id,
                current_fingerprint=str(current_fingerprint or ""),
                allow_queue_rebound=True,
            )
            if collection in _QUEUE_COLLECTIONS
            else validate_consumed_final_review_recovery_receipt(
                current_receipt,
                candidate_id=candidate_id,
                grant_id=grant_id,
                consumed_row=current_row,
            )
        )
    except (TypeError, ValueError):
        return False
    if not current_valid or initial != dict(embedded):
        return False
    receipt_rows = [
        *[row for _collection, row in current_rows],
        *(
            state.get("talk_superseded_attempts")
            if isinstance(state.get("talk_superseded_attempts"), list)
            else []
        ),
    ]
    for row in receipt_rows:
        if not isinstance(row, Mapping):
            continue
        receipt = row.get(_RECEIPT_FIELD)
        if receipt is None:
            continue
        receipt_candidate = _candidate_id(receipt)
        if _candidate_id(row) != candidate_id and receipt_candidate != candidate_id:
            continue
        if not (
            _candidate_id(row) == candidate_id
            and isinstance(receipt, Mapping)
            and receipt_candidate == candidate_id
            and reconstruct_initial_final_review_receipt(receipt) == dict(embedded)
            and _receipt_transitions_are_prefix(receipt, current_receipt)
        ):
            return False
    return True


def validate_terminal_handoff_state(
    state: Mapping[str, object], candidate_id: str, marker: Mapping[str, object]
) -> bool:
    """Validate the original v7 head or one marker-preserving v8 descendant."""

    from src.autoslice.selected_final_review_terminal_regrant import (
        RECOVERY_RECEIPT_FIELD as TERMINAL_REGRANT_RECEIPT_FIELD,
        validate_terminal_regrant_descendant_state,
    )

    current_rows = _target_rows(state, _ACTIVE_TALK_COLLECTIONS, candidate_id)
    history = (
        state.get("talk_superseded_attempts")
        if isinstance(state.get("talk_superseded_attempts"), list)
        else []
    )
    has_v8_evidence = any(
        isinstance(row, Mapping)
        and _candidate_id(row) == candidate_id
        and row.get(TERMINAL_REGRANT_RECEIPT_FIELD) is not None
        for row in [*(row for _collection, row in current_rows), *history]
    )
    if has_v8_evidence:
        return validate_terminal_regrant_descendant_state(
            state,
            candidate_id,
            marker,
            parent_validator=_validate_terminal_handoff_state_v7,
        )
    return _validate_terminal_handoff_state_v7(state, candidate_id, marker)


def build_terminal_handoff_marker(
    marker: Mapping[str, object],
    *,
    candidate_id: str,
    recording_date: str,
    grant: Mapping[str, object],
    old_row: Mapping[str, object],
    queue_collection: str,
    queued_row: Mapping[str, object],
) -> dict[str, object] | None:
    """Append the sole v5->v7 terminal transition and reseal the marker."""

    transitions = marker.get("transitions")
    previous = marker.get("current_row_binding")
    receipt = queued_row.get(_RECEIPT_FIELD)
    grant_id = str(grant.get("grant_id") or "")
    if not (
        isinstance(transitions, list)
        and terminal_handoff_transition(marker) is None
        and isinstance(previous, Mapping)
        and previous
        == {"collection": "picks", "row_sha256": _sha256(old_row)}
        and queue_collection in _QUEUE_COLLECTIONS
        and isinstance(receipt, Mapping)
        and reconstruct_initial_final_review_receipt(receipt) == dict(receipt)
        and receipt.get("old_row") == old_row
        and _grant_is_valid(
            grant,
            candidate_id=candidate_id,
            recording_date=recording_date,
            grant_id=grant_id,
        )
    ):
        return None
    next_binding = {
        "collection": queue_collection,
        "row_sha256": _sha256(queued_row),
    }
    transition: dict[str, object] = {
        "schema_version": _TRANSITION_SCHEMA,
        "transition_index": len(transitions),
        "transition_kind": HANDOFF_TRANSITION_KIND,
        "previous_row_binding": deepcopy(dict(previous)),
        "next_row_binding": next_binding,
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "operator_scope_grant_id": grant_id,
        "operator_scope_intent": _GRANT_INTENT,
        "upload_authorized": False,
        "operator_scope_grant": deepcopy(dict(grant)),
        "operator_scope_grant_sha256": _sha256(grant),
        "initial_recovery_receipt": deepcopy(dict(receipt)),
        "initial_recovery_receipt_sha256": _sha256(receipt),
        "old_row_sha256": receipt.get("old_row_sha256"),
        "initial_queue_row_sha256": receipt.get("initial_queue_row_sha256"),
        "recorded_failure_recovery_fingerprint": receipt.get(
            "recorded_failure_recovery_fingerprint"
        ),
        "current_failure_recovery_fingerprint": receipt.get(
            "current_failure_recovery_fingerprint"
        ),
    }
    transition["transition_sha256"] = _sha256(transition)
    if validate_handoff_transition_structure(
        transition, index=len(transitions), expected_previous=previous
    ) != next_binding:
        return None
    next_marker = deepcopy(dict(marker))
    next_marker["current_row_binding"] = next_binding
    next_marker["transitions"] = [*deepcopy(transitions), transition]
    next_marker["marker_sha256"] = _sha256(
        {key: value for key, value in next_marker.items() if key != "marker_sha256"}
    )
    return next_marker


def inspect_initial_final_review_handoff(
    state: Mapping[str, object],
    *,
    candidate_id: str,
    recording_date: str,
    repo_root: Path | None = None,
    publication_registry: Mapping[str, object] | None = None,
) -> str:
    """Classify a fresh v7 rejection with absent, valid, or blocked v5 ancestry."""

    try:
        from src.autoslice.published_topic_collision import (
            DEFAULT_REPO_ROOT,
            RECOVERY_CONVERGED,
            _marker_is_valid,
            _one_recovery_target_row,
            _recovery_ledger_entries,
            inspect_published_topic_resolution_recovery,
        )
        from src.autoslice.selected_final_review_recovery import (
            READY_TO_REQUEUE,
            inspect_selected_final_review_recovery,
        )

        grant = state.get("operator_processing_scope")
        grant_id = str(grant.get("grant_id") or "") if isinstance(grant, Mapping) else ""
        if not _grant_is_valid(
            grant,
            candidate_id=candidate_id,
            recording_date=recording_date,
            grant_id=grant_id,
        ):
            return HANDOFF_BLOCKED
        inspection = inspect_selected_final_review_recovery(
            state, candidate_id=candidate_id, grant_id=grant_id
        )
        if inspection.outcome != READY_TO_REQUEUE:
            return HANDOFF_ABSENT
        raw_ledger = state.get("published_topic_resolution_recovery")
        if raw_ledger is None:
            return HANDOFF_ABSENT
        entries = _recovery_ledger_entries(state)
        marker = entries.get(candidate_id)
        if marker is None:
            return HANDOFF_ABSENT
        root = repo_root or DEFAULT_REPO_ROOT
        row = _one_recovery_target_row(state, candidate_id)
        valid, _queued, _current = _marker_is_valid(
            state,
            candidate_id,
            marker,
            repo_root=root,
            publication_registry=publication_registry,
        )
        return (
            HANDOFF_READY
            if valid
            and row is not None
            and row[0] == "picks"
            and marker.get("current_row_binding")
            == {"collection": "picks", "row_sha256": _sha256(row[1])}
            and inspect_published_topic_resolution_recovery(
                state,
                candidate_id,
                repo_root=root,
                publication_registry=publication_registry,
            )
            == RECOVERY_CONVERGED
            else HANDOFF_BLOCKED
        )
    except Exception:  # noqa: BLE001 - an authority probe must fail closed
        return HANDOFF_BLOCKED


def _without_target(rows: object, candidate_id: str) -> list[object] | None:
    if not isinstance(rows, list):
        return None
    return [deepcopy(row) for row in rows if _candidate_id(row) != candidate_id]


def seal_published_topic_final_review_handoff(
    state: dict,
    candidate_id: str,
    *,
    pre_state: Mapping[str, object],
    recording_date: str,
    repo_root: Path | None = None,
    publication_registry: Mapping[str, object] | None = None,
) -> bool:
    """Build and atomically install one terminal v5 marker upgrade."""

    try:
        from src.autoslice.published_topic_collision import (
            DEFAULT_REPO_ROOT,
            RECOVERY_CONVERGED,
            RECOVERY_MARKER_FIELD,
            _ledger_with_replaced_marker,
            _marker_is_valid,
            _one_recovery_target_row,
            _recovery_ledger_entries,
            inspect_published_topic_resolution_recovery,
        )

        root = repo_root or DEFAULT_REPO_ROOT
        entries = _recovery_ledger_entries(state)
        pre_entries = _recovery_ledger_entries(pre_state)
        marker = pre_entries.get(candidate_id)
        before = _one_recovery_target_row(pre_state, candidate_id)
        after = _one_recovery_target_row(state, candidate_id)
        grant = pre_state.get("operator_processing_scope")
        if not (
            entries == pre_entries
            and marker is not None
            and before is not None
            and before[0] == "picks"
            and after is not None
            and after[0] in _QUEUE_COLLECTIONS
            and isinstance(grant, Mapping)
            and pre_state.get("upload_allowed") is False
            and state.get("upload_allowed") is False
            and state.get("operator_processing_scope") == grant
            and inspect_initial_final_review_handoff(
                pre_state,
                candidate_id=candidate_id,
                recording_date=recording_date,
                repo_root=root,
                publication_registry=publication_registry,
            )
            == HANDOFF_READY
            and all(
                _without_target(pre_state.get(key), candidate_id)
                == _without_target(state.get(key), candidate_id)
                for key in _ACTIVE_TALK_COLLECTIONS
            )
            and _without_target(pre_state.get("talk_superseded_attempts"), candidate_id)
            == _without_target(state.get("talk_superseded_attempts"), candidate_id)
            and all(
                pre_state.get(key) == state.get(key) for key in _SONG_COLLECTIONS
            )
        ):
            return False
        next_marker = build_terminal_handoff_marker(
            marker,
            candidate_id=candidate_id,
            recording_date=recording_date,
            grant=grant,
            old_row=before[1],
            queue_collection=after[0],
            queued_row=after[1],
        )
        if next_marker is None:
            return False
        next_ledger = _ledger_with_replaced_marker(state, candidate_id, next_marker)
        shadow = deepcopy(dict(state))
        shadow[RECOVERY_MARKER_FIELD] = next_ledger
        valid, _queued, _current = _marker_is_valid(
            shadow,
            candidate_id,
            next_marker,
            repo_root=root,
            publication_registry=publication_registry,
        )
        if not (
            valid
            and validate_terminal_handoff_state(shadow, candidate_id, next_marker)
            and inspect_published_topic_resolution_recovery(
                shadow,
                candidate_id,
                repo_root=root,
                publication_registry=publication_registry,
            )
            == RECOVERY_CONVERGED
        ):
            return False
        state[RECOVERY_MARKER_FIELD] = next_ledger
        return True
    except Exception:  # noqa: BLE001 - the transaction owner restores preimage
        return False
