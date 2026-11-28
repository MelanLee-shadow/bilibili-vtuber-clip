"""Canonical CPA transports for producer final subtitle review."""

from __future__ import annotations
from typing import TYPE_CHECKING

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager


if TYPE_CHECKING:
    from src.autoslice.finding_closure_shell import CanonicalCheckpoint, FinalReviewCall, Route
    from src.autoslice.provider_admission import ProviderController, RateFeedback


def closure_call_with_quota_fallback(
    router: FinalReviewCall, route: Route, prompt: str,
    admit: Callable[[ProviderController, Route], AbstractContextManager[Callable[[RateFeedback], None]]],
) -> str:
    from src.autoslice.cpa_model_ladder import CPA_QUOTA_FALLBACK_MODEL, is_definite_quota_exception
    try:
        with admit(router.controller, route):
            return router.raw_call(route, dispatch=True)(prompt)
    except Exception as exc:
        if not is_definite_quota_exception(exc) or route.model == CPA_QUOTA_FALLBACK_MODEL:
            raise
        fb = router.route(CPA_QUOTA_FALLBACK_MODEL)
        with admit(router.controller, fb):
            return router.raw_call(fb, dispatch=True)(prompt)


def closure_invoke_with_quota_fallback(
    router: FinalReviewCall, invoke: Callable[[], str], send: Callable[[Route], str],
    checkpoints: Mapping[str, CanonicalCheckpoint],
    admit: Callable[[ProviderController, Route], AbstractContextManager[Callable[[RateFeedback], None]]],
    wrap_completion: Callable[[str, str], str],
) -> tuple[str, None] | tuple[None, tuple[str, bool, CanonicalCheckpoint]]:
    from src.autoslice.cpa_model_ladder import CPA_QUOTA_FALLBACK_MODEL, is_definite_quota_exception
    try:
        return invoke(), None
    except Exception as exc:
        if not is_definite_quota_exception(exc):
            raise
        fb_route = router.route(CPA_QUOTA_FALLBACK_MODEL)
        with admit(router.controller, fb_route):
            fb_raw = send(fb_route)
        ans = wrap_completion(str(fb_raw), CPA_QUOTA_FALLBACK_MODEL)
        return None, (ans, bool(getattr(fb_raw, "ruling_cache_hit", False)), checkpoints[CPA_QUOTA_FALLBACK_MODEL])


