from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

from src.autoslice.final_media_review_inputs import (
    JOB_SCHEMA_VERSION,
    canonical_sha256,
)
from src.autoslice.final_media_review_materialization import (
    CONTRACT_REFRESH_AUTHORITY,
    CONTRACT_REFRESH_CONTRACT_ID,
    CONTRACT_REFRESH_SCHEMA_VERSION,
    FinalMediaReviewMaterializationError,
    MATERIALIZATION_SCHEMA_VERSION,
    MATERIALIZER_AUTHORITY,
    MATERIALIZER_CONTRACT_ID,
    _default_extract_full_audio,
    materialize_review_successor,
    refresh_review_job_contract,
    resolve_or_materialize_review_job,
)
from src.autoslice.final_media_review_raw_av import (
    PACKAGE_BINDING_SCHEMA_VERSION,
)
from tests.final_media_review_test_support import (
    package_model_capability_fields,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _wav(path: Path, *, duration_us: int, sample_rate: int = 16_000) -> None:
    frames = round(duration_us * sample_rate / 1_000_000)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\0\0" * frames)


def _artifact(path: Path, **extra: object) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha(path),
        **extra,
    }


def _attach_transport_binding(
    root: Path,
    job: dict[str, object],
    *,
    provider_audio: bool,
    provider_video: bool,
) -> None:
    document = {
        "schema_version": PACKAGE_BINDING_SCHEMA_VERSION,
        "capability_id": "synthetic-test-capability",
        "provider": "cpa",
        "transport": "content_bound_command",
        "model": "synthetic-raw-av-model",
        "endpoint_family": "synthetic_raw_av",
        "accepts": {
            "raw_audio": provider_audio,
            "continuous_source_video": provider_video,
        },
        "runtime_capability_sha256": "1" * 64,
        "executable_sha256": "2" * 64,
        "command_contract_sha256": "3" * 64,
        **package_model_capability_fields(),
        "result_schema_version": "final-media-perceptual-review-result.v1",
    }
    path = root / "verification" / "transport-capability.json"
    _write_json(path, document)
    job["transport_capability"] = _artifact(path)


def _package_job(
    tmp_path: Path,
    *,
    audio_duration_us: int,
    exact_clock_required: bool = False,
    provider_audio: bool = True,
    provider_video: bool = True,
) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "package"
    verification = root / "verification"
    verification.mkdir(parents=True)
    source = root / "final.mp4"
    source.write_bytes(b"synthetic exact final video bytes")
    partial = verification / "partial.wav"
    _wav(partial, duration_us=audio_duration_us)
    frame = verification / "frame.jpg"
    frame.write_bytes(b"jpeg-like diagnostic frame")
    manifest = {
        "schema": "synthetic-review-assets.v1",
        "source_video": _artifact(source),
        "artifacts": [
            _artifact(
                partial,
                kind="diagnostic_wav",
                window="partial",
                range_seconds=[0.0, audio_duration_us / 1_000_000],
            ),
            _artifact(frame, kind="frame", window="sample", timestamp_seconds=0.5),
        ],
    }
    manifest_path = verification / "review-assets.json"
    _write_json(manifest_path, manifest)
    job: dict[str, object] = {
        "schema_version": JOB_SCHEMA_VERSION,
        "candidate_id": "auto_200000_1_2",
        "asset_manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": _sha(manifest_path),
        },
        "media_clock": {
            "source_video_sha256": _sha(source),
            "source_video_bytes": source.stat().st_size,
            "duration_us": 2_000_000,
            "first_video_pts_us": 0,
            "last_video_pts_us": 1_960_000,
            "video_frame_count": 50,
        },
        "requirements": {
            "continuous_audio_required": True,
            "continuous_visual_required": True,
            "content_review_required": True,
            "exact_media_clock_evidence_required": exact_clock_required,
            "provider_accepts_bound_source_video": provider_video,
            "provider_accepts_bound_audio": provider_audio,
        },
    }
    if provider_audio or provider_video:
        _attach_transport_binding(
            root,
            job,
            provider_audio=provider_audio,
            provider_video=provider_video,
        )
    job_path = verification / "auto_200000_1_2.final-media-review-job.json"
    _write_json(job_path, job)
    return root, job_path, job


def _attach_parent_clock_evidence(
    root: Path,
    job_path: Path,
    job: dict[str, object],
) -> None:
    manifest_path = Path(job["asset_manifest"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = manifest["source_video"]
    evidence = {
        "scope": {
            "manifest_sha256": _sha(manifest_path),
            "review_status": "input_integrity_and_timing_only",
            "perceptual_content_review": "not_performed",
        },
        "source_bindings": {
            "source_video": {
                "actual_sha256": source["sha256"],
                "actual_bytes": source["bytes"],
                "sha256_match": True,
                "bytes_match": True,
                "regular_nonlinked": True,
                "ffprobe_format": {"duration": "2.000000"},
                "decoded_frame_bounds": {
                    "decoded_frame_count": 50,
                    "first_best_effort_timestamp_seconds": 0.0,
                    "last_best_effort_timestamp_seconds": 1.96,
                },
            }
        },
    }
    evidence_path = root / "verification" / "clock-evidence.json"
    _write_json(evidence_path, evidence)
    receipt = {
        "source_manifest_sha256": _sha(manifest_path),
        "verified_assets": len(manifest["artifacts"]),
        "source_video_sha256": source["sha256"],
        "duration_seconds": 2.0,
        "validation_file": str(evidence_path.resolve()),
        "validation_sha256": _sha(evidence_path),
        "disposition": "ACCEPTED_INPUT_INTEGRITY_AND_TIMING_ONLY",
        "content_review_passed": False,
        "quality_release": False,
        "upload": False,
    }
    receipt_path = root / "verification" / "integrity.json"
    _write_json(receipt_path, receipt)
    job["integrity_receipt"] = {
        "path": str(receipt_path.resolve()),
        "sha256": _sha(receipt_path),
    }
    _write_json(job_path, job)


def test_materializer_replaces_partial_audio_and_reuses_deterministic_target(
    tmp_path: Path,
):
    root, job_path, _job = _package_job(tmp_path, audio_duration_us=1_000_000)
    calls = {"extract": 0}

    def extract(_source: Path, output: Path, *, duration_us: int) -> None:
        calls["extract"] += 1
        assert duration_us == 2_000_000
        _wav(output, duration_us=duration_us)

    first = materialize_review_successor(
        job_path, allowed_root=root, extract_full_audio=extract
    )

    assert first["status"] == "MATERIALIZED_SUCCESSOR_INPUTS"
    assert first["cache_reused"] is False
    assert calls["extract"] == 1
    assessment = first["assessment"]
    assert assessment["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    assert assessment["content_review_status"] == "UNASSESSED"
    assert assessment["audio"]["diagnostic_wav_count"] == 0
    assert assessment["audio"]["exact_full_audio_wav_count"] == 1
    assert assessment["audio"]["actual_gaps"] == []
    assert assessment["visual"]["frame_count"] == 1
    assert assessment["integrity"]["independent_receipt_reused"] is False
    assert assessment["integrity"]["receipt"]["authority"] == MATERIALIZER_AUTHORITY

    receipt = json.loads(Path(first["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["provider_calls"] == 0
    assert receipt["image_generation_calls"] == 0
    assert receipt["content_review_status"] == "UNASSESSED"
    assert receipt["media_reencodes"] == 0

    def forbidden(*_args, **_kwargs):
        raise AssertionError("deterministic successor must be reused")

    second = materialize_review_successor(
        job_path, allowed_root=root, extract_full_audio=forbidden
    )
    assert second["cache_reused"] is True
    assert second["active_job_path"] == first["active_job_path"]
    assert second["receipt_path"] == first["receipt_path"]
    assert calls["extract"] == 1


def test_materializer_reuses_parent_exact_clock_by_manifest_lineage(tmp_path: Path):
    root, job_path, job = _package_job(
        tmp_path,
        audio_duration_us=1_000_000,
        exact_clock_required=True,
    )
    _attach_parent_clock_evidence(root, job_path, job)

    def extract(_source: Path, output: Path, *, duration_us: int) -> None:
        _wav(output, duration_us=duration_us)

    result = materialize_review_successor(
        job_path, allowed_root=root, extract_full_audio=extract
    )

    assessment = result["assessment"]
    clock = assessment["integrity"]["exact_media_clock_evidence"]
    assert assessment["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    assert clock["materialized_parent_reuse"] is True
    assert clock["manifest_sha256"] == assessment["asset_manifest"]["sha256"]
    assert clock["clock_evidence_manifest_sha256"] != clock["manifest_sha256"]
    successor_manifest = json.loads(
        Path(result["successor_manifest_path"]).read_text(encoding="utf-8")
    )
    assert (
        successor_manifest["parent_manifest_sha256"]
        == clock["clock_evidence_manifest_sha256"]
    )


def test_complete_audio_job_is_a_noop(tmp_path: Path):
    root, job_path, _job = _package_job(tmp_path, audio_duration_us=2_000_000)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("complete input must not be rematerialized")

    result = materialize_review_successor(
        job_path, allowed_root=root, extract_full_audio=forbidden
    )

    assert result["status"] == "NO_MATERIALIZATION_REQUIRED"
    assert result["active_job_path"] == str(job_path.resolve())
    assert result["receipt_path"] is None
    assert result["assessment"]["status"] == "READY_FOR_PERCEPTUAL_REVIEW"


def test_resolver_rejects_manifest_escape_before_writing(tmp_path: Path):
    root, job_path, job = _package_job(tmp_path, audio_duration_us=1_000_000)
    external = tmp_path / "external-manifest.json"
    external.write_bytes(Path(job["asset_manifest"]["path"]).read_bytes())
    job["asset_manifest"] = {
        "path": str(external.resolve()),
        "sha256": _sha(external),
    }
    _write_json(job_path, job)

    with pytest.raises(
        FinalMediaReviewMaterializationError,
        match="FINAL_MEDIA_REVIEW_PATH_ESCAPE",
    ):
        resolve_or_materialize_review_job(job_path, allowed_root=root)

    output_parent = root / "verification" / "final-media-review-inputs"
    assert not output_parent.exists()


def test_successor_audio_duration_mismatch_remains_blocked(tmp_path: Path):
    root, job_path, _job = _package_job(tmp_path, audio_duration_us=1_000_000)

    def short_extract(_source: Path, output: Path, *, duration_us: int) -> None:
        _wav(output, duration_us=duration_us - 250_000)

    result = materialize_review_successor(
        job_path, allowed_root=root, extract_full_audio=short_extract
    )

    assessment = result["assessment"]
    assert assessment["status"] == "BLOCKED_INPUT"
    assert assessment["content_review_status"] == "UNASSESSED"
    assert "FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH" in assessment["reason_codes"]
    assert "FINAL_MEDIA_REVIEW_AUDIO_COVERAGE_INCOMPLETE" in assessment["reason_codes"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_default_extractor_trims_aac_to_exact_nearest_sample_count(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.m4a"
    output = tmp_path / "exact.wav"
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1.050000",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    duration_us = 1_003_333

    _default_extract_full_audio(source, output, duration_us=duration_us)

    with wave.open(str(output), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        channels = handle.getnchannels()
    expected_frames = (duration_us * 16_000 + 500_000) // 1_000_000
    actual_duration_us = round(frames * 1_000_000 / rate)
    assert (rate, channels, frames) == (16_000, 1, expected_frames)
    assert abs(actual_duration_us - duration_us) <= 63


def test_materializer_contract_identity_preserves_old_job_only_cache(
    tmp_path: Path,
) -> None:
    root, job_path, job = _package_job(
        tmp_path, audio_duration_us=1_000_000
    )
    old_target = (
        root
        / "verification"
        / "final-media-review-inputs"
        / str(job["candidate_id"])
        / canonical_sha256(job)[:24]
    )
    old_target.mkdir(parents=True)
    sentinel = old_target / "legacy-v1-cache.txt"
    sentinel.write_text("preserve legacy target\n", encoding="utf-8")

    def extract(_source: Path, output: Path, *, duration_us: int) -> None:
        _wav(output, duration_us=duration_us)

    result = materialize_review_successor(
        job_path, allowed_root=root, extract_full_audio=extract
    )

    target = Path(result["receipt_path"]).parent
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    successor_manifest = json.loads(
        Path(result["successor_manifest_path"]).read_text(encoding="utf-8")
    )
    successor_job = json.loads(
        Path(result["active_job_path"]).read_text(encoding="utf-8")
    )
    assert target != old_target
    assert sentinel.read_text(encoding="utf-8") == "preserve legacy target\n"
    assert receipt["schema_version"] == MATERIALIZATION_SCHEMA_VERSION
    assert receipt["materializer_contract_id"] == MATERIALIZER_CONTRACT_ID
    assert target.name == receipt["materializer_binding_sha256"][:24]
    assert (
        successor_manifest["materialization"]["materializer_contract_id"]
        == MATERIALIZER_CONTRACT_ID
    )
    assert (
        successor_job["materialization_lineage"]["materializer_contract_id"]
        == MATERIALIZER_CONTRACT_ID
    )
    assert (
        successor_job["materialization_lineage"][
            "materializer_binding_sha256"
        ]
        == receipt["materializer_binding_sha256"]
    )


def test_contract_refresh_creates_json_only_successor_and_reuses_media(
    tmp_path: Path,
) -> None:
    root, job_path, job = _package_job(
        tmp_path,
        audio_duration_us=2_000_000,
    )
    old_points = [
        {
            "point_id": "old-clock",
            "final_video_start_ms": 0,
            "final_video_end_ms": 1_000,
            "expectation": "old content-clock interval",
        }
    ]
    job["review_plan"] = {
        "authority": "test contract",
        "review_contract_sha256": "1" * 64,
        "subtitle_review_points": old_points,
        "content_review_status": "UNASSESSED",
    }
    _write_json(job_path, job)
    source_path = root / "final.mp4"
    audio_path = root / "verification" / "partial.wav"
    source_before = source_path.read_bytes()
    audio_before = audio_path.read_bytes()
    job_before = job_path.read_bytes()
    target_points = [
        {
            "point_id": "final-clock",
            "final_video_start_ms": 250,
            "final_video_end_ms": 1_750,
            "expectation": "current final-video interval",
        }
    ]

    first = refresh_review_job_contract(
        job_path,
        allowed_root=root,
        review_contract_sha256="sha256:" + "2" * 64,
        review_points=target_points,
    )

    assert first["status"] == "REFRESHED_REVIEW_CONTRACT"
    assert first["cache_reused"] is False
    assert first["provider_calls"] == 0
    assert first["media_copies"] == 0
    assert first["media_reencodes"] == 0
    successor_path = Path(first["active_job_path"])
    receipt_path = Path(first["receipt_path"])
    assert successor_path != job_path
    assert successor_path.is_file() and receipt_path.is_file()
    assert sorted(path.name for path in successor_path.parent.iterdir()) == [
        "contract-refresh-receipt.json",
        "review-job.successor.json",
    ]
    successor = json.loads(successor_path.read_text(encoding="utf-8"))
    assert successor["asset_manifest"] == job["asset_manifest"]
    assert successor["media_clock"] == job["media_clock"]
    assert successor["review_plan"]["review_contract_sha256"] == "2" * 64
    assert successor["review_plan"]["subtitle_review_points"] == target_points
    lineage = successor["review_contract_refresh_lineage"]
    assert lineage["authority"] == CONTRACT_REFRESH_AUTHORITY
    assert lineage["contract_refresh_contract_id"] == CONTRACT_REFRESH_CONTRACT_ID
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == CONTRACT_REFRESH_SCHEMA_VERSION
    assert receipt["authority"] == CONTRACT_REFRESH_AUTHORITY
    assert receipt["provider_calls"] == 0
    assert receipt["media_copies"] == 0
    assert receipt["media_reencodes"] == 0
    assert source_path.read_bytes() == source_before
    assert audio_path.read_bytes() == audio_before
    assert job_path.read_bytes() == job_before

    second = refresh_review_job_contract(
        job_path,
        allowed_root=root,
        review_contract_sha256="2" * 64,
        review_points=target_points,
    )
    assert second["status"] == "REFRESHED_REVIEW_CONTRACT"
    assert second["cache_reused"] is True
    assert second["active_job_path"] == first["active_job_path"]
    assert second["receipt_path"] == first["receipt_path"]

    current = refresh_review_job_contract(
        successor_path,
        allowed_root=root,
        review_contract_sha256="2" * 64,
        review_points=target_points,
    )
    assert current["status"] == "REVIEW_CONTRACT_CURRENT"
    assert current["active_job_path"] == str(successor_path)
    assert current["receipt_path"] is None
