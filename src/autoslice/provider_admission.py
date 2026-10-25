"""Shared provider admission, independent of finding closure and ruling policy.

Public API: ProviderRoute is the structural (provider, modality) port; Route,
ProviderWindow and RateFeedback are frozen values. ProviderController.acquire /
finish implement AIMD per provider/modality (initial window 20, no maximum).
provider_admission(provider, modality, *, controller=None) yields a report hook
for structured provider feedback; nested owners share one lease. FinalReviewCall
wraps an existing callable with admission only, forwarding metadata and original
return values. It adds no model route, retry, hedge, cache or verdict. Existing
checkpoint owners still perform single-flight before reaching this callable.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import update_wrapper
import math
from threading import Condition
import time
from typing import Literal, Protocol, cast


class ProviderAdmissionError(ValueError):
    """Invalid admission bookkeeping or provider feedback."""


class ProviderRoute(Protocol):
    @property
    def provider(self) -> str: ...
    @property
    def modality(self) -> Literal['text', 'audio']: ...


@dataclass(frozen=True)
class Route:
    provider: str
    modality: Literal['text', 'audio']


@dataclass(frozen=True)
class ProviderWindow:
    limit: int = 20
    inflight: int = 0
    retry_at: float = 0.0


@dataclass(frozen=True)
class RateFeedback:
    rate_limited: bool = False
    retry_after: float = 0.0
    successful: bool = True


class ProviderController:
    """AIMD admission per (provider, modality); no fixed maximum."""
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.condition = Condition()
        self.windows: dict[tuple[str, str], ProviderWindow] = {}

    def acquire(self, route: ProviderRoute) -> None:
        key = route.provider, route.modality
        with self.condition:
            while True:
                window = self.windows.get(key, ProviderWindow())
                remaining = window.retry_at - self.clock()
                if window.inflight < window.limit and remaining <= 0:
                    self.windows[key] = ProviderWindow(window.limit, window.inflight + 1, window.retry_at)
                    return
                self.condition.wait(timeout=max(0.001, remaining) if remaining > 0 else None)

    def finish(self, route: ProviderRoute, feedback: RateFeedback) -> None:
        if not math.isfinite(feedback.retry_after) or feedback.retry_after < 0:
            raise ProviderAdmissionError('PROVIDER_ADMISSION_RETRY_AFTER_INVALID')
        key = route.provider, route.modality
        with self.condition:
            old = self.windows[key]
            if old.inflight < 1:
                raise ProviderAdmissionError('PROVIDER_ADMISSION_INFLIGHT_DRIFT')
            limit = max(1, old.limit // 2) if feedback.rate_limited else old.limit + int(feedback.successful)
            retry_at = max(old.retry_at, self.clock() + feedback.retry_after) if feedback.rate_limited else old.retry_at
            self.windows[key] = ProviderWindow(limit, old.inflight - 1, retry_at)
            self.condition.notify_all()


def _get(value: object, key: str, default: object = None) -> object:
    return cast(object, value.get(key, default)) if isinstance(value, Mapping) else default


def _seconds(value: object) -> float:
    seconds = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    return seconds if math.isfinite(seconds) and seconds >= 0 else 0.0


def observed_rate_feedback(value: object) -> RateFeedback:
    """Consume structured status/quota/Retry-After fields, never error text."""
    children: list[RateFeedback] = []
    if isinstance(value, (list, tuple)):
        children = [observed_rate_feedback(child) for child in value]
    elif isinstance(value, Mapping):
        children = [observed_rate_feedback(child) for child in value.values()
                    if isinstance(child, (Mapping, list, tuple))]
        code = _get(value, 'provider_http_status', _get(value, 'http_status'))
        quota = _get(value, 'provider_error_code', _get(value, 'error_code'))
        limited = code in (429, '429') or _get(value, 'quota_exhausted') is True or (
            isinstance(quota, str) and quota in {
                'insufficient_quota', 'quota_exhausted', 'resource_exhausted', 'RESOURCE_EXHAUSTED'})
        after = _get(value, 'provider_retry_after_seconds',
                     _get(value, 'retry_after_seconds', _get(value, 'retry_after', 0.0)))
        children.append(RateFeedback(limited, _seconds(after)))
    return RateFeedback(any(child.rate_limited for child in children),
                        max((child.retry_after for child in children), default=0.0))


def rate_feedback(error: BaseException | None) -> RateFeedback:
    signals = observed_rate_feedback((
        getattr(error, 'provider_diagnostics', None), getattr(error, 'metadata', None),
        {'http_status': getattr(error, 'http_status', None),
         'quota_exhausted': getattr(error, 'quota_exhausted', False),
         'retry_after': getattr(error, 'retry_after', None)},
        {'retry_after_seconds': getattr(error, 'retry_after_seconds', None)},
    ))
    return RateFeedback(signals.rate_limited, signals.retry_after, error is None)


PROVIDER_CONTROLLER = ProviderController()
Report = Callable[[object], None]
_ADMITTED_PROVIDERS: ContextVar[tuple[tuple[ProviderController, str, str, Report], ...]] = ContextVar(
    'provider_admission_leases', default=())


@contextmanager
def provider_admission(provider: str, modality: Literal['text', 'audio'], *,
                       controller: ProviderController | None = None) -> Iterator[Report]:
    """One shared lease for nested acquisition owners; no dispatch or retry."""
    owner = PROVIDER_CONTROLLER if controller is None else controller
    for held_owner, held_provider, held_modality, held_report in _ADMITTED_PROVIDERS.get():
        if owner is held_owner and (provider, modality) == (held_provider, held_modality):
            yield held_report
            return
    feedback = RateFeedback()

    def report(value: object) -> None:
        nonlocal feedback
        fresh = observed_rate_feedback(value)
        feedback = RateFeedback(feedback.rate_limited or fresh.rate_limited,
                                max(feedback.retry_after, fresh.retry_after), feedback.successful)

    route = Route(provider, modality)
    owner.acquire(route)
    token = _ADMITTED_PROVIDERS.set((*_ADMITTED_PROVIDERS.get(), (owner, provider, modality, report)))
    error: BaseException | None = None
    try:
        yield report
    except BaseException as exc:
        error = exc
        raise
    finally:
        _ADMITTED_PROVIDERS.reset(token)
        failed = rate_feedback(error)
        owner.finish(route, RateFeedback(feedback.rate_limited or failed.rate_limited,
            max(feedback.retry_after, failed.retry_after), error is None))


class FinalReviewCall:
    """Transparent admission adapter for the main factory's existing transport.

    update_wrapper preserves the transport's module and closure introspection,
    including CompletedReviewCheckpoint.for_call's existing binding/decline path.
    No canonical metadata is invented for an unidentified legacy callable.
    """
    def __init__(self, primary: Callable[[str], str], *, provider: str,
                 controller: ProviderController | None = None) -> None:
        update_wrapper(self, primary)
        self.primary = primary
        self.provider = provider
        self.controller = PROVIDER_CONTROLLER if controller is None else controller

    def __getattr__(self, name: str) -> object:
        return cast(object, getattr(self.primary, name))

    def __call__(self, prompt: str) -> str:
        with provider_admission(self.provider, 'text', controller=self.controller):
            return self.primary(prompt)
