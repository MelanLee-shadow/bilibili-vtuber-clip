"""Provider adjudication accounting and zero-provider replay helpers.

``ContextAdjudicationBudget.limit`` is retained as a compatibility name for
callers that still pass the old exact-final cap.  It is a soft threshold now:
crossing it records pressure, but it does not discard a new finding or claim
that the finding was adjudicated when provider calls are disabled.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Mapping


AdjudicateContext = Callable[..., tuple[str, dict[str, Any]]]


class _CacheMiss(RuntimeError):
    """A cache-only replay reached a provider seam."""


_FINGERPRINT_AUDIT_KEYS = frozenset(
    {
        "cpa_resource_pressure",
        "context_audio_adjudication",
        "exact_release_adjudication",
        "provider_budget_replay",
        "same_input_replay",
        "_trusted_priority_candidate",
        "_transient_attempt",
    }
)


def _nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _public_identity(value: object) -> object:
    """Return the public, JSON-shaped identity of a finding/context value.

    Audit bookkeeping is deliberately excluded so that feeding a finding back
    through the same budget instance cannot turn the exact same source input
    into a new provider admission.  Source, ``why``, provenance and all other
    public fields remain part of the identity.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _public_identity(item)
            for key, item in value.items()
            if str(key) not in _FINGERPRINT_AUDIT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public_identity(item) for item in value]
    if isinstance(value, set):
        items = [_public_identity(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, default=repr
            ),
        )
    return value


def _adjudication_input_fingerprint(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    clip_context: Mapping[str, object] | None,
    source_media_timeline_offset_ms: int,
    judge_llm_call: Callable[[str], str] | None,
) -> str:
    identity = {
        "srt_text": srt_text,
        "finding": _public_identity(finding),
        "clip_context": _public_identity(clip_context),
        "source_media_timeline_offset_ms": source_media_timeline_offset_ms,
        "cpa_cache_identity": _public_identity(
            getattr(judge_llm_call, "cpa_cache_identity", None)
        ),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ContextAdjudicationBudget:
    """Count fresh adjudications while preserving strict cache-only replay.

    ``limit`` is a soft pressure threshold.  It remains present for old
    callers and in audit output, but it never truncates fresh provider work.
    ``provider_calls_allowed=False`` is reserved for an explicit offline or
    cache-only pass; a cache miss then returns an unresolved sentinel.
    """

    def __init__(
        self,
        *,
        limit: int,
        entity_verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
        clip_context: Mapping[str, object] | None,
        source_media_timeline_offset_ms: int,
        judge_llm_call: Callable[[str], str] | None,
        screen_read_probe: Callable[[int, int], Mapping[str, Any]] | None,
        adjudicate_context: AdjudicateContext,
        provider_calls_allowed: bool = True,
        initial_provider_adjudication_count: int = 0,
    ) -> None:
        self.limit = _nonnegative_int(limit, name="limit")
        if not isinstance(provider_calls_allowed, bool):
            raise TypeError("provider_calls_allowed must be a bool")
        self.provider_calls_allowed = provider_calls_allowed
        self.initial_provider_adjudication_count = _nonnegative_int(
            initial_provider_adjudication_count,
            name="initial_provider_adjudication_count",
        )
        self.entity_verifier = entity_verifier
        self.exact_source_transcript_provider = getattr(
            entity_verifier, "exact_source_transcript", None
        )
        self.clip_context = clip_context
        self.source_media_timeline_offset_ms = source_media_timeline_offset_ms
        self.judge_llm_call = judge_llm_call
        self.screen_read_probe = screen_read_probe
        self.adjudicate_context = adjudicate_context
        self.provider_adjudication_count = self.initial_provider_adjudication_count
        self._adjudications_by_input: dict[str, dict[str, Any]] = {}

    def adjudicate(self, srt_text: str, finding: Mapping[str, Any]) -> dict[str, Any]:
        input_fingerprint = _adjudication_input_fingerprint(
            srt_text,
            finding,
            clip_context=self.clip_context,
            source_media_timeline_offset_ms=self.source_media_timeline_offset_ms,
            judge_llm_call=self.judge_llm_call,
        )
        judge_call = with_cpa_resource_pressure(
            self.judge_llm_call, self.provider_adjudication_count + 1, self.limit
        )
        replayed = replay_cached_context_adjudication(
            srt_text,
            finding,
            entity_verifier=self.entity_verifier,
            clip_context=self.clip_context,
            source_media_timeline_offset_ms=(self.source_media_timeline_offset_ms),
            adjudicate_context=self.adjudicate_context,
            judge_llm_call=judge_call,
        )
        if replayed is not None:
            result = self._with_resource_pressure(
                replayed,
                provider_calls_this_decision=0,
                reason_code="CACHE_REPLAY",
            )
            self._adjudications_by_input[input_fingerprint] = copy.deepcopy(result)
            return result

        previous = self._adjudications_by_input.get(input_fingerprint)
        if previous is not None:
            return self._same_input_replay(previous, input_fingerprint)

        if not self.provider_calls_allowed:
            result = {
                "schema_version": "subtitle-span-adjudication.v1",
                "status": "SKIPPED_PROVIDER_DISABLED",
                "repaired": False,
                "provider_adjudication_count": (self.provider_adjudication_count),
                "provider_adjudication_budget": self.limit,
            }
            result = self._with_resource_pressure(
                result,
                provider_calls_this_decision=0,
                reason_code="PROVIDER_CALLS_DISABLED_CACHE_MISS",
            )
            self._adjudications_by_input[input_fingerprint] = copy.deepcopy(result)
            return result

        self.provider_adjudication_count += 1
        _output, adjudication = self.adjudicate_context(
            srt_text,
            finding,
            entity_verifier=self.entity_verifier,
            clip_context=self.clip_context,
            source_media_timeline_offset_ms=(self.source_media_timeline_offset_ms),
            judge_llm_call=judge_call,
            screen_read_probe=self.screen_read_probe,
            exact_source_transcript_provider=(
                self.exact_source_transcript_provider
                if callable(self.exact_source_transcript_provider)
                else None
            ),
        )
        result = self._with_resource_pressure(
            adjudication,
            provider_calls_this_decision=1,
            reason_code=(
                "SOFT_LIMIT_EXCEEDED_CONTINUED"
                if self.provider_adjudication_count > self.limit
                else "WITHIN_SOFT_LIMIT"
            ),
        )
        self._adjudications_by_input[input_fingerprint] = copy.deepcopy(result)
        return result

    def _with_resource_pressure(
        self,
        adjudication: Mapping[str, Any],
        *,
        provider_calls_this_decision: int,
        reason_code: str,
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(adjudication))
        result["cpa_resource_pressure"] = {
            "provider_adjudication_count": self.provider_adjudication_count,
            "soft_limit": self.limit,
            "soft_limit_exceeded": self.provider_adjudication_count > self.limit,
            "provider_calls_this_decision": provider_calls_this_decision,
            "reason_code": reason_code,
        }
        return result

    def _same_input_replay(
        self, adjudication: Mapping[str, Any], input_fingerprint: str
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(adjudication))
        result["same_input_replay"] = {
            "schema_version": "context-adjudication-same-input-replay.v1",
            "status": "PASS",
            "provider_call_count": 0,
            "input_fingerprint_sha256": input_fingerprint,
            "original_status": result.get("status"),
        }
        result["cpa_resource_pressure"] = {
            "provider_adjudication_count": self.provider_adjudication_count,
            "soft_limit": self.limit,
            "soft_limit_exceeded": self.provider_adjudication_count > self.limit,
            "provider_calls_this_decision": 0,
            "reason_code": "SAME_INPUT_REPLAY",
        }
        return result


def with_cpa_resource_pressure(
    call: Callable[[str], str] | None, count: int, soft_target: int,
) -> Callable[[str], str] | None:
    """Expose growing resource pressure to the existing CPA word-choice prompt."""
    if call is None or count <= soft_target:
        return call

    def pressured_judge(prompt: str) -> str:
        return call(prompt)

    for attr in ("cpa_cache_identity", "provider_runtime_binding", "provider_diagnostics"):
        if hasattr(call, attr):
            setattr(pressured_judge, attr, getattr(call, attr))
    # Keep exact counts in the decision ledger. Changing only that counter
    # must not change every judge prompt and defeat strict cached replay.
    ratio = count // max(soft_target, 1)
    band = 8 if ratio >= 8 else 4 if ratio >= 4 else 2 if ratio >= 2 else 1
    pressured_judge.cpa_resource_pressure = {
        "pressure_multiple": band, "soft_target": soft_target,
    }
    return pressured_judge


def replay_cached_context_adjudication(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    entity_verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    clip_context: Mapping[str, object] | None,
    source_media_timeline_offset_ms: int,
    adjudicate_context: AdjudicateContext,
    judge_llm_call: Callable[[str], str] | None = None,
) -> dict[str, Any] | None:
    """Return a strict witness+CPA double/triple hit, or ``None`` on any miss.

    The sentinel transport makes this safe in an explicit cache-only pass and
    after the soft provider-call threshold.  The witness probe owns physical
    source/audio/provider binding; the judge cache owns the exact
    current/base/proposed/context request.
    """

    cache_probe = getattr(entity_verifier, "probe_witness_cache", None)
    if not callable(cache_probe):
        return None
    witness_hits: list[Mapping[str, Any]] = []
    witness_misses = 0
    exact_cache_probe = getattr(
        entity_verifier, "probe_exact_source_transcript_cache", None
    )
    exact_hits: list[Mapping[str, Any]] = []
    exact_misses = 0
    provider_seam_attempts = 0

    def cached_witness_only(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        nonlocal witness_misses
        result = cache_probe(request)
        if isinstance(result, Mapping) and result.get("served_from_cache") is True:
            witness_hits.append(result)
            return result
        witness_misses += 1
        return None

    cached_witness_only.probe_witness_cache = cached_witness_only

    def provider_forbidden(_prompt: str) -> str:
        nonlocal provider_seam_attempts
        provider_seam_attempts += 1
        raise _CacheMiss("cache-only replay reached a provider seam")

    # A no-network sentinel still needs the actual caller's model/effort.
    # Unknown identity remains a cache miss; it must not inherit an old model.
    provider_forbidden.cpa_cache_identity = getattr(
        judge_llm_call, "cpa_cache_identity", None
    )
    if hasattr(judge_llm_call, "cpa_resource_pressure"):
        provider_forbidden.cpa_resource_pressure = judge_llm_call.cpa_resource_pressure

    def cached_exact_only(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        nonlocal exact_misses
        result = exact_cache_probe(request) if callable(exact_cache_probe) else None
        if isinstance(result, Mapping) and result.get("served_from_cache") is True:
            exact_hits.append(result)
            return result
        exact_misses += 1
        return None

    try:
        _output, adjudication = adjudicate_context(
            srt_text,
            finding,
            entity_verifier=cached_witness_only,
            clip_context=clip_context,
            source_media_timeline_offset_ms=source_media_timeline_offset_ms,
            judge_llm_call=provider_forbidden,
            screen_read_probe=None,
            exact_source_transcript_provider=(
                cached_exact_only
                if callable(getattr(entity_verifier, "exact_source_transcript", None))
                else None
            ),
        )
    except _CacheMiss:
        return None
    witness = adjudication.get("verdict")
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    exact_handoff = adjudication.get("exact_source_transcript_handoff")
    exact_attempted = bool(exact_hits or exact_misses)
    if not (
        witness_hits
        and witness_misses == 0
        and provider_seam_attempts == 0
        and isinstance(witness, Mapping)
        and witness.get("served_from_cache") is True
        and isinstance(request, Mapping)
        and isinstance(judge, Mapping)
        and judge.get("status") == "JUDGED"
        and judge.get("served_from_cache") is True
        and _digest(judge.get("check_request_sha256")) == _digest(request.get("request_sha256"))
        and _digest(witness_judge.get("witness_request_sha256"))
        == _digest(witness.get("request_sha256"))
        and exact_misses == 0
        and (
            not exact_attempted
            or (
                exact_hits
                and isinstance(exact_handoff, Mapping)
                and exact_handoff.get("status") == "PROPOSED"
            )
        )
    ):
        return None
    replayed = dict(adjudication)
    replayed["provider_budget_replay"] = {
        "schema_version": "final-review-provider-budget-replay.v1",
        "status": "PASS",
        "provider_call_count": 0,
        "witness_cache_hit": True,
        "judge_cache_hit": True,
        "exact_source_transcript_cache_hit": exact_attempted,
        "witness_request_sha256": _digest(witness.get("request_sha256")),
        "witness_audio_clip_sha256": _digest(witness.get("audio_clip_sha256")),
        "witness_prompt_sha256": _digest(witness.get("prompt_sha256")),
        "witness_response_sha256": _digest(witness.get("response_sha256")),
        "judge_prompt_sha256": _digest(judge.get("prompt_sha256")),
        "judge_completion_sha256": _digest(judge.get("completion_sha256")),
        **(
            {
                "exact_audio_clip_sha256": _digest(
                    exact_hits[-1].get("audio_clip_sha256")
                ),
                "exact_observation_sha256": _digest(
                    exact_hits[-1].get("observation_sha256")
                ),
            }
            if exact_attempted
            else {}
        ),
    }
    return replayed


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")
