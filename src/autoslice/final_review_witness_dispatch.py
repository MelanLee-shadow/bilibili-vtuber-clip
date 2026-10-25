"""Use already bound acoustic evidence before making a text-only decision."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from src.autoslice.acoustic_witness_adjudication import build_witness_request, valid_witness_evidence
from src.autoslice.acoustic_witness_availability import pending_acoustic_witness
from src.autoslice.acoustic_witness_protocol import bind_blind_witness_protocol
from src.autoslice.exact_final_witness_authority import (
    valid_history_audio_recovery,
)


def dispatch_context_witness(
    *, request: Mapping[str, Any], convergence: Mapping[str, Any] | None,
    entity_verifier: Callable | None, judge_llm_call: Callable | None,
    structured_chat_context: str, fetch_witness: Callable, judge_word_choice: Callable,
    history_audio_recovery: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """A cache hit spends no audio call; cache misses retain CPA text-first routing.

    The verifier's read-only probe owns source/audio/prompt/provider/hash checks.
    Only a request-bound OBSERVED hit is consumed. Its existence is evidence,
    never a decision: the normal CPA acoustic judge still chooses the wording.
    """
    witness_request = build_witness_request(request)
    typed_history_audio = valid_history_audio_recovery(
        history_audio_recovery,
        request=request,
    )
    if history_audio_recovery is not None and not typed_history_audio:
        # A supplied history marker that is not a valid typed BLOCK gate is
        # rejected before touching either the cache/provider or text-first
        # judge.  The auditor emits the fail-closed receipt; this guard keeps
        # direct callers from turning a forged envelope into a closure.
        return (
            pending_acoustic_witness(witness_request),
            witness_request,
            None,
            None,
        )
    if request.get("argon_escalation_evidence") is not None:
        from src.autoslice.argon_escalation_integration import transcript_witness
        observed = transcript_witness(request, request["argon_escalation_evidence"])
        return observed, witness_request, None, None
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
    # A missing proposal that survived the text-only convergence pass is a
    # different case from an ordinary semantic KEEP.  The latter deliberately
    # remains text-first; the former has no real PROPOSED side for CPA to
    # compare and therefore needs one candidate-blind bounded observation
    # before it can close.  The marker is hash-bound on the caller's request
    # and contains no candidate text, so it cannot turn an observation into a
    # mutation authority.
    audio_required = bool(
        request.get("repair_class") == "acoustic_drop_cue"
        or request.get("candidate_blind_observation_scaffolding") is True
        or (isinstance(convergence, Mapping)
            and convergence.get("decision") == "REPLACE_WITH_EXACT_TEXT")
        or typed_history_audio
    )
    # force_acoustic exits deterministic T1; it does not require a fresh listen.
    text_judge = None
    if judge_llm_call is not None and not audio_required:
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


def original_language_scope_evidence(request, witness, provider):
    """Acquire original-language evidence for a Japanese-script target repair.

    An OBSERVED Mandarin report still cannot supply original Japanese words.
    Keep that report unchanged; the separate candidate-blind native protocol
    supplies evidence only, with its own physical request and cache/usage rules.
    """
    from src.autoslice.text_scripts import script_profile
    from src.autoslice.exact_source_transcript_contract import (
        build_exact_source_transcript_request, valid_exact_source_transcript_observation,
    )
    from src.autoslice.independent_context_adjudication import bind_request
    if (request.get('require_complete_utterance_support') is not True
            or not script_profile(str(request.get('replacement') or ''))['kana']
            or not callable(provider)):
        return dict(request), None
    physical = build_exact_source_transcript_request(build_witness_request(request))
    try:
        observation = provider(physical)
    except Exception as exc:
        return dict(request), {'status': 'FAILED', 'error_type': type(exc).__name__,
            'physical_request': physical, 'prior_primary_status': witness.get('status'),
            'authority': 'EVIDENCE_ONLY', 'mutation_authorized': False}
    audit = {'status': 'OBSERVED' if isinstance(observation, Mapping)
                and valid_exact_source_transcript_observation(observation, request=physical)
                else 'UNAVAILABLE_OR_INVALID',
             'physical_request': physical, 'observation': observation,
             'prior_primary_status': witness.get('status'),
             'prior_primary_reason': witness.get('reason_code'),
             'authority': 'EVIDENCE_ONLY', 'mutation_authorized': False}
    if audit['status'] != 'OBSERVED':
        return dict(request), audit
    return bind_request(request, primary_original_language_observations=[dict(observation)]), audit
