"""Append-only local runner alerts."""

from __future__ import annotations

import time
from pathlib import Path


def write_alert(base: Path, name: str, message: str) -> None:
    path = base / "reports" / f"ALERT_{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as sink:
        sink.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
