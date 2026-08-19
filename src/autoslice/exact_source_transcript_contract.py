"""Candidate-blind exact-source transcript handoff for exact-final review.

The audio provider receives only immutable cue/timeline geometry.  Textual
identity (current/rejected candidates, final SRT, candidate and run binding)
is attached only after the provider returns, and the resulting transcript is
still merely a third PROPOSED candidate for the ordinary CPA judge.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.autoslice.exact_source_transcript_provider_policy import build_provider_route
from src.autoslice.exact_source_transcript_provider_policy import valid_provider_route


REQUEST_SCHEMA = "exact-final-source-transcript-request.v2"
OBSERVATION_SCHEMA = "exact-final-source-transcript-observation.v2"
HANDOFF_SCHEMA = "exact-final-source-transcript-handoff.v2"
PROMPT_CONTRACT = "exact-final-candidate-blind-target-marked-transcript.v1"
PROVIDER_REPORT_SCHEMA = "exact-final-source-transcript-provider-report.v1"
_GEOMETRY_KEYS = (
    "cue_indexes", "matched_start_ms", "matched_end_ms",
    "context_start_ms", "context_end_ms",
    "source_media_timeline_offset_ms",
)
_REQUEST_KEYS = {"schema_version", "kind", *_GEOMETRY_KEYS, "request_sha256"}
_FORBIDDEN_PROVIDER_KEYS = {
    "syllable_count_hint", "candidate_entities", "current_cue", "proposed_cue",
    "matched_audio_text", "context_before", "context_after", "suspect",
    "replacement", "candidate_id", "final_srt_sha256", "input_srt_sha256",
    "run_binding_sha256",
}
_ALGORITHMS = {
    "agy": "agy-exact-final-candidate-blind-target-transcript-v1",
    "gemini_api": "gemini-api-exact-final-candidate-blind-target-transcript-v1",
}
_ALLOWED_LANGUAGES = frozenset({"zh", "ja", "en", "mixed", "none"})
_HANDOFF_KEYS = {
    "schema_version", "status", "decision_authority", "authority",
    "mutation_authorized", "candidate_id", "clip_context_sha256",
    "whole_clip_draft_srt_sha256", "input_request_sha256", "input_request",
    "input_srt_sha256", "final_srt_sha256", "current_cue_sha256",
    "rejected_proposed_sha256", "run_binding_sha256",
    "parent_witness_request_sha256", "parent_witness_request",
    "parent_witness_observation_sha256",
    "physical_request_sha256", "physical_request",
    "provider_observation_sha256", "source_media_sha256", "audio_clip_sha256",
    "prompt_sha256", "response_sha256", "provider", "model", "algorithm_id",
    "timeline_binding", "proposed_cue", "proposed_cue_sha256",
    "provider_observation", "receipt_sha256",
}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def _valid_digest(value: object) -> bool:
    digest = _digest(value)
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _sealed(payload: Mapping[str, Any], field: str) -> bool:
    expected = _digest(payload.get(field))
    unsigned = dict(payload)
    unsigned.pop(field, None)
    return _valid_digest(expected) and _canonical_sha256(unsigned) == expected


def _witness_api() -> Any:
    from src.autoslice import acoustic_witness_adjudication
    return acoustic_witness_adjudication


def build_exact_source_transcript_request(
    witness_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Strip a blind-pinyin request down to physical source geometry only."""

    try:
        cue_indexes = list(witness_request["cue_indexes"])
        request: dict[str, Any] = {
            "schema_version": REQUEST_SCHEMA,
            "kind": "exact_final_source_transcript",
            "cue_indexes": cue_indexes,
            "matched_start_ms": int(witness_request["matched_start_ms"]),
            "matched_end_ms": int(witness_request["matched_end_ms"]),
            "context_start_ms": int(witness_request["context_start_ms"]),
            "context_end_ms": int(witness_request["context_end_ms"]),
            "source_media_timeline_offset_ms": int(
                witness_request["source_media_timeline_offset_ms"]
            ),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_GEOMETRY_INVALID") from exc
    if (
        len(cue_indexes) != 1
        or isinstance(cue_indexes[0], bool)
        or not isinstance(cue_indexes[0], int)
        or cue_indexes[0] <= 0
        or request["matched_end_ms"] <= request["matched_start_ms"]
        or request["context_end_ms"] <= request["context_start_ms"]
        or request["context_start_ms"] > request["matched_start_ms"]
        or request["context_end_ms"] < request["matched_end_ms"]
        or request["source_media_timeline_offset_ms"] < 0
    ):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_GEOMETRY_INVALID")
    request["request_sha256"] = _canonical_sha256(request)
    return request


def valid_exact_source_transcript_request(request: Mapping[str, Any]) -> bool:
    if set(request) != _REQUEST_KEYS or not _FORBIDDEN_PROVIDER_KEYS.isdisjoint(request):
        return False
    try:
        rebuilt = build_exact_source_transcript_request(request)
    except (TypeError, ValueError):
        return False
    return rebuilt == dict(request)


def exact_source_transcript_prompt(
    *, request: Mapping[str, Any], timeline_binding: Mapping[str, Any]
) -> str:
    """Return the fixed candidate-free prompt with exact in-clip markers."""

    source = timeline_binding.get("source_media")
    if not isinstance(source, Mapping):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_TIMELINE_INVALID")
    try:
        target_start = int(source["target_start_ms"])
        target_end = int(source["target_end_ms"])
        crop_start = int(source["crop_start_ms"])
        crop_end = int(source["crop_end_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_TIMELINE_INVALID") from exc
    if not (
        crop_start <= target_start < target_end <= crop_end
        and target_start - crop_start >= 0
        and target_end - crop_start > target_start - crop_start
    ):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_TIMELINE_INVALID")
    marker_start = target_start - crop_start
    marker_end = target_end - crop_start
    return f"""# Candidate-blind exact target transcription

Use only the attached black-frame audio.  You are a transcription witness,
not a subtitle judge.  The target speech begins at {marker_start} ms and ends
at {marker_end} ms within the attached clip.  Audio outside those markers is
context only: never include its words in exact_transcript.

Transcribe exactly what is audibly spoken inside the target markers, in the
original spoken language and native script.  Keep genuine English in Latin
letters and genuine numbers as heard.  Do not translate, normalize, guess
unheard words, consult subtitles/chat/context text, or choose between any
candidates (none were supplied).

Reply with exactly one JSON object, no markdown or other text:
{{"schema_version":"{PROVIDER_REPORT_SCHEMA}",
 "status":"OBSERVED" or "UNCERTAIN",
 "target_audible":true or false,
 "audible_language":"zh"|"ja"|"en"|"mixed"|"none",
 "exact_transcript":"single-line verbatim target transcript; empty only when no speech",
 "reason":"short acoustic note"}}
"""


def _timeline_matches_request(
    request: Mapping[str, Any], timeline: Mapping[str, Any]
) -> bool:
    delivery = timeline.get("delivery_local")
    source = timeline.get("source_media")
    if not isinstance(delivery, Mapping) or not isinstance(source, Mapping):
        return False
    try:
        offset = int(request["source_media_timeline_offset_ms"])
        matched_start = int(request["matched_start_ms"])
        matched_end = int(request["matched_end_ms"])
        context_start = int(request["context_start_ms"])
        context_end = int(request["context_end_ms"])
        source_target_start = int(source["target_start_ms"])
        source_target_end = int(source["target_end_ms"])
        crop_start = int(source["crop_start_ms"])
        crop_end = int(source["crop_end_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        timeline.get("schema_version") == "subtitle-audio-timeline-binding.v1"
        and timeline.get("source_media_timeline_offset_ms") == offset
        and delivery
        == {
            "target_start_ms": matched_start,
            "target_end_ms": matched_end,
            "context_start_ms": context_start,
            "context_end_ms": context_end,
        }
        and source_target_start == matched_start + offset
        and source_target_end == matched_end + offset
        and crop_start <= source_target_start < source_target_end <= crop_end
        and source_target_start - crop_start <= 499
        and crop_end - source_target_end <= 499
    )


def seal_exact_source_transcript_observation(
    *,
    request: Mapping[str, Any],
    exact_transcript: str,
    audible_language: str,
    source_media_sha256: str,
    audio_clip_sha256: str,
    provider: str,
    model: str,
    response_sha256: str,
    timeline_binding: Mapping[str, Any],
    key_tier: str | None = None,
    provider_route: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Seal a successful provider observation; it has no mutation authority."""

    if not valid_exact_source_transcript_request(request):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_REQUEST_INVALID")
    transcript = " ".join(str(exact_transcript or "").split())
    try:
        route = (
            dict(provider_route)
            if isinstance(provider_route, Mapping)
            else build_provider_route(
                provider=provider, model=model,
                audio_clip_sha256=audio_clip_sha256,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_OBSERVATION_INVALID") from exc
    if (
        not transcript
        or transcript != str(exact_transcript).strip()
        or "\n" in transcript
        or "\r" in transcript
        or "-->" in transcript
        or len(transcript) > 96
        or audible_language not in _ALLOWED_LANGUAGES - {"none"}
        or provider not in _ALGORITHMS
        or not model
        or not _valid_digest(source_media_sha256)
        or not _valid_digest(audio_clip_sha256)
        or not _valid_digest(response_sha256)
        or not _timeline_matches_request(request, timeline_binding)
        or not valid_provider_route(
            route, provider=provider, model=model,
            audio_clip_sha256=audio_clip_sha256,
        )
        or key_tier != route.get("accepted_key_tier")
    ):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_OBSERVATION_INVALID")
    prompt = exact_source_transcript_prompt(
        request=request, timeline_binding=timeline_binding
    )
    source = timeline_binding["source_media"]
    observation: dict[str, Any] = {
        "schema_version": OBSERVATION_SCHEMA,
        "status": "OBSERVED",
        "request_sha256": request["request_sha256"],
        "candidate_blind": True,
        "exact_transcript": transcript,
        "audible_language": audible_language,
        "source_media_sha256": _digest(source_media_sha256),
        "audio_clip_sha256": _digest(audio_clip_sha256),
        "prompt_contract": PROMPT_CONTRACT,
        "prompt_sha256": _text_sha256(prompt),
        "response_sha256": _digest(response_sha256),
        "provider": provider,
        "model": model,
        "algorithm_id": _ALGORITHMS[provider],
        "audio_start_ms": int(source["crop_start_ms"]),
        "audio_end_ms": int(source["crop_end_ms"]),
        "timeline_binding": dict(timeline_binding),
        "provider_route": route,
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        **({"key_tier": key_tier} if key_tier else {}),
    }
    observation["observation_sha256"] = _canonical_sha256(observation)
    return observation


def valid_exact_source_transcript_observation(
    observation: Mapping[str, Any], *, request: Mapping[str, Any]
) -> bool:
    allowed = {
        "schema_version", "status", "request_sha256", "candidate_blind",
        "exact_transcript", "audible_language", "source_media_sha256",
        "audio_clip_sha256", "prompt_contract", "prompt_sha256",
        "response_sha256", "provider", "model", "algorithm_id",
        "audio_start_ms", "audio_end_ms", "timeline_binding", "provider_route",
        "authority", "mutation_authorized", "observation_sha256", "key_tier",
        "served_from_cache",
    }
    transcript = observation.get("exact_transcript")
    timeline = observation.get("timeline_binding")
    route = observation.get("provider_route")
    provider = str(observation.get("provider") or "")
    if (
        not set(observation) <= allowed
        or not (allowed - {"key_tier", "served_from_cache"}) <= set(observation)
        or not valid_exact_source_transcript_request(request)
        or observation.get("schema_version") != OBSERVATION_SCHEMA
        or observation.get("status") != "OBSERVED"
        or observation.get("request_sha256") != request.get("request_sha256")
        or observation.get("candidate_blind") is not True
        or not isinstance(transcript, str)
        or not transcript
        or transcript != transcript.strip()
        or "\n" in transcript
        or "\r" in transcript
        or "-->" in transcript
        or len(transcript) > 96
        or observation.get("audible_language") not in _ALLOWED_LANGUAGES - {"none"}
        or provider not in _ALGORITHMS
        or observation.get("algorithm_id") != _ALGORITHMS[provider]
        or not isinstance(observation.get("model"), str)
        or not observation.get("model")
        or observation.get("prompt_contract") != PROMPT_CONTRACT
        or not isinstance(route, Mapping)
        or not valid_provider_route(
            route, provider=provider, model=str(observation.get("model") or ""),
            audio_clip_sha256=str(observation.get("audio_clip_sha256") or ""),
        )
        or observation.get("key_tier") != route.get("accepted_key_tier")
        or not all(
            _valid_digest(observation.get(key))
            for key in (
                "source_media_sha256",
                "audio_clip_sha256",
                "prompt_sha256",
                "response_sha256",
                "observation_sha256",
            )
        )
        or not isinstance(timeline, Mapping)
        or not _timeline_matches_request(request, timeline)
        or observation.get("audio_start_ms")
        != timeline["source_media"]["crop_start_ms"]
        or observation.get("audio_end_ms")
        != timeline["source_media"]["crop_end_ms"]
        or observation.get("prompt_sha256")
        != _text_sha256(
            exact_source_transcript_prompt(request=request, timeline_binding=timeline)
        )
        or observation.get("authority") != "EVIDENCE_ONLY"
        or observation.get("mutation_authorized") is not False
        or ("served_from_cache" in observation and observation["served_from_cache"] is not True)
        or not _sealed(observation, "observation_sha256")
    ):
        return False
    return True


def _same_timeline(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return dict(left) == dict(right)


def build_exact_source_transcript_handoff(
    *,
    observation: Mapping[str, Any],
    physical_request: Mapping[str, Any],
    initial_check_request: Mapping[str, Any],
    witness_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    srt_text: str,
    clip_context: Mapping[str, object] | None,
) -> dict[str, Any]:
    """Bind a valid observation to this exact-final input as candidate only."""

    candidate_id = (
        str(clip_context.get("candidate_id") or "").strip()
        if isinstance(clip_context, Mapping)
        else ""
    )
    context_sha = (
        _digest(clip_context.get("context_sha256"))
        if isinstance(clip_context, Mapping)
        else ""
    )
    draft_sha = (
        _digest(clip_context.get("whole_clip_draft_srt_sha256"))
        if isinstance(clip_context, Mapping)
        else ""
    )
    final_srt_sha = _text_sha256(srt_text)
    current = str(initial_check_request.get("current_cue") or "")
    rejected = str(initial_check_request.get("proposed_cue") or "")
    transcript = str(observation.get("exact_transcript") or "")
    witness_timeline = witness.get("timeline_binding")
    observation_timeline = observation.get("timeline_binding")
    witness_api = _witness_api()
    canonical_witness_request = witness_api.build_witness_request(initial_check_request)
    expected_physical = build_exact_source_transcript_request(canonical_witness_request)
    if not (
        dict(witness_request) == canonical_witness_request
        and expected_physical == dict(physical_request)
        and valid_exact_source_transcript_observation(
            observation, request=physical_request
        )
        and candidate_id
        and _valid_digest(context_sha)
        and _valid_digest(draft_sha)
        and current
        and transcript not in {current, rejected}
        and _valid_digest(initial_check_request.get("request_sha256"))
        and _canonical_sha256({key: value for key, value in initial_check_request.items() if key != "request_sha256"})
        == _digest(initial_check_request.get("request_sha256"))
        and witness_api.valid_witness_evidence(
            witness, request_sha256=canonical_witness_request["request_sha256"]
        )
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
        and witness.get("witness_protocol") == "blind_pinyin"
        and witness.get("source_media_sha256")
        == observation.get("source_media_sha256")
        and witness.get("audio_clip_sha256") == observation.get("audio_clip_sha256")
        and isinstance(witness_timeline, Mapping)
        and isinstance(observation_timeline, Mapping)
        and _same_timeline(witness_timeline, observation_timeline)
    ):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_HANDOFF_BINDING_INVALID")
    witness_sha = _canonical_sha256(dict(witness))
    run_payload = {
        "candidate_id": candidate_id,
        "clip_context_sha256": context_sha,
        "whole_clip_draft_srt_sha256": draft_sha,
        "input_request_sha256": _digest(
            initial_check_request.get("request_sha256")
        ),
        "parent_witness_request_sha256": _digest(
            witness_request.get("request_sha256")
        ),
        "final_srt_sha256": final_srt_sha,
    }
    handoff: dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA,
        "status": "PROPOSED",
        "decision_authority": "NONE",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "candidate_id": candidate_id,
        "clip_context_sha256": "sha256:" + context_sha,
        "whole_clip_draft_srt_sha256": "sha256:" + draft_sha,
        "input_request_sha256": "sha256:"
        + _digest(initial_check_request.get("request_sha256")),
        "input_request": dict(initial_check_request),
        "input_srt_sha256": "sha256:" + final_srt_sha,
        "final_srt_sha256": "sha256:" + final_srt_sha,
        "current_cue_sha256": "sha256:" + _text_sha256(current),
        "rejected_proposed_sha256": "sha256:" + _text_sha256(rejected),
        "run_binding_sha256": "sha256:" + _canonical_sha256(run_payload),
        "parent_witness_request_sha256": "sha256:"
        + _digest(witness_request.get("request_sha256")),
        "parent_witness_request": dict(witness_request),
        "parent_witness_observation_sha256": "sha256:" + witness_sha,
        "physical_request_sha256": "sha256:"
        + _digest(physical_request.get("request_sha256")),
        "physical_request": dict(physical_request),
        "provider_observation_sha256": "sha256:"
        + _digest(observation.get("observation_sha256")),
        "source_media_sha256": "sha256:"
        + _digest(observation.get("source_media_sha256")),
        "audio_clip_sha256": "sha256:"
        + _digest(observation.get("audio_clip_sha256")),
        "prompt_sha256": "sha256:" + _digest(observation.get("prompt_sha256")),
        "response_sha256": "sha256:"
        + _digest(observation.get("response_sha256")),
        "provider": observation.get("provider"),
        "model": observation.get("model"),
        "algorithm_id": observation.get("algorithm_id"),
        "timeline_binding": dict(observation_timeline),
        "proposed_cue": transcript,
        "proposed_cue_sha256": "sha256:" + _text_sha256(transcript),
        "provider_observation": dict(observation),
    }
    handoff["receipt_sha256"] = "sha256:" + _canonical_sha256(handoff)
    return handoff


def valid_exact_source_transcript_handoff(
    handoff: Mapping[str, Any],
    *,
    srt_text: str | None = None,
    check_request: Mapping[str, Any] | None = None,
    witness: Mapping[str, Any] | None = None,
    clip_context: Mapping[str, object] | None = None,
) -> bool:
    observation = handoff.get("provider_observation")
    physical_request = handoff.get("physical_request")
    input_request = handoff.get("input_request")
    parent_witness_request = handoff.get("parent_witness_request")
    timeline = handoff.get("timeline_binding")
    try:
        witness_api = _witness_api()
        canonical_parent_request = witness_api.build_witness_request(input_request)
        expected_physical = build_exact_source_transcript_request(
            canonical_parent_request
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    if not (
        set(handoff) == _HANDOFF_KEYS
        and handoff.get("schema_version") == HANDOFF_SCHEMA
        and handoff.get("status") == "PROPOSED"
        and handoff.get("decision_authority") == "NONE"
        and handoff.get("authority") == "EVIDENCE_ONLY"
        and handoff.get("mutation_authorized") is False
        and isinstance(handoff.get("candidate_id"), str)
        and bool(handoff.get("candidate_id"))
        and isinstance(handoff.get("proposed_cue"), str)
        and bool(handoff.get("proposed_cue"))
        and isinstance(observation, Mapping)
        and isinstance(physical_request, Mapping)
        and isinstance(input_request, Mapping)
        and isinstance(parent_witness_request, Mapping)
        and isinstance(timeline, Mapping)
        and valid_exact_source_transcript_observation(
            observation, request=physical_request
        )
        and _sealed(handoff, "receipt_sha256")
        and all(
            _valid_digest(handoff.get(key))
            for key in (
                "clip_context_sha256",
                "whole_clip_draft_srt_sha256",
                "input_request_sha256",
                "input_srt_sha256",
                "final_srt_sha256",
                "current_cue_sha256",
                "rejected_proposed_sha256",
                "run_binding_sha256",
                "parent_witness_request_sha256",
                "parent_witness_observation_sha256",
                "physical_request_sha256",
                "provider_observation_sha256",
                "source_media_sha256",
                "audio_clip_sha256",
                "prompt_sha256",
                "response_sha256",
                "proposed_cue_sha256",
                "receipt_sha256",
            )
        )
        and _digest(handoff.get("provider_observation_sha256"))
        == _digest(observation.get("observation_sha256"))
        and _digest(handoff.get("input_request_sha256"))
        == _digest(input_request.get("request_sha256"))
        and _digest(handoff.get("input_srt_sha256"))
        == _digest(handoff.get("final_srt_sha256"))
        and _canonical_sha256(
            {key: value for key, value in input_request.items() if key != "request_sha256"}
        )
        == _digest(input_request.get("request_sha256"))
        and _digest(handoff.get("current_cue_sha256"))
        == _text_sha256(str(input_request.get("current_cue") or ""))
        and _digest(handoff.get("rejected_proposed_sha256"))
        == _text_sha256(str(input_request.get("proposed_cue") or ""))
        and _digest(handoff.get("physical_request_sha256"))
        == _digest(physical_request.get("request_sha256"))
        and expected_physical == dict(physical_request)
        and dict(parent_witness_request) == canonical_parent_request
        and _digest(handoff.get("parent_witness_request_sha256"))
        == _digest(canonical_parent_request.get("request_sha256"))
        and _digest(handoff.get("source_media_sha256"))
        == _digest(observation.get("source_media_sha256"))
        and _digest(handoff.get("audio_clip_sha256"))
        == _digest(observation.get("audio_clip_sha256"))
        and _digest(handoff.get("prompt_sha256"))
        == _digest(observation.get("prompt_sha256"))
        and _digest(handoff.get("response_sha256"))
        == _digest(observation.get("response_sha256"))
        and handoff.get("provider") == observation.get("provider")
        and handoff.get("model") == observation.get("model")
        and handoff.get("algorithm_id") == observation.get("algorithm_id")
        and dict(timeline) == dict(observation.get("timeline_binding") or {})
        and _digest(handoff.get("proposed_cue_sha256"))
        == _text_sha256(str(handoff.get("proposed_cue") or ""))
    ):
        return False
    run_payload = {
        "candidate_id": handoff.get("candidate_id"),
        "clip_context_sha256": _digest(handoff.get("clip_context_sha256")),
        "whole_clip_draft_srt_sha256": _digest(
            handoff.get("whole_clip_draft_srt_sha256")
        ),
        "input_request_sha256": _digest(handoff.get("input_request_sha256")),
        "parent_witness_request_sha256": _digest(
            handoff.get("parent_witness_request_sha256")
        ),
        "final_srt_sha256": _digest(handoff.get("final_srt_sha256")),
    }
    if _digest(handoff.get("run_binding_sha256")) != _canonical_sha256(run_payload):
        return False
    if srt_text is not None:
        final_sha = _text_sha256(srt_text)
        if not (
            _digest(handoff.get("input_srt_sha256")) == final_sha
            and _digest(handoff.get("final_srt_sha256")) == final_sha
        ):
            return False
    if check_request is not None:
        current = str(check_request.get("current_cue") or "")
        proposed = str(check_request.get("proposed_cue") or "")
        provenance = check_request.get("candidate_provenance")
        if not (
            current == str(input_request.get("current_cue") or "")
            and _digest(handoff.get("current_cue_sha256"))
            == _text_sha256(current)
            and handoff.get("proposed_cue") == proposed
            and _digest(handoff.get("proposed_cue_sha256"))
            == _text_sha256(proposed)
            and isinstance(provenance, Mapping)
            and provenance.get("kind") == "bounded_exact_source_audio_transcript"
            and _digest(provenance.get("handoff_receipt_sha256"))
            == _digest(handoff.get("receipt_sha256"))
            and _digest(provenance.get("rejected_proposed_sha256"))
            == _digest(handoff.get("rejected_proposed_sha256"))
        ):
            return False
    if witness is not None:
        parent_request_match = (
            _digest(handoff.get("parent_witness_request_sha256"))
            == _digest(witness.get("request_sha256"))
        )
        active_witness_request = canonical_parent_request
        replay_geometry_match = False
        if not parent_request_match and check_request is not None:
            try:
                active_witness_request = witness_api.build_witness_request(
                    check_request
                )
                replay_geometry_match = (
                    _canonical_sha256({key: value for key, value in check_request.items() if key != "request_sha256"})
                    == _digest(check_request.get("request_sha256"))
                    and build_exact_source_transcript_request(active_witness_request)
                    == dict(physical_request)
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                replay_geometry_match = False
        if not (
            witness_api.valid_witness_evidence(
                witness,
                request_sha256=active_witness_request["request_sha256"],
            )
            and witness.get("status") == "OBSERVED"
            and witness.get("target_audible") is True
            and witness.get("witness_protocol") == "blind_pinyin"
            and (
                (
                    parent_request_match
                    and _digest(
                        handoff.get("parent_witness_observation_sha256")
                    )
                    == _canonical_sha256(dict(witness))
                )
                or (not parent_request_match and replay_geometry_match)
            )
            and _digest(handoff.get("source_media_sha256"))
            == _digest(witness.get("source_media_sha256"))
            and _digest(handoff.get("audio_clip_sha256"))
            == _digest(witness.get("audio_clip_sha256"))
            and dict(timeline) == dict(witness.get("timeline_binding") or {})
        ):
            return False
    if clip_context is not None:
        candidate_id = str(clip_context.get("candidate_id") or "")
        context_sha = _digest(clip_context.get("context_sha256"))
        draft_sha = _digest(clip_context.get("whole_clip_draft_srt_sha256"))
        if not (
            handoff.get("candidate_id") == candidate_id
            and _digest(handoff.get("clip_context_sha256")) == context_sha
            and _digest(handoff.get("whole_clip_draft_srt_sha256")) == draft_sha
        ):
            return False
    return True
