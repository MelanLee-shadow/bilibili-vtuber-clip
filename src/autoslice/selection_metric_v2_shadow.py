"""Persistable, observation-only production projection for selection metric v2.

The production selector is still v1.  This module deliberately consumes a
separate, candidate-keyed evidence surface and returns a detached snapshot;
it never edits candidate rows, ranks a queue, grants quota, or authorizes a
release.  Missing evidence is a typed ``UNAVAILABLE`` result, not an inferred
topic or speaker identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice import host_occupancy
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice import selection_metric_v2
from src.autoslice.selection_scorecard import selection_scorecard_is_valid


_PROFILE = load_channel_profile(Path(__file__).resolve().parents[2])

SNAPSHOT_SCHEMA_VERSION = f"{_PROFILE.profile_id}-selection-metric-v2-shadow-snapshot.v1"
RECEIPT_SCHEMA_VERSION = f"{_PROFILE.profile_id}-selection-metric-v2-shadow-receipt.v1"
INPUT_SCHEMA_VERSION = f"{_PROFILE.profile_id}-selection-metric-v2-shadow-input.v1"
TOPIC_SCHEMA_VERSION = f"{_PROFILE.profile_id}-selection-topic-fingerprint.v1"
INPUT_STATE_FIELD = "selection_metric_v2_shadow_inputs"
REPORT_FILENAME = "SELECTION_METRIC_V2_SHADOW.json"
PURPOSE = "OBSERVATION_ONLY_NOT_SELECTION_QUOTA_RELEASE_OR_UPLOAD_AUTHORITY"
PRODUCTION_INPUT_PRODUCER_STATUS = "NOT_WIRED"

# Active rows are authoritative for the current candidate projection.  A
# superseded attempt is consulted only when no active row for that candidate
# exists; otherwise an ordinary retry would make the current scorecard look
# conflicted merely because its historical attempt omitted or carried an older
# scorecard.  Song collections are intentionally absent: metric v2 is Talk-only.
ACTIVE_TALK_COLLECTIONS: tuple[str, ...] = (
    "pending_talk",
    "talk_backlog",
    "talk_below_confidence_threshold",
    "picks",
)
HISTORICAL_TALK_COLLECTIONS: tuple[str, ...] = (
    "talk_superseded_attempts",
)
TALK_COLLECTIONS = ACTIVE_TALK_COLLECTIONS + HISTORICAL_TALK_COLLECTIONS

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


BRIDGE_POLICY = {
    "policy_version": "v1-scorecard-topic-host-occupancy-broad-shadow.v1",
    "purpose": PURPOSE,
    "metric_schema_version": selection_metric_v2.SCHEMA_VERSION,
    "metric_decision_policy_version": selection_metric_v2.DECISION_POLICY_VERSION,
    "metric_rubric_version": selection_metric_v2.RUBRIC_VERSION,
    "metric_config_hash": selection_metric_v2.CONFIG_HASH,
    "host_occupancy_schema_version": host_occupancy.SCHEMA_VERSION,
    "host_occupancy_estimator_version": host_occupancy.ESTIMATOR_VERSION,
    "host_occupancy_threshold_version": host_occupancy.THRESHOLD_VERSION,
    "host_occupancy_config_hash": host_occupancy.CONFIG_HASH,
    "topic_input_schema_version": TOPIC_SCHEMA_VERSION,
    # No proof contract is persisted in production yet.  Passing no atoms
    # leaves every single-axis OR path closed; only the broad path is observed.
    "proof_atoms_source": "NONE_BROAD_PATH_ONLY",
    "axis_uncertainty_source": "NONE",
    "global_uncertainty_source": "selection_scorecard.uncertainty_penalty",
    "topic_counts_source": "COMPLETE_TYPED_CURRENT_SESSION_CANDIDATE_SET",
    # The schemas below are consumers for a future typed producer.  Semantic
    # recall currently drops its event key before final candidate binding and
    # host occupancy has no production runner caller.  Until both producers
    # persist their own source/runtime/boundary receipts, even a mapping that
    # merely resembles this input shape must remain UNAVAILABLE.
    "production_input_producer_status": PRODUCTION_INPUT_PRODUCER_STATUS,
    "decision_influence": False,
}
POLICY_SHA256 = canonical_sha256(BRIDGE_POLICY)


def _candidate_id(row: Mapping[str, object]) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "").strip()


def _metric_projection(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "candidate_id": _candidate_id(row),
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "session_id": row.get("session_id"),
        "selection_scorecard": row.get("selection_scorecard"),
    }


def _collect_talk_rows(
    state: Mapping[str, object],
) -> list[tuple[str, Mapping[str, object], bool]]:
    by_id: dict[str, list[Mapping[str, object]]] = {}
    order: list[str] = []
    for field in ACTIVE_TALK_COLLECTIONS:
        rows = state.get(field)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            candidate_id = _candidate_id(row)
            if not candidate_id:
                continue
            if candidate_id not in by_id:
                order.append(candidate_id)
                by_id[candidate_id] = []
            by_id[candidate_id].append(row)

    active_ids = set(by_id)
    for field in HISTORICAL_TALK_COLLECTIONS:
        rows = state.get(field)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            candidate_id = _candidate_id(row)
            if not candidate_id or candidate_id in active_ids:
                continue
            if candidate_id not in by_id:
                order.append(candidate_id)
                by_id[candidate_id] = []
            by_id[candidate_id].append(row)

    collected: list[tuple[str, Mapping[str, object], bool]] = []
    for candidate_id in order:
        rows = by_id[candidate_id]
        projections: set[str] = set()
        noncanonical = False
        for row in rows:
            try:
                projections.add(canonical_sha256(_metric_projection(row)))
            except (TypeError, ValueError):
                noncanonical = True
        collected.append(
            (candidate_id, rows[0], noncanonical or len(projections) != 1)
        )
    return collected


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _hash_or_reason(
    value: object, reasons: list[str], reason: str
) -> str | None:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError):
        _append_reason(reasons, reason)
        return None


def _sha256_text(value: object) -> str | None:
    text = str(value or "")
    return text if _SHA256_RE.fullmatch(text) else None


def _typed_input(
    state: Mapping[str, object], candidate_id: str
) -> Mapping[str, object] | None:
    inputs = state.get(INPUT_STATE_FIELD)
    if not isinstance(inputs, Mapping):
        return None
    value = inputs.get(candidate_id)
    return value if isinstance(value, Mapping) else None


def _topic_input(
    raw_input: Mapping[str, object] | None,
) -> tuple[dict[str, object] | None, list[str]]:
    reasons: list[str] = []
    if raw_input is None:
        return None, ["SHADOW_INPUT_MISSING"]
    topic = raw_input.get("topic")
    if not isinstance(topic, Mapping):
        return None, ["TOPIC_INPUT_MISSING"]
    fingerprint = _sha256_text(topic.get("fingerprint"))
    source_sha256 = _sha256_text(topic.get("source_sha256"))
    policy_version = str(topic.get("policy_version") or "").strip()
    if topic.get("schema_version") != TOPIC_SCHEMA_VERSION:
        _append_reason(reasons, "TOPIC_SCHEMA_MISMATCH")
    if fingerprint is None:
        _append_reason(reasons, "TOPIC_FINGERPRINT_INVALID")
    if source_sha256 is None:
        _append_reason(reasons, "TOPIC_SOURCE_SHA256_INVALID")
    if not policy_version:
        _append_reason(reasons, "TOPIC_POLICY_VERSION_MISSING")
    if reasons:
        return None, reasons
    return {
        "schema_version": TOPIC_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "source_sha256": source_sha256,
        "policy_version": policy_version,
    }, []


def _host_input(
    raw_input: Mapping[str, object] | None,
    *,
    candidate_id: str,
    start_ms: object,
    end_ms: object,
) -> tuple[Mapping[str, object] | None, list[str]]:
    if raw_input is None:
        return None, ["SHADOW_INPUT_MISSING"]
    report = raw_input.get("host_occupancy")
    if not isinstance(report, Mapping):
        return None, ["HOST_OCCUPANCY_INPUT_MISSING"]
    reasons: list[str] = []
    expected = {
        "schema_version": host_occupancy.SCHEMA_VERSION,
        "estimator_version": host_occupancy.ESTIMATOR_VERSION,
        "threshold_version": host_occupancy.THRESHOLD_VERSION,
        "config_hash": host_occupancy.CONFIG_HASH,
        "candidate_id": candidate_id,
        "candidate_start_ms": start_ms,
        "candidate_end_ms": end_ms,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            _append_reason(reasons, f"HOST_OCCUPANCY_{key.upper()}_MISMATCH")
    if report.get("provisional_calibration") is not True:
        _append_reason(reasons, "HOST_OCCUPANCY_PROVISIONAL_MARKER_MISSING")
    occupancy = report.get("occupancy")
    if not isinstance(occupancy, Mapping):
        _append_reason(reasons, "HOST_OCCUPANCY_BODY_INVALID")
    else:
        try:
            expected_status = host_occupancy.map_attribution_status(occupancy)
        except host_occupancy.HostOccupancyError:
            expected_status = None
            _append_reason(reasons, "HOST_OCCUPANCY_STATE_INVALID")
        if report.get("attribution_status") != expected_status:
            _append_reason(reasons, "HOST_OCCUPANCY_ATTRIBUTION_STATUS_MISMATCH")
    status = str(report.get("attribution_status") or "")
    if status not in selection_metric_v2.ATTRIBUTION_STATUSES:
        _append_reason(reasons, "HOST_OCCUPANCY_ATTRIBUTION_STATUS_INVALID")
    if report.get("requires_speaker_manual_review") is not (
        status == selection_metric_v2.ATTRIBUTION_UNVERIFIED
    ):
        _append_reason(reasons, "HOST_OCCUPANCY_REVIEW_FLAG_MISMATCH")
    return (None, reasons) if reasons else (report, [])


def _candidate_identity(
    candidate_id: str, row: Mapping[str, object]
) -> tuple[dict[str, object] | None, list[str]]:
    start_ms = row.get("start_ms")
    end_ms = row.get("end_ms")
    session_id = str(row.get("session_id") or "").strip()
    reasons: list[str] = []
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or end_ms <= start_ms
    ):
        _append_reason(reasons, "CANDIDATE_BOUNDARY_INVALID")
    if not session_id:
        _append_reason(reasons, "CANDIDATE_SESSION_ID_MISSING")
    if reasons:
        return None, reasons
    return {
        "candidate_id": candidate_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "session_id": session_id,
    }, []


def _v1_projection(
    scorecard: object, reasons: list[str]
) -> tuple[dict[str, object], str | None]:
    scorecard_hash = _hash_or_reason(
        scorecard, reasons, "SELECTION_SCORECARD_NON_CANONICAL"
    )
    if not selection_scorecard_is_valid(scorecard):
        _append_reason(reasons, "SELECTION_SCORECARD_MISSING_OR_INVALID")
        return {
            "status": "UNAVAILABLE",
            "tier": None,
            "raw_score": None,
            "effective_score": None,
            "selection_scorecard_sha256": scorecard_hash,
        }, scorecard_hash
    assert isinstance(scorecard, Mapping)
    return {
        "status": "VALID",
        "tier": scorecard.get("tier"),
        "raw_score": scorecard.get("raw_score"),
        "effective_score": scorecard.get("effective_score"),
        "selection_scorecard_sha256": scorecard_hash,
    }, scorecard_hash


def _session_topic_context(
    state: Mapping[str, object],
    rows: Sequence[tuple[str, Mapping[str, object], bool]],
) -> tuple[dict[str, dict[str, int]], set[str]]:
    session_candidates: dict[str, set[str]] = {}
    session_topics: dict[str, dict[str, str]] = {}
    for candidate_id, row, _conflict in rows:
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            continue
        session_candidates.setdefault(session_id, set()).add(candidate_id)
        topic, topic_reasons = _topic_input(_typed_input(state, candidate_id))
        if topic is not None and not topic_reasons:
            session_topics.setdefault(session_id, {})[candidate_id] = str(
                topic["fingerprint"]
            )

    counts: dict[str, dict[str, int]] = {}
    incomplete: set[str] = set()
    for session_id, candidate_ids in session_candidates.items():
        topics = session_topics.get(session_id, {})
        if set(topics) != candidate_ids:
            incomplete.add(session_id)
            continue
        session_counts: dict[str, int] = {}
        for fingerprint in topics.values():
            session_counts[fingerprint] = session_counts.get(fingerprint, 0) + 1
        counts[session_id] = session_counts
    return counts, incomplete


def _build_receipt(
    state: Mapping[str, object],
    *,
    candidate_id: str,
    row: Mapping[str, object],
    row_conflict: bool,
    topic_counts: Mapping[str, Mapping[str, int]],
    incomplete_topic_sessions: set[str],
) -> dict[str, object]:
    reasons: list[str] = []
    if PRODUCTION_INPUT_PRODUCER_STATUS != "WIRED_AND_HASH_BOUND":
        _append_reason(reasons, "PRODUCTION_TOPIC_HOST_INPUT_PRODUCERS_NOT_WIRED")
    if row_conflict:
        _append_reason(reasons, "CANDIDATE_METRIC_INPUT_CONFLICT")
    identity, identity_reasons = _candidate_identity(candidate_id, row)
    for reason in identity_reasons:
        _append_reason(reasons, reason)
    scorecard = row.get("selection_scorecard")
    metric_v1, scorecard_hash = _v1_projection(scorecard, reasons)

    raw_input = _typed_input(state, candidate_id)
    input_hash = (
        _hash_or_reason(raw_input, reasons, "SHADOW_INPUT_NON_CANONICAL")
        if raw_input is not None
        else None
    )
    if raw_input is None:
        _append_reason(reasons, "SHADOW_INPUT_MISSING")
    elif (
        raw_input.get("schema_version") != INPUT_SCHEMA_VERSION
        or str(raw_input.get("candidate_id") or "") != candidate_id
    ):
        _append_reason(reasons, "SHADOW_INPUT_ENVELOPE_INVALID")

    topic, topic_reasons = _topic_input(raw_input)
    for reason in topic_reasons:
        _append_reason(reasons, reason)
    topic_hash = (
        _hash_or_reason(topic, reasons, "TOPIC_INPUT_NON_CANONICAL")
        if topic is not None
        else None
    )

    start_ms = identity.get("start_ms") if identity else row.get("start_ms")
    end_ms = identity.get("end_ms") if identity else row.get("end_ms")
    host, host_reasons = _host_input(
        raw_input,
        candidate_id=candidate_id,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    for reason in host_reasons:
        _append_reason(reasons, reason)
    host_hash = (
        _hash_or_reason(host, reasons, "HOST_OCCUPANCY_INPUT_NON_CANONICAL")
        if host is not None
        else None
    )

    session_id = str(identity.get("session_id") or "") if identity else ""
    if session_id and session_id in incomplete_topic_sessions:
        _append_reason(reasons, "SESSION_TOPIC_COVERAGE_INCOMPLETE")
    candidate_hash = (
        _hash_or_reason(identity, reasons, "CANDIDATE_IDENTITY_NON_CANONICAL")
        if identity is not None
        else None
    )

    metric_v2: dict[str, object] | None = None
    if not reasons:
        assert identity is not None
        assert isinstance(scorecard, Mapping)
        assert topic is not None
        assert host is not None
        session_counts = topic_counts.get(session_id)
        if session_counts is None:
            _append_reason(reasons, "SESSION_TOPIC_COUNTS_UNAVAILABLE")
        else:
            try:
                metric_v2 = selection_metric_v2.evaluate_selection_metric_v2(
                    scorecard=scorecard,
                    attribution_status=str(host["attribution_status"]),
                    candidate_start_ms=int(identity["start_ms"]),
                    candidate_end_ms=int(identity["end_ms"]),
                    proof_atoms=(),
                    axis_uncertainty=None,
                    global_uncertainty=float(scorecard["uncertainty_penalty"]),
                    topic_fingerprint=str(topic["fingerprint"]),
                    session_fingerprint_counts=session_counts,
                    fatigue_label="",
                    model_version=str(host["estimator_version"]),
                )
            except (KeyError, TypeError, ValueError):
                metric_v2 = None
            if metric_v2 is None:
                _append_reason(reasons, "V2_EVALUATOR_REJECTED_INPUT")

    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "purpose": PURPOSE,
        "candidate_id": candidate_id,
        "status": "AVAILABLE" if metric_v2 is not None and not reasons else "UNAVAILABLE",
        "reason_codes": reasons,
        "metric_v1": metric_v1,
        "metric_v2": metric_v2,
        "observed_inputs": {
            "topic_fingerprint": topic.get("fingerprint") if topic else None,
            "topic_policy_version": topic.get("policy_version") if topic else None,
            "session_topic_occurrences": (
                topic_counts.get(session_id, {}).get(str(topic["fingerprint"]))
                if topic is not None
                else None
            ),
            "host_attribution_status": (
                host.get("attribution_status") if host is not None else None
            ),
        },
        "bindings": {
            "candidate_input_sha256": candidate_hash,
            "selection_scorecard_sha256": scorecard_hash,
            "shadow_input_sha256": input_hash,
            "topic_input_sha256": topic_hash,
            "host_occupancy_input_sha256": host_hash,
            "policy_sha256": POLICY_SHA256,
        },
        "decision_surfaces": {
            "decision_influence": False,
            "selection_authorized": False,
            "quota_authorized": False,
            "release_gate": False,
            "upload_authorized": False,
        },
    }
    return {**payload, "receipt_sha256": canonical_sha256(payload)}


def build_shadow_snapshot(state: Mapping[str, object]) -> dict[str, object]:
    """Build a deterministic v1/v2 snapshot without mutating ``state``."""

    rows = _collect_talk_rows(state)
    topic_counts, incomplete = _session_topic_context(state, rows)
    receipts = [
        _build_receipt(
            state,
            candidate_id=candidate_id,
            row=row,
            row_conflict=row_conflict,
            topic_counts=topic_counts,
            incomplete_topic_sessions=incomplete,
        )
        for candidate_id, row, row_conflict in rows
    ]
    available = sum(row.get("status") == "AVAILABLE" for row in receipts)
    payload: dict[str, object] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "purpose": PURPOSE,
        "policy": dict(BRIDGE_POLICY),
        "policy_sha256": POLICY_SHA256,
        "input_state_field": INPUT_STATE_FIELD,
        "candidate_collections": list(TALK_COLLECTIONS),
        "summary": {
            "candidate_count": len(receipts),
            "available_count": available,
            "unavailable_count": len(receipts) - available,
        },
        "candidates": receipts,
        "decision_surfaces": {
            "decision_influence": False,
            "selection_authorized": False,
            "quota_authorized": False,
            "release_gate": False,
            "upload_authorized": False,
        },
    }
    return {**payload, "snapshot_sha256": canonical_sha256(payload)}


def receipts_by_candidate(snapshot: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("candidate_id") or ""): row
        for row in rows
        if isinstance(row, Mapping) and str(row.get("candidate_id") or "")
    }
