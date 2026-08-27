from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.incremental_artifact_audit import (
    ArtifactPaths,
    IncrementalArtifactAuditError,
    build_incremental_audit,
    build_video_edit_map,
    seal_incremental_review,
    validate_incremental_receipt,
    write_create_only,
)


CID = "auto_synthetic_1"
DATE = "2026-08-27"


def _srt(second_text: str = "第二句") -> str:
    return (
        "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
        f"2\n00:00:01,000 --> 00:00:02,000\n{second_text}\n"
    )


def _image(path: Path, *, changed: bool = False, box: tuple[int, int, int, int] = (5, 6, 9, 10)) -> None:
    image = Image.new("RGB", (32, 32), "black")
    if changed:
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                image.putpixel((x, y), (255, 255, 255))
    image.save(path)


def _version(
    root: Path,
    *,
    version: str,
    subtitle: str | None = None,
    video: bytes = b"video-v1",
    cover_changed: bool = False,
    boundary: dict | None = None,
    title: str = "标题一",
) -> ArtifactPaths:
    root.mkdir(parents=True, exist_ok=True)
    record = root / "record.json"
    record.write_text(
        json.dumps(
            {"candidate_id": CID, "recording_date": DATE, "version": version},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    video_path = root / "video.mp4"
    video_path.write_bytes(video)
    subtitle_path = root / "subtitle.srt"
    subtitle_path.write_text(subtitle if subtitle is not None else _srt(), encoding="utf-8")
    cover_path = root / "cover.png"
    _image(cover_path, changed=cover_changed)
    boundary_path = root / "boundary.json"
    boundary_path.write_text(json.dumps(boundary or {"start_ms": 0, "end_ms": 2_000}), encoding="utf-8")
    title_path = root / "title.txt"
    title_path.write_text(title, encoding="utf-8")
    return ArtifactPaths(record, video_path, subtitle_path, cover_path, boundary_path, title_path)


def _base_pair(tmp_path: Path, **current_kwargs: object) -> tuple[ArtifactPaths, ArtifactPaths]:
    parent = _version(tmp_path / "parent", version="old")
    current = _version(tmp_path / "current", version="new", **current_kwargs)
    return parent, current


def test_subtitle_delta_is_cue_scoped_and_latest_record_is_bound(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, subtitle=_srt("改后的第二句"))
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        issue_count=4,
        operator_change_points=[{"component": "subtitle", "start_ms": 1_000, "end_ms": 2_000}],
    )
    assert plan["changed_components"] == ["subtitle"]
    delta = plan["component_deltas"]["subtitle"]
    assert delta["status"] == "SCOPED_REVIEW_REQUIRED"
    assert delta["changed_windows"] == [[1_000, 2_000]]
    assert plan["operator_review"]["plan"]["operator_scope"] == "EXHAUSTIVE_CANDIDATE"
    assert plan["operator_review"]["coverage"]["status"] == "COVERAGE_PASS"

    receipt = seal_incremental_review(
        plan,
        review_results={
            "subtitle": {
                "status": "PASS",
                "scope": "CUE_WINDOWS",
                "windows": [[1_000, 2_000]],
            }
        },
        sealed_by="Codex root",
    )
    assert receipt["inherited_components"] == ["video", "cover", "boundary", "title"]
    assert receipt["reviewed_components"] == ["subtitle"]
    assert receipt["parent_snapshot"]["record"]["sha256"] != receipt["current_snapshot"]["record"]["sha256"]
    validate_incremental_receipt(receipt, current=current)

    current.video.write_bytes(b"drift-after-seal")
    with pytest.raises(IncrementalArtifactAuditError, match="CURRENT_ARTIFACT_SNAPSHOT_DRIFT"):
        validate_incremental_receipt(receipt, current=current)


def test_one_or_two_points_default_to_whole_clip_but_explicit_only_is_scoped(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, subtitle=_srt("改后的第二句"))
    default_plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        issue_count=2,
    )
    assert default_plan["operator_review"]["plan"]["mode"] == "WHOLE_CLIP_RERUN_AND_REVIEW"
    assert default_plan["operator_review"]["coverage"]["status"] == "WHOLE_CLIP_REQUIRED"
    receipt = seal_incremental_review(
        default_plan,
        review_results={"subtitle": {"status": "PASS", "scope": "WHOLE_CLIP"}},
        sealed_by="Codex root",
    )
    assert receipt["reviewed_components"] == ["subtitle"]

    explicit_plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        issue_count=2,
        only_these_errors=True,
        operator_change_points=[{"component": "subtitle", "start_ms": 1_000, "end_ms": 2_000}],
    )
    assert explicit_plan["operator_review"]["plan"]["mode"] == "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"
    assert explicit_plan["operator_review"]["coverage"]["covered"] is True


def test_exactly_three_points_are_conservative_whole_clip(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, subtitle=_srt("改后的第二句"))
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        issue_count=3,
        operator_change_points=[{"component": "subtitle", "start_ms": 1_000, "end_ms": 2_000}],
    )
    assert plan["operator_review"]["plan"]["mode"] == "WHOLE_CLIP_RERUN_AND_REVIEW"
    assert plan["operator_review"]["coverage"]["status"] == "WHOLE_CLIP_REQUIRED"


def test_exhaustive_shortcut_requires_every_changed_window_to_be_reported(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, subtitle=_srt("改后的第二句"))
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        issue_count=4,
    )
    assert plan["operator_review"]["coverage"]["status"] == "MISSING"
    with pytest.raises(IncrementalArtifactAuditError, match="OPERATOR_CHANGE_POINTS_MISSING"):
        seal_incremental_review(plan, review_results={}, sealed_by="Codex root")


def test_cover_delta_scopes_real_pixels_and_rejects_outside_roi(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, cover_changed=True)
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        declared_changes={"cover": {"roi": [4, 5, 10, 10]}},
    )
    delta = plan["component_deltas"]["cover"]
    assert delta["status"] == "SCOPED_REVIEW_REQUIRED"
    assert delta["pixel_changed_bbox"] == [5, 6, 4, 4]
    receipt = seal_incremental_review(
        plan,
        review_results={"cover": {"status": "PASS", "scope": "PIXEL_ROI", "roi": [4, 5, 10, 10]}},
        sealed_by="Codex root",
    )
    assert receipt["reviewed_components"] == ["cover"]

    outside_parent, outside_current = _base_pair(tmp_path / "outside", cover_changed=True)
    with pytest.raises(IncrementalArtifactAuditError, match="COVER_CHANGE_OUTSIDE_DECLARED_ROI"):
        build_incremental_audit(
            parent=outside_parent,
            current=outside_current,
            parent_authority_id="authority-old",
            candidate_id=CID,
            recording_date=DATE,
            declared_changes={"cover": {"roi": [0, 0, 2, 2]}},
        )


def test_video_edit_windows_without_bound_hash_fall_back_to_full_review(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, video=b"video-v2")
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        declared_changes={"video": {"windows": [[4_000, 4_500]], "edit_map_id": "operation-1"}},
    )
    assert plan["component_deltas"]["video"]["reason_code"] == "VIDEO_EDIT_MAP_UNBOUND"


def test_video_without_edit_map_falls_back_to_full_component_review(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, video=b"video-v2")
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
    )
    delta = plan["component_deltas"]["video"]
    assert delta["status"] == "FULL_COMPONENT_REVIEW_REQUIRED"
    assert delta["reason_code"] == "VIDEO_EDIT_MAP_MISSING"

    edit_map = build_video_edit_map(
        parent_video=parent.video,
        current_video=current.video,
        edit_map_id="operation-1",
        windows=[[4_000, 4_500]],
    )
    scoped = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        declared_changes={"video": edit_map},
    )
    assert scoped["component_deltas"]["video"]["review_scope"] == "TIME_WINDOWS"
    receipt = seal_incremental_review(
        scoped,
        review_results={
            "video": {
                "status": "PASS",
                "scope": "TIME_WINDOWS",
                "windows": [[4_000, 4_500]],
                "edit_map_sha256": edit_map["edit_map_sha256"],
            }
        },
        sealed_by="Codex root",
    )
    assert receipt["reviewed_components"] == ["video"]


def test_missing_boundary_or_title_input_cannot_be_sealed(tmp_path: Path) -> None:
    parent_full, current_full = _base_pair(tmp_path)
    parent = ArtifactPaths(parent_full.record, parent_full.video, parent_full.subtitle, parent_full.cover)
    current = ArtifactPaths(current_full.record, current_full.video, current_full.subtitle, current_full.cover)
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
    )
    assert plan["component_deltas"]["boundary"]["status"] == "INPUT_REQUIRED"
    assert plan["component_deltas"]["title"]["status"] == "INPUT_REQUIRED"
    with pytest.raises(IncrementalArtifactAuditError, match="BOUNDARY_INPUT_MISSING"):
        seal_incremental_review(plan, review_results={}, sealed_by="Codex root")


def test_create_only_receipt_rejects_replacement(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, subtitle=_srt("改后的第二句"))
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        issue_count=2,
    )
    receipt = seal_incremental_review(
        plan,
        review_results={"subtitle": {"status": "PASS", "scope": "WHOLE_CLIP"}},
        sealed_by="Codex root",
    )
    output = tmp_path / "receipt.json"
    write_create_only(output, receipt)
    with pytest.raises(IncrementalArtifactAuditError, match="already exists"):
        write_create_only(output, receipt)
