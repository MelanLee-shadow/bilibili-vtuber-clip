from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import final_human_review as review
from src.autoslice.final_media_duration_binding import (
    final_burn_duration_binding,
)
from src.autoslice.production_final_media_review_admission import (
    ADMISSION_AUTHORITY,
    ProductionFinalMediaReviewAdmissionError,
    bootstrap_production_final_media_review_job,
    inspect_production_final_media_review_admission,
    production_final_media_review_is_required,
)


CANDIDATE = "auto_200000_1_2"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _contract(
    tmp_path: Path,
    *,
    full_media: bool = True,
    candidate_id: str = CANDIDATE,
) -> Path:
    path = tmp_path / "final-media-review-contracts.json"
    points = [
        {
            "point_id": "whole-final-media" if full_media else "local-window",
            "final_video_start_ms": 0 if full_media else 500,
            "final_video_end_ms": 2_000 if full_media else 1_500,
            "expectation": "inspect exact final audio and continuous video",
        }
    ]
    _write_json(
        path,
        {
            "schema_version": review.REVIEW_CONTRACT_SCHEMA,
            "authority": "SYNTHETIC_COMMITTED_CONTRACT_TEST_ONLY",
            "contracts": [
                {
                    "candidate_id": candidate_id,
                    "subtitle_review_points": points,
                }
            ],
        },
    )
    return path


def _package(
    tmp_path: Path,
    *,
    mismatch: bool = True,
    include_binding: bool = True,
    lane: str = "talk",
) -> tuple[Path, dict[str, Path]]:
    root = (tmp_path / "package").resolve()
    root.mkdir()
    files = {
        "video": root / "final.mp4",
        "subtitle": root / "final.srt",
        "cover": root / "final.cover.png",
        "record": root / "final.record.json",
    }
    files["video"].write_bytes(b"synthetic exact final video")
    files["subtitle"].write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n",
        encoding="utf-8",
    )
    files["cover"].write_bytes(b"synthetic exact cover")
    legacy_duration = 19_546 if mismatch else 2_000
    record = {
        "candidate_id": CANDIDATE,
        "burned_preview": {
            "verification": {"duration_ms": 2_000},
            "branding_intro": {
                "verification": {"duration_ms": legacy_duration}
            },
        },
    }
    _write_json(files["record"], record)
    item: dict[str, object] = {
        "candidate_id": CANDIDATE,
        "kind": lane,
        "classification": lane,
        "video": files["video"].name,
        "subtitle_srt": files["subtitle"].name,
        "cover": files["cover"].name,
        "record": files["record"].name,
        "sha256": {
            "video": _sha(files["video"]),
            "subtitle_srt": _sha(files["subtitle"]),
            "cover": _sha(files["cover"]),
            "evidence_json": _sha(files["record"]),
        },
    }
    if include_binding:
        item["final_media_duration_binding"] = final_burn_duration_binding(
            record["burned_preview"]
        )
    _write_json(
        root / "review_manifest.json",
        {
            "schema_version": "lidousha-daily-review-manifest.v1",
            "status": "review_ready",
            "run_mode": "PRODUCTION_REVIEW",
            "upload_allowed": False,
            "candidate_id": CANDIDATE,
            "items": [item],
        },
    )
    return root, files


def _clock() -> dict[str, object]:
    return {
        "duration_us": 2_000_000,
        "first_video_pts_us": 0,
        "last_video_pts_us": 1_960_000,
        "video_frame_count": 50,
        "format_duration_seconds": "2.000000",
        "stream_start_seconds": "0.000000",
        "stream_duration_seconds": "2.000000",
    }


def test_required_daily_anomaly_bootstraps_create_only_job_and_reuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _files = _package(tmp_path)
    contract = _contract(tmp_path)
    monkeypatch.setattr(review, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract)
    calls = {"probe": 0}

    def probe(_video: Path) -> dict[str, object]:
        calls["probe"] += 1
        return _clock()

    admission = inspect_production_final_media_review_admission(
        root, CANDIDATE
    )
    assert admission["status"] == "REQUIRED"
    assert admission["authority"] == ADMISSION_AUTHORITY
    assert admission["reason_code"] == (
        "COMMITTED_DURATION_MISMATCH_FULL_MEDIA_REVIEW"
    )
    assert admission["provider_calls"] == 0
    assert production_final_media_review_is_required(root, CANDIDATE) is True
    assert not (root / "verification").exists()

    first = bootstrap_production_final_media_review_job(
        root, CANDIDATE, probe=probe
    )
    assert first["status"] == "BOOTSTRAPPED_FINAL_MEDIA_REVIEW_JOB"
    assert first["created"] is True
    assert first["provider_calls"] == 0
    assert calls["probe"] == 1
    verification = root / "verification"
    assert verification.is_dir() and not verification.is_symlink()
    assert Path(first["job_path"]).is_file()
    assert Path(first["receipt_path"]).is_file()

    second = bootstrap_production_final_media_review_job(
        root,
        CANDIDATE,
        probe=lambda _video: pytest.fail("valid restart must not reprobe"),
    )
    assert second["status"] == "REUSED_EXISTING_JOB"
    assert second["created"] is False
    assert second["provider_calls"] == 0
    assert calls["probe"] == 1


def test_absent_projected_binding_is_noop_and_does_not_read_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _files = _package(tmp_path, include_binding=False)
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        tmp_path / "must-not-be-read.json",
    )

    result = inspect_production_final_media_review_admission(root, CANDIDATE)

    assert result["status"] == "NOT_REQUIRED"
    assert result["reason_code"] == "NO_PROJECTED_DURATION_BINDING"
    assert result["provider_calls"] == 0
    assert not (root / "verification").exists()


def test_no_duration_mismatch_is_noop_and_does_not_read_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _files = _package(tmp_path, mismatch=False)
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        tmp_path / "must-not-be-read.json",
    )

    result = bootstrap_production_final_media_review_job(root, CANDIDATE)

    assert result["status"] == "NOT_REQUIRED"
    assert result["reason_code"] == "NO_DURATION_WITNESS_MISMATCH"
    assert result["created"] is False
    assert not (root / "verification").exists()


def test_non_talk_daily_package_is_not_admitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _files = _package(tmp_path, lane="song")
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        tmp_path / "must-not-be-read.json",
    )

    result = inspect_production_final_media_review_admission(root, CANDIDATE)

    assert result["status"] == "NOT_REQUIRED"
    assert result["reason_code"] == "NON_TALK_DAILY_PACKAGE"
    assert not (root / "verification").exists()


def test_local_only_committed_point_does_not_create_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _files = _package(tmp_path)
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        _contract(tmp_path, full_media=False),
    )

    result = bootstrap_production_final_media_review_job(root, CANDIDATE)

    assert result["status"] == "NOT_REQUIRED"
    assert result["reason_code"] == "NO_FULL_MEDIA_COMMITTED_REVIEW_POINT"
    assert not (root / "verification").exists()


def test_artifact_hash_drift_is_rejected_before_contract_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, files = _package(tmp_path)
    files["video"].write_bytes(b"drifted final video")
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        tmp_path / "must-not-be-read.json",
    )

    with pytest.raises(
        ProductionFinalMediaReviewAdmissionError,
        match="FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_BINDING_MISMATCH",
    ):
        inspect_production_final_media_review_admission(root, CANDIDATE)

    assert not (root / "verification").exists()


def test_projected_duration_binding_drift_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, files = _package(tmp_path)
    record = json.loads(files["record"].read_text(encoding="utf-8"))
    record["burned_preview"]["verification"]["duration_ms"] = 2_500
    _write_json(files["record"], record)
    manifest_path = root / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"][0]["sha256"]["evidence_json"] = _sha(files["record"])
    _write_json(manifest_path, manifest)
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        tmp_path / "must-not-be-read.json",
    )

    with pytest.raises(
        ProductionFinalMediaReviewAdmissionError,
        match="FINAL_MEDIA_REVIEW_PRODUCTION_DURATION_BINDING_MISMATCH",
    ):
        inspect_production_final_media_review_admission(root, CANDIDATE)

    assert not (root / "verification").exists()


def test_required_admission_rejects_verification_symlink_without_outside_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _files = _package(tmp_path)
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        _contract(tmp_path),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "verification").symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ProductionFinalMediaReviewAdmissionError,
        match="FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
    ):
        bootstrap_production_final_media_review_job(
            root, CANDIDATE, probe=lambda _video: _clock()
        )

    assert list(outside.iterdir()) == []


def test_ordinary_producer_legacy_only_witness_never_loads_contract_or_creates_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, files = _package(tmp_path)
    record = json.loads(files["record"].read_text(encoding="utf-8"))
    record["burned_preview"].pop("verification")
    _write_json(files["record"], record)
    manifest_path = root / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"][0]["sha256"]["evidence_json"] = _sha(files["record"])
    manifest["items"][0]["final_media_duration_binding"] = (
        final_burn_duration_binding(record["burned_preview"])
    )
    _write_json(manifest_path, manifest)
    monkeypatch.setattr(
        review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        tmp_path / "must-not-be-read.json",
    )

    result = bootstrap_production_final_media_review_job(root, CANDIDATE)

    assert result["status"] == "NOT_REQUIRED"
    assert result["reason_code"] == "NO_DURATION_WITNESS_MISMATCH"
    assert result["provider_calls"] == 0
    assert not (root / "verification").exists()
