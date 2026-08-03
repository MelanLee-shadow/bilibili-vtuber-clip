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
