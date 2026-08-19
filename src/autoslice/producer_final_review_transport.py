"""CPA transport configuration for producer final subtitle review."""

from __future__ import annotations

from collections.abc import Callable

from src.autoslice.llm_client import LlmConfig, build_llm_call


def build_final_review_llm_call() -> Callable[[str], str]:
    """Give each approved model one attempt within the outer 600-second cap."""

    return build_llm_call(
        LlmConfig(
            transport="command",
            command_template=(
                "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
                "'gpt-5.6-sol gpt-5.5 gpt-5.4' medium 1"
            ),
            timeout_seconds=600.0,
        )
    )


def build_pronoun_audit_llm_call() -> Callable[[str], str]:
    """Transport for the candidate-level pronoun consistency audit.

    2026-08-19 (Ivan): this is a closed 4-token set (TA/他/她/它) classified
    against already-written rules and rerun every exact-final/self-heal
    round -- it does not need the same reasoning effort as the A-class
    entity/boundary judges.  Kept as its own builder (same approved model
    chain, effort dropped medium -> low) so the A-class calls that still
    share ``build_final_review_llm_call`` are never touched by this change.
    """

    return build_llm_call(
        LlmConfig(
            transport="command",
            command_template=(
                "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
                "'gpt-5.6-terra gpt-5.5 gpt-5.4' low 1"
            ),
            timeout_seconds=600.0,
        )
    )
