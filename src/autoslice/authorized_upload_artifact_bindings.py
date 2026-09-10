"""Bind uploader inputs to their reviewed artifacts and audio timing evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_sha_prefix(value: object) -> str:
    text = str(value or "")
    return text.removeprefix("sha256:")


def _record_artifact_hash_problems(
    record: dict,
    *,
    video: Path,
    cover: Path,
    subtitle: Path,
    title: str,
    story_contract_required: bool = True,
) -> list[str]:
    problems: list[str] = []
    artifact_hashes = record.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        return ["record.json has no artifact_hashes object"]

    expected = {
        "video": sha256_file(video),
        "cover": sha256_file(cover),
        "subtitle": sha256_file(subtitle),
    }
    accepted_video = {
        _strip_sha_prefix(artifact_hashes.get("burned_video_sha256")),
        _strip_sha_prefix(artifact_hashes.get("video_sha256")),
    }
    if expected["video"] not in accepted_video:
        problems.append("record artifact hashes do not bind the reviewed video")
    if expected["cover"] != _strip_sha_prefix(artifact_hashes.get("cover_sha256")):
        problems.append("record artifact hashes do not bind the reviewed cover")
    accepted_subtitle = {
        _strip_sha_prefix(artifact_hashes.get("delivery_subtitle_sha256")),
        _strip_sha_prefix(artifact_hashes.get("subtitle_sha256")),
    }
    if expected["subtitle"] not in accepted_subtitle:
        problems.append("record artifact hashes do not bind the reviewed SRT")

    publish_staging = record.get("publish_staging")
    record_title = publish_staging.get("title") if isinstance(publish_staging, dict) else None
    if record_title != title:
        problems.append(
            f"record publish title mismatch: record={record_title!r} manifest={title!r}"
        )
    if story_contract_required:
        story_contract = record.get("story_contract")
        if not isinstance(story_contract, dict):
            problems.append("record.json has no story_contract object")
        else:
            if not str(story_contract.get("schema_version") or "").strip():
                problems.append("record story_contract has no schema_version")
            if not str(story_contract.get("candidate_id") or "").strip():
                problems.append("record story_contract has no candidate_id")
            if not str(story_contract.get("transcript_sha256") or "").strip():
                problems.append("record story_contract has no transcript_sha256")
    return problems


def _subtitle_audio_correspondence_problems(
    record: dict, *, video: Path, subtitle: Path, package_root: Path,
) -> list[str]:
    """Recheck actual-media timing evidence before constructing an uploader."""
    if not isinstance(record.get("subtitle_audio_correspondence"), dict):
        return ["final subtitle/audio correspondence evidence is missing"]
    from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check

    try:
        validate_final_subtitle_audio_check(
            record, final_srt=subtitle, actual_media=video, package_root=package_root,
        )
    except (ValueError, OSError) as exc:
        return [f"final subtitle/audio correspondence rejected: {exc}"]
    return []
