from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.final_media_review_bootstrap import (
    BOOTSTRAP_AUTHORITY,
    FinalMediaReviewBootstrapError,
    bootstrap_final_media_review_job,
    bootstrap_is_required,
)
from src.autoslice.final_media_review_inputs import assess_review_inputs


CID = "auto_200000_1_2"
CONTRACT_SHA = "sha256:" + "a" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "package"
    (root / "verification").mkdir(parents=True)
    files = {
        "video": root / "final.mp4",
        "subtitle": root / "final.srt",
        "cover": root / "final.cover.png",
        "record": root / "final.record.json",
    }
    files["video"].write_bytes(b"synthetic video bytes")
    files["subtitle"].write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        encoding="utf-8",
    )
    files["cover"].write_bytes(b"synthetic cover pixels")
    files["record"].write_text(
        json.dumps({"candidate_id": CID}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    closure = {
        "artifacts": {
            "video": files["video"].name,
            "subtitle": files["subtitle"].name,
            "cover": files["cover"].name,
        },
        "record_path": files["record"].name,
        "final_duration_ms": 2_000,
        "final_duration_source": "burned_preview.verification.duration_ms",
        "current_final_duration_ms": 2_000,
        "legacy_branding_intro_duration_ms": 19_546,
        "duration_witness_mismatch_ms": 17_546,
        "duration_witness_mismatch": True,
    }
    return root, closure


def _points(*, full: bool = True) -> list[dict[str, object]]:
    return [
        {
            "point_id": "whole-clip" if full else "local-window",
            "final_video_start_ms": 0 if full else 500,
            "final_video_end_ms": 2_000 if full else 1_500,
            "expectation": "inspect the exact final media without claiming human playback",
        }
    ]


def _clock(*, duration_us: int = 2_000_000) -> dict[str, object]:
    return {
        "duration_us": duration_us,
        "first_video_pts_us": 0,
        "last_video_pts_us": max(0, duration_us - 40_000),
        "video_frame_count": 50,
        "format_duration_seconds": f"{duration_us / 1_000_000:.6f}",
        "stream_start_seconds": "0.000000",
        "stream_duration_seconds": f"{duration_us / 1_000_000:.6f}",
    }


def test_bootstrap_requires_both_concrete_mismatch_and_full_media_contract(
    tmp_path: Path,
):
    _root, closure = _package(tmp_path)
    assert bootstrap_is_required(closure, _points()) is True
    assert bootstrap_is_required(closure, _points(full=False)) is False
    assert bootstrap_is_required(
        {**closure, "duration_witness_mismatch": False}, _points()
    ) is False


def test_bootstrap_creates_exact_clock_job_last_and_reuses_without_reprobe(
    tmp_path: Path,
):
    root, closure = _package(tmp_path)
    calls = {"probe": 0}

    def probe(_video: Path) -> dict[str, object]:
        calls["probe"] += 1
        return _clock()

    first = bootstrap_final_media_review_job(
        package_root=root,
        candidate_id=CID,
        closure=closure,
        review_contract_sha256=CONTRACT_SHA,
        review_points=_points(),
        probe=probe,
    )

    assert first["status"] == "BOOTSTRAPPED_FINAL_MEDIA_REVIEW_JOB"
    assert first["created"] is True
    assert calls["probe"] == 1
    job_path = Path(first["job_path"])
    receipt_path = Path(first["receipt_path"])
    assert job_path.is_file() and receipt_path.is_file()
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["review_plan"]["authority"] == BOOTSTRAP_AUTHORITY
    assert job["review_plan"]["trigger_reason_codes"] == [
        "FINAL_MEDIA_REVIEW_LEGACY_DURATION_WITNESS_MISMATCH"
    ]
    assert job["requirements"] == {
        "continuous_audio_required": True,
        "continuous_visual_required": True,
        "content_review_required": True,
        "exact_media_clock_evidence_required": True,
        "provider_accepts_bound_source_video": False,
        "provider_accepts_bound_audio": False,
    }
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["provider_calls"] == 0
    assert receipt["image_generation_calls"] == 0
    assert receipt["content_review_status"] == "UNASSESSED"
    assessment = assess_review_inputs(job, allowed_root=root)
    assert assessment["status"] == "BLOCKED_INPUT"
    assert assessment["content_review_status"] == "UNASSESSED"
    assert assessment["integrity"]["exact_media_clock_evidence"][
        "duration_us"
    ] == 2_000_000
    assert assessment["audio"]["actual_gaps"] == [
        {"start_us": 0, "end_us": 2_000_000, "duration_us": 2_000_000}
    ]

    def forbidden(_video: Path) -> dict[str, object]:
        raise AssertionError("committed bootstrap must not probe twice")

    second = bootstrap_final_media_review_job(
        package_root=root,
        candidate_id=CID,
        closure=closure,
        review_contract_sha256=CONTRACT_SHA,
        review_points=_points(),
        probe=forbidden,
    )
    assert second["status"] == "REUSED_EXISTING_JOB"
    assert second["created"] is False
    assert second["job_path"] == first["job_path"]
    assert calls["probe"] == 1


def test_bootstrap_discloses_current_probe_duration_mismatch(tmp_path: Path):
    root, closure = _package(tmp_path)
    result = bootstrap_final_media_review_job(
        package_root=root,
        candidate_id=CID,
        closure=closure,
        review_contract_sha256=CONTRACT_SHA,
        review_points=_points(),
        probe=lambda _video: _clock(duration_us=3_000_000),
    )
    job = json.loads(Path(result["job_path"]).read_text(encoding="utf-8"))
    assert job["review_plan"]["trigger_reason_codes"] == [
        "FINAL_MEDIA_REVIEW_LEGACY_DURATION_WITNESS_MISMATCH",
        "FINAL_MEDIA_REVIEW_CURRENT_DURATION_PROBE_MISMATCH",
    ]


def test_bootstrap_noop_does_not_probe_or_write(tmp_path: Path):
    root, closure = _package(tmp_path)

    def forbidden(_video: Path) -> dict[str, object]:
        raise AssertionError("non-full contract must not probe media")

    result = bootstrap_final_media_review_job(
        package_root=root,
        candidate_id=CID,
        closure=closure,
        review_contract_sha256=CONTRACT_SHA,
        review_points=_points(full=False),
        probe=forbidden,
    )
    assert result == {"status": "NOT_REQUIRED", "job_path": None, "created": False}
    assert not list((root / "verification").glob("*.final-media-review-*.json"))


def test_existing_bootstrap_sidecar_drift_is_rejected(tmp_path: Path):
    root, closure = _package(tmp_path)
    first = bootstrap_final_media_review_job(
        package_root=root,
        candidate_id=CID,
        closure=closure,
        review_contract_sha256=CONTRACT_SHA,
        review_points=_points(),
        probe=lambda _video: _clock(),
    )
    manifest_path = root / "verification" / f"{CID}.final-media-review-assets.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    with pytest.raises(
        FinalMediaReviewBootstrapError,
        match="FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
    ):
        bootstrap_final_media_review_job(
            package_root=root,
            candidate_id=CID,
            closure=closure,
            review_contract_sha256=CONTRACT_SHA,
            review_points=_points(),
            probe=lambda _video: _clock(),
        )
    assert Path(first["job_path"]).is_file()
