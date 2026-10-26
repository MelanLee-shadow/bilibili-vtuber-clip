"""D: a local retry-admission refusal is typed, never a provider outage.

Rules: SUB-20 / 41 continuation (the refusal itself stays TRUE: three
unresolved answers without a plan are not re-sent); the refusal must not be
folded into JUDGE_CALL_FAILED, the provider-outage family that the
continuation terminal never consumes (guimi 69eb9ae diagnosis D). Same typed
form as the missing-candidate path: RETRY_BLOCKED / RetryNeedsPlan /
NOT_DISPATCHED. A real provider failure still fails closed. Zero network.
"""
from copy import deepcopy

from src.autoslice.acoustic_witness_adjudication import judge_word_choice
from src.autoslice.exact_final_witness_authority import _NO_LATER_DECISION
from src.autoslice.foreign_source_failure_evidence import _judge_row_provider_transient
from src.autoslice.unreadable_span_policy import MACHINE_UNDECIDED_WITNESS_REASON_CODES
from test_context_retry_material_sync import (  # noqa: F401  (fixtures)
    FINDING, MODEL, isolated_runtime, observed, request_for, run_calls, three_text_calls, uncached_judge,
)


def test_local_refusal_is_typed_and_reaches_the_continuation_exit(uncached_judge):
    finding = {k: v for k, v in FINDING.items() if k != "continuation_plan"}
    request = request_for(finding)
    calls, receipts, _ = run_calls(three_text_calls(request) + [(request, observed(request), {})],
                                   finding=finding)
    refused = receipts[-1]
    assert len(calls) == 3  # The refusal itself is unchanged: nothing was sent.
    assert refused["status"] == "RETRY_BLOCKED" and refused["error_type"] == "RetryNeedsPlan"
    assert refused["reason_code"] == "CONTINUATION_VALUE_PLAN_REQUIRED" != "JUDGE_CALL_FAILED"
    assert refused["dispatch_outcome"] == "NOT_DISPATCHED" and refused["provider_calls_this_attempt"] == 0
    assert refused["retry_reason_code"] == "CONTINUATION_VALUE_PLAN_REQUIRED"
    assert refused["reason_code"] not in MACHINE_UNDECIDED_WITNESS_REASON_CODES
    # Not the provider-outage family, even with a transient-looking message.
    assert not _judge_row_provider_transient({"adjudication": {"judge": {**refused, "error": "HTTP 503"}}})
    # An earlier explicit audio request still stands after this refusal.
    assert refused["status"] in _NO_LATER_DECISION


def test_real_provider_failure_still_fails_closed(uncached_judge):
    calls = []

    def broken(prompt):
        calls.append(prompt)
        raise ValueError("synthetic provider contract failure after dispatch")
    broken.cpa_cache_identity = deepcopy(MODEL)
    request = request_for()
    result = judge_word_choice(llm_call=broken, check_request=request, witness=observed(request))
    assert result["status"] == "JUDGE_UNAVAILABLE" and result["reason_code"] == "JUDGE_CALL_FAILED"
    assert "RETRY_BLOCKED" != result["status"] and len(calls) >= 1


def test_local_error_after_the_provider_returned_keeps_the_answer(uncached_judge, monkeypatch):
    """M2: a post-return local failure is typed and retains the completed raw
    answer; it is not folded into the provider-outage JUDGE_CALL_FAILED."""
    import json
    from src.autoslice import span_judgment_history
    calls = []
    answer = json.dumps({"choice": "PROPOSED", "needs_audio": False, "reason": "fixture answer"})

    def provider(prompt):
        calls.append(prompt)
        return answer
    provider.cpa_cache_identity = deepcopy(MODEL)

    def broken_history(*args, **kwargs):
        raise KeyError("request_sha256")
    monkeypatch.setattr(span_judgment_history, "expected_execution", broken_history)
    request = request_for()
    result = judge_word_choice(llm_call=provider, check_request=request, witness=observed(request))
    assert len(calls) == 1  # Answered once; no transient retry, no resend.
    assert result["status"] == "JUDGE_LOCAL_ERROR_AFTER_RETURN" != "JUDGE_UNAVAILABLE"
    assert result["reason_code"] == "LOCAL_CONTRACT_ERROR_AFTER_PROVIDER_RETURN"
    assert result["raw_completion"] == answer and result["dispatch_outcome"] == "COMPLETED"
    assert not _judge_row_provider_transient({"adjudication": {"judge": result}})
