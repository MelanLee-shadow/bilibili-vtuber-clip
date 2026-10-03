"""Final-media release binding used by the authorized upload boundary."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from src.autoslice import final_media_review_release_gate as final_media_release


def _candidate_id(
    record: Mapping[str, object],
    review_item: Mapping[str, object] | None,
    *,
    package_root: Path,
    manifest: Mapping[str, object],
) -> tuple[str | None, list[str]]:
    """Resolve one candidate only when all configured identities agree."""

    declared: list[str] = []
    if review_item is not None:
        value = str(review_item.get("candidate_id") or "").strip()
        if value:
            declared.append(value)
    story = record.get("story_contract")
    for value in (
        record.get("candidate_id"),
        record.get("delivery_candidate_id"),
        story.get("candidate_id") if isinstance(story, Mapping) else None,
    ):
        normalized = str(value or "").strip()
        if normalized:
            declared.append(normalized)

    configured: list[str] = []
    verification = package_root / "verification"
    for suffix in (
        ".final-media-review-job.json",
        ".final-media-review-state.json",
    ):
        try:
            # ``Path.iterdir`` is lazy on Python 3.11/3.12, so materialize it
            # while the missing-directory/error policy is still in scope.
            children = tuple(verification.iterdir())
        except FileNotFoundError:
            children = ()
        except OSError:
            return None, ["FINAL_MEDIA_REVIEW_STATE_INVALID"]
        for child in children:
            if child.name.endswith(suffix):
                configured.append(child.name[: -len(suffix)])

    attestation = manifest.get("package_attestation")
    frozen = (
        attestation.get("final_media_review")
        if isinstance(attestation, Mapping)
        else None
    )
    if isinstance(frozen, Mapping):
        frozen_candidate = str(frozen.get("candidate_id") or "").strip()
        if not frozen_candidate:
            return None, ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_INVALID"]
        configured.append(frozen_candidate)

    declared_ids = set(declared)
    configured_ids = set(configured)
    if not configured_ids:
        if len(declared_ids) > 1:
            return None, ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH"]
        return (next(iter(declared_ids)) if declared_ids else None), []
    if len(configured_ids) != 1:
        return None, ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH"]
    candidate_id = next(iter(configured_ids))
    if declared_ids and declared_ids != {candidate_id}:
        return None, ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH"]
    return candidate_id, []


def validation_problems(
    manifest: Mapping[str, object],
    record: Mapping[str, object],
    review_item: Mapping[str, object] | None,
    *,
    package_root: Path,
    source_video_sha256: str,
) -> list[str]:
    candidate_id, problems = _candidate_id(
        record,
        review_item,
        package_root=package_root,
        manifest=manifest,
    )
    if candidate_id is not None:
        problems.extend(
            final_media_release.release_attestation_problems(
                manifest,
                package_root=package_root,
                candidate_id=candidate_id,
                source_video_sha256=source_video_sha256,
            )
        )
    return problems


def attach_release_attestation(
    manifest: dict[str, object],
    record: Mapping[str, object],
    review_item: Mapping[str, object] | None,
    *,
    package_root: Path,
    source_video_sha256: str,
) -> list[str]:
    candidate_id, problems = _candidate_id(
        record,
        review_item,
        package_root=package_root,
        manifest=manifest,
    )
    if candidate_id is not None:
        problems.extend(
            final_media_release.attach_release_attestation(
                manifest,
                package_root=package_root,
                candidate_id=candidate_id,
                source_video_sha256=source_video_sha256,
            )
        )
    return problems


__all__ = ["attach_release_attestation", "validation_problems"]
