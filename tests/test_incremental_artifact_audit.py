from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)
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


def test_record_identity_is_required_for_latest_binding(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path)
    record = json.loads(current.record.read_text(encoding="utf-8"))
    record.pop("candidate_id")
    current.record.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(IncrementalArtifactAuditError, match="record candidate identity drift"):
        build_incremental_audit(
            parent=parent,
            current=current,
            parent_authority_id="authority-old",
            candidate_id=CID,
            recording_date=DATE,
        )


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


def test_receipt_self_hash_rejects_tampering(tmp_path: Path) -> None:
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
    receipt["sealed_by"] = "tampered"
    with pytest.raises(IncrementalArtifactAuditError, match="self-hash mismatch"):
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


@pytest.mark.parametrize(
    ("issue_count", "only_these_errors", "expected_mode"),
    [
        (1, False, "WHOLE_CLIP_RERUN_AND_REVIEW"),
        (2, False, "WHOLE_CLIP_RERUN_AND_REVIEW"),
        (1, True, "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"),
        (2, True, "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"),
        (3, False, "WHOLE_CLIP_RERUN_AND_REVIEW"),
        (4, False, "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"),
        (5, True, "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"),
    ],
)
def test_operator_scope_boundary_matrix(
    tmp_path: Path,
    issue_count: int,
    only_these_errors: bool,
    expected_mode: str,
) -> None:
    parent, current = _base_pair(tmp_path, subtitle=_srt("改后的第二句"))
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        issue_count=issue_count,
        only_these_errors=only_these_errors,
        operator_change_points=(
            [{"component": "subtitle", "start_ms": 1_000, "end_ms": 2_000}]
            if issue_count <= 2 and only_these_errors or issue_count > 3
            else None
        ),
    )
    assert plan["operator_review"]["plan"]["mode"] == expected_mode
    assert plan["operator_review"]["plan"]["reported_issue_count"] == issue_count
    if expected_mode == "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX":
        assert plan["operator_review"]["coverage"]["covered"] is True
    else:
        assert plan["operator_review"]["coverage"]["status"] == "WHOLE_CLIP_REQUIRED"


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


def test_record_projections_supply_boundary_and_title_hashes(tmp_path: Path) -> None:
    parent_full, current_full = _base_pair(tmp_path)
    for path, start, title in (
        (parent_full.record, 0, "标题一"),
        (current_full.record, 10, "标题二"),
    ):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["boundary_audit"] = {"start_ms": start, "end_ms": 2_000}
        record["publish_staging"] = {"title": title}
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    parent = ArtifactPaths(parent_full.record, parent_full.video, parent_full.subtitle, parent_full.cover)
    current = ArtifactPaths(current_full.record, current_full.video, current_full.subtitle, current_full.cover)
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
    )
    assert plan["changed_components"] == ["boundary", "title"]
    assert plan["component_deltas"]["boundary"]["changed_pointers"] == ["/start_ms"]
    assert plan["component_deltas"]["title"]["review_scope"] == "FULL_COMPONENT"


def test_boundary_and_title_hashes_are_independent_components(tmp_path: Path) -> None:
    parent, current = _base_pair(
        tmp_path,
        boundary={"start_ms": 10, "end_ms": 2_000},
        title="标题二",
    )
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
    )
    assert plan["changed_components"] == ["boundary", "title"]
    assert plan["component_deltas"]["boundary"]["changed_pointers"] == ["/start_ms"]
    assert plan["component_deltas"]["title"]["review_scope"] == "FULL_COMPONENT"
    receipt = seal_incremental_review(
        plan,
        review_results={
            "boundary": {"status": "PASS", "scope": "FULL_COMPONENT"},
            "title": {"status": "PASS", "scope": "FULL_COMPONENT"},
        },
        sealed_by="Codex root",
    )
    assert receipt["reviewed_components"] == ["boundary", "title"]


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


def test_operator_coverage_must_include_changed_video_component(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, video=b"video-v2")
    edit_map = build_video_edit_map(
        parent_video=parent.video,
        current_video=current.video,
        edit_map_id="operation-1",
        windows=[[4_000, 4_500]],
    )
    plan = build_incremental_audit(
        parent=parent,
        current=current,
        parent_authority_id="authority-old",
        candidate_id=CID,
        recording_date=DATE,
        declared_changes={"video": edit_map},
        issue_count=4,
        operator_change_points=[{"component": "subtitle", "start_ms": 4_000, "end_ms": 4_500}],
    )
    assert plan["operator_review"]["coverage"]["covered"] is False
    assert plan["operator_review"]["coverage"]["uncovered_components"] == ["video"]


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


def test_cli_hook_seals_and_revalidates_receipt(tmp_path: Path) -> None:
    parent, current = _base_pair(tmp_path, subtitle=_srt("改后的第二句"))
    review_results = tmp_path / "review-results.json"
    review_results.write_text(
        json.dumps({"subtitle": {"status": "PASS", "scope": "WHOLE_CLIP"}}),
        encoding="utf-8",
    )
    plan_out = tmp_path / "plan.json"
    receipt_out = tmp_path / "receipt.json"
    script = Path(__file__).parents[1] / "scripts" / "build_incremental_artifact_audit.py"
    command = [
        sys.executable,
        str(script),
        "--candidate-id",
        CID,
        "--recording-date",
        DATE,
        "--parent-authority-id",
        "authority-old",
        "--parent-record",
        str(parent.record),
        "--parent-video",
        str(parent.video),
        "--parent-subtitle",
        str(parent.subtitle),
        "--parent-cover",
        str(parent.cover),
        "--parent-boundary",
        str(parent.boundary),
        "--parent-title",
        str(parent.title),
        "--current-record",
        str(current.record),
        "--current-video",
        str(current.video),
        "--current-subtitle",
        str(current.subtitle),
        "--current-cover",
        str(current.cover),
        "--current-boundary",
        str(current.boundary),
        "--current-title",
        str(current.title),
        "--issue-count",
        "2",
        "--out",
        str(plan_out),
        "--review-results",
        str(review_results),
        "--sealed-by",
        "test-hook",
        "--receipt-out",
        str(receipt_out),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert plan_out.is_file() and receipt_out.is_file()
    receipt = json.loads(receipt_out.read_text(encoding="utf-8"))
    validate_incremental_receipt(receipt, current=current)


def test_existing_package_and_release_gates_ignore_incremental_receipt(tmp_path: Path) -> None:
    from scripts.audit_lidousha_review_package import audit_package

    without_receipt = audit_package(tmp_path)
    (tmp_path / "incremental-artifact-audit.json").write_text(
        json.dumps(
            {
                "schema_version": "incremental-artifact-audit.v1",
                "receipt_status": "INCREMENTAL_REVIEW_COMPLETE",
            }
        ),
        encoding="utf-8",
    )
    with_receipt = audit_package(tmp_path)
    for key in ("passed", "blocking_issue_count", "issue_count"):
        assert with_receipt[key] == without_receipt[key]
    assert [issue["code"] for issue in with_receipt["issues"]] == [
        issue["code"] for issue in without_receipt["issues"]
    ]

    root = Path(__file__).parents[1]
    gate_sources = (
        "scripts/audit_lidousha_review_package.py",
        "scripts/build_lidousha_recovery_review_manifest.py",
        "scripts/authorized_upload.py",
        "src/autoslice/final_review_contract.py",
        "src/autoslice/cover_route_evidence.py",
    )
    for relative in gate_sources:
        source = (root / relative).read_text(encoding="utf-8")
        assert "incremental_artifact_audit" not in source
        assert "incremental-artifact-audit" not in source


def test_incremental_receipt_cannot_satisfy_final_review_gate(tmp_path: Path) -> None:
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
    with pytest.raises(FinalReviewContractError, match="FINAL_REVIEW_AUDIT_SCHEMA_INVALID"):
        validate_final_review_release(receipt)
