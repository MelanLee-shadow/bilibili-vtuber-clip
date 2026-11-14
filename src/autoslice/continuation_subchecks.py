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

from contextvars import ContextVar

_SESSION: ContextVar[dict[str, dict] | None] = ContextVar("continuation_cold_read_session", default=None)


def session_memo(kind: str) -> dict | None:
    session = _SESSION.get()
    return None if session is None else session.setdefault(kind, {})


def _session_validation_memo():
    from .validation_memo import SessionValidationMemo

    session = session_memo('validation_results')
    if session is None:
        return None
    if 'memo' not in session:
        session['memo'] = SessionValidationMemo()
    return session['memo']


def session_cached_validation(kind: str, *, cache_result, evict=True, named_blobs=False):
    """Reuse selected completed results with current content and dependencies.

    A typed blocker is still a blocker, not permission to resume or release.
    The original caller owns that decision and all its scope/binding checks.
    """
    from functools import wraps
    from .validation_memo import (validation_content_key,
                                  capture_validation_dependencies)

    def decorate(function):
        @wraps(function)
        def validate(*args, **kwargs):
            memo = _session_validation_memo()
            if memo is None:
                return function(*args, **kwargs)
            try:
                key = (kind, function, validation_content_key((args, kwargs)))
            except (TypeError, ValueError, RecursionError):
                return function(*args, **kwargs)
            hit, result = memo.recall(key)
            if hit:
                return result
            with capture_validation_dependencies() as dependencies:
                result = function(*args, **kwargs)
            if cache_result(result):
                memo.remember(key, result, tuple(dependencies.values()), evict=evict, named_blobs=named_blobs)
            return result
        return validate
    return decorate


