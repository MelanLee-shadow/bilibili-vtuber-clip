"""Pure path and mirror projections for the sealed Qixi public-surface lane."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from src.autoslice.cover_punch_semantics import validate_cover_punch_semantic_review
from src.autoslice.cover_route_evidence import (
    validate_cover_route_decision,
    validate_final_host_identity_verification,
    validate_final_participant_verification,
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_evidence import SourceCue


class QixiPostCorrectionPublicSurfaceError(RuntimeError):
    """The candidate-specific closure cannot safely continue."""


def package_cover_paths(value: object, *, package_root: Path) -> set[Path]:
    found: set[Path] = set()
    if isinstance(value, Mapping):
        for child in value.values():
            found.update(package_cover_paths(child, package_root=package_root))
    elif isinstance(value, list):
        for child in value:
            found.update(package_cover_paths(child, package_root=package_root))
    elif isinstance(value, str) and value.startswith(str(package_root) + "/"):
        found.add(Path(value))
    return found


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def source_cues(srt_path: Path) -> list[SourceCue]:
    return [
        SourceCue(
            f"text_final_{index:04d}",
            cue.start_ms,
            cue.end_ms,
            cue.text.strip(),
            "zh",
            "speech",
            1.0,
        )
        for index, cue in enumerate(
            parse_srt_cues(srt_path.read_text(encoding="utf-8")), start=1
        )
    ]


def copy_sealed_stage_file(source: Path, destination: Path) -> None:
    """Copy a sealed input into a distinct, fsynced private-stage inode."""

    source_info = os.lstat(source)
    if not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
        raise QixiPostCorrectionPublicSurfaceError("sealed stage source is not a regular file")
    source_mode = stat.S_IMODE(source_info.st_mode)
    source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    source_fd = os.open(source, os.O_RDONLY)
    created: os.stat_result | None = None
    destination_fd: int | None = None
    try:
        opened_source = os.fstat(source_fd)
        if (opened_source.st_dev, opened_source.st_ino) != (source_info.st_dev, source_info.st_ino):
            raise QixiPostCorrectionPublicSurfaceError("sealed stage source inode drifted before copy")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            source_mode,
        )
        created = os.fstat(destination_fd)
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                view = view[os.write(destination_fd, view) :]
        os.fchmod(destination_fd, source_mode)
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        destination_info = os.lstat(destination)
        if (
            not stat.S_ISREG(destination_info.st_mode)
            or stat.S_ISLNK(destination_info.st_mode)
            or (destination_info.st_dev, destination_info.st_ino)
            == (source_info.st_dev, source_info.st_ino)
            or stat.S_IMODE(destination_info.st_mode) != source_mode
            or "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest() != source_sha256
        ):
            raise QixiPostCorrectionPublicSurfaceError("private stage copy verification failed")
        source_after = os.lstat(source)
        if (
            (source_after.st_dev, source_after.st_ino) != (source_info.st_dev, source_info.st_ino)
            or stat.S_IMODE(source_after.st_mode) != source_mode
            or "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256
        ):
            raise QixiPostCorrectionPublicSurfaceError("sealed stage source drifted during copy")
    except BaseException:
        if destination_fd is not None:
            os.close(destination_fd)
        if created is not None and os.path.lexists(destination):
            observed = os.lstat(destination)
            if (observed.st_dev, observed.st_ino) == (created.st_dev, created.st_ino):
                destination.unlink()
        raise
    finally:
        os.close(source_fd)


def _stage_owner_marker(path: Path) -> Path:
    return path.with_name(path.name + ".owner")


def _stage_owner_payload(path: Path, *, device: int, inode: int, sha256: str, mode: int) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": "qixi-public-surface-staged-owner.v1",
                "staged_name": path.name,
                "device": device,
                "inode": inode,
                "sha256": sha256,
                "mode": mode,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def mark_staged_file_owner(path: Path, *, device: int, inode: int, sha256: str, mode: int) -> None:
    marker = _stage_owner_marker(path)
    payload = _stage_owner_payload(path, device=device, inode=inode, sha256=sha256, mode=mode)
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    opened = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if os.path.lexists(marker):
            observed = os.lstat(marker)
            if (observed.st_dev, observed.st_ino) == (opened.st_dev, opened.st_ino):
                marker.unlink()
        raise


def create_staged_file(path: Path, *, payload: bytes, sha256: str, mode: int) -> tuple[int, int]:
    """Create a private replacement plus a durable pre-checkpoint owner marker."""

    if os.path.lexists(path):
        raise QixiPostCorrectionPublicSurfaceError("transaction staged path already exists")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    opened = os.fstat(fd)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_IMODE(info.st_mode) != mode
            or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != sha256
        ):
            raise QixiPostCorrectionPublicSurfaceError("staged transaction write verification failed")
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        mark_staged_file_owner(path, device=info.st_dev, inode=info.st_ino, sha256=sha256, mode=mode)
        return info.st_dev, info.st_ino
    except BaseException as exc:
        if fd >= 0:
            os.close(fd)
        ownership_lost = False
        if os.path.lexists(path):
            observed = os.lstat(path)
            if (observed.st_dev, observed.st_ino) == (opened.st_dev, opened.st_ino):
                path.unlink()
            else:
                ownership_lost = True
        if ownership_lost:
            raise QixiPostCorrectionPublicSurfaceError(
                "staged transaction artifact ownership drifted"
            ) from exc
        raise


def _verify_stage_owner_marker(path: Path, *, device: int, inode: int, sha256: str, mode: int) -> Path:
    marker = _stage_owner_marker(path)
    if not os.path.lexists(marker):
        raise QixiPostCorrectionPublicSurfaceError("uncheckpointed staged transaction owner is missing")
    info = os.lstat(marker)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise QixiPostCorrectionPublicSurfaceError("uncheckpointed staged transaction owner drifts")
    if marker.read_bytes() != _stage_owner_payload(
        path, device=device, inode=inode, sha256=sha256, mode=mode
    ):
        raise QixiPostCorrectionPublicSurfaceError("uncheckpointed staged transaction owner drifts")
    return marker


def adopt_staged_file(path: Path, *, sha256: str, mode: int) -> tuple[int, int]:
    """Adopt exactly the deterministic temp left before its journal checkpoint."""

    if not os.path.lexists(path):
        raise QixiPostCorrectionPublicSurfaceError("staged transaction artifact disappeared")
    info = os.lstat(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != mode
        or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != sha256
    ):
        raise QixiPostCorrectionPublicSurfaceError("uncheckpointed staged transaction artifact drifts")
    _verify_stage_owner_marker(
        path, device=info.st_dev, inode=info.st_ino, sha256=sha256, mode=mode
    )
    return info.st_dev, info.st_ino


def clear_staged_file_owner(
    path: Path, *, device: int, inode: int, sha256: str, mode: int
) -> None:
    marker = _stage_owner_marker(path)
    if not os.path.lexists(marker):
        return
    _verify_stage_owner_marker(path, device=device, inode=inode, sha256=sha256, mode=mode)
    marker.unlink()
    directory_fd = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def validate_replayed_cover(
    generation: object,
    *,
    story: Mapping[str, object],
    package_root: Path,
    after: Mapping[Path, bytes],
) -> None:
    if not isinstance(generation, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("journal cover generation is invalid")
    if not (
        validate_cover_route_decision(generation, allow_legacy_v1=False)
        and validate_rendered_text_pixel_evidence(generation)
        and validate_final_host_identity_verification(generation)
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal cover route/pixel/identity evidence drifts")
    route = generation.get("route_decision")
    participants = route.get("required_participant_ids") if isinstance(route, Mapping) else None
    if participants and not validate_final_participant_verification(generation):
        raise QixiPostCorrectionPublicSurfaceError("journal cover participant evidence drifts")
    art_direction = generation.get("art_direction")
    if generation.get("cover_text_mode") == "punch" and not validate_cover_punch_semantic_review(
        art_direction.get("cover_punch_semantic_review") if isinstance(art_direction, Mapping) else None,
        rendered_lines=generation.get("rendered_lines") or [],
        cover_text=str(generation.get("cover_text") or ""),
        story_hook=str(story.get("selection_hook") or ""),
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal cover punch semantic evidence drifts")
    for path_key, sha_key in (
        ("final_cover", "final_cover_sha256"),
        ("pre_overlay_path", "pre_overlay_sha256"),
        ("ai_background", "ai_background_sha256"),
    ):
        path, digest = generation.get(path_key), generation.get(sha_key)
        if path is None and digest is None:
            continue
        candidate = Path(str(path))
        if not candidate.is_relative_to(package_root) or candidate not in after or sha256_bytes(after[candidate]) != digest:
            raise QixiPostCorrectionPublicSurfaceError("journal cover materialized hash binding drifts")


def validate_manual_title_projection(
    staging: object,
    publish: object,
    *,
    source_fact: Mapping[str, object],
    public_title: str,
) -> None:
    """Replay every manual-title field across record/publish mirrors."""

    if not isinstance(staging, Mapping) or not isinstance(publish, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("journal manual title projection is missing")
    mirror_keys = set(staging) - {"status", "publish_json_path"}
    if any(key not in publish or publish.get(key) != staging.get(key) for key in mirror_keys):
        raise QixiPostCorrectionPublicSurfaceError("journal manual title projection mirror drifts")
    if (
        staging.get("status") != "STAGED"
        or staging.get("title") != public_title
        or staging.get("title_source") != "ivan_manual_override"
        or staging.get("title_authority_status") != "RESOLVED_MANUAL"
        or staging.get("title_authority_error") is not None
        or staging.get("title_policy_violations") != []
        or staging.get("source_fact_review") != source_fact
        or staging.get("manual_title_repair_authority_consumption") is not None
        or staging.get("public_text_surface_authority_consumption") is not None
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal manual title authority projection drifts")
    if not isinstance(staging.get("title_story_audit"), Mapping):
        raise QixiPostCorrectionPublicSurfaceError("journal title StoryContract audit is missing")
    for key in ("entity_projection_audit", "cover_entity_projection_audit"):
        value = staging.get(key)
        if value is not None and not isinstance(value, Mapping):
            raise QixiPostCorrectionPublicSurfaceError("journal entity projection audit drifts")


def replace_stage_locators(
    value: object,
    *,
    stage_artifacts: Path,
    public_artifacts: Path,
    materialized: set[str],
) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): replace_stage_locators(
                child,
                stage_artifacts=stage_artifacts,
                public_artifacts=public_artifacts,
                materialized=materialized,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            replace_stage_locators(
                child,
                stage_artifacts=stage_artifacts,
                public_artifacts=public_artifacts,
                materialized=materialized,
            )
            for child in value
        ]
    if not isinstance(value, str) or not value.startswith(str(stage_artifacts) + "/"):
        return value
    relative = Path(value).relative_to(stage_artifacts).as_posix()
    if relative not in materialized:
        raise QixiPostCorrectionPublicSurfaceError(
            f"staged locator has no materialized target: {relative}"
        )
    return str(public_artifact_path(public_artifacts, relative))


def stage_relative_locators(value: object, *, stage_artifacts: Path) -> set[str]:
    """Return only stage files surfaced by the resulting public documents."""

    if isinstance(value, Mapping):
        return {
            relative
            for child in value.values()
            for relative in stage_relative_locators(child, stage_artifacts=stage_artifacts)
        }
    if isinstance(value, list):
        return {
            relative
            for child in value
            for relative in stage_relative_locators(child, stage_artifacts=stage_artifacts)
        }
    if not isinstance(value, str) or not value.startswith(str(stage_artifacts) + "/"):
        return set()
    try:
        return {Path(value).relative_to(stage_artifacts).as_posix()}
    except ValueError as exc:
        raise QixiPostCorrectionPublicSurfaceError("staged locator escapes artifact root") from exc


def public_artifact_root(package_root: Path, authority: Mapping[str, object]) -> Path:
    authority_sha256 = str(authority["authority_sha256"])
    if not authority_sha256.startswith("sha256:"):
        raise QixiPostCorrectionPublicSurfaceError("public artifact namespace authority drifts")
    return package_root / f".qixi-public-surface-{authority_sha256[7:23]}--"


def public_artifact_path(namespace: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise QixiPostCorrectionPublicSurfaceError("public artifact relative path drifts")
    encoded = base64.urlsafe_b64encode(relative.encode("utf-8")).decode("ascii").rstrip("=")
    return namespace.parent / f"{namespace.name}{encoded}"


def validate_public_artifact_namespace_absent(
    package_root: Path,
    authority: Mapping[str, object],
    *,
    require_safe_parent: Callable[[Path], None],
) -> None:
    namespace = public_artifact_root(package_root, authority)
    require_safe_parent(namespace)
    if any(package_root.glob(namespace.name + "*")):
        raise QixiPostCorrectionPublicSurfaceError("public artifact namespace already exists")


def assert_no_stage_locator(value: object, stage_root: Path) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            assert_no_stage_locator(child, stage_root)
    elif isinstance(value, list):
        for child in value:
            assert_no_stage_locator(child, stage_root)
    elif isinstance(value, str) and str(stage_root) in value:
        raise QixiPostCorrectionPublicSurfaceError("after-image retains a private staging locator")


def prepare_stage_documents(
    staged: Mapping[str, object],
    staged_publish: Mapping[str, object],
    *,
    stage_artifacts: Path,
    stage_publish_path: Path,
    public_publish_path: Path,
) -> tuple[dict[str, object], set[str]]:
    staging = staged.get("publish_staging")
    stage_publish_relative = stage_publish_path.relative_to(stage_artifacts).as_posix()
    if (
        not isinstance(staging, Mapping)
        or staging.get("publish_json_path") != str(stage_publish_path)
        or stage_publish_relative in stage_relative_locators(
            staged_publish, stage_artifacts=stage_artifacts
        )
    ):
        raise QixiPostCorrectionPublicSurfaceError("staged publish self-locator drifts")
    record = copy.deepcopy(dict(staged))
    projected = record.get("publish_staging")
    assert isinstance(projected, dict)
    projected["publish_json_path"] = str(public_publish_path)
    if stage_publish_relative in stage_relative_locators(record, stage_artifacts=stage_artifacts):
        raise QixiPostCorrectionPublicSurfaceError("staged publish self-locator escapes its only field")
    return record, stage_relative_locators(record, stage_artifacts=stage_artifacts) | stage_relative_locators(
        staged_publish, stage_artifacts=stage_artifacts
    )


def materialized_public_targets(
    *,
    stage_artifacts: Path,
    namespace: Path,
    relatives: set[str],
    require_regular: Callable[[Path], None],
) -> dict[Path, bytes]:
    targets: dict[Path, bytes] = {}
    for relative in sorted(relatives):
        source = stage_artifacts / relative
        target = public_artifact_path(namespace, relative)
        if target in targets:
            raise QixiPostCorrectionPublicSurfaceError("public artifact path mapping collides")
        require_regular(source)
        targets[target] = source.read_bytes()
    return targets


def project_state_pick(
    state: Mapping[str, object],
    *,
    pick_index: int,
    candidate_id: str,
    record: Mapping[str, object],
    publish: Mapping[str, object],
) -> dict[str, object]:
    projected = copy.deepcopy(dict(state))
    picks = projected.get("picks")
    if not isinstance(picks, list) or pick_index >= len(picks) or not isinstance(picks[pick_index], dict):
        raise QixiPostCorrectionPublicSurfaceError("state pick disappeared during projection")
    staging = record.get("publish_staging")
    if not isinstance(staging, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("staged record lacks publish mirror")
    source_fact, generation = staging.get("source_fact_review"), staging.get("cover_generation")
    if not isinstance(source_fact, Mapping) or not isinstance(generation, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("staged source-fact or cover evidence is absent")
    if publish.get("source_fact_review") != source_fact or publish.get("cover_generation") != generation:
        raise QixiPostCorrectionPublicSurfaceError("publish does not mirror staged public surfaces")
    picks[pick_index].update(
        {
            "title": staging.get("title"),
            "title_source": staging.get("title_source"),
            "title_authority_status": staging.get("title_authority_status"),
            "title_authority_error": staging.get("title_authority_error"),
            "cover_status": staging.get("cover_status"),
            "cover_path": staging.get("cover_path"),
            "cover_sha256": (record.get("artifact_hashes") or {}).get("cover_sha256"),
            "cover_generation": copy.deepcopy(generation),
            "source_fact_review": copy.deepcopy(source_fact),
            "upload_enabled": False,
        }
    )
    if picks[pick_index].get("candidate_id") != candidate_id:
        raise QixiPostCorrectionPublicSurfaceError("state pick candidate drifts")
    return projected


def state_diff_is_exact(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    pick_index: int,
    candidate_id: str,
) -> bool:
    mutable = {
        "title", "title_source", "title_authority_status", "title_authority_error",
        "cover_status", "cover_path", "cover_sha256", "cover_generation",
        "source_fact_review", "upload_enabled",
    }
    old, new = copy.deepcopy(dict(before)), copy.deepcopy(dict(after))
    old_picks, new_picks = old.get("picks"), new.get("picks")
    if not isinstance(old_picks, list) or not isinstance(new_picks, list) or len(old_picks) != len(new_picks):
        return False
    if pick_index >= len(old_picks) or not isinstance(old_picks[pick_index], Mapping) or not isinstance(new_picks[pick_index], Mapping):
        return False
    if any(old_picks[index] != new_picks[index] for index in range(len(old_picks)) if index != pick_index):
        return False
    old_pick, new_pick = old_picks[pick_index], new_picks[pick_index]
    if old_pick.get("candidate_id") != candidate_id or new_pick.get("candidate_id") != candidate_id:
        return False
    if any(old_pick.get(key) != new_pick.get(key) for key in set(old_pick) | set(new_pick) if key not in mutable):
        return False
    old["picks"] = [None] * len(old_picks)
    new["picks"] = [None] * len(new_picks)
    return old == new
