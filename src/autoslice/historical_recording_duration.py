"""Resolve finalized recording duration without reopening cold CloudFS media."""

from __future__ import annotations

import json
import math
import re
import stat
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_MAX_ADAPTER_STATE_BYTES = 128 * 1024 * 1024


def _normalized_sha256(value: object) -> str | None:
    match = _SHA256_RX.fullmatch(str(value or "").strip().lower())
    return match.group(1) if match is not None else None


def _record_target_sha256(record: Mapping[str, object]) -> str | None:
    for key in ("source_media_sha256", "source_sha256"):
        if (normalized := _normalized_sha256(record.get(key))) is not None:
            return normalized
    relation = record.get("session_relation_authority")
    if not isinstance(relation, dict):
        return None
    if (normalized := _normalized_sha256(relation.get("bound_source_sha256"))) is not None:
        return normalized
    evidence = relation.get("evidence")
    if not isinstance(evidence, list):
        return None
    candidates = {
        normalized
        for row in evidence
        if isinstance(row, dict)
        and (
            row.get("source_alias_id") or row.get("evidence_class") == "HASH_BOUND_OFFICIAL_REPLAY"
        )
        if (normalized := _normalized_sha256(row.get("source_sha256"))) is not None
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _aware_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _duration_ms(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        duration = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not duration.is_finite() or duration <= 0:
        return None
    milliseconds = int(duration * 1000)
    return milliseconds if milliseconds > 0 else None


def _read_adapter_state(path: Path) -> dict[str, Any] | None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_size <= 0
        or path_stat.st_size > _MAX_ADAPTER_STATE_BYTES
    ):
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _event_is_bound(event_ids: Mapping[str, object], event_id: object, event_type: str) -> bool:
    row = event_ids.get(str(event_id or ""))
    return bool(
        isinstance(row, dict)
        and row.get("event_type") == event_type
        and _normalized_sha256(row.get("sha256")) is not None
    )


def _bound_duration_ms(
    state: Mapping[str, object],
    *,
    date: str,
    segment: Path,
    expected_target_sha256: str | None,
) -> int | None:
    webhook_files = state.get("webhook_files")
    finalized = state.get("finalized")
    event_ids = state.get("webhook_event_ids")
    if (
        state.get("schema_version") != "bililive-recorder-adapter-state.v1"
        or not isinstance(webhook_files, dict)
        or not isinstance(finalized, dict)
        or not isinstance(event_ids, dict)
    ):
        return 0
    relative = f"{date}/{segment.with_suffix('.flv').name}"
    webhook = webhook_files.get(relative)
    ledger = finalized.get(relative)
    if webhook is None and ledger is None:
        return None
    if not isinstance(webhook, dict) or not isinstance(ledger, dict):
        return 0

    duration_ms = _duration_ms(webhook.get("duration"))
    opened = _aware_datetime(webhook.get("file_open_time"))
    closed = _aware_datetime(webhook.get("file_close_time"))
    opening_event_id = webhook.get("opening_event_id")
    closing_event_id = webhook.get("closing_event_id")
    target_sha256 = _normalized_sha256(ledger.get("target_sha256"))
    expected_sha256 = _normalized_sha256(expected_target_sha256)
    try:
        webhook_size = int(webhook.get("file_size"))
        ledger_size = int(ledger.get("source_size"))
        source_mtime_ns = int(ledger.get("source_mtime_ns"))
    except (TypeError, ValueError):
        return 0
    target = str(ledger.get("target") or "").replace("\\", "/")
    expected_suffix = f"/{date}/{segment.name}"
    finalized_at = _aware_datetime(ledger.get("finalized_at"))
    wall_seconds = (
        (closed - opened).total_seconds() if opened is not None and closed is not None else math.nan
    )
    if (
        duration_ms is None
        or webhook.get("status") != "CLOSED"
        or not str(webhook.get("session_id") or "")
        or not opening_event_id
        or not closing_event_id
        or opening_event_id == closing_event_id
        or not _event_is_bound(event_ids, opening_event_id, "FileOpening")
        or not _event_is_bound(event_ids, closing_event_id, "FileClosed")
        or not math.isfinite(wall_seconds)
        or wall_seconds <= 0
        or duration_ms > int((wall_seconds + 60) * 1000)
        or webhook_size <= 0
        or webhook_size != ledger_size
        or source_mtime_ns <= 0
        or not target.endswith(expected_suffix)
        or target_sha256 is None
        or (expected_sha256 is not None and expected_sha256 != target_sha256)
        or finalized_at is None
    ):
        return 0
    return duration_ms


def resolve(runner: object, date: str, segment: Path, record: Mapping[str, object]) -> int:
    """Use an exact local adapter ledger, falling back only for ledger absence.

    A present-but-drifted ledger returns zero so the caller's existing duration
    gate fails closed. Only recordings with no adapter row use the legacy probe.
    """

    state_path = getattr(runner, "RECORDER_ADAPTER_STATE_PATH", None)
    if state_path is None:
        return runner.ffprobe_ms(segment)  # type: ignore[attr-defined]
    canonical_root = Path(
        getattr(runner, "CANONICAL_REC_ROOT", getattr(runner, "REC_ROOT"))
    )
    if segment.parent != canonical_root / date:
        return runner.ffprobe_ms(segment)  # type: ignore[attr-defined]
    state = _read_adapter_state(Path(state_path))
    if state is None:
        return 0
    duration_ms = _bound_duration_ms(
        state,
        date=date,
        segment=segment,
        expected_target_sha256=_record_target_sha256(record),
    )
    if duration_ms is None:
        return runner.ffprobe_ms(segment)  # type: ignore[attr-defined]
    return duration_ms
