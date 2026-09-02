"""Small runtime heartbeat writer kept outside the orchestration module."""

from __future__ import annotations

import time
from pathlib import Path


def write_heartbeat(base: Path, body: str) -> None:
    path = base / "reports" / "heartbeat.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {body}\n", encoding="utf-8")
