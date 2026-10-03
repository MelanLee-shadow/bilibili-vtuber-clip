"""Bounded ordinary-runner projection of the authorized publication queue."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def consume_tick(
    queue_module: Any,
    *,
    enabled: bool,
    blocked: bool,
    repository_root: Path,
    runtime_root: Path,
    log: Callable[[str], None],
) -> str:
    """Consume one eligible queue tick and return its heartbeat suffix."""

    if not enabled or blocked:
        return ""
    try:
        result = queue_module.consume_ready_publication_queue(
            repository_root=repository_root,
            runtime_root=runtime_root,
            enabled=True,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed and stay observable
        result = {
            "status": "QUEUE_EXCEPTION",
            "detail": f"{type(exc).__name__}: {exc}"[:400],
        }
    status = str(result.get("status") or "UNKNOWN")
    skipped = result.get("skipped_backoff")
    skipped_count = len(skipped) if isinstance(skipped, list) else 0
    suffix = f" publication_queue={status}"
    if skipped_count:
        suffix += f" publication_skipped_backoff={skipped_count}"
    if status not in {"NO_READY_ACTION", "ACTION_COMPLETED"} or skipped_count:
        log(f"publication queue: {status} {result}")
    return suffix


__all__ = ["consume_tick"]
