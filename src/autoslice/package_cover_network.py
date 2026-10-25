"""Hash-bound cover request/response closure shared by packagers and consumers."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from collections.abc import Mapping

from .publish_staging_paths import validate_package_editorial_binding


def _regular(path: Path, *, root: Path | None = None) -> Path:
    path = Path(os.path.abspath(path))
    if root is not None and not path.is_relative_to(root):
        raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_EVIDENCE_OUTSIDE_AUTHORITY")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if stat.S_ISLNK(os.lstat(cursor).st_mode):
            raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_EVIDENCE_SYMLINK")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_EVIDENCE_NOT_REGULAR")
    return path


def _digest(path: Path) -> str:
    from .evidence_identity import file_sha256
    return "sha256:" + file_sha256(path)


def _generation(record, publish, *, candidate_id=None):
    staging = record.get("publish_staging") or {}
    generation = publish.get("cover_generation") or {}
    mirrored = staging.get("cover_generation") or {}
    if not all(isinstance(value, Mapping) for value in (staging, generation, mirrored)):
        raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_SHAPE_INVALID")
    modern = any(
        (
            publish.get("current_binding") is not None,
            staging.get("current_binding") is not None,
            generation.get("input_binding") is not None,
            publish.get("title_selection") is not None,
            staging.get("title_selection") is not None,
        )
    )
    declared = any(
        generation.get(role + "_sha256") is not None or mirrored.get(role + "_sha256") is not None
        for role in ("request", "response")
    )
    if not modern and not declared:
        return {}, None
    if not isinstance(generation, Mapping) or generation != mirrored:
        raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_GENERATION_MIRROR_DRIFT")
    source_id = record.get("source_candidate_id") or record.get("candidate_id")
    if (
        not isinstance(source_id, str)
        or not source_id
        or record.get("candidate_id", source_id) != source_id
        or publish.get("candidate_id") != source_id
        or (staging.get("candidate_id") is not None and staging["candidate_id"] != source_id)
        or (
            candidate_id is not None
            and record.get("delivery_candidate_id", source_id) != candidate_id
        )
    ):
        raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_CANDIDATE_DRIFT")
    return generation, source_id


def _check_bytes(generation, paths, *, cover_path):
    if generation.get("final_cover_sha256") != _digest(_regular(cover_path)):
        raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_FINAL_COVER_DRIFT")
    for role in ("request", "response"):
        expected = generation.get(role + "_sha256")
        if expected is None:
            continue
        if not isinstance(expected, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is None:
            raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_EVIDENCE_SHA_INVALID")
        if role not in paths or _digest(paths[role]) != expected:
            raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_EVIDENCE_HASH_DRIFT:" + role)


def source_cover_network(record, publish, *, subtitle_path: Path, cover_path: Path):
    """Resolve only the frozen generation's own files, never a directory search."""
    generation, candidate = _generation(record, publish)
    if candidate is None:
        return {}
    staging = record["publish_staging"]
    paths = {}
    if any(generation.get(role + "_sha256") is not None for role in ("request", "response")):
        # The bound publish locator owns this producer attempt. The exact
        # final-cover locator narrows it to its generation, including cached
        # .normal-cover-generations attempts, rather than the whole source tree.
        publish_origin = _regular(Path(str(staging.get("publish_json_path") or "")))
        import json

        if json.loads(publish_origin.read_bytes()) != publish:
            raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_SOURCE_PUBLISH_DRIFT")
        origin = _regular(
            Path(str(generation.get("final_cover") or "")), root=publish_origin.parent
        )
        generation_root = origin.parent.parent if origin.parent.name == "covers" else origin.parent
        if _digest(origin) != _digest(cover_path):
            raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_SOURCE_COVER_DRIFT")
        for role in ("request", "response"):
            if generation.get(role + "_sha256") is not None:
                raw = generation.get(role + "_path")
                if not isinstance(raw, str) or not Path(raw).is_absolute():
                    raise ValueError(
                        "CURRENT_EDITORIAL_COVER_NETWORK_SOURCE_LOCATOR_MISSING:" + role
                    )
                paths[role] = _regular(Path(raw), root=generation_root)
    _check_bytes(generation, paths, cover_path=cover_path)
    aliases = {}
    if generation.get("method") == "skill.candidates":
        aliases["choice_path"] = _regular(Path(generation["candidate_selection"]["choose_path"]))
    if (staging.get("title_selection") or {}).get("title_cover_skill") is not None and record.get("speaker_review_srt_path"):
        aliases["speaker_srt_path"] = _regular(Path(record["speaker_review_srt_path"]))
    validate_package_editorial_binding(
        record,
        publish,
        candidate_id=candidate,
        subtitle_path=subtitle_path,
        cover_path=cover_path,
        **{role + "_path": path for role, path in paths.items()}, **aliases,
    )
    return {role: (path, generation[role + "_sha256"]) for role, path in paths.items()}


def validate_package_cover_network(
    root: Path, item, record, publish, *, subtitle_path: Path, cover_path: Path
):
    """Resolve and replay the same closure in a portable package before models."""
    generation, candidate = _generation(
        record, publish, candidate_id=item.get("candidate_id") or item.get("id")
    )
    if candidate is None:
        return
    root = root.resolve(strict=True)
    subtitle_path = _regular(subtitle_path, root=root)
    cover_path = _regular(cover_path, root=root)
    video = item.get("video")
    if not isinstance(video, str) or Path(video).name != video or not video.endswith(".mp4"):
        raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_VIDEO_STEM_INVALID")
    stem = video[:-4]
    paths = {}
    for role in ("request", "response"):
        if generation.get(role + "_sha256") is None:
            continue
        name = item.get("cover_" + role) or stem + ".cover-" + role + ".json"
        if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
            raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_PORTABLE_LOCATOR_INVALID:" + role)
        paths[role] = _regular(root / name, root=root)
    _check_bytes(generation, paths, cover_path=cover_path)
    hashes = item.get("sha256") or {}
    for role in paths:
        claimed = hashes.get("cover_" + role) if isinstance(hashes, Mapping) else None
        if claimed is not None and str(claimed).removeprefix("sha256:") != generation[
            role + "_sha256"
        ].removeprefix("sha256:"):
            raise ValueError("CURRENT_EDITORIAL_COVER_NETWORK_ITEM_HASH_DRIFT:" + role)
    aliases = {}
    if generation.get("method") == "skill.candidates":
        from .title_cover_package import package_choice_problems
        problems = package_choice_problems(root, item, generation)
        if problems:
            raise ValueError("CURRENT_EDITORIAL_COVER_CHOICE_INVALID:" + "; ".join(problems))
        aliases["choice_path"] = _regular(root / item["cover_choice"], root=root)
    staging = record.get("publish_staging") or {}
    if (staging.get("title_selection") or {}).get("title_cover_skill") is not None and record.get("speaker_review_srt_path"):
        aliases["speaker_srt_path"] = _regular(
            root / str(item.get("speaker_srt") or record["speaker_review_srt_path"]), root=root,
        )
    validate_package_editorial_binding(
        record,
        publish,
        candidate_id=candidate,
        subtitle_path=subtitle_path,
        cover_path=cover_path,
        **{role + "_path": path for role, path in paths.items()}, **aliases,
    )
