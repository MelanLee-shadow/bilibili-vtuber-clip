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


