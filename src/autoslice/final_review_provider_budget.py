"""Zero-provider replay used by exact-final's adjudication-call budget."""

from __future__ import annotations

from typing import Any, Callable, Mapping


AdjudicateContext = Callable[..., tuple[str, dict[str, Any]]]


class _CacheMiss(RuntimeError):
    """A cache-only replay reached a provider seam."""


class ContextAdjudicationBudget:
    """Count fresh provider adjudications while replaying double cache hits."""

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
    ) -> None:
        self.limit = limit
        self.entity_verifier = entity_verifier
        self.clip_context = clip_context
        self.source_media_timeline_offset_ms = source_media_timeline_offset_ms
        self.judge_llm_call = judge_llm_call
        self.screen_read_probe = screen_read_probe
        self.adjudicate_context = adjudicate_context
        self.provider_adjudication_count = 0

    def adjudicate(self, srt_text: str, finding: Mapping[str, Any]) -> dict[str, Any]:
        replayed = replay_cached_context_adjudication(
            srt_text,
            finding,
            entity_verifier=self.entity_verifier,
            clip_context=self.clip_context,
            source_media_timeline_offset_ms=(self.source_media_timeline_offset_ms),
            adjudicate_context=self.adjudicate_context,
        )
        if replayed is not None:
            return replayed
        if self.provider_adjudication_count >= self.limit:
            return {
                "schema_version": "subtitle-span-adjudication.v1",
                "status": "SKIPPED_BUDGET",
                "repaired": False,
                "provider_adjudication_count": (self.provider_adjudication_count),
                "provider_adjudication_budget": self.limit,
            }
        self.provider_adjudication_count += 1
        _output, adjudication = self.adjudicate_context(
            srt_text,
            finding,
            entity_verifier=self.entity_verifier,
            clip_context=self.clip_context,
            source_media_timeline_offset_ms=(self.source_media_timeline_offset_ms),
            judge_llm_call=self.judge_llm_call,
            screen_read_probe=self.screen_read_probe,
        )
        return adjudication


def replay_cached_context_adjudication(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    entity_verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    clip_context: Mapping[str, object] | None,
    source_media_timeline_offset_ms: int,
    adjudicate_context: AdjudicateContext,
) -> dict[str, Any] | None:
    """Return a strict witness+CPA double hit, or ``None`` on any miss.

    The sentinel transport makes this safe after the provider-call cap.  The
    witness probe owns physical source/audio/provider binding; the judge cache
    owns the exact current/base/proposed/context request.
    """

    cache_probe = getattr(entity_verifier, "probe_witness_cache", None)
    if not callable(cache_probe):
        return None
    witness_hits: list[Mapping[str, Any]] = []
    witness_misses = 0
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

    def provider_forbidden(_prompt: str) -> str:
        nonlocal provider_seam_attempts
        provider_seam_attempts += 1
        raise _CacheMiss("cache-only replay reached a provider seam")

    try:
        _output, adjudication = adjudicate_context(
            srt_text,
            finding,
            entity_verifier=cached_witness_only,
            clip_context=clip_context,
            source_media_timeline_offset_ms=source_media_timeline_offset_ms,
            judge_llm_call=provider_forbidden,
            screen_read_probe=None,
        )
    except _CacheMiss:
        return None
    witness = adjudication.get("verdict")
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
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
    ):
        return None
    replayed = dict(adjudication)
    replayed["provider_budget_replay"] = {
        "schema_version": "final-review-provider-budget-replay.v1",
        "status": "PASS",
        "provider_call_count": 0,
        "witness_cache_hit": True,
        "judge_cache_hit": True,
        "witness_request_sha256": _digest(witness.get("request_sha256")),
        "witness_audio_clip_sha256": _digest(witness.get("audio_clip_sha256")),
        "witness_prompt_sha256": _digest(witness.get("prompt_sha256")),
        "witness_response_sha256": _digest(witness.get("response_sha256")),
        "judge_prompt_sha256": _digest(judge.get("prompt_sha256")),
        "judge_completion_sha256": _digest(judge.get("completion_sha256")),
    }
    return replayed


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")
