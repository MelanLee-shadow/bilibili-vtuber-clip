"""Use already bound acoustic evidence before making a text-only decision."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from src.autoslice.acoustic_witness_adjudication import build_witness_request, valid_witness_evidence
from src.autoslice.acoustic_witness_availability import pending_acoustic_witness
from src.autoslice.acoustic_witness_protocol import bind_blind_witness_protocol


def dispatch_context_witness(
    *, request: Mapping[str, Any], convergence: Mapping[str, Any] | None,
    entity_verifier: Callable | None, judge_llm_call: Callable | None,
    structured_chat_context: str, fetch_witness: Callable, judge_word_choice: Callable,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """A cache hit spends no audio call; cache misses retain CPA text-first routing.

    The verifier's read-only probe owns source/audio/prompt/provider/hash checks.
    Only a request-bound OBSERVED hit is consumed. Its existence is evidence,
    never a decision: the normal CPA acoustic judge still chooses the wording.
    """
    witness_request = build_witness_request(request)
    probe = getattr(entity_verifier, "probe_witness_cache", None)
    try:
        cached = probe(witness_request) if callable(probe) else None
    except Exception:
        cached = None
    if (
        isinstance(cached, Mapping) and cached.get("served_from_cache") is True
        and cached.get("status") == "OBSERVED"
        and valid_witness_evidence(cached, request_sha256=witness_request["request_sha256"])
    ):
        verdict = bind_blind_witness_protocol(cached, witness_request=witness_request)
        return verdict, witness_request, None, None

    pending = pending_acoustic_witness(witness_request)
    audio_required = bool(
        request.get("repair_class") == "acoustic_drop_cue"
        or (isinstance(convergence, Mapping)
            and convergence.get("decision") == "REPLACE_WITH_EXACT_TEXT")
    )
    # force_acoustic exits deterministic T1; it does not require a fresh listen.
    text_judge = None
    if judge_llm_call is not None and entity_verifier is not None and not audio_required:
        text_judge = judge_word_choice(
            llm_call=judge_llm_call, check_request=request,
            witness=pending, structured_chat_context=structured_chat_context,
        )
    text_resolved = bool(
        text_judge is not None and text_judge.get("status") == "JUDGED"
        and text_judge.get("needs_audio") is False
        and text_judge.get("choice") in {"CURRENT", "PROPOSED"}
    )
    if text_resolved and not audio_required:
        return pending, witness_request, text_judge, text_judge
    verdict, witness_request = fetch_witness(request)
    return verdict, witness_request, None, text_judge
