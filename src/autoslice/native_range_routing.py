"""Classify native range failures without granting evidence or retry authority."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from threading import Lock
import json

from src.autoslice.discovery_channels import Json, JsonObject
from src.autoslice.local_asr_target_evidence import MODELS as NATIVE_MODELS

_Row = TypeVar('_Row', bound=Mapping[str, Json])
_Check = TypeVar('_Check')
_Range = TypeVar('_Range')


# Only these configuration failures precede provider-attempt reservation.
_NATIVE_RUNTIME_CONFIG_REASONS = frozenset(
    {
        "NATIVE_AUDIO_RUNTIME_MISSING",
        "NATIVE_AUDIO_RUNTIME_UNSAFE",
        "NATIVE_AUDIO_RUNTIME_CONFIG_MISSING",
        "NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE",
        "NATIVE_AUDIO_RUNTIME_CONFIG_INVALID",
        "NATIVE_AUDIO_RUNTIME_CONFIG_FIELDS_MISSING",
    }
)
_PRE_HTTP_CONFIG_FAILURES = frozenset(
    {
        "MAI_ENDPOINT_INVALID",
        "MAI_API_KEY_MISSING",
        "MAI_API_KEY_INVALID",
        "MAI_API_KEY_FILE_INVALID",
        "MAI_API_KEY_FILE_PERMISSIONS",
        "MOSS_API_KEY_MISSING",
        "MOSS_API_KEY_INVALID",
        "MOSS_API_KEY_FILE_INVALID",
        "MOSS_API_KEY_FILE_PERMISSIONS",
    }
    | {
        f"{provider}_{reason}"
        for provider in ("MAI", "MOSS")
        for reason in _NATIVE_RUNTIME_CONFIG_REASONS
    }
)


@dataclass(frozen=True)
class _RangeReviewError:
    status: str
    provider: str
    reason_code: str


@dataclass(frozen=True)
class _NativeRangeFailure:
    provider: str
    status: str
    reason_code: str
    original_failure_reason_code: str | None
    review_error: _RangeReviewError | None


def _review_error_fields(value: object) -> _RangeReviewError | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    provider = value.get("provider")
    reason = value.get("reason_code")
    if not isinstance(status, str) or not isinstance(provider, str) or not isinstance(reason, str):
        return None
    return _RangeReviewError(status, provider, reason)


def _failure_fields(row: object) -> _NativeRangeFailure | None:
    if not isinstance(row, Mapping):
        return None
    provider = row.get("provider")
    status = row.get("status")
    reason = row.get("reason_code")
    if not isinstance(provider, str) or not isinstance(status, str) or not isinstance(reason, str):
        return None
    original = row.get("original_failure_reason_code")
    return _NativeRangeFailure(
        provider, status, reason, original if isinstance(original, str) else None,
        _review_error_fields(row.get("review_error")),
    )


def native_range_service_failure(row: object) -> bool:
    """A definite service/config failure permits another existing audio route.

    The untrusted boundary validates only fields used by this decision. Unknown
    outcomes, content and binding failures retain their existing owner; this
    classification never authorizes resending the failed native attempt.
    """
    fields = _failure_fields(row)
    return fields is not None and _is_service_failure(fields)


def _is_service_failure(fields: _NativeRangeFailure) -> bool:
    provider = fields.provider
    reason = fields.reason_code
    if provider not in NATIVE_MODELS or fields.status != "UNCERTAIN":
        return False
    if reason == "NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY":
        original = fields.original_failure_reason_code
        if original is None or original not in _PRE_HTTP_CONFIG_FAILURES:
            return False
        reason = original
    if reason in _PRE_HTTP_CONFIG_FAILURES:
        return reason.startswith(provider.upper() + "_")
    error = fields.review_error
    return (error is not None and error.status == "CALL_FAILURE"
        and error.provider == provider and error.reason_code == reason
        and reason in {f"{provider.upper()}_{suffix}"
                       for suffix in ("HTTP_ERROR", "TIMEOUT", "TRANSPORT_ERROR")})


class NativeRangeRoutes:
    """One wrapper's ordinary fallback account and serialization lock."""

    def __init__(self, fallback: object, budget_receipt_path: Path) -> None:
        self.fallback = fallback
        self.budget_receipt_path = budget_receipt_path
        self.lock = Lock()

    def complete(
        self, output: list[_Row], *, provider_names: Sequence[str], cache_only: bool,
        check_request: _Check, range_witness_request: _Range,
    ) -> None:
        """Use the ordinary Gemini owner once a definite native service route fails.

        The owner retains source/window validation, sealed cache and actual cost.
        Serialize concurrent native failures for this wrapper so the second caller
        consumes that cache rather than submitting an overlapping provider request.
        """
        def position(row: _Row) -> int:
            provider = row.get('provider')
            return (provider_names.index(provider)
                    if isinstance(provider, str) and provider in provider_names else len(provider_names))

        output.sort(key=position)
        if cache_only or not any(native_range_service_failure(row) for row in output):
            return
        observe_gemini = getattr(self.fallback, 'observe_gemini_range_context', None)
        if callable(observe_gemini):
            with self.lock:
                output.extend(observe_gemini(
                    check_request=check_request, range_witness_request=range_witness_request,
                    budget_receipt_path=self.budget_receipt_path,
                ))


def configuration_recovery_observation(
    request: JsonObject, provider: str, reason: str, *, source_sha256: str,
    geometry: JsonObject, original_envelope: str | bytes,
    build_uncertain: Callable[..., Mapping[str, Json]],
) -> JsonObject:
    """Bind a recovery diagnostic to the original validated pre-HTTP failure.

    The original bound pre-HTTP failure still explains this route. Keep it
    available to the ordinary fallback owner; a recovery diagnostic must not
    erase that failure class. The caller has already validated the old envelope.
    """
    return {**build_uncertain(
        request, provider, reason,
        detail='current native provider configuration is not ready',
        source_sha256=source_sha256, geometry=geometry),
        'original_failure_reason_code': json.loads(original_envelope)['witness']['reason_code']}
