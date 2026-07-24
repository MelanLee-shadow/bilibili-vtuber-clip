"""Recorder chat-event clocks and payload extraction.

This module owns only acquisition-time normalization.  It deliberately does
not construct :class:`ChatEvidence` rows, so the evidence model can consume
these helpers without creating a dependency cycle.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


_SEGMENT_TIME = re.compile(
    r"(?P<date>20\d{6})[-_](?P<hour>\d{2})[-_](?P<minute>\d{2})[-_](?P<second>\d{2})"
)


def recording_start_epoch_ms(
    path: str | Path,
    *,
    timezone: str = "Asia/Shanghai",
) -> int | None:
    """Derive t=0 from RecordStartTime; China-wall-clock filename is fallback."""

    source = Path(path)
    meta_path = source.with_suffix(".meta.json")
    if meta_path.is_file():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            description = payload.get("description") if isinstance(payload, dict) else None
            value = description.get("RecordStartTime") if isinstance(description, dict) else None
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is not None:
                return int(parsed.timestamp() * 1000)
        except (OSError, TypeError, ValueError):
            pass
    match = _SEGMENT_TIME.search(source.stem)
    if match is None:
        return None
    value = (
        match.group("date") + match.group("hour") + match.group("minute") + match.group("second")
    )
    parsed = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo(timezone))
    return int(parsed.timestamp() * 1000)


def event_epoch_ms(value: object) -> int | None:
    """Normalize Bilibili second/millisecond epoch fields."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value >= 100_000_000_000:
        return int(value)
    if value >= 100_000_000:
        return int(float(value) * 1000)
    return int(value)


def send_time_ms(payload: dict, command: str = "") -> int | None:
    """Select the authoritative event clock for one recorder payload."""

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    # Prefer Bilibili event clocks over recorder-ingestion send_time.
    if command.startswith("DANMU_MSG"):
        info = payload.get("info") or data.get("info")
        if isinstance(info, list) and info and isinstance(info[0], list) and len(info[0]) > 4:
            event_ms = event_epoch_ms(info[0][4])
            if event_ms is not None:
                return event_ms
    if command.startswith("SUPER_CHAT_MESSAGE"):
        top_level = payload.get("send_time")
        # CN keeps ms here; JPN twins fall back to second-precision data fields.
        if isinstance(top_level, (int, float)) and top_level >= 100_000_000_000:
            return event_epoch_ms(top_level)
        for key in ("ts", "start_time", "send_time"):
            event_ms = event_epoch_ms(data.get(key))
            if event_ms is not None:
                return event_ms
    if command.startswith("GUARD_BUY"):
        return event_epoch_ms(data.get("start_time"))
    value = payload.get("send_time")
    if not isinstance(value, (int, float)):
        value = data.get("send_time")
    return event_epoch_ms(value)


def danmaku_text(payload: dict) -> str:
    """Extract the message surface from supported DANMU payload variants."""

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    info = payload.get("info") or data.get("info")
    if isinstance(info, list) and len(info) > 1 and isinstance(info[1], str):
        return info[1].strip()
    for key in ("message", "text", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
