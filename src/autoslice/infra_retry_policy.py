"""何时重排一条"在等基础设施"的 talk 候选。

公开规则#9 逐字：「仅仅是一个CPA请求失败为什么会让整条候选判死…CPA
请求失败的逻辑是积极重试，而不是判候选死」。

"不判死"那一半 talk lane 早就有了：``provider_transient`` /
``runtime_prerequisite`` 属于 ``INFRASTRUCTURE_WAIT_FAILURE_KINDS``，永不落
``candidate_rejected``，到点必被 ``_talk_retry_decision`` 重排。缺的是**间隔**：
此前一律 ``SONG_INFRA_RETRY_BASE_SECONDS``（15 分钟）且不随次数增长，而一次
produce 要 15–55 分钟——于是一次多小时的上游故障里，每个候选都在"产完即刻重
产"，机时全烧在必然重复的失败上（free state 实测：
``provider_transient`` 一天 8 条 superseded 尝试）。

本模块只决定"等多久"，不决定"要不要等"——可恢复性仍归 ``talk_lane`` 的分类面，
重排资格仍归 ``delivery_recovery._talk_retry_decision``。曲线本身不新造一条：
直接调用歌 lane 已在生产验证的 ``song_infra_retry_delay_seconds``（15m → 30m →
… → 6h 上限），两个 lane 早已共用 ``SONG_INFRA_RETRY_*`` 常量。**这是放慢重试，
不是放弃重试**：没有终止上限，故障持续多久就等多久。
"""

from __future__ import annotations

import time
import json
import math
import random
import re
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping

from src.autoslice import provider_failure as _provider_failure
from src.autoslice.runner_proxy import RunnerProxy

_runner = RunnerProxy()


# The terminal judge can take several minutes for a large request.  A transport
# retry therefore belongs inside one logical judge call, where the existing
# CPA HTTP/attempt recorder can retain every physical attempt.  This budget is
# deliberately a wall-clock budget, not an attempt cap.  It is provisional
# until the <=30K production prompt is measured; two 600s calls plus a small
# backoff margin is the minimum useful local envelope.
TERMINAL_RETRY_DEFAULT_WALLCLOCK_SECONDS = 1_800.0
TERMINAL_RETRY_BASE_SECONDS = 2.0
TERMINAL_RETRY_MAX_SECONDS = 30.0
TERMINAL_RETRY_WALLCLOCK_EXHAUSTED = "TERMINAL_RETRY_WALLCLOCK_EXHAUSTED"

_terminal_retry_scope: ContextVar[dict[str, object] | None] = ContextVar(
    "terminal_retry_scope", default=None
)


# Administrative ruling, one-shot CPA TEXT requests with no
# response, or an explicit 429/503, may retry within the same call. Audio
# STARTED/UNKNOWN is governed by K22 and never enters here. Existing terminal
# wall-clock scopes keep their own policy instead of multiplying this budget.
CPA_TEXT_MAX_ATTEMPTS = 10
CPA_TEXT_IMMEDIATE_RETRIES = 1
CPA_TEXT_RETRY_BASE_SECONDS = 2.0
CPA_TEXT_RETRY_MAX_SECONDS = 60.0
CPA_TEXT_RETRY_JITTER_FRACTION = 0.20
CPA_TEXT_RETRY_EXHAUSTED = 'CPA_TEXT_RETRY_EXHAUSTED'
CPA_TEXT_TRANSIENT_HTTP_STATUSES = frozenset({429, 503})
CPA_TEXT_REJECTED_ERROR_CODES = frozenset({
    'content_filter', 'content_policy_violation', 'refusal', 'request_rejected',
    'invalid_request_error', 'safety',
})
CPA_TEXT_NO_RESPONSE_ERROR_CODES = frozenset({
    '', 'LLM_COMMAND_FAILED', 'LLM_COMMAND_TIMEOUT', 'CPA_COMMAND_ATTEMPT_FAILED',
    'CPA_WRAPPER_FAILED', 'SSH_RUNTIME_RESPONSE_INVALID',
})
_cpa_text_attempt: ContextVar[int | None] = ContextVar('cpa_text_attempt', default=None)


def cpa_text_attempt_environment() -> dict[str, str]:
    attempt = _cpa_text_attempt.get()
    return {} if attempt is None else {'AUTOSLICE_CPA_TEXT_ATTEMPT': str(attempt)}


def cpa_text_retry_active() -> bool:
    """Whether the current transport is inside the bounded text retry owner."""
    return _cpa_text_attempt.get() is not None


def cpa_text_failure_category(exc: BaseException) -> str | None:
    """Classify only the transport failures authorized for one-shot retry."""
    from .llm_client import LlmCallError

    diagnostics = getattr(exc, 'provider_diagnostics', {})
    if not isinstance(diagnostics, Mapping):
        diagnostics = {}
    status = diagnostics.get('provider_http_status')
    received = getattr(exc, 'response_received', None)
    if isinstance(exc, ConnectionResetError) and received is not True and status is None:
        return 'CONNECTION_RESET'
    if not isinstance(exc, LlmCallError):
        return None
    code = str(diagnostics.get('provider_error_code') or '')
    from .cpa_runtime import CPA_RESPONSE_LOCAL_FAILURE
    if exc.safe_reason == 'LLM_RESPONSE_LOCAL_FAILURE' or code == CPA_RESPONSE_LOCAL_FAILURE:
        return None
    if code.lower() in CPA_TEXT_REJECTED_ERROR_CODES:
        return None
    if getattr(exc, 'cpa_bridge_retryable', False):
        return 'BRIDGE_TRANSIENT'
    if status in CPA_TEXT_TRANSIENT_HTTP_STATUSES:
        return f'HTTP_{status}'
    if received is True:
        return None
    if isinstance(status, int) and 100 <= status <= 599:
        return None
    # An explicit refusal/contract response is not a response-less failure,
    # even when an old bridge omitted its HTTP status.
    if code not in CPA_TEXT_NO_RESPONSE_ERROR_CODES and not code.startswith('CURL_EXIT_'):
        return None
    if exc.safe_reason == 'LLM_COMMAND_TIMEOUT':
        return 'NO_RESPONSE_TIMEOUT'
    if isinstance(exc.__cause__, ConnectionResetError):
        return 'CONNECTION_RESET'
    if exc.safe_reason == 'LLM_COMMAND_FAILED':
        return 'NO_RESPONSE_COMMAND'
    # Native direct transport marks this only around an actual attempted I/O,
    # never configuration, JSON/schema or content validation failures.
    return 'NO_RESPONSE_TRANSPORT' if received is False else None


def cpa_text_failure_has_no_response(exc: BaseException) -> bool:
    """Legacy transport handoff name, now also covering explicit 429/503.

    The direct model ladder already uses this hook to yield to the unified
    owner. Keep that handoff for every newly retryable outcome, so its inner
    attempts do not multiply this policy's ten-attempt budget.
    """
    return cpa_text_failure_category(exc) is not None


def _cpa_text_provider_delay(exc: BaseException) -> float:
    """Read delay hints without retaining response bodies or error text."""
    from email.utils import parsedate_to_datetime
    from urllib.error import HTTPError

    delays = [0.0]

    def seconds(value):
        if isinstance(value, bool):
            return
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return
        if math.isfinite(number) and number >= 0:
            delays.append(number)

    def fields(value, depth=0):
        if not isinstance(value, Mapping) or depth > 4:
            return
        for key, item in value.items():
            if key in {'reset_seconds', 'provider_reset_seconds',
                       'retry_after_seconds', 'provider_retry_after_seconds'}:
                seconds(item)
            elif isinstance(item, Mapping):
                fields(item, depth + 1)

    diagnostics = getattr(exc, 'provider_diagnostics', {})
    if not isinstance(diagnostics, Mapping):
        diagnostics = {}
    fields(diagnostics)
    seconds(getattr(exc, 'retry_after_seconds', None))
    seconds(getattr(exc, 'reset_seconds', None))
    # Some bridges preserve CPA's reset_seconds only in their safe message.
    # Numeric extraction is closed; neither that message nor a body is logged.
    for text in (str(exc), str(diagnostics.get('provider_error_message') or '')):
        for match in re.finditer(
            r'''(?i)\b(?:reset_seconds|provider_reset_seconds|provider_retry_after_seconds|retry[-_]after)'''
            r'''["']?\s*[:=]\s*["']?([0-9]+(?:\.[0-9]+)?)(?![\w.])''', text
        ):
            seconds(match.group(1))
    # Native direct transport preserves HTTPError as its cause. Its header and
    # JSON error body remain available even when the safe diagnostic whitelist
    # cannot carry reset_seconds. Only the bounded failure body is consumed.
    cause = exc.__cause__
    if isinstance(cause, HTTPError):
        headers = cause.headers
        value = headers.get('Retry-After') if headers is not None else None
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            try:
                date = parsedate_to_datetime(str(value))
                if date.tzinfo is None:
                    from datetime import timezone
                    date = date.replace(tzinfo=timezone.utc)
                seconds(max(0.0, date.timestamp() - time.time()))
            except (TypeError, ValueError, OverflowError):
                pass
        else:
            seconds(numeric)
        try:
            fields(json.loads(cause.read(65536)))
        except (OSError, TypeError, ValueError, AttributeError):
            pass
    return max(delays)


def cpa_text_retry_delay_seconds(attempt: int, exc: BaseException, *, jitter=None) -> float:
    """Immediate first retry, then 2/4/8/... with jitter and a local 60s cap.

    Provider hints are lower bounds, including on the immediate retry. They
    may exceed the local cap and are never shortened by negative jitter.
    """
    local = 0.0
    if attempt > CPA_TEXT_IMMEDIATE_RETRIES:
        base = min(CPA_TEXT_RETRY_MAX_SECONDS,
                   CPA_TEXT_RETRY_BASE_SECONDS * 2 ** (attempt - CPA_TEXT_IMMEDIATE_RETRIES - 1))
        jitter = jitter or random.uniform
        local = min(CPA_TEXT_RETRY_MAX_SECONDS,
                    base * (1 + jitter(-CPA_TEXT_RETRY_JITTER_FRACTION,
                                       CPA_TEXT_RETRY_JITTER_FRACTION)))
    return max(local, _cpa_text_provider_delay(exc))


def cpa_text_retry_call(call, *, record, sleeper=None, clock=None, jitter=None):
    """Consume the successful response; append every unknown attempt first.

    ``record`` must retain the row durably before another dispatch. It receives
    only closed metadata, not prompts, exception messages or credentials.
    The public text transport supplies the existing CPA usage recorder.
    """
    if terminal_retry_state() is not None:
        return call()
    sleeper = sleeper or time.sleep
    clock = clock or time.monotonic
    waited = 0.0
    for attempt in range(1, CPA_TEXT_MAX_ATTEMPTS + 1):
        try:
            token = _cpa_text_attempt.set(attempt)
            try:
                return call()
            finally:
                _cpa_text_attempt.reset(token)
        except Exception as exc:
            category = cpa_text_failure_category(exc)
            if category is None:
                raise
            exc.dispatch_outcome = 'UNKNOWN'
            retry_scheduled = attempt < CPA_TEXT_MAX_ATTEMPTS
            delay = cpa_text_retry_delay_seconds(attempt, exc, jitter=jitter) if retry_scheduled else None
            original_reason = getattr(exc, 'safe_reason', None) or category
            record({'kind': 'text', 'status': 'UNKNOWN', 'attempt': attempt,
                    'max_attempts': CPA_TEXT_MAX_ATTEMPTS,
                    'reason_code': original_reason if retry_scheduled else CPA_TEXT_RETRY_EXHAUSTED,
                    'transport_reason_code': original_reason, 'error_category': category,
                    'waited_seconds': waited, 'retry_scheduled': retry_scheduled,
                    'delay_seconds': delay})
            if not retry_scheduled:
                # Preserve the transport exception type, safe_reason and closed
                # diagnostics consumed by the runner's infrastructure-wait lane.
                exc.cpa_text_retry_reason = CPA_TEXT_RETRY_EXHAUSTED
                exc.cpa_text_retry = {'reason': CPA_TEXT_RETRY_EXHAUSTED,
                    'attempts': attempt, 'max_attempts': CPA_TEXT_MAX_ATTEMPTS,
                    'error_category': category, 'retryable': True}
                if CPA_TEXT_RETRY_EXHAUSTED not in str(exc):
                    exc.args = (f'{exc}: {CPA_TEXT_RETRY_EXHAUSTED}', *exc.args[1:])
                raise
            started = clock()
            sleeper(delay)
            waited = max(0.0, clock() - started)


SONG_INFRA_TRANSIENT_REASON_CODES = frozenset(
    {
        "AGY_SOURCE_CONTEXT_RUNNER_FAILED",
        "AGY_QUOTA_EXHAUSTED",
        "AGY_EMPTY_OUTPUT",
        "AGY_FAILED_RC",
        "AGY_TIMEOUT",
        "AGY_AND_GEMINI_API_FAILED",
        "JINGTING_PROVENANCE_MISSING",
        "JINGTING_PROVIDER_NOT_AGY",
        "JINGTING_AGY_FAILED",
        "JINGTING_MODEL_MISSING",
        "JINGTING_PROVIDER_FALLBACK_USED",
        "JINGTING_PROVIDER_FALLBACK_UNKNOWN",
        "SONG_WINDOW_CUT_FAILED",
        "SONG_SOURCE_TRANSCRIPT_EMPTY",
        "CPA_RATE_LIMITED",
        "CPA_MODEL_DOWN",
        "CPA_UPSTREAM_5XX",
        "CPA_UPSTREAM_TIMEOUT",
    }
)


def _bounded_retry_delay_seconds(completed_retry_count: int, *,
                                 base_seconds: int = 15 * 60,
                                 maximum_seconds: int = 6 * 60 * 60) -> int:
    """The shared infrastructure curve, also usable outside the runner."""
    exponent = min(32, max(0, int(completed_retry_count)))
    return min(maximum_seconds, base_seconds * (2**exponent))


def terminal_retry_delay_seconds(
    completed_retry_count: int,
    *,
    retry_after_seconds: float | int | None = None,
) -> float:
    """Return the existing exponential transport backoff plus provider signal.

    The retry curve is the same bounded exponential helper already used by
    infrastructure retries, with the CPA wrapper's 2s/30s transport values.
    A provider ``Retry-After`` is a lower bound supplied by the service and
    therefore wins over the local curve when it is longer.
    """

    delay = float(
        _bounded_retry_delay_seconds(
            completed_retry_count,
            base_seconds=int(TERMINAL_RETRY_BASE_SECONDS),
            maximum_seconds=int(TERMINAL_RETRY_MAX_SECONDS),
        )
    )
    try:
        provider_delay = float(retry_after_seconds)
    except (TypeError, ValueError):
        provider_delay = 0.0
    if math.isfinite(provider_delay) and provider_delay > delay:
        delay = provider_delay
    return delay


def terminal_retry_state() -> dict[str, object] | None:
    """Return the active terminal retry state for the current logical call."""

    return _terminal_retry_scope.get()


def terminal_retry_remaining_seconds() -> float | None:
    """Return the current terminal budget, or ``None`` outside a scope.

    Provider adapters call this at each physical transport boundary.  Keeping
    the clock in the scope (rather than copying a value into a closure) makes
    slot waits and format retries consume the same logical wall-clock budget.
    """

    state = terminal_retry_state()
    if state is None:
        return None
    remaining = float(state["deadline_monotonic"]) - float(state["clock"]())
    return max(0.0, remaining)


@contextmanager
def terminal_retry_scope(
    *,
    wallclock_seconds: float | int | None = None,
    clock=None,
    sleeper=None,
):
    """Install one bounded wall-clock retry scope for a terminal judge call."""

    if wallclock_seconds is None:
        wallclock_seconds = TERMINAL_RETRY_DEFAULT_WALLCLOCK_SECONDS
    if isinstance(wallclock_seconds, bool) or float(wallclock_seconds) < 0:
        raise ValueError("terminal retry wall-clock budget must be non-negative")
    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    started = float(clock())
    state: dict[str, object] = {
        "started_monotonic": started,
        "deadline_monotonic": started + float(wallclock_seconds),
        "wallclock_seconds": float(wallclock_seconds),
        "clock": clock,
        "sleeper": sleeper,
        "attempts": 0,
        "retries": 0,
        "unknown_attempts": 0,
        "events": [],
    }
    token = _terminal_retry_scope.set(state)
    try:
        yield state
    finally:
        _terminal_retry_scope.reset(token)


def terminal_failure_is_transient(exc: BaseException) -> bool:
    """Use the repository's provider classifier for terminal transport retry."""

    # Import lazily: provider_failure imports LlmCallError from llm_client,
    # while llm_client imports this module only when a terminal call runs.
    from src.autoslice import provider_failure

    # The provider classifier consumes provider text, never an arbitrary code
    # or validation exception whose repr may contain a saved timeout/UNKNOWN
    # from the supplied materials. Such a substring must not turn a failing
    # assertion or the semantic recorder's non-dispatch gate into inference.
    names = {type(exc).__name__, type(getattr(exc, '__cause__', None)).__name__}
    if not names.intersection(provider_failure.TRANSPORT_EXCEPTION_NAMES):
        return False

    diagnostics = getattr(exc, "provider_diagnostics", {})
    if not isinstance(diagnostics, Mapping):
        diagnostics = {}
    status = diagnostics.get("provider_http_status")
    error_code = str(diagnostics.get("provider_error_code") or "")
    from .cpa_runtime import CPA_RESPONSE_LOCAL_FAILURE
    if getattr(exc, 'safe_reason', None) == 'LLM_RESPONSE_LOCAL_FAILURE' or error_code == CPA_RESPONSE_LOCAL_FAILURE:
        return False
    if error_code == CPA_TEXT_RETRY_EXHAUSTED:
        return False
    if getattr(exc, 'cpa_bridge_retryable', False):
        return True
    text = " ".join(
        value
        for value in (
            type(exc).__name__,
            str(exc),
            str(getattr(exc, "safe_reason", "") or ""),
            type(getattr(exc, "__cause__", None)).__name__
            if getattr(exc, "__cause__", None) is not None
            else "",
            json.dumps(dict(diagnostics), ensure_ascii=False, sort_keys=True),
            f"HTTP {status}" if isinstance(status, int) else "",
        )
        if value
    )
    # A response which explicitly says that the request was rejected must not
    # be reopened by the broad historical ``auth_unavailable`` service marker.
    # Keep the canonical 400 group-capability routing exception (it is already
    # classified as SERVICE), while treating ordinary auth 4xx responses as
    # deterministic.  408/429 continue through the canonical service/quota
    # branches below.
    lowered = text.lower()
    if status in {401, 403} or (
        status == 400
        and "auth_unavailable" in lowered
    ):
        return False
    if "OUTCOME_UNKNOWN" in text or getattr(exc, "dispatch_outcome", None) == "UNKNOWN":
        return True
    classification = provider_failure.classify_provider_failure(text)
    if classification in {
        provider_failure.SERVICE,
        provider_failure.QUOTA,
    }:
        return True
    if classification == provider_failure.REJECTED:
        return False
    if error_code.startswith("CURL_EXIT_"):
        # The existing CPA bridge uses curl's transport exit codes for a
        # response-less connection failure; HTTP 4xx failures carry a status
        # and were already rejected above.
        return True
    upper_error_code = error_code.upper()
    if any(token in upper_error_code for token in ("TIMEOUT", "CONNECTION", "DISCONNECT")):
        return True
    transient_safe_reasons = {
        "LLM_COMMAND_TIMEOUT",
        "LLM_PROVIDER_CAPACITY_TIMEOUT",
        "LLM_PROVIDER_CAPACITY_UNAVAILABLE",
    }
    if getattr(exc, "safe_reason", None) in transient_safe_reasons:
        return True
    # Connection/timeout exception names are already the repository's closed
    # transport set; do not create another parallel list here.
    transport_names = provider_failure.TRANSPORT_EXCEPTION_NAMES - {"LlmCallError"}
    return bool(
        {type(exc).__name__, type(getattr(exc, "__cause__", None)).__name__}
        & transport_names
    )


def _retry_after_from_exception(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    if value is None:
        diagnostics = getattr(exc, "provider_diagnostics", {})
        if isinstance(diagnostics, Mapping):
            value = diagnostics.get("provider_retry_after_seconds")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _mark_terminal_retry_failure(
    exc: BaseException,
    *,
    reason: str,
    state: Mapping[str, object],
) -> None:
    details = {
        "reason": reason,
        "attempts": state.get("attempts", 0),
        "retries": state.get("retries", 0),
        "unknown_attempts": state.get("unknown_attempts", 0),
        "events": list(state.get("events") or []),
    }
    setattr(exc, "terminal_retry_reason", reason)
    setattr(exc, "terminal_retry", details)
    # Keep the explicit exhaustion reason visible to callers that currently
    # persist only ``str(exc)`` while preserving the original exception type.
    if reason not in str(exc):
        args = getattr(exc, "args", ())
        exc.args = (f"{reason}: {exc}", *args[1:])


def terminal_retry_call(call, *, timeout_seconds: float | int | None = None):
    """Run one provider call with transient retry inside the active scope."""

    state = terminal_retry_state()
    if state is None:
        return call()
    last_exc: BaseException | None = None
    while True:
        clock = state["clock"]
        now = float(clock())
        if last_exc is not None and now >= float(state["deadline_monotonic"]):
            _mark_terminal_retry_failure(
                last_exc, reason=TERMINAL_RETRY_WALLCLOCK_EXHAUSTED, state=state
            )
            raise last_exc
        if last_exc is not None and timeout_seconds is not None:
            remaining = float(state["deadline_monotonic"]) - now
            if remaining < float(timeout_seconds):
                _mark_terminal_retry_failure(
                    last_exc, reason=TERMINAL_RETRY_WALLCLOCK_EXHAUSTED, state=state
                )
                raise last_exc
        state["attempts"] = int(state["attempts"]) + 1
        try:
            result = call()
        except Exception as exc:
            last_exc = exc
            if "OUTCOME_UNKNOWN" in str(exc) or getattr(exc, "dispatch_outcome", None) == "UNKNOWN":
                state["unknown_attempts"] = int(state["unknown_attempts"]) + 1
            if not terminal_failure_is_transient(exc):
                state["events"].append({
                    "attempt": state["attempts"],
                    "status": "PERMANENT_FAILURE",
                    "error_type": type(exc).__name__,
                })
                raise
            if int(state.get('cpa_bridge_attempts', 0)) >= CPA_TEXT_MAX_ATTEMPTS:
                _mark_terminal_retry_failure(exc, reason=CPA_TEXT_RETRY_EXHAUSTED, state=state)
                raise
            retry_after = _retry_after_from_exception(exc)
            delay = terminal_retry_delay_seconds(
                int(state["retries"]), retry_after_seconds=retry_after
            )
            remaining = float(state["deadline_monotonic"]) - float(clock())
            if remaining <= delay:
                state["events"].append({
                    "attempt": state["attempts"],
                    "status": "WALLCLOCK_EXHAUSTED",
                    "error_type": type(exc).__name__,
                    "retry_after_seconds": retry_after,
                    "delay_seconds": delay,
                })
                _mark_terminal_retry_failure(
                    exc, reason=TERMINAL_RETRY_WALLCLOCK_EXHAUSTED, state=state
                )
                raise
            state["events"].append({
                "attempt": state["attempts"],
                "status": "RETRY_SCHEDULED",
                "error_type": type(exc).__name__,
                "retry_after_seconds": retry_after,
                "delay_seconds": delay,
            })
            state["retries"] = int(state["retries"]) + 1
            state["sleeper"](delay)
        else:
            state["events"].append({
                "attempt": state["attempts"], "status": "COMPLETED"
            })
            return result


def talk_infra_retry_delay_seconds(result: Mapping[str, object]) -> int:
    """这次失败之后应当等待的秒数。

    配额类失败直接从下一档起步：配额窗口按小时/天计（AGY 周配额尤甚），用 15
    分钟去撞一个还要几小时才开的窗口只是纯浪费一次整条重产。
    """

    completed_retries = int(result.get("talk_transient_retry_count") or 0)
    if result.get("failure_provider_class") == _provider_failure.QUOTA:
        completed_retries += 1
    return int(_runner.song_infra_retry_delay_seconds(completed_retries))


def talk_infra_retry_schedule(result: Mapping[str, object]) -> dict[str, object]:
    """``result`` 上要落盘的重排时刻三件套。

    ``next_retry_at_epoch`` 是 ``_talk_retry_decision`` 与
    ``scheduled_talk_retry_epoch`` 读的那一个字段；另外两个是给人看的。
    """

    delay = talk_infra_retry_delay_seconds(result)
    epoch = int(time.time()) + delay
    return {
        "retry_after_seconds": delay,
        "next_retry_at_epoch": epoch,
        "next_retry_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
    }


__all__ = [
    "CPA_TEXT_MAX_ATTEMPTS",
    "CPA_TEXT_IMMEDIATE_RETRIES",
    "CPA_TEXT_RETRY_BASE_SECONDS",
    "CPA_TEXT_RETRY_MAX_SECONDS",
    "CPA_TEXT_RETRY_JITTER_FRACTION",
    "CPA_TEXT_RETRY_EXHAUSTED",
    "CPA_TEXT_TRANSIENT_HTTP_STATUSES",
    "CPA_TEXT_NO_RESPONSE_ERROR_CODES",
    "cpa_text_failure_category",
    "cpa_text_retry_active",
    "cpa_text_failure_has_no_response",
    "cpa_text_retry_delay_seconds",
    "cpa_text_retry_call",
    "TERMINAL_RETRY_DEFAULT_WALLCLOCK_SECONDS",
    "TERMINAL_RETRY_WALLCLOCK_EXHAUSTED",
    "talk_infra_retry_delay_seconds",
    "talk_infra_retry_schedule",
    "terminal_failure_is_transient",
    "terminal_retry_call",
    "terminal_retry_delay_seconds",
    "terminal_retry_remaining_seconds",
    "terminal_retry_scope",
    "terminal_retry_state",
]
