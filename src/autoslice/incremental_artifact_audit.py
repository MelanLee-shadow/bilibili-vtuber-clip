"""Component-scoped review planning for small, hash-bound delivery changes.

This module does not replace package audit, final perceptual review, title/cover QC,
or upload authorization.  It records a narrower *review scope* when a new
artifact set is derived from a previous one:

* unchanged video/subtitle/cover/boundary/title components can inherit their
  parent's evidence;
* changed subtitle cues and cover pixels are localized deterministically;
* changed video needs an explicit edit-window map, otherwise it falls back to a
  full-component review;
* a new receipt points at the newest real record/media and keeps the parent as
  immutable history.

The receipt is therefore an input to the existing release gates, not a release
verdict.  All writes are create-only and hash-bound.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, ImageChops

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.operator_correction_policy import plan_operator_correction


SCHEMA_VERSION = "incremental-artifact-audit.v1"
POLICY_VERSION = "2026-08-27.incremental-artifact-scope.v1"
COMPONENTS = ("video", "subtitle", "cover", "boundary", "title")


class IncrementalArtifactAuditError(ValueError):
    """The component delta cannot be localized or safely sealed."""


@dataclass(frozen=True)
class ArtifactPaths:
    """The independently hashable files for one delivery version."""

    record: Path
    video: Path
    subtitle: Path
    cover: Path
    boundary: Path | None = None
    title: Path | None = None



def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")



def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()



def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()



def _regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise IncrementalArtifactAuditError(f"{label} may not be a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise IncrementalArtifactAuditError(f"{label} is missing or unsafe: {path}")
    return resolved



def _raw_entry(path: Path, *, label: str) -> dict[str, object]:
    resolved = _regular_file(path, label=label)
    raw = resolved.read_bytes()
    return {
        "path": str(path),
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
    }



def _canonical_json_entry(path: Path, *, label: str) -> dict[str, object]:
    resolved = _regular_file(path, label=label)
    raw = resolved.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IncrementalArtifactAuditError(f"{label} is not valid UTF-8 JSON") from exc
    canonical = _canonical_json(value)
    entry = _raw_entry(path, label=label)
    entry["canonical_sha256"] = _sha256_bytes(canonical)
    return entry



def _canonical_text_entry(path: Path, *, label: str) -> dict[str, object]:
    resolved = _regular_file(path, label=label)
    raw = resolved.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IncrementalArtifactAuditError(f"{label} is not UTF-8 text") from exc
    canonical = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    entry = _raw_entry(path, label=label)
    entry["canonical_sha256"] = _sha256_bytes(canonical)
    return entry



def _cue_payload(cue: SrtCue) -> dict[str, object]:
    return {
        "index": str(cue.index),
        "start_ms": cue.start_ms,
        "end_ms": cue.end_ms,
        "text": cue.text,
        "text_sha256": _sha256_bytes(cue.text.encode("utf-8")),
    }



def _read_srt(path: Path, *, label: str) -> tuple[dict[str, object], list[SrtCue]]:
    resolved = _regular_file(path, label=label)
    raw = resolved.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IncrementalArtifactAuditError(f"{label} is not UTF-8") from exc
    cues = parse_srt_cues(text)
    if raw.strip() and not cues:
        raise IncrementalArtifactAuditError(f"{label} has no parseable cues")
    canonical = _canonical_json([_cue_payload(cue) for cue in cues])
    entry = {
        "path": str(path),
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
        "canonical_sha256": _sha256_bytes(canonical),
        "cue_count": len(cues),
    }
    return entry, cues



def _cue_windows(cues: Sequence[SrtCue]) -> list[list[int]]:
    return [[int(cue.start_ms), int(cue.end_ms)] for cue in cues if cue.end_ms > cue.start_ms]



def _merge_windows(windows: Sequence[Sequence[int]]) -> list[list[int]]:
    normalized = sorted(
        (int(window[0]), int(window[1]))
        for window in windows
        if len(window) == 2 and int(window[1]) > int(window[0])
    )
    merged: list[list[int]] = []
    for start, end in normalized:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged



def _srt_delta(
    before_path: Path,
    current_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    before_entry, before = _read_srt(before_path, label="parent subtitle")
    current_entry, current = _read_srt(current_path, label="current subtitle")
    matcher = difflib.SequenceMatcher(
        a=[(_cue_payload(cue)["index"], cue.start_ms, cue.end_ms, cue.text) for cue in before],
        b=[(_cue_payload(cue)["index"], cue.start_ms, cue.end_ms, cue.text) for cue in current],
        autojunk=False,
    )
    changes: list[dict[str, object]] = []
    changed_windows: list[list[int]] = []
    changed_cue_count = 0
    for tag, before_start, before_end, current_start, current_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        before_rows = [_cue_payload(cue) for cue in before[before_start:before_end]]
        current_rows = [_cue_payload(cue) for cue in current[current_start:current_end]]
        windows = _merge_windows(
            _cue_windows(before[before_start:before_end])
            + _cue_windows(current[current_start:current_end])
        )
        changes.append(
            {
                "operation": tag,
                "before": before_rows,
                "after": current_rows,
                "windows": windows,
            }
        )
        changed_windows.extend(windows)
        changed_cue_count += max(len(before_rows), len(current_rows))
    changed_windows = _merge_windows(changed_windows)
    canonical_changed = before_entry["canonical_sha256"] != current_entry["canonical_sha256"]
    raw_changed = before_entry["sha256"] != current_entry["sha256"]
    if not canonical_changed:
        status = "FORMAT_ONLY" if raw_changed else "UNCHANGED"
        review_scope = "NONE"
    else:
        status = "SCOPED_REVIEW_REQUIRED"
        review_scope = "CUE_WINDOWS"
    return (
        {
            "status": status,
            "review_scope": review_scope,
            "raw_changed": raw_changed,
            "canonical_changed": canonical_changed,
            "changed_cue_count": changed_cue_count,
            "changed_windows": changed_windows,
            "changes": changes,
            "parent": before_entry,
            "current": current_entry,
        },
        {"parent": before, "current": current},
    )



def _parse_windows(value: object, *, label: str) -> list[list[int]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise IncrementalArtifactAuditError(f"{label} must be a list")
    result: list[list[int]] = []
    for item in value:
        if isinstance(item, Mapping):
            start = item.get("start_ms")
            end = item.get("end_ms")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = item
        else:
            raise IncrementalArtifactAuditError(f"{label} contains an invalid window")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise IncrementalArtifactAuditError(f"{label} contains an invalid window")
        result.append([start, end])
    return _merge_windows(result)



def _parse_roi(value: object, *, size: tuple[int, int]) -> list[int]:
    if isinstance(value, Mapping):
        raw = [value.get("x"), value.get("y"), value.get("width"), value.get("height")]
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        raw = list(value)
    else:
        raise IncrementalArtifactAuditError("cover roi must be [x, y, width, height]")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in raw):
        raise IncrementalArtifactAuditError("cover roi coordinates must be integers")
    x, y, width, height = (int(item) for item in raw)
    if (
        x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > size[0]
        or y + height > size[1]
    ):
        raise IncrementalArtifactAuditError("cover roi is outside the image")
    return [x, y, width, height]



def _bbox_to_roi(bbox: tuple[int, int, int, int] | None) -> list[int] | None:
    if bbox is None:
        return None
    return [bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1]]



def _bbox_inside(bbox: tuple[int, int, int, int], roi: Sequence[int]) -> bool:
    x, y, width, height = (int(item) for item in roi)
    return bbox[0] >= x and bbox[1] >= y and bbox[2] <= x + width and bbox[3] <= y + height



def _cover_delta(
    before_path: Path,
    current_path: Path,
    declared: Mapping[str, object] | None,
) -> dict[str, object]:
    before_entry = _raw_entry(before_path, label="parent cover")
    current_entry = _raw_entry(current_path, label="current cover")
    raw_changed = before_entry["sha256"] != current_entry["sha256"]
    result: dict[str, object] = {
        "status": "UNCHANGED" if not raw_changed else "FULL_COMPONENT_REVIEW_REQUIRED",
        "review_scope": "NONE" if not raw_changed else "FULL_COMPONENT",
        "raw_changed": raw_changed,
        "parent": before_entry,
        "current": current_entry,
    }
    if not raw_changed:
        return result
    try:
        with Image.open(before_path) as before_image, Image.open(current_path) as current_image:
            before_rgb = before_image.convert("RGB")
            current_rgb = current_image.convert("RGB")
            if before_rgb.size != current_rgb.size:
                result["reason_code"] = "COVER_CANVAS_CHANGED"
                return result
            difference = ImageChops.difference(before_rgb, current_rgb)
            bbox = difference.getbbox()
    except (OSError, ValueError) as exc:
        raise IncrementalArtifactAuditError("cover pixels are not readable") from exc
    result["pixel_changed_bbox"] = _bbox_to_roi(bbox)
    if bbox is None:
        result.update(
            {
                "status": "PIXELS_UNCHANGED_METADATA_ONLY",
                "review_scope": "NONE",
                "reason_code": "COVER_RAW_CHANGED_PIXELS_UNCHANGED",
            }
        )
        return result
    declared_roi = _parse_roi((declared or {}).get("roi"), size=current_rgb.size) if declared else None
    if declared_roi is None:
        result["reason_code"] = "COVER_EDIT_MAP_MISSING"
        return result
    if not _bbox_inside(bbox, declared_roi):
        raise IncrementalArtifactAuditError("COVER_CHANGE_OUTSIDE_DECLARED_ROI")
    result.update(
        {
            "status": "SCOPED_REVIEW_REQUIRED",
            "review_scope": "PIXEL_ROI",
            "declared_roi": declared_roi,
        }
    )
    return result



def _json_diff_paths(before: object, current: object, prefix: str = "") -> list[str]:
    if isinstance(before, Mapping) and isinstance(current, Mapping):
        paths: list[str] = []
        for key in sorted(set(before) | set(current), key=str):
            child = f"{prefix}/{key}" if prefix else f"/{key}"
            if key not in before or key not in current:
                paths.append(child)
            else:
                paths.extend(_json_diff_paths(before[key], current[key], child))
        return paths
    if isinstance(before, list) and isinstance(current, list):
        paths = []
        for index in range(max(len(before), len(current))):
            child = f"{prefix}/{index}"
            if index >= len(before) or index >= len(current):
                paths.append(child)
            else:
                paths.extend(_json_diff_paths(before[index], current[index], child))
        return paths
    return [] if before == current else [prefix or "/"]



def _json_component_delta(
    before_path: Path,
    current_path: Path,
    *,
    label: str,
) -> dict[str, object]:
    before = json.loads(_regular_file(before_path, label=label).read_text(encoding="utf-8"))
    current = json.loads(_regular_file(current_path, label=label).read_text(encoding="utf-8"))
    before_entry = _canonical_json_entry(before_path, label=label)
    current_entry = _canonical_json_entry(current_path, label=label)
    changed = before_entry["canonical_sha256"] != current_entry["canonical_sha256"]
    return {
        "status": "FULL_COMPONENT_REVIEW_REQUIRED" if changed else "UNCHANGED",
        "review_scope": "FULL_COMPONENT" if changed else "NONE",
        "raw_changed": before_entry["sha256"] != current_entry["sha256"],
        "canonical_changed": changed,
        "changed_pointers": _json_diff_paths(before, current) if changed else [],
        "parent": before_entry,
        "current": current_entry,
    }



def _text_component_delta(
    before_path: Path,
    current_path: Path,
    *,
    label: str,
) -> dict[str, object]:
    before = _regular_file(before_path, label=label).read_text(encoding="utf-8").replace("\r\n", "\n")
    current = _regular_file(current_path, label=label).read_text(encoding="utf-8").replace("\r\n", "\n")
    before_entry = _canonical_text_entry(before_path, label=label)
    current_entry = _canonical_text_entry(current_path, label=label)
    changed = before != current
    return {
        "status": "FULL_COMPONENT_REVIEW_REQUIRED" if changed else "UNCHANGED",
        "review_scope": "FULL_COMPONENT" if changed else "NONE",
        "raw_changed": before_entry["sha256"] != current_entry["sha256"],
        "canonical_changed": changed,
        "changed_text": changed,
        "parent": before_entry,
        "current": current_entry,
    }



def _load_record(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(_regular_file(path, label="record").read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IncrementalArtifactAuditError("record is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise IncrementalArtifactAuditError("record must be a JSON object")
    return value



def _record_identity(path: Path) -> dict[str, object]:
    value = _load_record(path)
    identity: dict[str, object] = {}
    for key in ("candidate_id", "recording_date", "date"):
        if key in value:
            identity[key] = value[key]
    story = value.get("story_contract")
    if isinstance(story, Mapping):
        for key in ("candidate_id", "recording_date", "recordingDate"):
            if key in story and key not in identity:
                identity[key] = story[key]
    return identity



def _record_projection(path: Path, component: str) -> tuple[object | None, str | None]:
    value = _load_record(path)
    if component == "boundary":
        projected = value.get("boundary_audit")
        return (projected, "record:/boundary_audit") if isinstance(projected, Mapping) else (None, None)
    if component == "title":
        staging = value.get("publish_staging")
        if isinstance(staging, Mapping) and isinstance(staging.get("title"), str):
            return staging["title"], "record:/publish_staging/title"
        for key in ("title", "title_text"):
            if isinstance(value.get(key), str):
                return value[key], f"record:/{key}"
        story = value.get("story_contract")
        if isinstance(story, Mapping) and isinstance(story.get("title"), str):
            return story["title"], "record:/story_contract/title"
    return None, None



def _record_value_entry(value: object, *, source: str, kind: str) -> dict[str, object]:
    if kind == "boundary":
        canonical = _canonical_json(value)
    else:
        if not isinstance(value, str):
            raise IncrementalArtifactAuditError("record title projection is not text")
        canonical = value.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return {
        "source": source,
        "bytes": len(canonical),
        "sha256": _sha256_bytes(canonical),
        "canonical_sha256": _sha256_bytes(canonical),
    }



def _record_component_delta(
    parent_record: Path,
    current_record: Path,
    *,
    component: str,
) -> dict[str, object]:
    before, before_source = _record_projection(parent_record, component)
    current, current_source = _record_projection(current_record, component)
    if before is None or current is None or before_source is None or current_source is None:
        return {
            "status": "INPUT_REQUIRED",
            "review_scope": "INPUT_REQUIRED",
            "reason_code": f"{component.upper()}_INPUT_MISSING",
        }
    before_entry = _record_value_entry(before, source=before_source, kind=component)
    current_entry = _record_value_entry(current, source=current_source, kind=component)
    changed = before_entry["canonical_sha256"] != current_entry["canonical_sha256"]
    return {
        "status": "FULL_COMPONENT_REVIEW_REQUIRED" if changed else "UNCHANGED",
        "review_scope": "FULL_COMPONENT" if changed else "NONE",
        "raw_changed": changed,
        "canonical_changed": changed,
        "changed_pointers": _json_diff_paths(before, current) if component == "boundary" and changed else [],
        "changed_text": changed if component == "title" else None,
        "parent": before_entry,
        "current": current_entry,
    }



def _snapshot(paths: ArtifactPaths) -> dict[str, object]:
    record = _raw_entry(paths.record, label="record")
    record["identity"] = _record_identity(paths.record)
    subtitle_entry, _ = _read_srt(paths.subtitle, label="subtitle")
    components: dict[str, object] = {
        "video": _raw_entry(paths.video, label="video"),
        "subtitle": subtitle_entry,
        "cover": _raw_entry(paths.cover, label="cover"),
    }
    optional = {
        "boundary": paths.boundary,
        "title": paths.title,
    }
    for component, path in optional.items():
        if path is not None:
            components[component] = (
                _canonical_json_entry(path, label=component)
                if component == "boundary"
                else _canonical_text_entry(path, label=component)
            )
            continue
        projected, source = _record_projection(paths.record, component)
        components[component] = (
            _record_value_entry(projected, source=source or "", kind=component)
            if projected is not None and source is not None
            else {"status": "INPUT_REQUIRED", "reason_code": f"{component.upper()}_INPUT_MISSING"}
        )
    return {"record": record, "components": components}



def _declared_component(declared: Mapping[str, object] | None, component: str) -> Mapping[str, object] | None:
    if not isinstance(declared, Mapping):
        return None
    value = declared.get(component)
    return value if isinstance(value, Mapping) else None



def build_video_edit_map(
    *,
    parent_video: Path,
    current_video: Path,
    edit_map_id: str,
    windows: object,
) -> dict[str, object]:
    """Bind producer edit windows to both exact video byte hashes."""

    if not isinstance(edit_map_id, str) or not edit_map_id.strip():
        raise IncrementalArtifactAuditError("video edit_map_id is required")
    normalized = _parse_windows(windows, label="video windows")
    if not normalized:
        raise IncrementalArtifactAuditError("video edit map must contain windows")
    parent = _raw_entry(parent_video, label="parent video")
    current = _raw_entry(current_video, label="current video")
    binding = {
        "edit_map_id": edit_map_id,
        "windows": normalized,
        "source_video_sha256": parent["sha256"],
        "target_video_sha256": current["sha256"],
    }
    binding["edit_map_sha256"] = _sha256_bytes(_canonical_json(binding))
    return binding



def _video_delta(
    before_path: Path,
    current_path: Path,
    declared: Mapping[str, object] | None,
) -> dict[str, object]:
    before = _raw_entry(before_path, label="parent video")
    current = _raw_entry(current_path, label="current video")
    changed = before["sha256"] != current["sha256"]
    if not changed:
        return {
            "status": "UNCHANGED",
            "review_scope": "NONE",
            "raw_changed": False,
            "parent": before,
            "current": current,
        }
    windows = _parse_windows((declared or {}).get("windows"), label="video windows") if declared else []
    if not windows:
        return {
            "status": "FULL_COMPONENT_REVIEW_REQUIRED",
            "review_scope": "FULL_COMPONENT",
            "raw_changed": True,
            "reason_code": "VIDEO_EDIT_MAP_MISSING",
            "parent": before,
            "current": current,
        }
    edit_map_id = (declared or {}).get("edit_map_id")
    source_sha = (declared or {}).get("source_video_sha256")
    target_sha = (declared or {}).get("target_video_sha256")
    expected_map_sha = _sha256_bytes(
        _canonical_json(
            {
                "edit_map_id": edit_map_id,
                "windows": windows,
                "source_video_sha256": before["sha256"],
                "target_video_sha256": current["sha256"],
            }
        )
    )
    if (
        not isinstance(edit_map_id, str)
        or not edit_map_id.strip()
        or source_sha != before["sha256"]
        or target_sha != current["sha256"]
        or (declared or {}).get("edit_map_sha256") != expected_map_sha
    ):
        return {
            "status": "FULL_COMPONENT_REVIEW_REQUIRED",
            "review_scope": "FULL_COMPONENT",
            "raw_changed": True,
            "reason_code": "VIDEO_EDIT_MAP_UNBOUND",
            "parent": before,
            "current": current,
        }
    return {
        "status": "SCOPED_REVIEW_REQUIRED",
        "review_scope": "TIME_WINDOWS",
        "raw_changed": True,
        "declared_windows": windows,
        "edit_map_id": edit_map_id,
        "edit_map_sha256": expected_map_sha,
        "parent": before,
        "current": current,
    }



def _operator_coverage(
    operator_plan: Mapping[str, object] | None,
    subtitle_delta: Mapping[str, object],
    change_points: object,
) -> dict[str, object]:
    if operator_plan is None:
        return {"status": "NOT_CONFIGURED"}
    if operator_plan.get("whole_clip_rerun_required") is True:
        return {"status": "WHOLE_CLIP_REQUIRED", "covered": True}
    if not isinstance(change_points, list):
        return {"status": "MISSING", "covered": False, "reason_code": "OPERATOR_CHANGE_POINTS_MISSING"}
    points: list[dict[str, object]] = []
    for point in change_points:
        if not isinstance(point, Mapping):
            return {"status": "INVALID", "covered": False, "reason_code": "OPERATOR_CHANGE_POINT_INVALID"}
        component = str(point.get("component") or "subtitle")
        try:
            windows = _parse_windows([point], label="operator change point")
        except IncrementalArtifactAuditError:
            return {"status": "INVALID", "covered": False, "reason_code": "OPERATOR_CHANGE_POINT_INVALID"}
        points.append({"component": component, "windows": windows})
    subtitle_points = _merge_windows(
        [window for point in points if point["component"] == "subtitle" for window in point["windows"]]
    )
    changed_windows = subtitle_delta.get("changed_windows")
    if not isinstance(changed_windows, list):
        changed_windows = []
    uncovered = [
        window
        for window in changed_windows
        if not any(window[0] < point[1] and point[0] < window[1] for point in subtitle_points)
    ]
    if uncovered:
        return {
            "status": "INCOMPLETE",
            "covered": False,
            "covered_windows": [window for window in changed_windows if window not in uncovered],
            "uncovered_windows": uncovered,
            "reason_code": "OPERATOR_CHANGE_COVERAGE_INCOMPLETE",
        }
    return {
        "status": "COVERAGE_PASS",
        "covered": True,
        "covered_windows": changed_windows,
        "point_count": len(change_points),
    }



def build_incremental_audit(
    *,
    parent: ArtifactPaths,
    current: ArtifactPaths,
    parent_authority_id: str,
    candidate_id: str,
    recording_date: str,
    declared_changes: Mapping[str, object] | None = None,
    issue_count: int | None = None,
    explicitly_exhaustive: bool = False,
    only_these_errors: bool | None = None,
    operator_change_points: object = None,
) -> dict[str, object]:
    """Build a new, unsigned component-delta review plan from latest bytes."""

    if not parent_authority_id.strip():
        raise IncrementalArtifactAuditError("parent_authority_id is required")
    if not candidate_id.strip() or not recording_date.strip():
        raise IncrementalArtifactAuditError("candidate identity is required")
    parent_snapshot = _snapshot(parent)
    current_snapshot = _snapshot(current)
    for snapshot, label in ((parent_snapshot, "parent"), (current_snapshot, "current")):
        identity = snapshot["record"]["identity"]
        if isinstance(identity, Mapping):
            observed_candidate = identity.get("candidate_id")
            observed_date = identity.get("recording_date", identity.get("date"))
            if observed_candidate is not None and observed_candidate != candidate_id:
                raise IncrementalArtifactAuditError(f"{label} record candidate identity drift")
            if observed_date is not None and observed_date != recording_date:
                raise IncrementalArtifactAuditError(f"{label} record date identity drift")
    declared = declared_changes if isinstance(declared_changes, Mapping) else {}
    subtitle_delta, _ = _srt_delta(parent.subtitle, current.subtitle)
    deltas: dict[str, object] = {
        "video": _video_delta(parent.video, current.video, _declared_component(declared, "video")),
        "subtitle": subtitle_delta,
        "cover": _cover_delta(parent.cover, current.cover, _declared_component(declared, "cover")),
    }
    if parent.boundary is None and current.boundary is None:
        deltas["boundary"] = _record_component_delta(
            parent.record, current.record, component="boundary"
        )
    elif parent.boundary is None or current.boundary is None:
        raise IncrementalArtifactAuditError("boundary must be present in both versions or neither")
    else:
        deltas["boundary"] = _json_component_delta(parent.boundary, current.boundary, label="boundary")
    if parent.title is None and current.title is None:
        deltas["title"] = _record_component_delta(
            parent.record, current.record, component="title"
        )
    elif parent.title is None or current.title is None:
        raise IncrementalArtifactAuditError("title must be present in both versions or neither")
    else:
        deltas["title"] = _text_component_delta(parent.title, current.title, label="title")
    operator_plan = None
    if issue_count is not None:
        operator_plan = plan_operator_correction(
            candidate_id=candidate_id,
            issue_count=issue_count,
            explicitly_exhaustive=explicitly_exhaustive,
            only_these_errors=only_these_errors,
        )
    coverage = _operator_coverage(operator_plan, subtitle_delta, operator_change_points)
    changed_components = [
        component
        for component in COMPONENTS
        if isinstance(deltas[component], Mapping)
        and deltas[component].get("status") not in {"UNCHANGED", "FORMAT_ONLY", "PIXELS_UNCHANGED_METADATA_ONLY"}
    ]
    plan = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "parent_authority_id": parent_authority_id,
        "parent_snapshot": parent_snapshot,
        "current_snapshot": current_snapshot,
        "changed_components": changed_components,
        "component_deltas": deltas,
        "operator_review": {
            "plan": operator_plan,
            "change_points": operator_change_points if isinstance(operator_change_points, list) else [],
            "coverage": coverage,
        },
        "release_gate_disclaimer": (
            "This scoped receipt does not replace package audit, final perceptual review, "
            "title-cover QC, authorized manifest, or upload authorization."
        ),
    }
    plan["plan_sha256"] = _sha256_bytes(_canonical_json(plan))
    return plan



def _review_result_covers(
    component: str,
    delta: Mapping[str, object],
    result: object,
    operator_plan: Mapping[str, object] | None,
) -> bool:
    if not isinstance(result, Mapping) or result.get("status") != "PASS":
        return False
    whole_clip = bool(operator_plan and operator_plan.get("whole_clip_rerun_required") is True)
    expected_scope = (
        "WHOLE_CLIP" if component == "subtitle" else "FULL_COMPONENT"
    ) if whole_clip else delta.get("review_scope")
    if result.get("scope") != expected_scope:
        return False
    if whole_clip:
        # A whole-clip result must not smuggle a narrower edit map into the
        # receipt.  The result may omit optional windows/ROI entirely.
        return not any(key in result for key in ("windows", "roi"))
    if component == "video":
        expected = delta.get("declared_windows") or []
        return (
            result.get("windows") == expected
            and result.get("edit_map_sha256") == delta.get("edit_map_sha256")
        )
    if component == "subtitle":
        expected = delta.get("changed_windows") or []
        return result.get("windows") == expected
    if component == "cover":
        return result.get("roi") == delta.get("declared_roi")
    return expected_scope == "FULL_COMPONENT"



def seal_incremental_review(
    plan: Mapping[str, object],
    *,
    review_results: Mapping[str, object] | None = None,
    sealed_by: str,
) -> dict[str, object]:
    """Seal scope coverage without converting it into a publication verdict."""

    if plan.get("schema_version") != SCHEMA_VERSION:
        raise IncrementalArtifactAuditError("incremental plan schema mismatch")
    plan_without_hash = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("plan_sha256") != _sha256_bytes(_canonical_json(plan_without_hash)):
        raise IncrementalArtifactAuditError("incremental plan self-hash mismatch")
    if not sealed_by.strip():
        raise IncrementalArtifactAuditError("sealed_by is required")
    results = review_results if isinstance(review_results, Mapping) else {}
    deltas = plan.get("component_deltas")
    if not isinstance(deltas, Mapping):
        raise IncrementalArtifactAuditError("component deltas are missing")
    operator_review = plan.get("operator_review")
    operator_plan = (
        operator_review.get("plan")
        if isinstance(operator_review, Mapping)
        and isinstance(operator_review.get("plan"), Mapping)
        else None
    )
    coverage = operator_review.get("coverage") if isinstance(operator_review, Mapping) else None
    if isinstance(coverage, Mapping) and coverage.get("covered") is False:
        raise IncrementalArtifactAuditError(str(coverage.get("reason_code") or "OPERATOR_COVERAGE_INCOMPLETE"))
    inherited: list[str] = []
    reviewed: list[str] = []
    for component in COMPONENTS:
        delta = deltas.get(component)
        if not isinstance(delta, Mapping):
            raise IncrementalArtifactAuditError(f"missing {component} delta")
        status = str(delta.get("status") or "")
        if status == "INPUT_REQUIRED":
            raise IncrementalArtifactAuditError(str(delta.get("reason_code") or "COMPONENT_INPUT_MISSING"))
        if status in {"UNCHANGED", "FORMAT_ONLY", "PIXELS_UNCHANGED_METADATA_ONLY"}:
            inherited.append(component)
            continue
        if not _review_result_covers(
            component, delta, results.get(component), operator_plan
        ):
            raise IncrementalArtifactAuditError(f"{component} scoped review evidence is incomplete")
        reviewed.append(component)
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": plan.get("policy_version"),
        "receipt_status": "INCREMENTAL_REVIEW_COMPLETE",
        "sealed_by": sealed_by,
        "candidate_id": plan.get("candidate_id"),
        "recording_date": plan.get("recording_date"),
        "parent_authority_id": plan.get("parent_authority_id"),
        "plan_sha256": plan.get("plan_sha256"),
        "parent_snapshot": plan.get("parent_snapshot"),
        "current_snapshot": plan.get("current_snapshot"),
        "changed_components": plan.get("changed_components"),
        "reviewed_components": reviewed,
        "inherited_components": inherited,
        "component_deltas": deltas,
        "operator_review": operator_review,
        "release_gate_disclaimer": plan.get("release_gate_disclaimer"),
    }
    receipt = dict(unsigned)
    receipt["receipt_sha256"] = _sha256_bytes(_canonical_json(unsigned))
    return receipt



def _snapshot_equivalent(expected: object, observed: object) -> bool:
    if isinstance(expected, Mapping) and isinstance(observed, Mapping):
        keys = set(expected) | set(observed)
        return all(
            key == "path"
            or key in expected
            and key in observed
            and _snapshot_equivalent(expected[key], observed[key])
            for key in keys
        )
    if isinstance(expected, list) and isinstance(observed, list):
        return len(expected) == len(observed) and all(
            _snapshot_equivalent(left, right) for left, right in zip(expected, observed)
        )
    return expected == observed



def validate_incremental_receipt(
    receipt: Mapping[str, object],
    *,
    current: ArtifactPaths,
) -> None:
    """Re-read current bytes and reject a stale or edited scoped receipt."""

    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise IncrementalArtifactAuditError("incremental receipt schema mismatch")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != _sha256_bytes(_canonical_json(unsigned)):
        raise IncrementalArtifactAuditError("incremental receipt self-hash mismatch")
    snapshot = _snapshot(current)
    if not _snapshot_equivalent(receipt.get("current_snapshot"), snapshot):
        raise IncrementalArtifactAuditError("CURRENT_ARTIFACT_SNAPSHOT_DRIFT")
    if receipt.get("receipt_status") != "INCREMENTAL_REVIEW_COMPLETE":
        raise IncrementalArtifactAuditError("incremental receipt is not complete")



def write_create_only(path: Path, receipt: Mapping[str, object]) -> None:
    """Persist one receipt with O_EXCL, file fsync, and parent-directory fsync."""

    if path.exists() or path.is_symlink():
        raise IncrementalArtifactAuditError(f"receipt output already exists: {path}")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise IncrementalArtifactAuditError(f"receipt parent is unsafe: {parent}")
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while persisting incremental receipt")
            view = view[written:]
        os.fsync(fd)
    except FileExistsError as exc:
        raise IncrementalArtifactAuditError(f"receipt output already exists: {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    directory_fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


__all__ = [
    "ArtifactPaths",
    "COMPONENTS",
    "IncrementalArtifactAuditError",
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "build_incremental_audit",
    "build_video_edit_map",
    "seal_incremental_review",
    "validate_incremental_receipt",
    "write_create_only",
]
