"""Append a derived recovery record; never relabel an old unknown answer."""

from __future__ import annotations

import hashlib
import json
import time

from .cpa_request_lifecycle import (
    RECOVERY_POLICY,
    LegacyUnknown,
    json_object,
    persistent_text_call,
)
from .argon_escalation_storage import atomic_write
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal, TypedDict, cast

if TYPE_CHECKING:
    from .finding_context import JsonValue
    from .semantic_retry_progress import SemanticAttemptRecorder


class OriginalUnknownRecord(TypedDict):
    request_sha256: str
    status: str
    # Earlier semantic request schemas can add fields. Keep their complete
    # validated JSON rather than rebuilding a narrower request document.
    request: dict[str, JsonValue]


class RecoveryOrigin(TypedDict):
    policy: str
    request_sha256: str


class RecoveryReason(TypedDict):
    basis: Literal["H104_BOUNDED_TRANSPORT_RECOVERY"]
    original_unknown: LegacyUnknown


class RecoveryEntry(TypedDict):
    schema_version: str
    issue_sha256: str
    request_sha256: str
    request: dict[str, JsonValue]
    status: Literal["COMPLETED"]
    started_ns: int
    continuation_reason: RecoveryReason
    completion: str
    completion_sha256: str
    answering_model_identity: dict[str, JsonValue] | None
    answer_binding_sha256: str


class RecoveryEvent(TypedDict):
    status: Literal["UNKNOWN_RECOVERY_COMPLETED"]
    request_sha256: str
    original_request_sha256: str
    unresolved_answer: bool
    provider_calls: None
    physical_cost_source: str


def _original_record(value: object) -> OriginalUnknownRecord:
    from .semantic_retry_progress import RetryNeedsPlan

    document = json_object(value, "SEMANTIC_RECOVERY_ORIGINAL_INVALID")
    if (
        not isinstance(document.get("request_sha256"), str)
        or not isinstance(document.get("status"), str)
        or not isinstance(document.get("request"), dict)
    ):
        raise RetryNeedsPlan("SEMANTIC_RECOVERY_ORIGINAL_INVALID")
    return cast(OriginalUnknownRecord, document)


def recorded_call(
    recorder: SemanticAttemptRecorder,
    call: Callable[[str], str],
    prompt: str,
    model: object,
    *,
    legacy: Sequence[object] = (),
) -> str:
    if recorder.directory is None:
        from .semantic_retry_progress import RetryNeedsPlan

        raise RetryNeedsPlan("SEMANTIC_PERSISTENT_DIRECTORY_REQUIRED")
    return persistent_text_call(
        call,
        prompt,
        model=model,
        root=recorder.directory,
        issue=recorder.issue,
        legacy=legacy,
        managed=bool(getattr(call, "durable_text_transport", False)),
    )


def recover_unknown(
    recorder: SemanticAttemptRecorder,
    original: object,
    call: Callable[[str], str],
    prompt: str,
    model: object,
) -> str:
    from .semantic_retry_progress import digest, SCHEMA, _uncertain, RetryNeedsPlan
    from .llm_client import completion_identity

    if recorder.directory is None:
        raise RetryNeedsPlan("SEMANTIC_PERSISTENT_DIRECTORY_REQUIRED")
    original_record = _original_record(original)
    path = recorder.directory / (original_record["request_sha256"] + ".json")
    raw = path.read_bytes()
    legacy: LegacyUnknown = {
        "request_sha256": original_record["request_sha256"],
        "record_sha256": hashlib.sha256(raw).hexdigest(),
        "status": original_record["status"],
        "physical_sends": None,
    }
    completion = recorded_call(recorder, call, prompt, model, legacy=[legacy])
    origin: RecoveryOrigin = {
        "policy": RECOVERY_POLICY,
        "request_sha256": original_record["request_sha256"],
    }
    request = {
        **original_record["request"],
        "recovery_origin": json_object(origin, "SEMANTIC_RECOVERY_ORIGIN_INVALID"),
    }
    request_sha = digest(request)
    answering_value: object = completion_identity(
        json_object(model, "CPA_TEXT_MODEL_BINDING_INVALID"), completion
    )
    answering = (
        None if answering_value is None
        else json_object(answering_value, "SEMANTIC_RECOVERY_MODEL_INVALID")
    )
    completion_sha = hashlib.sha256(completion.encode()).hexdigest()
    entry: RecoveryEntry = {
        "schema_version": SCHEMA,
        "issue_sha256": recorder.issue,
        "request_sha256": request_sha,
        "request": request,
        "status": "COMPLETED",
        "started_ns": time.time_ns(),
        "continuation_reason": {
            "basis": "H104_BOUNDED_TRANSPORT_RECOVERY",
            "original_unknown": legacy,
        },
        "completion": completion,
        "completion_sha256": completion_sha,
        "answering_model_identity": answering,
        "answer_binding_sha256": digest({"model": answering, "completion_sha256": completion_sha}),
    }
    derived = recorder.directory / (request_sha + ".json")
    if not atomic_write(
        derived,
        json.dumps(entry, ensure_ascii=False, sort_keys=True, allow_nan=False).encode(),
        create_only=True,
    ):
        saved = json_object(json.loads(derived.read_text()), "SEMANTIC_RECOVERY_COMPLETION_DRIFT")
        if (
            saved.get("request") != entry["request"]
            or saved.get("request_sha256") != entry["request_sha256"]
            or saved.get("completion") != entry["completion"]
            or saved.get("completion_sha256") != entry["completion_sha256"]
            or saved.get("answer_binding_sha256") != entry["answer_binding_sha256"]
        ):
            raise RetryNeedsPlan("SEMANTIC_RECOVERY_COMPLETION_DRIFT")
    assert path.read_bytes() == raw
    event: RecoveryEvent = {
        "status": "UNKNOWN_RECOVERY_COMPLETED",
        "request_sha256": request_sha,
        "original_request_sha256": original_record["request_sha256"],
        "unresolved_answer": _uncertain(completion, prompt),
        "provider_calls": None,
        "physical_cost_source": "persistent CPA request and transport attempts; not inferred from a logical response",
    }
    # The existing recorder owns a JSON-document list. Adapt only at that
    # legacy boundary; the recovery event above keeps its concrete shape.
    recorder.events.append(json_object(event, "SEMANTIC_RECOVERY_EVENT_INVALID"))
    return completion
