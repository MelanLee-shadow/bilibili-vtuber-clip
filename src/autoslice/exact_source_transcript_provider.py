"""Exact-source transcript candidate handoff for exact-final review."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from src.autoslice.exact_source_transcript_contract import (
    HANDOFF_SCHEMA,
    OBSERVATION_SCHEMA,
    _canonical_sha256,
    _digest,
    _sealed,
    _text_sha256,
    _valid_digest,
    build_exact_source_transcript_handoff,
    build_exact_source_transcript_request,
    valid_exact_source_transcript_observation,
    valid_exact_source_transcript_request,
)

_TRANSIENT_FAILURE_CATEGORIES = frozenset(
    {
        "AGY_QUOTA_EXHAUSTED",
        "AGY_RATE_LIMITED",
        "AGY_SERVER_ERROR",
        "AGY_SUBPROCESS_ERROR",
        "AGY_TIMEOUT",
        "GEMINI_API_QUOTA_EXHAUSTED",
        "GEMINI_API_SERVER_ERROR",
        "GEMINI_API_TIMEOUT",
        "GEMINI_API_REQUEST_FAILED",
    }
)
_FAILURE_ROW_KEYS = frozenset(
    {
        "provider",
        "category",
        "attempted",
        "error_type",
        "http_status",
        "key_tier",
        "key_ordinal",
        "attempt_round",
        "model",
        "circuit_breaker",
    }
)
_NON_ATTEMPT_CATEGORIES = frozenset(
    {
        "AGY_BINARY_ABSENT",
        "AGY_DISABLED_BY_ENV",
        "AGY_STALE_OUTPUT_CLEANUP_FAILED",
        "GEMINI_API_AUDIO_EXTRACTION_FAILED",
    }
)
_FAILURE_BINDING_SCHEMA = "exact-final-source-transcript-provider-failure-binding.v1"
_RETRYABLE_GEMINI_HTTP_STATUSES = frozenset({408, 409, 425, 429})
_TRANSIENT_TRANSPORT_ERROR_TYPES = frozenset(
    {
        "BrokenPipeError", "ConnectionAbortedError", "ConnectionError",
        "ConnectionRefusedError", "ConnectionResetError", "gaierror",
        "HTTPException", "IncompleteRead", "OSError", "RemoteDisconnected",
        "SSLError", "TimeoutError", "URLError",
    }
)


def _valid_transient_failure_row(row: Mapping[str, Any]) -> bool:
    category = str(row.get("category") or "")
    http_status = row.get("http_status")
    request_failure_retryable = bool(
        category != "GEMINI_API_REQUEST_FAILED"
        or (
            http_status is None
            and row.get("error_type") in _TRANSIENT_TRANSPORT_ERROR_TYPES
        )
        or (
            isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and (
                http_status in _RETRYABLE_GEMINI_HTTP_STATUSES
                or 500 <= http_status <= 599
            )
        )
    )
    return bool(
        set(row) <= _FAILURE_ROW_KEYS
        and row.get("provider") in {"agy", "gemini_api"}
        and row.get("attempted") is True
        and category in _TRANSIENT_FAILURE_CATEGORIES
        and request_failure_retryable
    )


def normalize_exact_source_transcript_provider_attempts(
    rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize legacy entity-provider rows only at the exact-lane boundary."""

    attempts: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        category = str(row.get("category") or "")
        skipped = (
            row.get("attempted") is False
            or category in _NON_ATTEMPT_CATEGORIES
            or category.startswith("PAID_BACKUP_SKIPPED:")
            or row.get("circuit_breaker") == "OPEN"
        )
        inferred = row.get("provider") in {"agy", "gemini_api"} and bool(category)
        if skipped or not (row.get("attempted") is True or inferred):
            continue
        attempts.append({key: row[key] for key in _FAILURE_ROW_KEYS if key in row} | {"attempted": True})
    return attempts


def exact_source_transcript_candidate_provenance(
    handoff: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "kind": "bounded_exact_source_audio_transcript",
        "surface": handoff.get("proposed_cue"),
        "source_media_sha256": handoff.get("source_media_sha256"),
        "audio_clip_sha256": handoff.get("audio_clip_sha256"),
        "handoff_receipt_sha256": handoff.get("receipt_sha256"),
        "rejected_proposed_sha256": handoff.get("rejected_proposed_sha256"),
        "mutation_authorized": False,
    }


def seal_exact_source_transcript_provider_failure(
    *, request: Mapping[str, Any], provider_failures: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Seal an actually attempted provider-chain failure for transient routing."""

    failures = [dict(row) for row in provider_failures if isinstance(row, Mapping)]
    if (
        not valid_exact_source_transcript_request(request)
        or not failures
        or not all(_valid_transient_failure_row(row) for row in failures)
    ):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_FAILURE_INVALID")
    receipt: dict[str, Any] = {
        "schema_version": OBSERVATION_SCHEMA,
        "status": "UNCERTAIN",
        "request_sha256": request["request_sha256"],
        "candidate_blind": True,
        "reason_code": "EXACT_SOURCE_TRANSCRIPT_PROVIDER_FAILED",
        "provider_attempted": True,
        "retry_class": "provider_transient",
        "provider_failures": failures,
        "authority": "NONE",
        "mutation_authorized": False,
    }
    receipt["failure_receipt_sha256"] = _canonical_sha256(receipt)
    return receipt


def valid_exact_source_transcript_provider_failure(
    receipt: Mapping[str, Any], *, request: Mapping[str, Any]
) -> bool:
    failures = receipt.get("provider_failures")
    return bool(
        valid_exact_source_transcript_request(request)
        and set(receipt)
        == {
            "schema_version",
            "status",
            "request_sha256",
            "candidate_blind",
            "reason_code",
            "provider_attempted",
            "retry_class",
            "provider_failures",
            "authority",
            "mutation_authorized",
            "failure_receipt_sha256",
        }
        and receipt.get("schema_version") == OBSERVATION_SCHEMA
        and receipt.get("status") == "UNCERTAIN"
        and receipt.get("request_sha256") == request.get("request_sha256")
        and receipt.get("candidate_blind") is True
        and receipt.get("reason_code") == "EXACT_SOURCE_TRANSCRIPT_PROVIDER_FAILED"
        and receipt.get("provider_attempted") is True
        and receipt.get("retry_class") == "provider_transient"
        and isinstance(failures, list)
        and bool(failures)
        and all(
            isinstance(row, Mapping) and _valid_transient_failure_row(row)
            for row in failures
        )
        and receipt.get("authority") == "NONE"
        and receipt.get("mutation_authorized") is False
        and _sealed(receipt, "failure_receipt_sha256")
    )


def seal_exact_source_transcript_provider_unavailable_audit(
    *,
    physical_request: Mapping[str, Any],
    provider_failure: Mapping[str, Any],
    initial_check_request: Mapping[str, Any],
    witness_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    srt_text: str,
    clip_context: Mapping[str, object] | None,
) -> dict[str, Any]:
    """Attach current-run identity only after the blind provider has failed."""

    try:
        from src.autoslice import acoustic_witness_adjudication as witness_api

        canonical_witness_request = witness_api.build_witness_request(
            initial_check_request
        )
        expected_physical = build_exact_source_transcript_request(
            canonical_witness_request
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "EXACT_SOURCE_TRANSCRIPT_PROVIDER_FAILURE_BINDING_INVALID"
        ) from exc
    context_binding = initial_check_request.get("clip_context_binding")
    candidate_id = (
        str(clip_context.get("candidate_id") or "").strip()
        if isinstance(clip_context, Mapping)
        else ""
    )
    context_sha = (
        _digest(clip_context.get("context_sha256"))
        if isinstance(clip_context, Mapping)
        else ""
    )
    draft_sha = (
        _digest(clip_context.get("whole_clip_draft_srt_sha256"))
        if isinstance(clip_context, Mapping)
        else ""
    )
    request_sha = _digest(initial_check_request.get("request_sha256"))
    if not (
        valid_exact_source_transcript_provider_failure(
            provider_failure, request=physical_request
        )
        and dict(witness_request) == canonical_witness_request
        and dict(physical_request) == expected_physical
        and candidate_id
        and _valid_digest(context_sha)
        and _valid_digest(draft_sha)
        and _valid_digest(request_sha)
        and _canonical_sha256(
            {
                key: value
                for key, value in initial_check_request.items()
                if key != "request_sha256"
            }
        )
        == request_sha
        and isinstance(context_binding, Mapping)
        and _digest(context_binding.get("context_sha256")) == context_sha
        and _digest(context_binding.get("whole_clip_draft_srt_sha256"))
        == draft_sha
        and witness_api.valid_witness_evidence(
            witness,
            request_sha256=canonical_witness_request["request_sha256"],
        )
    ):
        raise ValueError(
            "EXACT_SOURCE_TRANSCRIPT_PROVIDER_FAILURE_BINDING_INVALID"
        )
    binding: dict[str, Any] = {
        "schema_version": _FAILURE_BINDING_SCHEMA,
        "status": "PROVIDER_UNAVAILABLE",
        "candidate_id": candidate_id,
        "clip_context_sha256": "sha256:" + context_sha,
        "whole_clip_draft_srt_sha256": "sha256:" + draft_sha,
        "final_srt_sha256": "sha256:" + _text_sha256(srt_text),
        "input_request_sha256": "sha256:" + request_sha,
        "current_cue_sha256": "sha256:"
        + _text_sha256(str(initial_check_request.get("current_cue") or "")),
        "rejected_proposed_sha256": "sha256:"
        + _text_sha256(str(initial_check_request.get("proposed_cue") or "")),
        "parent_witness_request_sha256": "sha256:"
        + _digest(canonical_witness_request.get("request_sha256")),
        "parent_witness_observation_sha256": "sha256:"
        + _canonical_sha256(dict(witness)),
        "physical_request_sha256": "sha256:"
        + _digest(physical_request.get("request_sha256")),
        "provider_failure_receipt_sha256": "sha256:"
        + _digest(provider_failure.get("failure_receipt_sha256")),
        "mutation_authorized": False,
    }
    binding["binding_receipt_sha256"] = "sha256:" + _canonical_sha256(binding)
    return {
        "schema_version": HANDOFF_SCHEMA,
        "status": "PROVIDER_UNAVAILABLE",
        "reason_code": "EXACT_SOURCE_TRANSCRIPT_PROVIDER_FAILED",
        "provider_attempted": True,
        "provider_transient": True,
        "decision_authority": "NONE",
        "mutation_authorized": False,
        "physical_request": dict(physical_request),
        "provider_failure": dict(provider_failure),
        "failure_binding": binding,
    }


def valid_exact_source_transcript_provider_unavailable_audit(
    audit: Mapping[str, Any],
    *,
    check_request: Mapping[str, Any],
    srt_text: str,
    clip_context: Mapping[str, object] | None,
    witness: Mapping[str, Any],
) -> bool:
    request = audit.get("physical_request")
    failure = audit.get("provider_failure")
    if not isinstance(request, Mapping) or not isinstance(failure, Mapping):
        return False
    try:
        from src.autoslice import acoustic_witness_adjudication as witness_api

        expected = seal_exact_source_transcript_provider_unavailable_audit(
            physical_request=request,
            provider_failure=failure,
            initial_check_request=check_request,
            witness_request=witness_api.build_witness_request(check_request),
            witness=witness,
            srt_text=srt_text,
            clip_context=clip_context,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return dict(audit) == expected


def valid_exact_source_transcript_provider_unavailable_finding(
    finding: Mapping[str, Any],
    *,
    srt_text: str,
    clip_context: Mapping[str, object] | None,
) -> bool:
    """Validate the local failure seal against the current unresolved row."""

    adjudication = finding.get("exact_release_adjudication")
    request = adjudication.get("request") if isinstance(adjudication, Mapping) else None
    witness = adjudication.get("verdict") if isinstance(adjudication, Mapping) else None
    audit = (
        adjudication.get("exact_source_transcript_handoff")
        if isinstance(adjudication, Mapping)
        else None
    )
    return bool(
        isinstance(request, Mapping)
        and isinstance(witness, Mapping)
        and isinstance(audit, Mapping)
        and request.get("cue_indexes") == [finding.get("cue_index")]
        and request.get("base_text_sha256") == finding.get("base_text_sha256")
        and request.get("proposed_cue") == finding.get("proposed_full_cue")
        and valid_exact_source_transcript_provider_unavailable_audit(
            audit,
            check_request=request,
            srt_text=srt_text,
            clip_context=clip_context,
            witness=witness,
        )
    )


def seal_exact_source_transcript_capability_unavailable(
    *, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind the absence of any configured/attempted transcript transport."""

    if not valid_exact_source_transcript_request(request):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_REQUEST_INVALID")
    receipt: dict[str, Any] = {
        "schema_version": OBSERVATION_SCHEMA,
        "status": "NOT_CONFIGURED",
        "request_sha256": request["request_sha256"],
        "candidate_blind": True,
        "reason_code": "EXACT_SOURCE_TRANSCRIPT_CAPABILITY_UNAVAILABLE",
        "provider_attempted": False,
        "authority": "NONE",
        "mutation_authorized": False,
    }
    receipt["capability_receipt_sha256"] = _canonical_sha256(receipt)
    return receipt


def valid_exact_source_transcript_capability_unavailable(
    receipt: Mapping[str, Any], *, request: Mapping[str, Any]
) -> bool:
    return bool(
        valid_exact_source_transcript_request(request)
        and set(receipt)
        == {
            "schema_version",
            "status",
            "request_sha256",
            "candidate_blind",
            "reason_code",
            "provider_attempted",
            "authority",
            "mutation_authorized",
            "capability_receipt_sha256",
        }
        and receipt.get("schema_version") == OBSERVATION_SCHEMA
        and receipt.get("status") == "NOT_CONFIGURED"
        and receipt.get("request_sha256") == request.get("request_sha256")
        and receipt.get("candidate_blind") is True
        and receipt.get("reason_code")
        == "EXACT_SOURCE_TRANSCRIPT_CAPABILITY_UNAVAILABLE"
        and receipt.get("provider_attempted") is False
        and receipt.get("authority") == "NONE"
        and receipt.get("mutation_authorized") is False
        and _sealed(receipt, "capability_receipt_sha256")
    )


def rebuild_candidate_from_exact_source_transcript(
    *,
    finding: Mapping[str, Any],
    initial_check_request: Mapping[str, Any],
    witness_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    srt_text: str,
    clip_context: Mapping[str, object] | None,
    provider: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Produce one exact-audio third candidate, never a mutation decision."""

    try:
        physical_request = build_exact_source_transcript_request(witness_request)
    except (TypeError, ValueError) as exc:
        return None, {
            "schema_version": HANDOFF_SCHEMA,
            "status": "INVALID",
            "reason_code": str(exc),
            "decision_authority": "NONE",
            "mutation_authorized": False,
        }
    try:
        observed = provider(physical_request)
    except Exception as exc:
        return None, {
            "schema_version": HANDOFF_SCHEMA,
            "status": "INVALID",
            "reason_code": "EXACT_SOURCE_TRANSCRIPT_PROVIDER_CALLBACK_ERROR",
            "error_type": type(exc).__name__,
            "provider_transient": False,
            "decision_authority": "NONE",
            "mutation_authorized": False,
            "physical_request": physical_request,
        }
    if not isinstance(observed, Mapping):
        return None, {
            "schema_version": HANDOFF_SCHEMA,
            "status": "INVALID",
            "reason_code": "EXACT_SOURCE_TRANSCRIPT_RECEIPT_MISSING",
            "provider_transient": False,
            "decision_authority": "NONE",
            "mutation_authorized": False,
            "physical_request": physical_request,
        }
    if valid_exact_source_transcript_provider_failure(
        observed, request=physical_request
    ):
        try:
            unavailable = seal_exact_source_transcript_provider_unavailable_audit(
                physical_request=physical_request,
                provider_failure=observed,
                initial_check_request=initial_check_request,
                witness_request=witness_request,
                witness=witness,
                srt_text=srt_text,
                clip_context=clip_context,
            )
        except (TypeError, ValueError) as exc:
            return None, {
                "schema_version": HANDOFF_SCHEMA,
                "status": "INVALID",
                "reason_code": str(exc),
                "provider_transient": False,
                "decision_authority": "NONE",
                "mutation_authorized": False,
                "physical_request": physical_request,
            }
        return None, unavailable
    if valid_exact_source_transcript_capability_unavailable(
        observed, request=physical_request
    ):
        return None, {
            "schema_version": HANDOFF_SCHEMA,
            "status": "CAPABILITY_UNAVAILABLE",
            "reason_code": "EXACT_SOURCE_TRANSCRIPT_CAPABILITY_UNAVAILABLE",
            "provider_attempted": False,
            "provider_transient": False,
            "decision_authority": "NONE",
            "mutation_authorized": False,
            "physical_request": physical_request,
            "capability_receipt": dict(observed),
        }
    if not valid_exact_source_transcript_observation(
        observed, request=physical_request
    ):
        return None, {
            "schema_version": HANDOFF_SCHEMA,
            "status": "INVALID",
            "reason_code": "EXACT_SOURCE_TRANSCRIPT_RECEIPT_INVALID",
            "provider_transient": False,
            "decision_authority": "NONE",
            "mutation_authorized": False,
            "physical_request": physical_request,
        }
    try:
        handoff = build_exact_source_transcript_handoff(
            observation=observed,
            physical_request=physical_request,
            initial_check_request=initial_check_request,
            witness_request=witness_request,
            witness=witness,
            srt_text=srt_text,
            clip_context=clip_context,
        )
    except (TypeError, ValueError) as exc:
        return None, {
            "schema_version": HANDOFF_SCHEMA,
            "status": "INVALID",
            "reason_code": str(exc),
            "provider_transient": False,
            "decision_authority": "NONE",
            "mutation_authorized": False,
            "physical_request": physical_request,
        }
    current = str(initial_check_request.get("current_cue") or "")
    proposed = str(handoff["proposed_cue"])
    suspect, replacement, start, end, error = derive_single_span_edit(
        current,
        proposed,
        allow_insertion=True,
        allow_deletion=True,
        allow_script_width_delta=True,
    )
    if error is not None:
        invalid = dict(handoff)
        invalid.update(
            status="INVALID",
            reason_code=error,
            provider_transient=False,
        )
        return None, invalid
    rebuilt = dict(finding)
    rebuilt.update(
        suspect=suspect,
        suggestion=replacement,
        span_start_codepoint=start,
        span_end_codepoint=end,
        proposed_full_cue=proposed,
        base_text_sha256=_text_sha256(current),
        repair_class="spoken_unit",
        candidate_provenance=exact_source_transcript_candidate_provenance(handoff),
        candidate_memory_id=None,
        _exact_source_transcript_handoff=handoff,
        why="[exact source transcript 第三候选] 候选盲同窗原声全文听写；仍须 CPA 选 PROPOSED",
    )
    return rebuilt, handoff
