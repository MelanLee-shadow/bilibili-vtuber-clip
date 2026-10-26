"""One-line producer stage timestamps for ``producer.log``.

The bounded supervisor and the free runner both capture the producer's
stdout/stderr as its log. Each event is one stderr line::

    PRODUCER_STAGE T03:13:51.408Z START exact_final_review.discovery n=1
    PRODUCER_STAGE T03:14:20.010Z END exact_final_review.discovery n=1 elapsed_s=28.602

Only fixed stage names, UTC ISO timestamps, a per-stage sequence number,
elapsed seconds and integer/enum-token fields are written: never credentials,
prompts, paths or subtitle text. A START without its END means that stage
raised (or the process was killed); no extra line is written on failure, so
the producer's own failure tail and its final JSON summary stay unchanged
(lines contain no braces). Logging failures are swallowed; stage results and
exceptions pass through untouched. This module makes no decision.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import re
import sys
import threading
import time
from typing import Any, TypeVar

PREFIX = "PRODUCER_STAGE"
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_counts: dict[str, int] = {}
_lock = threading.Lock()
T = TypeVar("T")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _field(value: object) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, str) and _TOKEN.fullmatch(value):
        return value
    return None  # Free text never reaches the log.


def emit(event: str, stage: str, **fields: object) -> None:
    try:
        parts = [PREFIX, _utc_now(), event, stage]
        for key in sorted(fields):
            rendered = _field(fields[key])
            if _TOKEN.fullmatch(key) and rendered is not None:
                parts.append(f"{key}={rendered}")
        sys.stderr.write(" ".join(parts) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - a log line can never change production.
        pass


@contextmanager
def producer_stage(stage: str, **fields: object) -> Iterator[None]:
    with _lock:
        _counts[stage] = number = _counts.get(stage, 0) + 1
    started = time.monotonic()
    emit("START", stage, n=number, **fields)
    yield
    emit("END", stage, n=number, elapsed_s=time.monotonic() - started, **fields)


def staged(stage: str, call: Callable[..., T]) -> Callable[..., T]:
    """Wrap one producer adapter call in START/END lines; behavior unchanged."""

    @wraps(call)
    def invoke(*args: Any, **kwargs: Any) -> T:
        with producer_stage(stage):
            return call(*args, **kwargs)

    return invoke
