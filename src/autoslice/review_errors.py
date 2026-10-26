"""Review failure taxonomy, independent of exception messages and content codes.

Only explicit contract and provider failures may become review outcomes. All
other exceptions retain their identity and traceback; the outer boundary logs
one PROGRAM_ERROR record and attaches that same record to the exception.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
import json
import logging
import traceback
from typing import Any, NotRequired, ParamSpec, TypeVar, TypedDict, cast

from .diarized_transcription import DiarizedTranscriptionError

PROGRAM_ERROR = "PROGRAM_ERROR"
_log = logging.getLogger(__name__)


class _TracebackFrame(TypedDict):
    file: str
    line: int | None
    function: str


class _ReviewErrorRecord(TypedDict):
    schema_version: str
    status: str
    reason_code: str
    provider: NotRequired[str]
    material_recovery: NotRequired[object]
    exception_type: NotRequired[str]
    boundary: NotRequired[str]
    traceback_summary: NotRequired[list[_TracebackFrame]]
    recording_error: NotRequired[_ReviewErrorRecord]


class _ObservationErrorRow(TypedDict, total=False):
    review_error: _ReviewErrorRecord | None
    reason_code: str | None
    status: str
    provider: str


class _AudioAnchor(TypedDict):
    candidate_id: object
    source_media_sha256s: list[str]
    physical_target_ms: list[int]
    source_media_timeline_offset_ms: int
    current_srt_sha256: str
    current_cue_sha256: str
    clip_context_binding: object


_P = ParamSpec("_P")
_R = TypeVar("_R")


class ContractRejection(ValueError):
    """A deliberate refusal by a local input/evidence contract."""

    def __init__(self, reason_code: str, detail: str = "", *,
                 material_recovery: object = None) -> None:
        super().__init__(detail or reason_code)
        self.reason_code = reason_code
        self.detail = detail
        self.material_recovery = material_recovery


class ProviderFailure(DiarizedTranscriptionError, ValueError):
    """A known transport, timeout, rate-limit or provider response failure.

    The legacy ASR base preserves its callers; ValueError preserves the
    existing material-owner refusal interface (including offline preflight).
    Neither base alone is enough to classify an exception as provider failure.
    Conversion takes place only at the explicit provider boundary below.
    """

    def __init__(self, reason_code: str, detail: str = "", *, provider: str,
                 metadata: object = None, material_recovery: object = None) -> None:
        super().__init__(reason_code, detail or reason_code, metadata)
        self.provider = provider
        self.detail = detail
        self.material_recovery = material_recovery


def review_error_record(exc: BaseException) -> _ReviewErrorRecord:
    """Serialize only the two declared outcome types, never guess from text."""
    if not isinstance(exc, (ContractRejection, ProviderFailure)):
        raise TypeError("review outcome requires a typed failure")
    record: _ReviewErrorRecord = {"schema_version": "review-error.v1",
              "status": "CALL_FAILURE" if isinstance(exc, ProviderFailure) else "CONTRACT_ERROR",
              "reason_code": exc.reason_code}
    if isinstance(exc, ProviderFailure):
        record["provider"] = exc.provider
    if exc.material_recovery is not None:
        record["material_recovery"] = exc.material_recovery
    return record


def observation_error_record(row: _ObservationErrorRow,
                             validation_reason: str) -> _ReviewErrorRecord:
    """Read the typed receipt; legacy execution states have explicit meaning.

    An untyped UNCERTAIN row does not establish a provider failure. Refusing
    that row is a local evidence contract rejection, with the validator's
    original reason retained by the caller.
    """
    if row.get("review_error") is not None:
        return cast(_ReviewErrorRecord, row["review_error"])
    reason = row.get("reason_code") or validation_reason
    pending = row.get("status") in {
        "UNKNOWN", "OUTCOME_UNKNOWN", "DISPATCHED", "PENDING", "ADMISSION_PENDING",
    }
    if pending or row.get("status") == "FAILED":
        return review_error_record(ProviderFailure(reason, provider=row.get("provider", "native"),
            material_recovery={"kind": "IN_FLIGHT_OR_UNKNOWN" if pending else "TRANSPORT_OR_SCHEMA_FAILURE"}))
    return review_error_record(ContractRejection(reason))


def record_program_error(exc: BaseException, *, boundary: str) -> _ReviewErrorRecord:
    """Record without replacing/rethrowing the exception or changing its stack.

    Stack frames omit source text and exception messages (which can contain
    provider credentials). The original exception still carries the complete
    traceback for the caller. Nested boundaries retain the first record.
    """
    existing = getattr(exc, "review_error", None)
    if isinstance(existing, dict) and existing.get("status") == PROGRAM_ERROR:
        return cast(_ReviewErrorRecord, existing)
    record: _ReviewErrorRecord = {"schema_version": "review-error.v1", "status": PROGRAM_ERROR,
              "reason_code": PROGRAM_ERROR, "exception_type": type(exc).__name__,
              "boundary": boundary,
              "traceback_summary": [
                  {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(exc.__traceback__)]}
    cast(Any, exc).review_error = record
    _log.error("review program error: %s", json.dumps(record, ensure_ascii=False),
               extra={"review_error": record})
    return record


def _legacy_contract_types() -> tuple[type[Exception], ...]:
    from .argon_escalation_contract import ContractError
    from .final_review_contract import FinalReviewContractError
    from .native_audio_budget_receipt import BudgetReceiptPersistenceError
    from .supplement_audio_budget import BudgetConfigurationError, BudgetExceeded
    return (ContractError, FinalReviewContractError, BudgetReceiptPersistenceError,
            BudgetConfigurationError, BudgetExceeded)


def validated_audio_anchor(check: object) -> _AudioAnchor:
    """Bridge the existing pure anchor validator's declared ValueError contract.

    This is a named legacy validation operation, not a catch around a workflow
    or provider. TypeError/KeyError/AttributeError retain their normal meaning.
    """
    from .context_assisted_audio import anchor
    try:
        return cast(Callable[[object], _AudioAnchor], anchor)(check)
    except ValueError as exc:
        raise ContractRejection(str(exc)) from exc


@contextmanager
def program_error_boundary(boundary: str, *,
                           on_error: Callable[[_ReviewErrorRecord], object] | None = None) -> Iterator[None]:
    try:
        yield
    except (ContractRejection, ProviderFailure):
        raise
    except _legacy_contract_types():
        # Physical budget owners retain their public exception identity and
        # pending reservations. Callers bridge them at their review boundary.
        raise
    except Exception as exc:
        record = record_program_error(exc, boundary=boundary)
        if on_error is not None:
            try:
                on_error(record)
            except Exception as recording_exc:
                # A ledger failure must be visible but must not replace the
                # original programming failure or its traceback.
                record["recording_error"] = record_program_error(
                    recording_exc, boundary=boundary + ".recording")
        raise


def records_program_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with program_error_boundary(function.__qualname__):
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def contract_boundary() -> Iterator[None]:
    """Bridge existing explicitly typed contracts; generic ValueError is not one."""
    from .argon_escalation_contract import ContractError
    from .final_review_contract import FinalReviewContractError
    from .native_audio_budget_receipt import BudgetReceiptPersistenceError
    from .supplement_audio_budget import BudgetConfigurationError, BudgetExceeded
    try:
        yield
    except ContractError as exc:
        raise ContractRejection(exc.reason, str(exc)) from exc
    except (FinalReviewContractError, BudgetReceiptPersistenceError,
            BudgetConfigurationError, BudgetExceeded) as exc:
        raise ContractRejection(exc.reason_code, str(exc)) from exc


@contextmanager
def provider_boundary(provider: str) -> Iterator[None]:
    """Normalize known provider types, without catching arbitrary RuntimeError."""
    from .mai_transcription import MaiTranscriptionError
    from .moss_transcription import MossTranscriptionError
    try:
        yield
    except ProviderFailure:
        raise
    except (DiarizedTranscriptionError, MaiTranscriptionError, MossTranscriptionError) as exc:
        raise ProviderFailure(exc.reason_code, str(exc), provider=provider,
                              metadata=exc.metadata) from exc
    except (TimeoutError, ConnectionError) as exc:
        raise ProviderFailure("SECONDARY_PROVIDER_CALL_FAILED", str(exc),
                              provider=provider) from exc
