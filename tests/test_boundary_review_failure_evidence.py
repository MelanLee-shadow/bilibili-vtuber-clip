from __future__ import annotations

import json

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmCallError, LlmJsonParseError, extract_json_object
from src.autoslice.producer_boundary_review_stage import review_final_boundary_semantics


def _srt(*texts: str) -> str:
    return (
        "\n\n".join(
            f"{index}\n00:00:{index * 5:02d},000 --> "
            f"00:00:{index * 5 + 4:02d},000\n{text}"
            for index, text in enumerate(texts, start=1)
        )
        + "\n"
    )


def _review(*, llm_call, extract_json=extract_json_object):
    return review_final_boundary_semantics(
        cues=parse_srt_cues(_srt("目标故事", "下一话题")),
        boundary_target_ms=9_000,
        candidate_id="provider-failure-evidence",
        selection_hook="目标故事",
        selection_scorecard={
            "status": "VALID",
            "dimensions": {"self_contained": 4, "comedic_payoff": 4},
        },
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=30_000,
        llm_call=llm_call,
        extract_json=extract_json,
    )


def test_boundary_review_preserves_only_safe_provider_failure_diagnostics() -> None:
    def unavailable(_prompt: str) -> str:
        raise LlmCallError(
            "bridge failed http=503 token=should-not-persist",
            safe_reason="LLM_COMMAND_FAILED",
            provider_diagnostics={
                "provider_transport": "runtime_cpa",
                "provider_endpoint_host": "cpacn.example.test",
                "provider_endpoint_path": "/v1",
                "provider_credential_source": "/runtime/cpa.env",
                "provider_error_code": "UPSTREAM_UNAVAILABLE",
                "provider_error_message": "token=should-not-persist",
            },
        )

    result = _review(llm_call=unavailable)

    assert result["status"] == "BLOCK"
    assert result["reason_codes"] == [
        "BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"
    ]
    evidence = result["unavailable_evidence"]
    assert evidence["failure_class"] == "provider_transport"
    assert evidence["semantic_verdict_observed"] is False
    assert evidence["safe_reason"] == "LLM_COMMAND_FAILED"
    assert evidence["provider_class"] == "service"
    assert evidence["provider_status_codes"] == [503]
    assert evidence["provider_transport"] == "runtime_cpa"
    assert evidence["provider_error_code"] == "UPSTREAM_UNAVAILABLE"
    assert "should-not-persist" not in json.dumps(evidence, ensure_ascii=False)


def test_boundary_review_separates_invalid_output_from_transport_failure() -> None:
    def invalid_json(_value: str):
        raise LlmJsonParseError("LLM_JSON_NO_OBJECT")

    result = _review(
        llm_call=lambda _prompt: "not-json",
        extract_json=invalid_json,
    )

    assert result["status"] == "BLOCK"
    assert result["reason_codes"] == [
        "BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmJsonParseError"
    ]
    assert result["unavailable_evidence"] == {
        "schema_version": "boundary-semantic-review-unavailable-evidence.v1",
        "failure_class": "provider_invalid_output",
        "error_type": "LlmJsonParseError",
        "semantic_verdict_observed": False,
        "provider_error_code": "LLM_JSON_NO_OBJECT",
    }
