"""Thin source-boundary review router for the producer text pipeline."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from src.autoslice.clip_context import clip_context_prompt_text
from src.autoslice.final_review_auditor import _final_review_structured_context
from src.autoslice.frozen_source_boundary_receipt import (
    load_boundary_review_authorities,
)
from src.autoslice.llm_client import extract_json_object
from src.autoslice.producer_boundary_review_stage import (
    review_final_boundary_semantics,
)
from src.autoslice.producer_final_review_transport import (
    build_final_review_llm_call,
)
from src.autoslice.reviewed_exact_source_interval import (
    authority_from_spec,
    build_source_semantic_review,
)


def review_source_boundary(
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    cues: Sequence[object],
    boundary_target_ms: int,
    boundary_search_scope: Mapping[str, object],
    current_owner_contract: object,
    authoritative_chat: object,
    clip_context: object,
    available_local_source_context_end_ms: int,
) -> tuple[object, dict[str, object], dict[str, object], dict[str, object] | None]:
    """Return frozen receipt, current review, carry audit, and exact disclosure."""

    exact_authority = authority_from_spec(spec)
    if exact_authority is not None:
        review = build_source_semantic_review(
            spec=spec,
            authority=exact_authority,
            boundary_search_scope=boundary_search_scope,
        )
        disclosure = {
            "status": "PASS",
            "authority_sha256": exact_authority["authority_sha256"],
            "fresh_asr_role": "diagnostic_witness_only",
        }
        return None, review, {}, disclosure

    frozen_receipt, frozen_review = load_boundary_review_authorities(
        spec,
        candidate_id=candidate_id,
        current_owner_contract=current_owner_contract,
    )
    replay: dict[str, object] = {}
    review = review_final_boundary_semantics(
        cues=cues,
        boundary_target_ms=boundary_target_ms,
        candidate_id=candidate_id,
        selection_hook=str(spec.get("selection_hook") or ""),
        selection_scorecard=spec.get("selection_scorecard"),
        structured_context=_final_review_structured_context(
            selection_hook=str(spec.get("selection_hook") or ""),
            authoritative_chat=authoritative_chat,
        ),
        candidate_context=clip_context_prompt_text(clip_context),
        boundary_max_forward_ms=int(boundary_search_scope["recommendation_forward_ms"]),
        llm_call=build_final_review_llm_call(),
        extract_json=extract_json_object,
        disabled=os.environ.get("AUTOSLICE_DISABLE_FINAL_REVIEW") == "1",
        boundary_search_scope=boundary_search_scope,
        available_local_source_context_end_ms=(available_local_source_context_end_ms),
        frozen_review=frozen_review,
        replay_audit=replay,
    )
    return frozen_receipt, review, replay, None
