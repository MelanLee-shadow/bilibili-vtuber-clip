"""Small schema-shape gate shared by source-fact receipt consumers."""

from __future__ import annotations

from collections.abc import Mapping


def source_fact_review_passes_shape(
    review: object,
    *,
    schema_version: str,
    manual_title_keep_decision: str,
    deterministic_text_narrowing_decision: str,
    terminal_text_preservation_decision: str,
    operator_exact_title_decision: str = "OPERATOR_EXACT_TITLE_WITH_RECORDED_SOURCE_FACT_DISSENT",
    qixi_operator_exact_title_decision: str = (
        "QIXI_OPERATOR_EXACT_TITLE_WITH_HISTORICAL_PROVIDER_PASS"
    ),
    candidate_public_text_refresh_decision: str = "CANDIDATE_PUBLIC_TEXT_SOURCE_FACT_REFRESH",
    c3_terminal_source_fact_supersession_decision: str = "FASTLANE_C3_TERMINAL_SOURCE_FACT_SUPERSESSION",
) -> bool:
    """Accept only one of the closed PASS receipt shapes."""

    if not isinstance(review, Mapping):
        return False
    decision = review.get("decision")
    decision_shape_valid = (
        decision in {"KEEP", "REPAIRED"}
        or (
            decision == manual_title_keep_decision
            and isinstance(review.get("blocked_source_fact_review"), Mapping)
            and isinstance(review.get("manual_title_keep_authority_consumption"), Mapping)
            and review["manual_title_keep_authority_consumption"].get("status")
            == "CONSUMED"
            and isinstance(review.get("recorded_dissent"), Mapping)
            and review["recorded_dissent"].get("status") == "RECORDED_NON_BLOCKING"
        )
        or (
            decision == deterministic_text_narrowing_decision
            and isinstance(review.get("blocked_source_fact_review"), Mapping)
            and review["blocked_source_fact_review"].get("status") == "FAILED"
            and isinstance(review.get("deterministic_text_surface_resolution"), Mapping)
            and review["deterministic_text_surface_resolution"].get("status") == "VALID"
            and review["deterministic_text_surface_resolution"].get("provider_call_required")
            is False
        )
        or (
            decision == operator_exact_title_decision
            and isinstance(
                review.get("operator_exact_title_source_fact_authority_consumption"), Mapping
            )
            and review["operator_exact_title_source_fact_authority_consumption"].get("status")
            == "CONSUMED"
            and review["operator_exact_title_source_fact_authority_consumption"].get(
                "provider_pass_claim"
            )
            is False
            and isinstance(review.get("recorded_source_fact_dissent"), Mapping)
            and review["recorded_source_fact_dissent"].get("status") == "FAILED"
        )
        or (
            decision == qixi_operator_exact_title_decision
            and isinstance(
                review.get("operator_exact_title_source_fact_authority_consumption"),
                Mapping,
            )
            and review["operator_exact_title_source_fact_authority_consumption"].get("status")
            == "CONSUMED"
            and review["operator_exact_title_source_fact_authority_consumption"].get(
                "provider_pass_claim"
            )
            is False
            and isinstance(review.get("historical_provider_pass"), Mapping)
            and review["historical_provider_pass"].get("status") == "PASS"
        )
        or (
            decision == terminal_text_preservation_decision
            and isinstance(review.get("historical_provider_receipt"), Mapping)
            and isinstance(review.get("terminal_text_preservation"), Mapping)
            and review["terminal_text_preservation"].get("status") == "VALID"
        )
        or (
            decision == candidate_public_text_refresh_decision
            and isinstance(review.get("historical_provider_receipt"), Mapping)
            and isinstance(review.get("candidate_public_text_source_fact_refresh"), Mapping)
            and review["candidate_public_text_source_fact_refresh"].get("status") == "VALID"
        )
        or (
            decision == c3_terminal_source_fact_supersession_decision
            and isinstance(review.get("historical_provider_receipt"), Mapping)
            and isinstance(review.get("sealed_outer_terminal_receipt"), Mapping)
            and isinstance(review.get("c3_terminal_source_fact_supersession"), Mapping)
            and review["c3_terminal_source_fact_supersession"].get("status") == "VALID"
        )
    )
    return bool(
        review.get("schema_version") == schema_version
        and review.get("status") == "PASS"
        and decision_shape_valid
        and isinstance(review.get("final_selection_hook"), str)
        and bool(str(review.get("final_selection_hook")).strip())
        and isinstance(review.get("final_title"), str)
        and bool(str(review.get("final_title")).strip())
    )
