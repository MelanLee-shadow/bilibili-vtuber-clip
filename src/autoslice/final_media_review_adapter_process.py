"""Durable record of one final-media adapter process ending.

The adapter's own stderr is the only witness of an ending that left no
response; 41:1648 keeps such a dispatch UNKNOWN and its failure on the books,
so the runner persists this record create-only instead of discarding it.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

SCHEMA_VERSION = "final-media-review-model-capability-sentinel-adapter-process.v1"
_SECRET_ENV_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
_MAX_TAIL_CHARS = 16_384


def captured_tail(value: object, environment: Mapping[str, str]) -> str | None:
    """Bounded text of one captured stream with env secret values redacted."""

    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = value if isinstance(value, str) else str(value)
    for name, secret in environment.items():
        if isinstance(secret, str) and len(secret) >= 8 and _SECRET_ENV_NAME_RE.search(name):
            text = text.replace(secret, f"<redacted:{name}>")
    return text[-_MAX_TAIL_CHARS:]


def adapter_process_record(
    response_path: Path,
    *,
    run_root: Path,
    attempt: int,
    environment: Mapping[str, str],
    dispatched_at: str,
    elapsed_seconds: float,
    returncode: int | None = None,
    timed_out: bool = False,
    spawn_error: BaseException | None = None,
    stdout: object = None,
    stderr: object = None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "attempt": attempt,
        "response_path": response_path.relative_to(run_root).as_posix(),
        "response_present": response_path.is_file() and not response_path.is_symlink(),
        "dispatched_at": dispatched_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "returncode": returncode,
        "timed_out": timed_out,
        "spawn_error": None if spawn_error is None else {
            "type": type(spawn_error).__name__,
            "message": captured_tail(str(spawn_error), environment),
        },
        "stdout_tail": captured_tail(stdout, environment),
        "stderr_tail": captured_tail(stderr, environment),
    }
