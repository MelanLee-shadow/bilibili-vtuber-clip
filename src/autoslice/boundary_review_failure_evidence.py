"""Typed, non-authoritative diagnostics for failed boundary review calls."""

from __future__ import annotations

from collections.abc import Mapping

from src.autoslice import provider_failure


def review_unavailable_evidence(exc: BaseException) -> dict[str, object]:
    """Classify the escaped cause without copying prompt, completion, or secrets."""

    cause = exc.__cause__
    if cause is None:
        return {}
    error_type = type(cause).__name__
    parse_code = getattr(cause, "reason_code", None)
    if parse_code in provider_failure.LLM_JSON_PARSE_REASON_CODES:
        failure_class = "provider_invalid_output"
    elif isinstance(cause, PermissionError):
        failure_class = "permission_denied"
    elif error_type in provider_failure.TRANSPORT_EXCEPTION_NAMES:
        failure_class = "provider_transport"
    else:
        failure_class = "stage_exception"

    evidence: dict[str, object] = {
        "schema_version": "boundary-semantic-review-unavailable-evidence.v1",
        "failure_class": failure_class,
        "error_type": error_type,
        "semantic_verdict_observed": False,
    }
    if failure_class == "provider_invalid_output":
        evidence["provider_error_code"] = parse_code
        return evidence
    if failure_class != "provider_transport":
        return evidence

    provider_detail = provider_failure.provider_failure_detail(cause)
    if provider_detail:
        evidence["provider_detail"] = provider_detail
        evidence.update(provider_failure.describe_provider_exception(cause))
    diagnostics = getattr(cause, "provider_diagnostics", None)
    if isinstance(diagnostics, Mapping):
        for key in (
            "provider_transport",
            "provider_endpoint_host",
            "provider_endpoint_path",
            "provider_runtime_host",
            "provider_credential_source",
            "provider_error_code",
        ):
            value = diagnostics.get(key)
            if value is not None:
                evidence[key] = value
    return evidence


__all__ = ["review_unavailable_evidence"]
