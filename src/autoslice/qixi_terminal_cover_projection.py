"""Terminal StoryContract projection for already-validated Qixi cover evidence."""

from __future__ import annotations

from collections.abc import Mapping

from src.autoslice.qixi_post_correction_projection_paths import (
    QixiPostCorrectionPublicSurfaceError,
)
from src.autoslice.story_contract import cover_story_contract_binding


def bind_terminal_story_to_validated_cover(
    *,
    projected_record: dict[str, object],
    projected_publish: dict[str, object],
) -> None:
    """Project the terminal StoryContract into both validated cover mirrors."""

    story = projected_record.get("story_contract")
    staging = projected_record.get("publish_staging")
    if not isinstance(story, Mapping) or not isinstance(staging, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("projected StoryContract is invalid")
    record_generation = staging.get("cover_generation")
    publish_generation = projected_publish.get("cover_generation")
    if not isinstance(record_generation, Mapping) or not isinstance(publish_generation, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("validated cover generation is missing")
    binding = cover_story_contract_binding(story)
    rebound_record_generation = dict(record_generation)
    rebound_publish_generation = dict(publish_generation)
    rebound_record_generation["story_contract"] = binding
    rebound_publish_generation["story_contract"] = binding
    rebound_staging = dict(staging)
    rebound_staging["cover_generation"] = rebound_record_generation
    projected_record["publish_staging"] = rebound_staging
    projected_publish["cover_generation"] = rebound_publish_generation


def validate_terminal_cover_story_projection_mirrors(
    *,
    projected_record: Mapping[str, object],
    projected_publish: Mapping[str, object],
) -> None:
    """Verify the rebound cover generation remains identical on both mirrors."""

    staging = projected_record.get("publish_staging")
    if not isinstance(staging, Mapping) or (
        staging.get("cover_generation") != projected_publish.get("cover_generation")
    ):
        raise QixiPostCorrectionPublicSurfaceError(
            "canonical publish and returned public surfaces drift before projection"
        )
