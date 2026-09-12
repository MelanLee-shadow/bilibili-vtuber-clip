"""Native HTTP failures retain typed retry semantics, not text guessing."""

import pytest
from src.autoslice.foreign_source_failure_evidence import foreign_source_provider_transient
from src.autoslice.talk_recovery_record_policy import _provider_backfilled_foreign_rejection


def violation(provider, code, status=None):
    return {
        "unresolved_findings": [{"cue_index": 1}],
        "witness_rows": [
            {
                "cue_index": 1,
                "provider": provider,
                "failure": "DiarizedTranscriptionError: sanitized message",
                "failure_reason_code": code,
                "failure_http_status": status,
            }
        ],
    }


@pytest.mark.parametrize("provider", ["mai", "moss"])
@pytest.mark.parametrize(
    "code,status,retry",
    [
        ("HTTP_ERROR", 429, True),
        ("HTTP_ERROR", 503, True),
        ("HTTP_ERROR", 408, True),
        ("HTTP_ERROR", 401, False),
        ("HTTP_ERROR", 403, False),
        ("HTTP_ERROR", 422, False),
        ("HTTP_ERROR", None, False),
        ("TIMEOUT", None, True),
        ("TRANSPORT_ERROR", None, True),
        ("TIME_INVALID", None, False),
        ("RESPONSE_INVALID", None, False),
    ],
)
def test_native_retry_classification(provider, code, status, retry):
    v = violation(provider, provider.upper() + "_" + code, status)
    assert foreign_source_provider_transient(v) is retry
    old = {
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_stage": "foreign_source_transcription",
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "gate_violation": v,
    }
    assert _provider_backfilled_foreign_rejection(old) is retry


def test_legacy_failure_contract_unchanged():
    v = violation("agy", None)
    v["witness_rows"][0]["failure"] = "RuntimeError: WITNESS_PROVIDERS_FAILED: HTTPError"
    assert foreign_source_provider_transient(v)
