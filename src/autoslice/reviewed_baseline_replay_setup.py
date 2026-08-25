"""Typed setup helpers for reviewed-baseline replay finalization."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Protocol


class _EntityContext(Protocol):
    verify_confusable_entity: Callable


def build_reviewed_baseline_replay_spec(
    *,
    date: str,
    candidate_id: str,
    output_root: Path,
    story: Mapping[str, object],
    normalized_piece: Mapping[str, object],
    spec_piece: Mapping[str, object],
    clip_context_path: Path,
    clip_context: Mapping[str, object],
    baseline_config: Mapping[str, object],
    boundary: Mapping[str, object],
    error_factory: Callable[[str], Exception],
    recovery_publication_authority: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Construct the bound replay spec without widening its authority surface."""

    from src.autoslice.reviewed_baseline_replay_projection import canonical_talk_delivery_basename

    selection_hook = str(story.get("selection_hook") or "")
    if story.get("candidate_id") != candidate_id or not selection_hook:
        raise error_factory("REPLAY_STORY_CONTRACT_BINDING_DRIFT")
    given_title = None
    if recovery_publication_authority is not None:
        from src.autoslice.recovery_title_authority import expected_recovery_publish_title

        given_title = expected_recovery_publish_title(recovery_publication_authority)
    return {
        "date": date, "candidate_id": candidate_id, "output_root": str(output_root),
        "given_title": given_title,
        "recovery_publication_authority": (
            dict(recovery_publication_authority)
            if recovery_publication_authority is not None else None
        ),
        # Use the ordinary CID-injective Talk basename; never fall back to a
        # bare candidate id when a historical replay is prepared privately.
        "delivery_name": canonical_talk_delivery_basename(
            selection_hook, candidate_id, error=error_factory,
        ),
        "selection_hook": selection_hook,
        "selection_scorecard": story.get("selection_scorecard"),
        "session_relation_authority": story.get("session_relation_authority"),
        "story_contract": dict(story), "source_piece": normalized_piece,
        "clip_context_path": str(clip_context_path), "clip_context": clip_context,
        "pieces": [spec_piece], "subtitle_redelivery_baseline": baseline_config,
        "boundary_semantic_review": boundary.get("boundary_semantic_review"),
    }


def resolve_replay_exact_final_reviewer(
    *,
    explicit_reviewer: Callable[..., dict[str, object]] | None,
    fallback_reviewer: object,
    use_production: bool,
    entity_verifier: Callable | None,
    text_adapters: object | None,
    plan: object,
    candidate_id: str,
    spec: Mapping[str, object],
    spec_piece: Mapping[str, object],
    padded: Path,
    out_root: Path,
    runtime_authority_root: Path,
    release_text_path: Path,
    read_text: Callable[[Path], str],
    reconstruct_chat: Callable[..., object],
    replay_reviewer: Callable[..., object],
    provider_invocation: Callable[[], None] | None,
    error_factory: Callable[[str], Exception],
) -> tuple[Callable[..., dict[str, object]], Callable | None]:
    """Resolve the mandatory fresh exact-final review closure for replay.

    The precedence is intentional: an explicit reviewer wins; otherwise the
    production mode builds a fresh reviewer before any adapter fallback; only
    with production mode off does the historical adapter fallback participate.
    """

    reviewer = explicit_reviewer
    if reviewer is None and use_production:
        if entity_verifier is None:
            if text_adapters is None:
                raise error_factory("REPLAY_ENTITY_VERIFIER_MISSING")
            from src.autoslice.jingting_chunker import parse_srt_cues
            from src.autoslice.producer_text_pipeline import _build_entity_verification_context

            release_text = read_text(release_text_path)
            if not parse_srt_cues(release_text):
                raise error_factory("REPLAY_RELEASE_TRUTH_INVALID")
            entity_context: _EntityContext = _build_entity_verification_context(
                spec=spec, padded=padded,
                padded_dur=spec_piece["end_ms"] - spec_piece["start_ms"],
                host="localhost", text_override_path=None, cid=candidate_id,
                out_root=out_root, srt_text=release_text,
                authoritative_chat=list(reconstruct_chat(
                    spec["clip_context"], plan=plan,
                    source_media_sha256=str(spec_piece["source_media_sha256"]),
                )),
                adapters=text_adapters,
            )
            entity_verifier = entity_context.verify_confusable_entity
        reviewer = replay_reviewer(
            plan, spec=spec, clip_context=spec["clip_context"],
            runtime_root=runtime_authority_root, out_root=out_root, padded=padded,
            verify_confusable_entity=entity_verifier,
            provider_invocation=provider_invocation,
        )
    reviewer = reviewer or fallback_reviewer
    if not callable(reviewer):
        raise error_factory("REPLAY_EXACT_FINAL_REVIEW_ADAPTER_MISSING")
    return reviewer, entity_verifier
