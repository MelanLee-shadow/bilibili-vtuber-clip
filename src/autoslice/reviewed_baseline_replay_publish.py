"""Frozen title/cover carry for the reviewed-baseline replay lane.

This remains lane-owned: the generic producer finalizer cannot opt into a
historical public-surface carry merely by passing a title or cover path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping


def replay_publish_adapter(
    plan: object, *, source_fact_llm: Callable[..., object], private_package: Path,
    runtime_authority_root: Path,
) -> Callable[..., dict[str, object]]:
    """Build the canonical finalizer adapter for a bound frozen surface."""

    # Delayed imports avoid a circular dependency: the replay module owns the
    # sealed plan and hash-bound private copy helpers, while this small module
    # owns the policy that fresh source-fact output cannot rewrite title/hook.
    from src.autoslice.publish_staging import _stage_publish_draft
    from src.autoslice.source_fact_review import source_fact_review_passes
    from src.autoslice.story_contract import cover_story_contract_binding
    from src.autoslice.candidate_public_text_surface_authority import (
        CandidatePublicTextSurfaceAuthorityError,
        load_candidate_public_text_surface_authority,
    )
    from src.autoslice import reviewed_baseline_replay as replay

    def stage(record: Mapping[str, object], **kwargs: object) -> dict[str, object]:
        old = replay._load_json(
            replay.regular_binding(plan.record_path, label="RECORD"), label="RECORD",
        )
        old_staging = old.get("publish_staging")
        if not isinstance(old_staging, Mapping):
            raise replay.ReviewedBaselineReplayError("REPLAY_PUBLISH_STAGING_MISSING")
        title = old_staging.get("title")
        generation = old_staging.get("cover_generation")
        cover_path = old_staging.get("cover_path")
        old_story = old.get("story_contract")
        old_hook = old_staging.get("selection_hook") or (
            old_story.get("selection_hook")
            if isinstance(old_story, Mapping)
            else None
        )
        hashes = old.get("artifact_hashes")
        cover_sha = hashes.get("cover_sha256") if isinstance(hashes, Mapping) else None
        if not all(
            isinstance(value, str)
            for value in (title, cover_path, cover_sha, old_hook)
        ) or not isinstance(generation, Mapping):
            raise replay.ReviewedBaselineReplayError("REPLAY_FROZEN_PUBLICATION_INVALID")
        try:
            authority = load_candidate_public_text_surface_authority(plan.candidate_id)
        except CandidatePublicTextSurfaceAuthorityError as exc:
            raise replay.ReviewedBaselineReplayError(
                f"REPLAY_PUBLIC_TEXT_AUTHORITY_INVALID:{exc}"
            ) from exc
        root_reviewed = bool(authority and authority.is_root_reviewed_resolution)
        expected_title = authority.resolved_title if root_reviewed else title
        expected_hook = authority.resolved_selection_hook if root_reviewed else old_hook
        cover = replay.regular_binding(Path(cover_path), label="COVER")
        if cover.sha256 != cover_sha or generation.get("final_cover_sha256") != cover_sha:
            raise replay.ReviewedBaselineReplayError("REPLAY_COVER_BINDING_DRIFT")
        private_cover, private_generation = replay._private_carried_cover_generation(
            plan, private_package=private_package, cover_path=cover.path,
            cover_sha256=cover_sha, generation=generation,
            runtime_authority_root=runtime_authority_root,
        )

        def carry(updated: Mapping[str, object], **_unused: object) -> dict[str, object]:
            story = updated.get("story_contract")
            if not isinstance(story, Mapping):
                raise replay.ReviewedBaselineReplayError("REPLAY_STORY_CONTRACT_MISSING")
            copied = dict(private_generation)
            copied["story_contract"] = cover_story_contract_binding(story)
            return {
                "status": "AI_COVER_READY", "cover_path": str(private_cover),
                "cover_sha256": cover.sha256, "cover_generation": copied,
                "reason_codes": [],
            }

        staged = _stage_publish_draft(
            dict(record), candidate_id=plan.candidate_id, title=expected_title,
            cues=kwargs["cues"], run_ffmpeg=bool(kwargs.get("run_ffmpeg")),
            title_llm_call=None, art_direction_llm_call=None, skip_cover=False,
            selection_hook=old_hook,
            stage_cover=(None if root_reviewed else carry), source_fact_llm_call=source_fact_llm,
            private_artifact_root=None,
        )
        if not isinstance(staged, Mapping):
            raise replay.ReviewedBaselineReplayError("REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT")
        staged_publish = staged.get("publish_staging")
        staged_story = staged.get("story_contract")
        if not isinstance(staged_publish, Mapping):
            raise replay.ReviewedBaselineReplayError("REPLAY_PUBLISH_STAGING_MISSING")
        if not isinstance(staged_story, Mapping):
            raise replay.ReviewedBaselineReplayError("REPLAY_STORY_CONTRACT_MISSING")
        review = staged_publish.get("source_fact_review")
        old_review = old_staging.get("source_fact_review")
        source_fact_verified = source_fact_review_passes(review) or (
            review is None and source_fact_review_passes(old_review)
            and old_staging.get("title") == expected_title and old_hook == staged_story.get("selection_hook")
        )
        if (
            review is None
            and source_fact_verified
            and isinstance(old_review, Mapping)
            and isinstance(staged_publish, dict)
        ):
            staged_publish["source_fact_review"] = dict(old_review)
        if (
            staged_publish.get("title") != expected_title
            or staged_publish.get("title_authority_error") is not None
            or not source_fact_verified
            or staged_story.get("selection_hook") != expected_hook
        ):
            raise replay.ReviewedBaselineReplayError("REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT")
        return dict(staged)

    return stage
