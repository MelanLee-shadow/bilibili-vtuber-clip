"""One sub-check list for cold continuation authority, shared by producer and preflight.

The producer runs every ``check`` fail-fast: the first exception propagates
unchanged, so production behavior and reason codes are exactly those of the
inline validator.  The offline preflight enters :func:`audit` and the same
calls then record PASS/FAIL, reason code, elapsed time and bytes read for each
item, continue to the next independent item, and finally raise the first
failure.  The preflight therefore cannot drift from the producer's checks and
never reports a later item as passing when an earlier one failed.

``audit(collect=True)`` is the only stop-versus-collect switch, used solely by
the offline whole-path observation (``whole_path_observation``): a failed group
is recorded (one ``COLLECTED`` row) instead of raised, so the observed path
continues to its later stages.  Everything observed after such a row runs on
state the producer would have rejected and is reported as tainted, never as a
production prediction.  Outside ``audit`` (production) nothing changes.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import sys
import time
from typing import Any

_ROWS: ContextVar[list[dict[str, Any]] | None] = ContextVar("continuation_subcheck_rows", default=None)
_COLLECT: ContextVar[bool] = ContextVar("continuation_subcheck_collect", default=False)
_SESSION: ContextVar[dict[str, dict] | None] = ContextVar("continuation_cold_read_session", default=None)


@contextmanager
def cold_read_session():
    """One producer cold read of persisted authority.

    The same immutable persisted content is validated by several ingress
    validators (snapshot, source clock, current checkpoint, continuation).
    Inside one session a content-addressed replay that already passed is not
    recomputed; a failure is never remembered. Provider stages run outside
    (:func:`end_cold_read`), so a live review revalidates everything.
    """
    if _SESSION.get() is not None:
        yield
        return
    token = _SESSION.set({})
    try:
        yield
    finally:
        _SESSION.reset(token)


def cold_read_session_scope(function):
    """Run ``function`` inside one cold-read session (source stays introspectable)."""
    from functools import wraps

    @wraps(function)
    def scoped(*args, **kwargs):
        with cold_read_session():
            return function(*args, **kwargs)
    return scoped


def end_cold_read() -> None:
    """Leave the session for the rest of this stage (its owner restores on exit)."""
    if _SESSION.get() is not None:
        _SESSION.set(None)


def session_memo(kind: str) -> dict | None:
    session = _SESSION.get()
    return None if session is None else session.setdefault(kind, {})


def session_validated(kind: str):
    """Decorate a pure persisted-content validator that returns ``None``.

    Inside a cold-read session an identical argument set that already passed
    is not revalidated; outside a session, and for every failure, the
    validator runs exactly as before.
    """
    import hashlib
    import json
    from functools import wraps

    def decorate(function):
        @wraps(function)
        def validate(*args, **kwargs):
            memo = session_memo(kind)
            if memo is None:
                return function(*args, **kwargs)
            try:
                key = hashlib.sha256(json.dumps([args, kwargs], ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            except (TypeError, ValueError):
                return function(*args, **kwargs)
            if key in memo:
                return None
            result = function(*args, **kwargs)
            if result is None:
                memo[key] = True
            return result
        return validate
    return decorate


def _rchar() -> int | None:
    try:
        with open("/proc/self/io", "rb") as handle:
            for line in handle:
                if line.startswith(b"rchar:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _reason(exc: BaseException) -> str:
    code = getattr(exc, "reason_code", None)
    return str(code if code is not None else exc)[:300]


@contextmanager
def audit(*, collect: bool = False):
    """Record every sub-check of the enclosed continuation validations.

    ``collect=True`` (offline whole-path observation only) records a failed
    group instead of raising it; see the module docstring.
    """
    rows: list[dict[str, Any]] = []
    token = _ROWS.set(rows)
    collect_token = _COLLECT.set(bool(collect))
    try:
        yield rows
    finally:
        _COLLECT.reset(collect_token)
        _ROWS.reset(token)


def auditing() -> bool:
    return _ROWS.get() is not None


def collecting() -> bool:
    """True only inside ``audit(collect=True)``."""
    return _ROWS.get() is not None and _COLLECT.get()


class Group:
    """Sub-checks of one validator invocation (one cold-read authority)."""

    def __init__(self, validator: str, *, depth: int = 2) -> None:
        self.validator = validator
        self.first: BaseException | None = None
        self.rows = _ROWS.get()
        frame = sys._getframe(depth) if self.rows is not None else None
        self.call_site = f"{frame.f_code.co_name}:{frame.f_lineno}" if frame is not None else None

    @contextmanager
    def check(self, name: str):
        if self.rows is None:
            yield
            return
        started, rchar = time.perf_counter(), _rchar()
        row = {"validator": self.validator, "call_site": self.call_site, "check": name}
        try:
            yield
        except Exception as exc:  # recorded, re-raised by finish()
            row.update(status="FAIL", reason_code=_reason(exc), error_type=type(exc).__name__)
            if self.first is None:
                self.first = exc
        else:
            row["status"] = "PASS"
        finally:
            row.setdefault("status", "INTERRUPTED")  # a BaseException left this item
            after = _rchar()
            row["elapsed_s"] = round(time.perf_counter() - started, 4)
            row["rchar_bytes"] = (after - rchar) if after is not None and rchar is not None else None
            self.rows.append(row)

    def skip(self, name: str, reason: str) -> None:
        if self.rows is not None:
            self.rows.append({"validator": self.validator, "call_site": self.call_site,
                              "check": name, "status": "SKIP", "reason_code": reason,
                              "elapsed_s": 0.0, "rchar_bytes": 0})

    def finish(self) -> None:
        if self.first is None:
            return
        if collecting():
            # Observation only: keep the first failure as evidence and let the
            # caller continue on the state it would have rejected.
            self.rows.append({"validator": self.validator, "call_site": self.call_site,
                              "check": "<finish>", "status": "COLLECTED",
                              "reason_code": _reason(self.first),
                              "error_type": type(self.first).__name__,
                              "elapsed_s": 0.0, "rchar_bytes": 0})
            return
        raise self.first
