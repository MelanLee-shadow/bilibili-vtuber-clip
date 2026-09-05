#!/usr/bin/env python3
"""Fail-closed, read-only audit for the recorder adapter's two JSON ledgers."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
from typing import Any


STATUS_SCHEMA = "recorder-neutral-status.v1"
STATE_SCHEMA = "bililive-recorder-adapter-state.v1"
AUDIT_SCHEMA = "record-health-audit.v1"
ROOM_ID = "22966160"
STATUS_MAX_AGE_SECONDS = 180
RELATIVE_PATH_RX = re.compile(
    rf"^20\d{{2}}-\d{{2}}-\d{{2}}/{re.escape(ROOM_ID)}_20\d{{6}}-\d{{2}}-\d{{2}}-\d{{2}}\.flv$"
)
CONNECTION_STUB_SCHEMA = "recording-connection-stub.v1"
TRUNCATED_SCHEMA = "recording-truncated-source-disposition.v1"
TRUNCATED_RECOVERED = "RECOVERED_TRUNCATED_RECORDING"
TRUNCATED_IGNORED = "IGNORED_TRUNCATED_RECONNECT_FRAGMENT"
CONNECTION_STUB_REASON = "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO"


class AuditError(ValueError):
    pass


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AuditError(f"{label} missing or not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"{label} unreadable: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"{label} must be a JSON object")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuditError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise AuditError(f"{label} must be a finite number")
    return result


def _relative_path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or not RELATIVE_PATH_RX.fullmatch(value):
        return ""
    return value


def _session_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _in_lookback(relative: str, *, now_epoch: float, lookback_hours: float) -> bool:
    try:
        date_name = relative.split("/", 1)[0]
        date = datetime.strptime(date_name, "%Y-%m-%d").date()
    except (ValueError, IndexError):
        return True
    cutoff = datetime.fromtimestamp(
        now_epoch - lookback_hours * 3600,
        tz=timezone.utc,
    ).date() - timedelta(days=1)
    return date >= cutoff


def _disposition_kind(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    if (
        row.get("schema_version") == CONNECTION_STUB_SCHEMA
        and row.get("status") == "IGNORED_CONNECTION_STUB"
        and row.get("reason_code") == CONNECTION_STUB_REASON
    ):
        return "ignored"
    if row.get("schema_version") != TRUNCATED_SCHEMA:
        return None
    action = row.get("action")
    if action == TRUNCATED_RECOVERED:
        return "recovered"
    if action == TRUNCATED_IGNORED:
        return "ignored"
    return None


def _inventory(
    state: dict[str, Any],
    *,
    status: dict[str, Any],
    now_epoch: float,
    lookback_hours: float,
) -> tuple[dict[str, list[str]], list[str]]:
    webhook = state.get("webhook_files")
    finalized = state.get("finalized")
    dispositions = state.get("source_dispositions")
    if not all(isinstance(value, dict) for value in (webhook, finalized, dispositions)):
        raise AuditError("adapter state ledgers are missing or malformed")

    inventory = {
        "open": [],
        "pending_active_session": [],
        "closed_unresolved": [],
        "finalized_unresolved": [],
        "disposition_unresolved": [],
    }
    unresolved: list[str] = []
    active_open_count = 0
    valid_open_sessions: list[tuple[str, str]] = []
    # Collect the live-session authority before walking sorted paths: older
    # CLOSED rows sort before the current OPEN row in normal recorder output.
    for relative, evidence in webhook.items():
        relative_text = _relative_path(relative)
        if (
            not relative_text
            or not _in_lookback(
                relative_text, now_epoch=now_epoch, lookback_hours=lookback_hours
            )
            or not isinstance(evidence, dict)
            or evidence.get("status") != "OPEN"
            or _disposition_kind(dispositions.get(relative)) == "ignored"
        ):
            continue
        active_open_count += 1
        session_id = _session_id(evidence.get("session_id"))
        if session_id is not None:
            valid_open_sessions.append((relative_text, session_id))
    for relative in sorted(set(webhook) | set(finalized) | set(dispositions)):
        relative_text = _relative_path(relative)
        if not relative_text:
            issue = f"invalid relative path: {relative}"
            inventory["disposition_unresolved"].append(issue)
            unresolved.append(issue)
            continue
        if not _in_lookback(
            relative_text,
            now_epoch=now_epoch,
            lookback_hours=lookback_hours,
        ):
            continue
        evidence = webhook.get(relative)
        ledger = finalized.get(relative)
        disposition = dispositions.get(relative)
        if ledger is not None and not isinstance(ledger, dict):
            inventory["finalized_unresolved"].append(relative_text)
            unresolved.append(f"finalized ledger row is malformed: {relative_text}")
        status_value = evidence.get("status") if isinstance(evidence, dict) else None
        disposition_kind = _disposition_kind(disposition)
        if disposition is not None and disposition_kind is None:
            inventory["disposition_unresolved"].append(relative_text)
            unresolved.append(f"invalid source disposition: {relative_text}")
        if relative not in webhook:
            issue = f"ledger has no webhook evidence: {relative_text}"
            inventory["finalized_unresolved" if ledger is not None else "disposition_unresolved"].append(
                relative_text
            )
            unresolved.append(issue)
            continue
        if status_value not in {"OPEN", "CLOSED"}:
            issue = f"webhook status is neither OPEN nor CLOSED: {relative_text}"
            inventory["closed_unresolved"].append(relative_text)
            unresolved.append(issue)
            continue
        if status_value == "OPEN":
            inventory["open"].append(relative_text)
            # The current recorder segment is expected to be OPEN while live.
            if disposition_kind == "ignored":
                unresolved.append(f"OPEN source has an ignored disposition: {relative_text}")
            else:
                session_id = _session_id(evidence.get("session_id"))
                if session_id is None:
                    unresolved.append(f"OPEN source has missing session_id: {relative_text}")
                if status.get("recording") is not True:
                    unresolved.append(f"OPEN source is not currently recording: {relative_text}")
            if ledger is not None or disposition_kind == "recovered":
                issue = f"OPEN source has a finalized binding: {relative_text}"
                inventory["finalized_unresolved"].append(relative_text)
                unresolved.append(issue)
            continue
        if disposition is None and ledger is None:
            closed_session_id = _session_id(evidence.get("session_id"))
            if (
                status.get("recording") is True
                and active_open_count == 1
                and len(valid_open_sessions) == 1
                and closed_session_id == valid_open_sessions[0][1]
            ):
                inventory["pending_active_session"].append(relative_text)
            else:
                inventory["closed_unresolved"].append(relative_text)
                unresolved.append(
                    f"CLOSED source lacks finalized or disposition binding: {relative_text}"
                )
                if status.get("recording") is True and len(valid_open_sessions) == 1:
                    if closed_session_id is None:
                        unresolved.append(f"CLOSED source has missing session_id: {relative_text}")
                    elif closed_session_id != valid_open_sessions[0][1]:
                        unresolved.append(
                            f"CLOSED source session_id does not match active OPEN session: {relative_text}"
                        )
        elif disposition_kind == "recovered" and ledger is None:
            inventory["closed_unresolved"].append(relative_text)
            unresolved.append(f"recovered disposition lacks finalized binding: {relative_text}")
        elif disposition_kind == "ignored" and ledger is not None:
            inventory["finalized_unresolved"].append(relative_text)
            unresolved.append(f"ignored disposition also has finalized binding: {relative_text}")

    recording = status.get("recording")
    if recording is True and active_open_count != 1:
        unresolved.append(
            f"recording=true but active OPEN source count is {active_open_count}"
        )
    if recording is True and len(valid_open_sessions) != 1:
        unresolved.append(
            f"recording=true but valid OPEN session count is {len(valid_open_sessions)}"
        )
    elif recording is False and active_open_count:
        unresolved.append(
            f"recording=false but active OPEN source count is {active_open_count}"
        )

    tasks = state.get("source_disposition_identity_rebind_tasks", {})
    if not isinstance(tasks, dict):
        raise AuditError("source disposition identity rebind tasks are malformed")
    for relative in sorted(tasks):
        relative_text = _relative_path(relative)
        if not relative_text:
            issue = f"invalid source disposition rebind task path: {relative}"
            inventory["disposition_unresolved"].append(issue)
            unresolved.append(issue)
        elif _in_lookback(
            relative_text,
            now_epoch=now_epoch,
            lookback_hours=lookback_hours,
        ):
            inventory["disposition_unresolved"].append(relative)
            unresolved.append(f"source disposition rebind task is pending: {relative_text}")
    for key in ("source_disposition_identity_rebinds",):
        value = state.get(key, {})
        if not isinstance(value, dict):
            raise AuditError(f"{key} is malformed")
    return inventory, sorted(set(unresolved))


def audit(
    *,
    status_path: Path,
    state_path: Path,
    lookback_hours: float,
    now_epoch: float | None = None,
    status_max_age_seconds: int = STATUS_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    now = time.time() if now_epoch is None else float(now_epoch)
    issues: list[str] = []
    status: dict[str, Any] = {}
    state: dict[str, Any] = {}
    state_age: float | None = None
    try:
        status = _read_object(status_path, "status.json")
        state = _read_object(state_path, "adapter-state.json")
        if status.get("schema_version") != STATUS_SCHEMA:
            raise AuditError("status schema mismatch")
        if str(status.get("room_id")) != ROOM_ID:
            raise AuditError("status room mismatch")
        generated = _number(status.get("generated_at_epoch"), "status.generated_at_epoch")
        age = now - generated
        if age < 0 or age > status_max_age_seconds:
            issues.append(f"status stale: {max(0, int(age))}s")
        if status.get("service_reachable") is not True:
            issues.append("recorder service is unreachable")
        if status.get("error") is not None:
            issues.append("status.error is not null")
        finalize_errors = status.get("finalize_errors")
        if not isinstance(finalize_errors, list):
            issues.append("status.finalize_errors is malformed")
            finalize_errors = []
        elif finalize_errors:
            issues.append("status.finalize_errors is non-empty")
        if status.get("recording") is not True and status.get("recording") is not False and status.get("recording") is not None:
            issues.append("status.recording is not boolean or null")
        if status.get("streaming") is not True and status.get("streaming") is not False and status.get("streaming") is not None:
            issues.append("status.streaming is not boolean or null")
        if state.get("schema_version") != STATE_SCHEMA:
            raise AuditError("adapter-state schema mismatch")
        last_room_status = _number(
            state.get("last_room_status_epoch"),
            "adapter-state.last_room_status_epoch",
        )
        state_age = now - last_room_status
        if state_age < 0 or state_age > status_max_age_seconds:
            issues.append(f"adapter-state stale: {max(0, int(state_age))}s")
        inventory, inventory_issues = _inventory(
            state,
            status=status,
            now_epoch=now,
            lookback_hours=lookback_hours,
        )
        issues.extend(inventory_issues)
    except AuditError as exc:
        issues.append(str(exc))
        age = None
        inventory = {
            "open": [],
            "pending_active_session": [],
            "closed_unresolved": [],
            "finalized_unresolved": [],
            "disposition_unresolved": [],
        }
        finalize_errors = []

    issues = sorted(set(issues))
    return {
        "schema_version": AUDIT_SCHEMA,
        "status": "PASS" if not issues else "FAIL",
        "healthy": not issues,
        "checked_at_epoch": int(now),
        "status_age_seconds": None if age is None else max(0, int(age)),
        "adapter_state_age_seconds": (
            None if state_age is None else max(0, int(state_age))
        ),
        "service_reachable": status.get("service_reachable"),
        "finalize_errors": len(finalize_errors),
        "inventory": inventory,
        "unresolved": issues,
        "lookback_hours": lookback_hours,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-hours", type=float, default=96.0)
    parser.add_argument(
        "--status-path",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_RECORD_HEALTH_STATUS_PATH", "/opt/bilive/recording/status.json")),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_RECORD_HEALTH_STATE_PATH", "/opt/bilive/recording/adapter-state.json")),
    )
    parser.add_argument("--now-epoch", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.lookback_hours) or args.lookback_hours <= 0:
        parser.error("--lookback-hours must be a positive finite number")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = audit(
        status_path=args.status_path,
        state_path=args.state_path,
        lookback_hours=args.lookback_hours,
        now_epoch=args.now_epoch,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
