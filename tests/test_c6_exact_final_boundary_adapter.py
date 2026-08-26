"""Provider-free C6 exact-final/boundary adapter coverage."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.c6_exact_final_boundary_adapter as adapter

ROOT = Path(__file__).parents[1]
LANE_ROOT = ROOT / "assets/lidousha/reviewed_subtitle_baselines"


def test_clean_pipeline_to_reviewed_projection_is_exactly_one_cue() -> None:
    pipeline = (LANE_ROOT / "auto_120032_753_816.pipeline-diagnostic.srt").read_bytes()
    reviewed = (LANE_ROOT / "auto_120032_753_816.reviewed.srt").read_bytes()
    grid = adapter._compare_projection(pipeline, reviewed)
    assert grid.startswith("sha256:")
    assert len(grid) == len("sha256:") + 64


def test_projection_rejects_timing_order_and_text_scope_mismatches() -> None:
    pipeline = (LANE_ROOT / "auto_120032_753_816.pipeline-diagnostic.srt").read_bytes()
    reviewed = (LANE_ROOT / "auto_120032_753_816.reviewed.srt").read_bytes()
    changed = reviewed.replace(b"00:00:51,070 --> 00:00:53,150", b"00:00:51,071 --> 00:00:53,150")
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError):
        adapter._compare_projection(pipeline, changed)
    changed_text = reviewed.replace(adapter.NEW_CUE17.encode(), b"tampered")
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError):
        adapter._compare_projection(pipeline, changed_text)


def test_stage_pin_mismatch_fails_closed_before_any_provider_path(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    unsigned = {
        "schema_version": "reviewed-baseline-replay-stage.v1",
        "date": adapter.RECORDING_DATE,
        "candidate_id": adapter.CANDIDATE_ID,
        "record_sha256": adapter.RECORD_SHA256,
        "expected_video_sha256": adapter.FROZEN_DELIVERY_VIDEO_SHA256,
        "baseline_manifest": "manifest",
        "baseline_sha256": adapter.REVIEWED_SRT_SHA256,
        "artifacts": {"video": adapter.FROZEN_DELIVERY_VIDEO_SHA256, "subtitle": adapter.REVIEWED_SRT_SHA256},
        "predicate_matrix": [],
        "upload_allowed": False,
    }
    document = dict(unsigned)
    document["stage_sha256"] = adapter._sha_bytes(adapter._canonical(unsigned))
    (stage / "stage.json").write_bytes(adapter._canonical(document))
    (stage / "reviewed.srt").write_bytes(
        (LANE_ROOT / "auto_120032_753_816.reviewed.srt").read_bytes()
    )
    plan = SimpleNamespace(
        candidate_id=adapter.CANDIDATE_ID,
        date=adapter.RECORDING_DATE,
        local_start_ms=adapter.SOURCE_LOCAL_START_MS,
        local_end_ms=adapter.SOURCE_LOCAL_END_MS,
        expected_video_sha256=adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
        package_root=tmp_path / "package",
        record_path=tmp_path / "package" / "replacement_recuts" / f"{adapter.CANDIDATE_ID}.record.json",
        padded_path=tmp_path / "package" / "padded.mp4",
    )
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError, match="STAGE_MANIFEST_HASH"):
        adapter._validate_stage(plan, stage)


def test_split_pin_accepts_plan_identity_but_rejects_stale_delivery_identity() -> None:
    plan = SimpleNamespace(
        candidate_id=adapter.CANDIDATE_ID,
        date=adapter.RECORDING_DATE,
        local_start_ms=adapter.SOURCE_LOCAL_START_MS,
        local_end_ms=adapter.SOURCE_LOCAL_END_MS,
        expected_video_sha256=adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
    )
    adapter._validate_plan_identity(plan)
    plan.expected_video_sha256 = adapter.FROZEN_DELIVERY_VIDEO_SHA256
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError, match="PLAN_COORDINATE_OR_IDENTITY_DRIFT"):
        adapter._validate_plan_identity(plan)


@pytest.mark.parametrize("field", ["candidate_id", "date", "local_start_ms", "local_end_ms"])
def test_split_pin_rejects_coordinate_mutations(field: str) -> None:
    values = {
        "candidate_id": adapter.CANDIDATE_ID,
        "date": adapter.RECORDING_DATE,
        "local_start_ms": adapter.SOURCE_LOCAL_START_MS,
        "local_end_ms": adapter.SOURCE_LOCAL_END_MS,
        "expected_video_sha256": adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
    }
    values[field] = values[field] + 1 if isinstance(values[field], int) else "mutated"
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError, match="PLAN_COORDINATE_OR_IDENTITY_DRIFT"):
        adapter._validate_plan_identity(SimpleNamespace(**values))


def test_split_pin_rejects_plan_path_mutation(tmp_path: Path) -> None:
    root = tmp_path / "package"
    plan = SimpleNamespace(
        candidate_id=adapter.CANDIDATE_ID, date=adapter.RECORDING_DATE,
        local_start_ms=adapter.SOURCE_LOCAL_START_MS, local_end_ms=adapter.SOURCE_LOCAL_END_MS,
        expected_video_sha256=adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
        package_root=root,
        record_path=root / "wrong.record.json",
        padded_path=tmp_path / "package" / "padded.mp4",
    )
    adapter._validate_plan_identity(plan)
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError, match="PLAN_PATH_OR_IDENTITY_DRIFT"):
        adapter._validate_plan_paths(plan)


def test_stage_delivery_pin_is_distinct_from_plan_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reviewed = (LANE_ROOT / "auto_120032_753_816.reviewed.srt").read_bytes()
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "reviewed.srt").write_bytes(reviewed)
    plan = SimpleNamespace(
        candidate_id=adapter.CANDIDATE_ID,
        date=adapter.RECORDING_DATE,
        local_start_ms=adapter.SOURCE_LOCAL_START_MS,
        local_end_ms=adapter.SOURCE_LOCAL_END_MS,
        expected_video_sha256=adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
        package_root=tmp_path / "package",
        record_path=tmp_path / "package" / "replacement_recuts" / f"{adapter.CANDIDATE_ID}.record.json",
        padded_path=tmp_path / "package" / "padded.mp4",
    )
    unsigned = {
        "schema_version": "reviewed-baseline-replay-stage.v1",
        "candidate_id": adapter.CANDIDATE_ID,
        "date": adapter.RECORDING_DATE,
        "record_sha256": adapter.RECORD_SHA256,
        "expected_video_sha256": adapter.FROZEN_DELIVERY_VIDEO_SHA256,
        "baseline_sha256": adapter.REVIEWED_SRT_SHA256,
        "artifacts": {"video": adapter.FROZEN_DELIVERY_VIDEO_SHA256, "subtitle": adapter.REVIEWED_SRT_SHA256},
        "upload_allowed": False,
    }
    document = dict(unsigned)
    document["stage_sha256"] = adapter._sha_bytes(adapter._canonical(unsigned))
    monkeypatch.setattr(adapter, "_load_json", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(adapter, "_binding", lambda path, expected, label: SimpleNamespace(path=path, sha256=expected))
    loaded, staged = adapter._validate_stage(plan, stage)
    assert loaded["expected_video_sha256"] == adapter.FROZEN_DELIVERY_VIDEO_SHA256
    assert staged == reviewed


@pytest.mark.parametrize("wrong", [
    adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
    "sha256:a5798e0000000000000000000000000000000000000000000000000000000000",
    "sha256:0f7d9900000000000000000000000000000000000000000000000000000000000",
])
def test_stage_rejects_recut_or_source_identity_as_delivery_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong: str,
) -> None:
    reviewed = (LANE_ROOT / "auto_120032_753_816.reviewed.srt").read_bytes()
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "reviewed.srt").write_bytes(reviewed)
    plan = SimpleNamespace(
        candidate_id=adapter.CANDIDATE_ID, date=adapter.RECORDING_DATE,
        local_start_ms=adapter.SOURCE_LOCAL_START_MS, local_end_ms=adapter.SOURCE_LOCAL_END_MS,
        expected_video_sha256=adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
        package_root=tmp_path / "package",
        record_path=tmp_path / "package" / "replacement_recuts" / f"{adapter.CANDIDATE_ID}.record.json",
        padded_path=tmp_path / "package" / "padded.mp4",
    )
    unsigned = {
        "schema_version": "reviewed-baseline-replay-stage.v1", "candidate_id": adapter.CANDIDATE_ID,
        "date": adapter.RECORDING_DATE, "record_sha256": adapter.RECORD_SHA256,
        "expected_video_sha256": adapter.FROZEN_DELIVERY_VIDEO_SHA256,
        "baseline_sha256": adapter.REVIEWED_SRT_SHA256,
        "artifacts": {"video": wrong, "subtitle": adapter.REVIEWED_SRT_SHA256}, "upload_allowed": False,
    }
    document = dict(unsigned)
    document["stage_sha256"] = adapter._sha_bytes(adapter._canonical(unsigned))
    monkeypatch.setattr(adapter, "_load_json", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(adapter, "_binding", lambda path, expected, label: SimpleNamespace(path=path, sha256=expected))
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError, match="STAGE_ARTIFACT_DRIFT"):
        adapter._validate_stage(plan, stage)


def test_source_logical_identity_cannot_be_substituted_by_padded_file_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {
        "sha256": adapter.REVIEWED_SRT_SHA256.removeprefix("sha256:"),
        "absolute_source_start_ms": adapter.ABSOLUTE_SOURCE_START_MS,
        "absolute_source_end_ms": adapter.ABSOLUTE_SOURCE_END_MS,
        "source_sha256": adapter.SOURCE_MEDIA_IDENTITY.removeprefix("sha256:"),
        "time_domain": "DELIVERY_LOCAL",
        "operator_truth_lanes": {
            "pipeline_diagnostic": {"sha256": adapter.PIPELINE_SRT_SHA256.removeprefix("sha256:"), "path": "pipeline.srt"},
            "decision_ledger": {"sha256": adapter.DECISION_LEDGER_SHA256.removeprefix("sha256:"), "path": "ledger.json"},
            "diff_receipt": {"sha256": adapter.TRUTH_DIFF_SHA256.removeprefix("sha256:"), "path": "diff.json"},
        },
    }
    baseline = SimpleNamespace(
        config=config, manifest_path=LANE_ROOT / "manifest.json",
        baseline_path=LANE_ROOT / "auto_120032_753_816.reviewed.srt",
    )
    monkeypatch.setattr(adapter, "_binding", lambda path, expected, label: SimpleNamespace(path=path, sha256=expected))
    plan = SimpleNamespace(baseline=baseline)
    adapter._validate_truth_lanes(plan)
    config["source_sha256"] = "0f7d9900000000000000000000000000000000000000000000000000000000000"
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError, match="BASELINE_BINDING_DRIFT"):
        adapter._validate_truth_lanes(plan)


def test_reviewer_is_deterministic_and_never_invokes_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewed = (LANE_ROOT / "auto_120032_753_816.reviewed.srt").read_bytes()
    fake_stage = Path("/private/c6-stage")
    plan = SimpleNamespace(
        candidate_id=adapter.CANDIDATE_ID,
        date=adapter.RECORDING_DATE,
        local_start_ms=adapter.SOURCE_LOCAL_START_MS,
        local_end_ms=adapter.SOURCE_LOCAL_END_MS,
        expected_video_sha256=adapter.REPLAY_PLAN_EXPECTED_VIDEO_SHA256,
        baseline=SimpleNamespace(manifest_path=LANE_ROOT / "manifest.json"),
        package_root=Path("/private/c6-package"),
    )
    monkeypatch.setattr(adapter, "_validate_authority", lambda: {})
    monkeypatch.setattr(adapter, "_validate_truth_lanes", lambda _plan: None)
    monkeypatch.setattr(adapter, "_validate_stage", lambda _plan, _stage: ({}, reviewed))
    monkeypatch.setattr(adapter, "_validate_old_authority", lambda _plan: ({}, {"status": "PASS", "review_scope": "final_delivery", "final_endpoint_binding": {"final_start_ms": 0, "final_end_ms": adapter.DELIVERY_LOCAL_END_MS}}))
    monkeypatch.setattr(adapter, "_chat_authority", lambda _plan: {"final_review_audit": {"reviewed_srt_sha256": adapter.REVIEWED_SRT_SHA256}})
    monkeypatch.setattr(adapter, "_binding", lambda path, expected, label: SimpleNamespace(path=path, sha256=expected))
    monkeypatch.setattr(adapter, "_audit", lambda **kwargs: {"schema_version": "final-review-audit.v2", "status": "CLEAN", "release_gate": "PASS", "reviewed_srt_sha256": adapter.REVIEWED_SRT_SHA256})
    calls = 0
    reviewer = adapter.build_c6_exact_final_reviewer(plan=plan, stage=fake_stage)

    result_a = reviewer(reviewed.decode(), {"schema_version": "chat-authority-audit.v2", "status": "NO_MATCH", "final_status": "FINAL_ARTIFACTS_VERIFIED"}, adapter.SOURCE_LOCAL_START_MS, adapter.SOURCE_LOCAL_END_MS)
    result_b = reviewer(reviewed.decode(), {"schema_version": "chat-authority-audit.v2", "status": "NO_MATCH", "final_status": "FINAL_ARTIFACTS_VERIFIED"}, adapter.SOURCE_LOCAL_START_MS, adapter.SOURCE_LOCAL_END_MS)
    assert result_a == result_b
    assert calls == 0
    with pytest.raises(adapter.C6ExactFinalBoundaryAdapterError, match="CALLBACK_COORDINATE"):
        reviewer(reviewed.decode(), {"schema_version": "chat-authority-audit.v2", "status": "NO_MATCH", "final_status": "FINAL_ARTIFACTS_VERIFIED"}, adapter.SOURCE_LOCAL_START_MS + 1, adapter.SOURCE_LOCAL_END_MS)
