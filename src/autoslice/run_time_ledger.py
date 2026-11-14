"""Offline, decision-neutral producer timing (stdlib only).

This module owns the source catalogue, interval rules and v1 schema. UTC/unix
timestamps are wall clock; audio ``duration_ms`` is *media* length, never latency.
Only explicit paired timestamps contribute to call-in-flight time. Missing ends
remain unknown (including process kills); cache hits are not provider calls.

``no_call_observed_s`` is the complement of the call interval union. It is NOT
CPU idle time. ``dead_time_s`` is the sum of gaps >= 300 s with no timed call,
completed leaf-stage/wait span, or observed artifact/stage-log write. Shorter
gaps are reported separately. This conservative operational indicator excludes
instrumented processing; untimed calls and overwritten mtimes limit certainty.
No threshold time is subtracted from a qualifying gap. All gaps remain in JSON.
Nested stages are reported individually but never summed into wall clock.

Future task-level instrumentation: append ``time-spans.jsonl`` records with
schema_version="run-time-span.v1", span_id, kind (stage/call/wait/write), name,
started_utc, ended_utc; calls also have modality (text/audio), status and optional
http_status/error_type. Emit a write as a zero-length paired timestamp. An open
span may omit ended_utc. Do not duplicate an existing exchange/Argon call here.
"""
from __future__ import annotations

from datetime import datetime
import math
import re


def timestamp(value: object) -> float | None:
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            result = float(value)
        else:
            # GNU date emits nanoseconds; Python 3.10 accepts at most six digits.
            iso = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)", r"\1", str(value))
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            result = dt.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


