import base64
import dataclasses
import hashlib
import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from scripts import run_auto_review_shadow_pipeline as shadow_pipeline
from src.autoslice.cpa_semantic_qa import (
    CpaSemanticQaRequest,
    CpaSemanticSourceRef,
    build_mock_cpa_response,
    write_cpa_semantic_request_artifact,
    write_cpa_semantic_response_artifact,
)
from src.autoslice.review_evidence import ReviewEvidence
from src.autoslice.song_repair import LrcLine, LrcResult
from tests.host_vocal_test_support import (
    bind_ready_live_performance_report,
    make_ready_audio_alignment_run,
    make_ready_host_vocal_claim,
)


def _ready_host_vocal_prover(source_media, candidate_id, boundary, alignment, output_dir):
    alignment_path = Path(str(alignment["alignment_report_path"]))
    alignment_payload = json.loads(alignment_path.read_text(encoding="utf-8"))
    alignment_payload.update(
        {
            "candidate_id": candidate_id,
            "first_lyric_start_ms": int(boundary["first_lyric_start_ms"]),
            "last_lyric_end_ms": int(boundary["last_lyric_end_ms"]),
        }
    )
    alignment_path.write_text(json.dumps(alignment_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    if "audio_alignment_provider" not in alignment_payload:
        bind_ready_live_performance_report(
            alignment_path,
            source_media=Path(source_media),
            candidate_id=candidate_id,
        )
    claim, _profile = make_ready_host_vocal_claim(
        output_dir,
        source_media=Path(source_media),
        alignment_report=alignment_path,
        candidate_id=candidate_id,
    )
    alignment["alignment_report_sha256"] = hashlib.sha256(alignment_path.read_bytes()).hexdigest()
    return claim


def _complete_evidence(candidate_id: str, **overrides) -> ReviewEvidence:
    data = {
        "candidate_id": candidate_id,
        "foreground_song_overlap_seconds": 0.0,
        "song_complete": True,
        "lyrics_alignment_ready": True,
        "start_boundary_score": 0.97,
        "end_boundary_score": 0.98,
        "standalone_score": 0.94,
        "payoff_score": 0.95,
        "open_loop_count": 0,
        "editorial_score": 88.0,
        "duplicate_similarity": 0.10,
        "subtitle_alignment_p95_ms": 120.0,
        "actual_cut_error_ms": 40.0,
        "checks": (),
        "source_cues": (),
        "metadata": {"test_fixture": True},
    }
    data.update(overrides)
    return ReviewEvidence(**data)


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _write_lyrics_alignment_proof(root: Path, *, stem: str = "travel-meaning") -> dict[str, object]:
    report_path = root / f"{stem}.alignment-report.json"
    _write(
        report_path,
        json.dumps(
            {
                "song": "旅行的意义",
                "external_lrc": "kugeci://travel-meaning",
                "aligned_line_count": 4,
                "max_offset_ms": 180,
            },
            ensure_ascii=False,
        ),
    )
    return {
        "status": "READY",
        "provider": "agy",
        "model": "Gemini 3.6 Flash (High)",
        "source": "external_lrc_plus_chunked_gemini35_spectrogram",
        "external_lrc": "kugeci://travel-meaning",
        "chunked_probe_count": 3,
        "spectrogram_verified": True,
        "alignment_report_path": str(report_path),
        "alignment_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }


def _bind_ready_alignment_claim(
    claim: dict[str, object],
    *,
    source_media: Path,
    candidate_id: str,
    first_ms: int,
    last_ms: int,
) -> None:
    report_path = Path(str(claim["alignment_report_path"]))
    span = last_ms - first_ms
    rows = []
    lyrics = []
    for index in range(8):
        start_ms = first_ms + round((span - 3_000) * index / 7)
        text = f"测试歌词{index}"
        lyrics.append({"lrc_time_ms": index * 10_000, "text": text})
        rows.append(
            {
                "lrc_time_ms": index * 10_000,
                "lrc_text": text,
                "cue_start_ms": start_ms,
                "cue_end_ms": start_ms + 3_000,
                "matched_cue_id": f"fixture-cue-{index}",
            }
        )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "candidate_id": candidate_id,
            "first_lyric_start_ms": first_ms,
            "last_lyric_end_ms": last_ms,
            "lyric_lines": lyrics,
            "alignment": rows,
        }
    )
    report_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    bind_ready_live_performance_report(
        report_path,
        source_media=source_media,
        candidate_id=candidate_id,
    )
    claim["provider"] = "agy"
    claim["model"] = "lrclib-agy-audio-lrc-global-shift-v1"
    claim["completion_basis"] = "FULL_STUDIO_SEQUENCE"
    claim["alignment_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()


def _rebind_background_playback_observation(claim: dict[str, object]) -> None:
    """Turn a bound AGY-v5 fixture into the speech-over-BGM hard negative."""

    report_path = Path(str(claim["alignment_report_path"]))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    first_ms = int(report["first_lyric_start_ms"])
    last_ms = int(report["last_lyric_end_ms"])
    span = last_ms - first_ms
    observation = {
        "mode": "STREAMER_TALKING_OVER_MUSIC",
        "confidence": 0.98,
        "continuous_live_song_performance": False,
        "background_recording_likelihood": 0.97,
        "same_lidousha_live_performer_across_all_lyrics": False,
        "other_singer_or_harmony_present": False,
        "recorded_or_playback_vocal_present": True,
        "evidence": [
            {"time_ms": first_ms + span // 6, "observation": "host speech over recorded song at head"},
            {"time_ms": first_ms + span // 2, "observation": "recorded singer continues under host speech"},
            {"time_ms": first_ms + span * 5 // 6, "observation": "background recording continues at tail"},
        ],
        "notes": "hard-negative fixture: host identity is present but is not singing",
    }
    artifacts = report["audio_alignment_artifacts"]
    raw_path = Path(str(artifacts["raw_output_path"]))
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    for raw_row in raw["observations"]:
        raw_row.update(
            lyric_vocal_subject="RECORDED_OR_PLAYBACK_SINGER",
            lidousha_role="SPEAKING_NOT_SINGING",
            same_live_vocal_source_as_lidousha=False,
            other_singer_or_harmony_audible=False,
            recorded_or_playback_vocal_audible=True,
        )
    for report_row in report["alignment"]:
        report_row.update(
            lyric_vocal_subject="RECORDED_OR_PLAYBACK_SINGER",
            lidousha_role="SPEAKING_NOT_SINGING",
            same_live_vocal_source_as_lidousha=False,
            other_singer_or_harmony_audible=False,
            recorded_or_playback_vocal_audible=True,
        )
    raw["live_performance"] = observation
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifacts["raw_output_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    manifest_path = Path(str(artifacts["run_manifest_path"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["output_sha256"] = artifacts["raw_output_sha256"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    artifacts["run_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    report["live_performance"] = observation
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    claim["alignment_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()


def _rebind_raw_report_timing_mismatch(claim: dict[str, object]) -> None:
    report_path = Path(str(claim["alignment_report_path"]))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["alignment"][3]["cue_start_ms"] += 10_000
    report["alignment"][3]["cue_end_ms"] += 10_000
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    claim["alignment_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()


def _rebind_raw_vocal_role_mismatch(claim: dict[str, object]) -> None:
    """Tamper the bound raw row while leaving the projected report READY."""

    report_path = Path(str(claim["alignment_report_path"]))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    artifacts = report["audio_alignment_artifacts"]
    raw_path = Path(str(artifacts["raw_output_path"]))
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw["observations"][3]["lidousha_role"] = "SPEAKING_NOT_SINGING"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifacts["raw_output_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    for index, row in enumerate(report["alignment"]):
        row["matched_cue_id"] = f"agy-audio:{artifacts['raw_output_sha256'][:12]}:line-{index}"
    manifest_path = Path(str(artifacts["run_manifest_path"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["output_sha256"] = artifacts["raw_output_sha256"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    artifacts["run_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    claim["alignment_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()


def _write_review_package(
    root: Path,
    *,
    stem: str,
    jingting_done: bool = True,
    jingting_manifest: dict | None = None,
    review_required: dict | None = None,
    include_cover: bool = True,
) -> Path:
    review_package = root / "review_package"
    subtitles_dir = review_package / "subtitles"
    evidence_dir = review_package / "evidence"
    _write(review_package / f"{stem}.flv", b"fake video bytes\n")
    _write(review_package / f"{stem}.mp4", b"fake mp4 bytes\n")
    if include_cover:
        _write(review_package / f"{stem}.cover.png", b"fake cover bytes\n")
    _write(subtitles_dir / f"{stem}.srt", "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
    _write(review_package / f"{stem}.jingting.srt", "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
    _write(evidence_dir / f"{stem}.evidence.json", "{}\n")
    _write(
        review_package / f"{stem}.publish.json",
        json.dumps({"title": stem, "upload_enabled": False}, ensure_ascii=False) + "\n",
    )
    if jingting_done:
        _write(review_package / f"{stem}.jingting.done", "{}\n")
    if jingting_manifest is not None:
        _write(
            review_package / f"{stem}.jingting.manifest.json",
            json.dumps(jingting_manifest, ensure_ascii=False, indent=2) + "\n",
        )
    if review_required is not None:
        _write(
            review_package / f"{stem}.jingting.review-required.json",
            json.dumps(review_required, ensure_ascii=False, indent=2) + "\n",
        )
    manifest = {
        "items": [
            {
                "stem": stem,
                "title": f"title-{stem}",
                "duration_sec": 42.0,
                "mp4": f"{stem}.mp4",
                "flv": f"{stem}.flv",
                "cover": f"{stem}.cover.png" if include_cover else "",
                "srt": f"subtitles/{stem}.srt",
                "evidence": f"evidence/{stem}.evidence.json",
                "publish_json": f"{stem}.publish.json",
                "jingting_srt": f"{stem}.jingting.srt",
                "jingting_done": f"{stem}.jingting.done" if jingting_done else "",
                "jingting_manifest": f"{stem}.jingting.manifest.json" if jingting_manifest is not None else "",
                "jingting_qc": "same_timing",
                "draft_cue_count": 1,
                "jingting_cue_count": 1,
                "jingting_review_required": f"{stem}.jingting.review-required.json" if review_required is not None else "",
            }
        ]
    }
    _write(review_package / "review_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return review_package


def _resolve(output_dir: Path, path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else output_dir / path


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _patch_complete_evidence(monkeypatch):
    monkeypatch.setattr(
        shadow_pipeline,
        "analyze_content_evidence",
        lambda *, candidate_id, cues, title: _complete_evidence(candidate_id),
    )
    monkeypatch.setattr(shadow_pipeline, "apply_style_profile", lambda evidence, profile, **kwargs: evidence)


def _write_live_source_inputs(root: Path, *, source_srt: str, refined_srt: str) -> tuple[Path, Path, Path]:
    source_video = _write(root / "source.mp4", b"fake video bytes\n")
    source_srt_path = _write(root / "source.srt", source_srt)
    refined_srt_path = _write(root / "refined.srt", refined_srt)
    return source_video, source_srt_path, refined_srt_path


def _write_passing_cpa_job_fields(
    root: Path,
    *,
    candidate_id: str,
    source_video: Path,
    source_srt: Path,
    start_ms: int,
    end_ms: int,
    text: str = "最后大家都笑了",
    response_overrides: dict | None = None,
) -> dict[str, str]:
    request_path = root / f"{candidate_id}.cpa.request.json"
    response_path = root / f"{candidate_id}.cpa.response.json"
    request = write_cpa_semantic_request_artifact(
        CpaSemanticQaRequest(
            candidate_id=candidate_id,
            room_id="22966160",
            source=CpaSemanticSourceRef(
                video_path=str(source_video),
                srt_path=str(source_srt),
                start_ms=start_ms,
                end_ms=end_ms,
            ),
            candidate_text=text,
            normalized_text=text,
            response_path=str(response_path),
        ),
        request_path,
    )
    response = build_mock_cpa_response(request)
    if response_overrides:
        response = dataclasses.replace(response, **response_overrides)
    assert response.release_ready is True or response_overrides is not None
    write_cpa_semantic_response_artifact(response, response_path)
    return {"cpa_semantic_request_path": str(request_path), "cpa_semantic_response_path": str(response_path)}


def _write_valid_source_video(path: Path, *, duration_seconds: float = 8.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=128x128:rate=30",
            "-t",
            f"{duration_seconds:.3f}",
            "-c:v",
            "libx264",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _write_valid_av_source_video(path: Path, *, duration_seconds: float = 8.0) -> Path:
    """Create a deterministic source with exactly one video and one audio stream."""

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=128x128:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            f"{duration_seconds:.3f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def test_done_only_candidate_blocks_and_never_would_upload(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    review_package = _write_review_package(tmp_path, stem="done-only", jingting_done=True, jingting_manifest=None)
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(review_package=review_package, output_dir=output_dir, no_upload=True)

    decision_path = output_dir / "done-only.auto_review.decision.json"
    shadow_path = output_dir / "done-only.auto_review.shadow.json"
    block_path = output_dir / "done-only.auto_review.block"
    done_path = output_dir / "done-only.auto_review.done"
    would_upload_path = output_dir / "done-only.auto_review.would_upload"

    assert summary["counts"]["block"] == 1
    assert decision_path.is_file()
    assert shadow_path.is_file()
    assert block_path.is_file()
    assert done_path.is_file()
    assert not would_upload_path.exists()

    decision = _load_json(decision_path)
    assert decision["decision"]["action"] == "BLOCK"
    assert "JINGTING_PROVENANCE_MISSING" in decision["decision"]["reason_codes"]
    assert decision["publish_gate_shadow"]["done_only_gate_satisfied"] is False
    assert decision["publish_gate_shadow"]["decision_manifest_gate_satisfied"] is False

    block_marker = _load_json(block_path)
    assert _resolve(output_dir, block_marker["decision_path"]) == decision_path
    assert _resolve(output_dir, block_marker["shadow_inputs_path"]) == shadow_path
    assert block_marker["no_upload"] is True


def test_review_required_candidate_blocks_with_mutually_exclusive_markers(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    review_package = _write_review_package(
        tmp_path,
        stem="review-required",
        jingting_manifest={
            "provider": "agy",
            "agy_rc": 0,
            "model": "Gemini 3.6 Flash (Low)",
            "provider_fallback_used": False,
        },
        review_required={"release_ready": False, "findings": ["missing payoff"]},
    )
    output_dir = tmp_path / "output"

    shadow_pipeline.run_shadow_pipeline(review_package=review_package, output_dir=output_dir, no_upload=True)

    action_markers = [
        output_dir / "review-required.auto_review.block",
        output_dir / "review-required.auto_review.retry",
        output_dir / "review-required.auto_review.drop",
        output_dir / "review-required.auto_review.would_upload",
    ]
    existing_action_markers = [path.name for path in action_markers if path.exists()]
    decision = _load_json(output_dir / "review-required.auto_review.decision.json")

    assert decision["decision"]["action"] == "BLOCK"
    assert "JINGTING_REVIEW_REQUIRED" in decision["decision"]["reason_codes"]
    assert existing_action_markers == ["review-required.auto_review.block"]
    assert (output_dir / "review-required.auto_review.done").is_file()


def test_auto_upload_candidate_only_writes_shadow_would_upload_marker_and_no_upload_refs(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    review_package = _write_review_package(
        tmp_path,
        stem="would-upload",
        jingting_manifest={
            "provider": "agy",
            "agy_rc": 0,
            "model": "Gemini 3.6 Flash (Low)",
            "provider_fallback_used": False,
        },
        review_required={"release_ready": True, "findings": []},
    )
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(review_package=review_package, output_dir=output_dir, no_upload=True)

    decision_path = output_dir / "would-upload.auto_review.decision.json"
    shadow_path = output_dir / "would-upload.auto_review.shadow.json"
    would_upload_path = output_dir / "would-upload.auto_review.would_upload"
    done_path = output_dir / "would-upload.auto_review.done"
    retry_path = output_dir / "would-upload.auto_review.retry"

    assert summary["validations"]["no_upload"] is True
    assert summary["validations"]["no_upload_or_free_deploy_performed"] is True
    assert decision_path.is_file()
    assert shadow_path.is_file()
    assert would_upload_path.is_file()
    assert done_path.is_file()
    assert not retry_path.exists()

    decision = _load_json(decision_path)
    assert decision["decision"]["action"] == "AUTO_UPLOAD"
    assert decision["decision"]["reason_codes"] == []
    assert decision["input_hash_status"]["cover_sha256"] == "present"
    assert decision["missing_required_artifact_hashes"] == []
    assert decision["publish_gate_shadow"]["decision_manifest_gate_satisfied"] is True
    review_required_check = next(check for check in decision["checks"] if check["code"] == "JINGTING_REVIEW_REQUIRED")
    assert review_required_check["pass"] is True
    assert decision["shadow_inputs_ref"] == "would-upload.auto_review.shadow.json"

    would_upload_marker = _load_json(would_upload_path)
    assert would_upload_marker["action"] == "AUTO_UPLOAD"
    assert _resolve(output_dir, would_upload_marker["decision_path"]) == decision_path
    assert _resolve(output_dir, would_upload_marker["shadow_inputs_path"]) == shadow_path


def test_missing_required_artifact_hash_blocks_auto_upload_and_skips_would_upload_marker(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    review_package = _write_review_package(
        tmp_path,
        stem="missing-cover",
        jingting_manifest={
            "provider": "agy",
            "agy_rc": 0,
            "model": "Gemini 3.6 Flash (Low)",
            "provider_fallback_used": False,
        },
        review_required={"release_ready": True, "findings": []},
        include_cover=False,
    )
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(review_package=review_package, output_dir=output_dir, no_upload=True)

    decision_path = output_dir / "missing-cover.auto_review.decision.json"
    block_path = output_dir / "missing-cover.auto_review.block"
    done_path = output_dir / "missing-cover.auto_review.done"
    would_upload_path = output_dir / "missing-cover.auto_review.would_upload"

    assert summary["counts"]["block"] == 1
    assert summary["counts"]["auto_upload"] == 0
    assert decision_path.is_file()
    assert block_path.is_file()
    assert done_path.is_file()
    assert not would_upload_path.exists()

    decision = _load_json(decision_path)
    assert decision["decision"]["action"] == "BLOCK"
    assert "MISSING_REQUIRED_ARTIFACT_HASH_COVER_SHA256" in decision["decision"]["reason_codes"]
    assert decision["input_hash_status"]["cover_sha256"] == "missing"
    assert decision["missing_required_artifact_hashes"] == ["cover_sha256"]
    assert decision["publish_gate_shadow"]["done_only_gate_satisfied"] is False
    assert decision["publish_gate_shadow"]["decision_manifest_gate_satisfied"] is False
    cover_hash_check = next(
        check for check in decision["checks"] if check["code"] == "PUBLISH_ARTIFACT_HASH_RECORDED_COVER_SHA256"
    )
    assert cover_hash_check["pass"] is False
    assert cover_hash_check["reason_code"] == "MISSING_REQUIRED_ARTIFACT_HASH_COVER_SHA256"


def test_missing_required_artifact_hash_overrides_retry_to_block_and_skips_retry_marker(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    review_package = _write_review_package(
        tmp_path,
        stem="pending-missing-cover",
        jingting_done=False,
        jingting_manifest={
            "provider": "agy",
            "agy_rc": 0,
            "model": "Gemini 3.6 Flash (Low)",
            "provider_fallback_used": False,
        },
        review_required={"release_ready": True, "findings": []},
        include_cover=False,
    )
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(review_package=review_package, output_dir=output_dir, no_upload=True)

    decision_path = output_dir / "pending-missing-cover.auto_review.decision.json"
    block_path = output_dir / "pending-missing-cover.auto_review.block"
    done_path = output_dir / "pending-missing-cover.auto_review.done"
    retry_path = output_dir / "pending-missing-cover.auto_review.retry"
    would_upload_path = output_dir / "pending-missing-cover.auto_review.would_upload"

    assert summary["counts"]["block"] == 1
    assert summary["counts"]["retry"] == 0
    assert decision_path.is_file()
    assert block_path.is_file()
    assert done_path.is_file()
    assert not retry_path.exists()
    assert not would_upload_path.exists()

    decision = _load_json(decision_path)
    assert decision["decision"]["action"] == "BLOCK"
    assert decision["decision"]["reason_codes"] == [
        "JINGTING_PENDING",
        "MISSING_REQUIRED_ARTIFACT_HASH_COVER_SHA256",
    ]
    assert decision["input_hash_status"]["cover_sha256"] == "missing"
    assert decision["missing_required_artifact_hashes"] == ["cover_sha256"]
    assert decision["publish_gate_shadow"]["done_only_gate_satisfied"] is False
    assert decision["publish_gate_shadow"]["decision_manifest_gate_satisfied"] is False
    failed_block_reason_codes = [
        check["reason_code"]
        for check in decision["checks"]
        if check["severity"] == "BLOCK" and check["pass"] is False and check.get("reason_code")
    ]
    assert failed_block_reason_codes == ["MISSING_REQUIRED_ARTIFACT_HASH_COVER_SHA256"]


def test_live_source_anchor_job_is_planned_and_recorded(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:00,000 --> 00:01:00,000\n开场\n\n"
            "2\n00:01:00,000 --> 00:02:00,000\n铺垫\n\n"
            "3\n00:02:00,000 --> 00:03:00,000\n重点片段\n\n"
            "4\n00:03:00,000 --> 00:05:00,000\n收尾\n"
        ),
        refined_srt="1\n00:00:00,000 --> 00:00:05,000\n重点片段\n",
    )
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={"candidate_id": "anchor-planned", "anchor_start_ms": 120_000, "anchor_end_ms": 150_000, "cpa_optional": True},
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=output_dir,
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    planned_job = record["source_context_job"]

    assert planned_job["candidate_id"] == "anchor-planned"
    assert planned_job["timeline"]["anchor_start_ms"] == 120_000
    assert planned_job["timeline"]["anchor_end_ms"] == 150_000
    assert planned_job["timeline"]["context_start_ms"] == 30_000
    assert planned_job["timeline"]["context_duration_ms"] == 270_000
    assert planned_job["provenance"]["planner_version"] == "source-context-planner.v1"


def test_planned_upstream_song_keeps_guard_and_never_materializes_as_talk(tmp_path):
    """A job that still needs context planning must retain its upstream song lane.

    The old planner replacement dropped all three song guard fields, resolved
    this exact transcript through the talk fallback as AUTO_UPLOAD, and created
    replacement recut/burn artifacts before the later review decision blocked.
    """

    transcript = (
        "1\n00:00:01,000 --> 00:00:05,000\n我跟你们说一个事\n\n"
        "2\n00:00:06,000 --> 00:00:10,000\n结果大家都笑了\n"
    )
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=transcript,
        refined_srt=transcript,
    )
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "upstream-song-needs-planning",
            "content_type_hint": "song",
            "song_candidate": True,
            "requires_full_source_song_boundary_redo": True,
            "selector_stage": "seeded_song_anchor",
            "timeline": {
                "anchor_start_ms": 1_000,
                "anchor_end_ms": 10_000,
            },
            "provenance": {"code_commit": "test-song-guard"},
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=output_dir,
        no_upload=True,
        source_context_run_ffmpeg=False,
        burn_preview=True,
        publish_staging=True,
    )

    record = summary["records"][0]
    planned_job = record["source_context_job"]
    assert planned_job["content_type_hint"] == "song"
    assert planned_job["song_candidate"] is True
    assert planned_job["requires_full_source_song_boundary_redo"] is True
    assert planned_job["selector_stage"] == "seeded_song_anchor"
    assert planned_job["timeline"]["context_start_ms"] == 0
    assert planned_job["timeline"]["context_duration_ms"] == 10_000
    assert planned_job["provenance"]["code_commit"] == "test-song-guard"
    assert record["decision_action"] == "BLOCK"
    assert "SONG_LIVE_PERFORMANCE_UNPROVEN" in record["reason_codes"]
    assert record["boundary_resolution"]["action"] == "BLOCK"
    assert record["materialized_recut"] is None
    assert not (output_dir / "replacement_recuts").exists()
    assert not list(output_dir.glob("**/*.publish.json"))
    assert not list(output_dir.glob("**/*.cover.png"))


def test_live_source_song_window_blocks_without_full_song_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_pipeline, "apply_style_profile", lambda evidence, profile, **kwargs: evidence)
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:00,000 --> 00:00:30,000\n《偶像》啦啦啦\n\n"
            "2\n00:00:30,000 --> 00:01:00,000\n《偶像》啦啦啦\n\n"
            "3\n00:01:00,000 --> 00:01:30,000\n《偶像》啦啦啦\n"
        ),
        refined_srt=(
            "1\n00:00:00,000 --> 00:00:30,000\n《偶像》啦啦啦\n\n"
            "2\n00:00:30,000 --> 00:01:00,000\n《偶像》啦啦啦\n\n"
            "3\n00:01:00,000 --> 00:01:30,000\n《偶像》啦啦啦\n"
        ),
    )
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "partial-song",
            "timeline": {
                "anchor_start_ms": 0,
                "anchor_end_ms": 90_000,
                "context_start_ms": 0,
                "context_duration_ms": 90_000,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=output_dir,
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["decision_action"] == "BLOCK"
    assert "SONG_PARTIAL" in record["reason_codes"]
    assert "LYRICS_ALIGNMENT_REQUIRED" in record["reason_codes"]
    assert evidence["metrics"]["song_complete"] is False
    assert evidence["metadata"]["song_duration_seconds"] is None


def test_live_source_song_window_auto_recuts_to_full_song_boundary_when_alignment_proof_present(tmp_path, monkeypatch):
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:25,000 --> 00:00:28,200\n你看过了许多美景\n\n"
            "2\n00:01:27,100 --> 00:01:29,900\n你累积了许多飞行\n\n"
            "3\n00:02:55,200 --> 00:03:00,500\n却说不出你欣赏我哪一种表情\n\n"
            "4\n00:03:30,300 --> 00:03:34,800\n就是旅行的意义\n\n"
            "5\n00:04:17,600 --> 00:04:21,800\n我怎么会是假唱啊？姐，这个是卡住\n"
        ),
        refined_srt=(
            "1\n00:00:25,000 --> 00:00:28,200\n你看过了许多美景\n\n"
            "2\n00:01:27,100 --> 00:01:29,900\n你累积了许多飞行\n\n"
            "3\n00:02:55,200 --> 00:03:00,500\n却说不出你欣赏我哪一种表情\n\n"
            "4\n00:03:30,300 --> 00:03:34,800\n就是旅行的意义\n\n"
            "5\n00:04:17,600 --> 00:04:21,800\n我怎么会是假唱啊？姐，这个是卡住\n"
        ),
    )
    cpa_fields = _write_passing_cpa_job_fields(
        tmp_path,
        candidate_id="travel-meaning-anchor",
        source_video=source_video,
        source_srt=source_srt,
        start_ms=0,
        end_ms=280_000,
        text="唱完旅行的意义以后伴奏卡住，最后大家都笑了",
    )
    lyrics_alignment = _write_lyrics_alignment_proof(tmp_path)
    _bind_ready_alignment_claim(
        lyrics_alignment,
        source_media=source_video,
        candidate_id="travel-meaning-anchor",
        first_ms=25_000,
        last_ms=214_800,
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "travel-meaning-anchor",
            **cpa_fields,
            "title": "【李豆沙】豆沙歌，唱完《旅行的意义》才发现伴奏像KTV录的",
            "timeline": {
                "anchor_start_ms": 174_000,
                "anchor_end_ms": 264_000,
                "context_start_ms": 0,
                "context_duration_ms": 280_000,
            },
            "song_boundary": {
                "status": "FULL_SONG_READY",
                "source": "external_lrc_plus_chunked_gemini35_spectrogram",
                "song_start_ms": 12_780,
                "first_lyric_start_ms": 25_000,
                "last_lyric_end_ms": 214_800,
                "post_song_reaction_start_ms": 251_500,
                "clip_start_ms": 0,
                "clip_end_ms": 280_000,
                "old_anchor_problem": "anchor starts mid-song and cuts off first/second verse",
            },
            "lyrics_alignment": lyrics_alignment,
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
        host_vocal_prover=_ready_host_vocal_prover,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["decision_action"] == "AUTO_RECUT"
    assert "SONG_PARTIAL" not in record["reason_codes"]
    assert "LYRICS_ALIGNMENT_REQUIRED" not in record["reason_codes"]
    assert "SONG_FULL_BOUNDARY_READY" in record["reason_codes"]
    assert record["decision_action"] == "AUTO_RECUT"
    assert record["recut_plan"]["start_ms"] == 0
    # The old 280s boundary included the verified post-song host speech used
    # as the CAM++ session anchor.  Proof input must not leak into song output.
    assert record["recut_plan"]["end_ms"] == 215_800
    assert record["materialized_recut"]["end_ms"] == 215_800
    assert "DUPLICATE_SIMILARITY_MISSING" not in record["reason_codes"]
    assert "ACTUAL_CUT_ERROR_MISSING" not in record["reason_codes"]
    assert "EDITORIAL_SCORE_LOW" not in record["reason_codes"]
    assert evidence["metrics"]["foreground_song_overlap_seconds"] == 189.8
    assert evidence["metrics"]["song_complete"] is True
    assert evidence["metrics"]["lyrics_alignment_ready"] is True
    assert evidence["metrics"]["start_boundary_score"] >= 0.98
    assert evidence["metrics"]["end_boundary_score"] >= 0.98
    assert evidence["metrics"]["duplicate_similarity"] < 0.90
    assert evidence["metrics"]["actual_cut_error_ms"] == 0.0
    assert evidence["metrics"]["editorial_score"] >= 82.0


def _run_song_ready_shadow_with_lyrics_alignment(
    tmp_path,
    lyrics_alignment: dict[str, object],
    response_overrides: dict | None = None,
    *,
    include_host_vocal: bool = True,
    bind_live_proof: bool = True,
    background_playback_with_ready_host: bool = False,
    raw_report_mismatch_with_ready_host: bool = False,
    raw_vocal_role_mismatch_with_ready_host: bool = False,
):
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:25,000 --> 00:00:28,200\n你看过了许多美景\n\n"
            "2\n00:01:27,100 --> 00:01:29,900\n你累积了许多飞行\n\n"
            "3\n00:03:30,300 --> 00:03:34,800\n就是旅行的意义\n\n"
            "4\n00:04:17,600 --> 00:04:21,800\n我怎么会是假唱啊？姐，这个是卡住\n"
        ),
        refined_srt=(
            "1\n00:00:25,000 --> 00:00:28,200\n你看过了许多美景\n\n"
            "2\n00:01:27,100 --> 00:01:29,900\n你累积了许多飞行\n\n"
            "3\n00:03:30,300 --> 00:03:34,800\n就是旅行的意义\n\n"
            "4\n00:04:17,600 --> 00:04:21,800\n我怎么会是假唱啊？姐，这个是卡住\n"
        ),
    )
    cpa_fields = _write_passing_cpa_job_fields(
        tmp_path,
        candidate_id="travel-meaning-anchor",
        source_video=source_video,
        source_srt=source_srt,
        start_ms=0,
        end_ms=280_000,
        text="唱完旅行的意义以后伴奏卡住，最后大家都笑了",
        response_overrides=response_overrides,
    )
    if bind_live_proof:
        _bind_ready_alignment_claim(
            lyrics_alignment,
            source_media=source_video,
            candidate_id="travel-meaning-anchor",
            first_ms=25_000,
            last_ms=214_800,
        )
    preexisting_host_claim: dict[str, object] = {}
    if background_playback_with_ready_host:
        # Mint identity evidence while the alignment still affirms singing.
        # Applying the hard-negative AGY result afterward proves a speaker hit
        # cannot override background/playback classification; proof generation
        # itself must never use recorded or spoken rows as singing checkpoints.
        preexisting_host_claim, _profile = make_ready_host_vocal_claim(
            tmp_path / "preexisting-host-proof",
            source_media=source_video,
            alignment_report=Path(str(lyrics_alignment["alignment_report_path"])),
            candidate_id="travel-meaning-anchor",
        )
        _rebind_background_playback_observation(lyrics_alignment)
    if raw_report_mismatch_with_ready_host:
        _rebind_raw_report_timing_mismatch(lyrics_alignment)
    if raw_vocal_role_mismatch_with_ready_host:
        _rebind_raw_vocal_role_mismatch(lyrics_alignment)
    if raw_report_mismatch_with_ready_host or raw_vocal_role_mismatch_with_ready_host:
        preexisting_host_claim, _profile = make_ready_host_vocal_claim(
            tmp_path / "preexisting-host-proof",
            source_media=source_video,
            alignment_report=Path(str(lyrics_alignment["alignment_report_path"])),
            candidate_id="travel-meaning-anchor",
        )
        lyrics_alignment["alignment_report_sha256"] = hashlib.sha256(
            Path(str(lyrics_alignment["alignment_report_path"])).read_bytes()
        ).hexdigest()
    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "travel-meaning-anchor",
            **cpa_fields,
            "title": "【李豆沙】豆沙歌，唱完《旅行的意义》才发现伴奏像KTV录的",
            "timeline": {
                "anchor_start_ms": 174_000,
                "anchor_end_ms": 264_000,
                "context_start_ms": 0,
                "context_duration_ms": 280_000,
            },
            "song_boundary": {
                "status": "FULL_SONG_READY",
                "source": "external_lrc_plus_chunked_gemini35_spectrogram",
                "song_start_ms": 12_780,
                "first_lyric_start_ms": 25_000,
                "last_lyric_end_ms": 214_800,
                "clip_start_ms": 0,
                "clip_end_ms": 280_000,
            },
            "lyrics_alignment": lyrics_alignment,
            "host_vocal_proof": preexisting_host_claim,
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
        host_vocal_prover=_ready_host_vocal_prover if include_host_vocal else None,
    )
    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))
    return record, evidence


def test_complete_lrc_without_lidousha_vocal_proof_stays_blocked(tmp_path):
    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path,
        _write_lyrics_alignment_proof(tmp_path),
        include_host_vocal=False,
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_HOST_VOCAL_UNPROVEN" in record["reason_codes"]
    assert evidence["metrics"]["song_complete"] is False
    assert evidence["metrics"]["foreground_song_overlap_seconds"] in {None, 0.0}


def test_song_agy_infrastructure_failure_continues_independent_lrc_and_vocal_proof(
    tmp_path, monkeypatch
):
    """AGY corrects talk text, but a song's final subtitles come from the
    verified LRC timeline.  Provider quota must not prevent boundary/audio/
    host-vocal proof from running, while the failure remains in audit metadata."""

    def quota_executor(
        job_manifest,
        *,
        source_video_path,
        output_dir,
        full_source_srt_path,
        **_kwargs,
    ):
        output_dir.mkdir(parents=True, exist_ok=True)
        marker = output_dir / "quota.jingting.review-required.json"
        marker.write_text(
            json.dumps(
                {
                    "schema_version": "jingting-review-required.v1",
                    "release_ready": False,
                    "findings": ["AGY_SOURCE_CONTEXT_RUNNER_FAILED", "AGY_QUOTA_EXHAUSTED"],
                    "metadata": {"retry_after_seconds": 2458},
                }
            ),
            encoding="utf-8",
        )
        return shadow_pipeline.SourceContextExecutionResult(
            decision="RETRY_INFRA",
            reason_codes=("AGY_SOURCE_CONTEXT_RUNNER_FAILED", "AGY_QUOTA_EXHAUSTED"),
            context_media_path=str(source_video_path),
            context_draft_srt_path=str(full_source_srt_path),
            context_refined_srt_path=None,
            jingting_manifest_path=None,
            review_required_path=str(marker),
            source_cues_path=None,
            jingting_done=False,
        )

    monkeypatch.setattr(shadow_pipeline, "execute_source_context_job", quota_executor)
    monkeypatch.setattr(
        shadow_pipeline,
        "_load_lyric_timeline",
        lambda *_args, **_kwargs: (
            [(25_000, "你看过了许多美景"), (87_100, "你累积了许多飞行"), (210_300, "就是旅行的意义")],
            0,
        ),
    )
    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path,
        _write_lyrics_alignment_proof(tmp_path),
    )

    assert record["decision_action"] != "RETRY"
    assert record["source_context_job"]["song_context_subtitle_fallback"]["status"] == (
        "USED_FOR_PROOF_CONTEXT_ONLY"
    )
    assert record["subtitle_source"] == "verified_external_lrc"
    assert record["materialized_recut"]["subtitle_source"] == "external_lrc_global_shift"
    assert evidence["metrics"]["song_complete"] is True
    assert "JINGTING_PENDING" not in record["reason_codes"]
    assert "JINGTING_REVIEW_REQUIRED" not in record["reason_codes"]


def test_ready_host_identity_plus_speech_over_background_music_stays_blocked(tmp_path):
    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path,
        _write_lyrics_alignment_proof(tmp_path),
        background_playback_with_ready_host=True,
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_BACKGROUND_PLAYBACK_ONLY" in record["reason_codes"]
    assert "SONG_NOT_LIDOUSHA_SINGING" in record["reason_codes"]
    assert record.get("materialized_recut") is None
    assert record.get("cover_path") is None
    assert evidence["metrics"]["song_complete"] is False
    assert evidence["metrics"]["foreground_song_overlap_seconds"] in {None, 0.0}
    assert evidence["metadata"]["source_context_job"]["host_vocal_proof"] == {}
    preexisting_proof = json.loads(
        next((tmp_path / "preexisting-host-proof").glob("*.host-vocal-proof.json")).read_text(encoding="utf-8")
    )
    assert preexisting_proof["status"] == "READY"


def test_background_mode_from_real_song_repair_cannot_fall_back_to_talk_or_materialize(tmp_path):
    """Exercise the production full-retry path, not a pre-bound report fixture.

    Before this regression, the valid negative AGY observation made song repair
    return ``repaired=False``; orchestration forgot the mode, resolved the
    seeded song as talk, and produced an AUTO_UPLOAD/MATERIALIZED recut.
    """

    candidate_id = "seededsong_50000_86000"
    lyric_texts = (
        "春风吹过山野",
        "我们看见花开",
        "音乐还在播放",
        "窗外落下星光",
        "故事慢慢展开",
        "人群经过夜晚",
        "回声留在远方",
        "最后大家都笑了",
    )
    lrc = LrcResult(
        provider="lrclib",
        song_title="背景歌",
        artist="original",
        source_ref="https://example.invalid/background.lrc",
        lines=tuple(LrcLine(index * 5_000, text) for index, text in enumerate(lyric_texts)),
    )
    def srt_time(total_seconds: int) -> str:
        return f"00:{total_seconds // 60:02d}:{total_seconds % 60:02d},000"

    srt = "\n\n".join(
        f"{index + 1}\n{srt_time(50 + index * 5)} --> {srt_time(53 + index * 5)}\n{text}"
        for index, text in enumerate(lyric_texts)
    ) + "\n"
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=srt,
        refined_srt=srt,
    )
    cpa_fields = _write_passing_cpa_job_fields(
        tmp_path,
        candidate_id=candidate_id,
        source_video=source_video,
        source_srt=source_srt,
        start_ms=50_000,
        end_ms=86_000,
        text="最后大家都笑了",
    )

    def background_audio_aligner(source_media, selected_lrc, selected_candidate_id, output_dir):
        run = make_ready_audio_alignment_run(
            source_media=Path(source_media),
            source_duration_ms=100_000,
            lrc=selected_lrc,
            candidate_id=selected_candidate_id,
            output_dir=output_dir,
            offset_ms=50_000,
        )
        payload = json.loads(json.dumps(run.payload))
        payload["live_performance"].update(
            mode="ORIGINAL_OR_BACKGROUND_PLAYBACK",
            confidence=0.99,
            continuous_live_song_performance=False,
            background_recording_likelihood=0.99,
            same_lidousha_live_performer_across_all_lyrics=False,
            other_singer_or_harmony_present=False,
            recorded_or_playback_vocal_present=True,
            notes="bound hard negative: original recording playback",
        )
        for row in payload["observations"]:
            row.update(
                lyric_vocal_subject="RECORDED_OR_PLAYBACK_SINGER",
                lidousha_role="SILENT_OR_NOT_AUDIBLE",
                same_live_vocal_source_as_lidousha=False,
                other_singer_or_harmony_audible=False,
                recorded_or_playback_vocal_audible=True,
            )
            canonicalized_path = Path(run.output_path)
            canonicalized_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            canonicalized_sha = hashlib.sha256(canonicalized_path.read_bytes()).hexdigest()
            provider_raw_path = Path(str(run.provider_raw_output_path))
            provider_raw_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            provider_raw_sha = hashlib.sha256(provider_raw_path.read_bytes()).hexdigest()
            manifest_path = Path(run.manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["provider_raw_output_sha256"] = provider_raw_sha
            manifest["artifacts"]["output_sha256"] = canonicalized_sha
            manifest["canonicalization"]["provider_raw_output_sha256"] = provider_raw_sha
            manifest["canonicalization"]["canonicalized_output_sha256"] = canonicalized_sha
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return dataclasses.replace(
                run,
                payload=payload,
                output_sha256=canonicalized_sha,
                provider_raw_output_sha256=provider_raw_sha,
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": candidate_id,
            "content_type_hint": "song",
            "song_candidate": True,
            "requires_full_source_song_boundary_redo": True,
            "timeline": {
                "source_duration_ms": 100_000,
                "anchor_start_ms": 50_000,
                "anchor_end_ms": 86_000,
                "context_start_ms": 0,
                "context_end_ms": 100_000,
                "context_duration_ms": 100_000,
            },
            **cpa_fields,
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
        lrc_provider=lambda _query: lrc,
        song_lrc_queries=("背景歌",),
        audio_lrc_aligner=background_audio_aligner,
        host_vocal_prover=_ready_host_vocal_prover,
        burn_preview=True,
        publish_staging=True,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))
    repair_gate = record["source_context_job"]["song_repair_gate"]
    assert record["decision_action"] == "BLOCK"
    assert "SONG_BACKGROUND_PLAYBACK_ONLY" in record["reason_codes"]
    assert "SONG_NOT_LIDOUSHA_SINGING" in record["reason_codes"]
    assert record["boundary_resolution"]["action"] == "BLOCK"
    assert record.get("materialized_recut") is None
    assert record.get("cover_path") is None
    assert not (tmp_path / "output" / "replacement_recuts").exists()
    assert repair_gate["status"] == "BLOCKED"
    assert repair_gate["live_performance"]["mode"] == "ORIGINAL_OR_BACKGROUND_PLAYBACK"
    assert repair_gate["reason_codes"] == [
        "SONG_BACKGROUND_PLAYBACK_ONLY",
        "SONG_NOT_LIDOUSHA_SINGING",
    ]
    assert evidence["metrics"]["song_complete"] is False
    assert evidence["metrics"]["foreground_song_overlap_seconds"] in {None, 0.0}


def test_raw_agy_timing_cannot_be_reprojected_in_report_before_materialization(tmp_path):
    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path,
        _write_lyrics_alignment_proof(tmp_path),
        raw_report_mismatch_with_ready_host=True,
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_LIVE_PERFORMANCE_UNPROVEN" in record["reason_codes"]
    assert record.get("materialized_recut") is None
    assert record.get("cover_path") is None
    assert evidence["metrics"]["song_complete"] is False
    live_check = next(check for check in evidence["checks"] if check.get("code") == "SONG_LIVE_PERFORMANCE_PROOF")
    assert "raw/report lyric row 3 mismatch" in live_check["evidence"]["error"]


def test_raw_agy_vocal_role_cannot_disagree_with_ready_report(tmp_path):
    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path,
        _write_lyrics_alignment_proof(tmp_path),
        raw_vocal_role_mismatch_with_ready_host=True,
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_LIVE_PERFORMANCE_UNPROVEN" in record["reason_codes"]
    assert record.get("materialized_recut") is None
    assert evidence["metrics"]["song_complete"] is False
    live_check = next(check for check in evidence["checks"] if check.get("code") == "SONG_LIVE_PERFORMANCE_PROOF")
    assert "raw/report lyric row 3 mismatch" in live_check["evidence"]["error"]


def test_live_source_complete_song_waives_boring_context_cpa_blocks(tmp_path):
    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path,
        _write_lyrics_alignment_proof(tmp_path),
        response_overrides={
            "release_ready": False,
            "semantic_complete": False,
            "title_hook_score": 0.18,
            "context_dependency_score": 0.86,
            "reason_codes": ("NOT_INTERESTING",),
            "required_fixes": ("完整歌不要按闲聊段子的包袱密度拦截",),
            "summary": "LLM incorrectly judged the complete song as boring/context-dependent",
        },
    )

    assert record["decision_action"] == "AUTO_RECUT"
    assert "SONG_FULL_BOUNDARY_READY" in record["reason_codes"]
    assert "NOT_INTERESTING" not in record["reason_codes"]
    assert "CONTEXT_DEPENDENCY_HIGH" not in record["reason_codes"]
    assert "CPA_SEMANTIC_INCOMPLETE" not in record["reason_codes"]
    cpa_metadata = evidence["metadata"]["cpa_semantic_qa"]
    assert cpa_metadata["complete_song_policy"]["applied"] is True
    assert set(cpa_metadata["complete_song_policy"]["waived_reason_codes"]) >= {
        "NOT_INTERESTING",
        "CONTEXT_DEPENDENCY_HIGH",
        "CPA_SEMANTIC_INCOMPLETE",
        "CPA_RELEASE_NOT_READY",
    }


def test_live_source_song_ready_without_alignment_report_fails_closed(tmp_path):
    lyrics_alignment = _write_lyrics_alignment_proof(tmp_path)
    lyrics_alignment.pop("alignment_report_path")
    lyrics_alignment.pop("alignment_report_sha256")

    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path, lyrics_alignment, bind_live_proof=False
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_PROOF_UNVERIFIED" in record["reason_codes"]
    assert "SONG_FULL_BOUNDARY_READY" not in record["reason_codes"]
    assert evidence["metadata"]["song_proof"] == {
        "verified": False,
        "error": "lyrics_alignment.alignment_report_path is missing",
    }
    assert all(check.get("code") != "SONG_FULL_BOUNDARY_READY" for check in evidence["checks"])


def test_live_source_song_ready_with_alignment_report_hash_mismatch_fails_closed(tmp_path):
    lyrics_alignment = _write_lyrics_alignment_proof(tmp_path)
    lyrics_alignment["alignment_report_sha256"] = "0" * 64

    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path, lyrics_alignment, bind_live_proof=False
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_PROOF_UNVERIFIED" in record["reason_codes"]
    assert "SONG_FULL_BOUNDARY_READY" not in record["reason_codes"]
    assert evidence["metadata"]["song_proof"]["error"] == "alignment report sha256 mismatch"
    assert all(check.get("code") != "SONG_FULL_BOUNDARY_READY" for check in evidence["checks"])


def test_live_source_song_ready_without_provider_model_source_fails_closed(tmp_path):
    lyrics_alignment = _write_lyrics_alignment_proof(tmp_path)
    lyrics_alignment.pop("provider")
    lyrics_alignment.pop("model")

    record, evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path, lyrics_alignment, bind_live_proof=False
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_PROOF_UNVERIFIED" in record["reason_codes"]
    assert "SONG_FULL_BOUNDARY_READY" not in record["reason_codes"]
    proof = evidence["metadata"].get("song_proof")
    assert proof is not None and "provider" in str(proof.get("error"))


def test_live_source_song_ready_with_missing_report_file_fails_closed(tmp_path):
    lyrics_alignment = _write_lyrics_alignment_proof(tmp_path)
    Path(str(lyrics_alignment["alignment_report_path"])).unlink()

    record, _evidence = _run_song_ready_shadow_with_lyrics_alignment(
        tmp_path, lyrics_alignment, bind_live_proof=False
    )

    assert record["decision_action"] == "BLOCK"
    assert "SONG_PROOF_UNVERIFIED" in record["reason_codes"]
    assert "SONG_FULL_BOUNDARY_READY" not in record["reason_codes"]


def test_lyrics_alignment_proof_accepts_existing_cwd_relative_report_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report_path = Path("song_repair/live.lyrics-alignment-report.json")
    report_path.parent.mkdir(parents=True)
    report_path.write_text('{"status":"ok"}\n', encoding="utf-8")

    assert (
        shadow_pipeline._verify_lyrics_alignment_proof(
            {
                "status": "READY",
                "provider": "netease",
                "model": "netease-lrc-fuzzy-align-v1",
                "source": "song_repair.netease",
                "alignment_report_path": str(report_path),
                "alignment_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            },
            output_dir=tmp_path / "candidate-dir",
        )
        is None
    )


def test_live_source_external_review_required_marker_blocks(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt="1\n00:00:00,000 --> 00:00:05,000\n我跟你们说一个事，最后大家都笑了\n",
        refined_srt="1\n00:00:00,000 --> 00:00:05,000\n我跟你们说一个事，最后大家都笑了\n",
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    marker_path = output_dir / "marker.jingting.review-required.json"
    marker_path.write_text(
        json.dumps(
            {
                "schema_version": "jingting-review-required.v1",
                "release_ready": False,
                "findings": ["LEXICON_LEAK"],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    def fake_executor(job_manifest, *, source_video_path, output_dir, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        refined_path = output_dir / "job.context.refined.srt"
        refined_path.write_text(
            "1\n00:00:00,000 --> 00:00:05,000\n我跟你们说一个事，最后大家都笑了\n", encoding="utf-8"
        )
        return shadow_pipeline.SourceContextExecutionResult(
            decision="READY",
            reason_codes=(),
            context_media_path=str(output_dir / "job.context.mp4"),
            context_draft_srt_path=str(refined_path),
            context_refined_srt_path=str(refined_path),
            jingting_manifest_path=None,
            review_required_path=str(marker_path),
            source_cues_path=None,
            jingting_done=True,
        )

    monkeypatch.setattr(shadow_pipeline, "execute_source_context_job", fake_executor)

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "external-review-required",
            "cpa_optional": True,
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 5_000, "context_start_ms": 0, "context_duration_ms": 5_000},
        },
        output_dir=output_dir,
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    assert record["decision_action"] == "BLOCK"
    assert "JINGTING_REVIEW_REQUIRED" in record["reason_codes"]


def test_live_source_runs_agy_runner_when_refined_srt_is_absent(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    source_video, source_srt, _refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt="1\n00:00:00,000 --> 00:00:02,000\n我跟你们说一个事\n",
        refined_srt="1\n00:00:00,000 --> 00:00:02,000\nunused\n",
    )
    calls: list[tuple[Path, Path, Path]] = []

    def fake_agy_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> shadow_pipeline.AgyExecutionResult:
        calls.append((media_path, draft_srt_path, output_srt_path))
        output_srt_path.write_text("1\n00:00:00,000 --> 00:00:02,000\n我跟你们说一个事\n", encoding="utf-8")
        return shadow_pipeline.AgyExecutionResult(provider="agy", model="Gemini", agy_rc=0, provider_fallback_used=False)

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=None,
        source_context_job={
            "candidate_id": "dialogue-runner",
            "cpa_optional": True,
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 2_000, "context_start_ms": 0, "context_duration_ms": 2_000},
        },
        source_context_agy_runner=fake_agy_runner,
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    assert len(calls) == 1
    record = summary["records"][0]
    assert record["source_context"]["decision"] == "READY"
    assert record["source_context"]["jingting_done"] is True
    assert record["subtitle_source"] == "source_context_refined_srt"
    assert record["source_context"]["review_required_path"] is None
    assert "REFINED_SRT_MISSING" not in record["reason_codes"]
    assert "JINGTING_REVIEW_REQUIRED" not in record["reason_codes"]


def test_live_song_without_pre_refined_srt_does_not_call_talk_agy(tmp_path):
    source_video, source_srt, _refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:00,000 --> 00:00:03,000\n唱歌上下文\n\n"
            "2\n00:00:04,000 --> 00:00:08,000\n还在唱歌\n"
        ),
        refined_srt="unused\n",
    )
    calls = []

    def forbidden_agy_runner(*args):
        calls.append(args)
        raise AssertionError("song proof context must bypass talk AGY")

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=None,
        source_context_job={
            "candidate_id": "song-no-talk-agy",
            "song_candidate": True,
            "content_type_hint": "song",
            "cpa_optional": True,
            "timeline": {
                "source_duration_ms": 8_000,
                "anchor_start_ms": 0,
                "anchor_end_ms": 8_000,
                "context_start_ms": 0,
                "context_duration_ms": 8_000,
            },
        },
        source_context_agy_runner=forbidden_agy_runner,
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    assert calls == []
    record = summary["records"][0]
    assert record["source_context"]["decision"] == "READY"
    assert record["source_context_job"]["song_context_subtitle_fallback"]["status"] == (
        "BYPASSED_NOT_AUTHORITATIVE_FOR_SONG_LRC"
    )


def test_default_source_context_runner_does_not_fail_over_from_agy(tmp_path, monkeypatch):
    from scripts import gemini_slice_jingting as jingting
    from src.autoslice.source_context_executor import AgyRunnerError

    media = tmp_path / "media.mp4"
    media.write_bytes(b"media")
    draft = tmp_path / "draft.srt"
    draft.write_text("1\n00:00:00,000 --> 00:00:01,000\n旧字\n", encoding="utf-8")
    output = tmp_path / "output.srt"
    monkeypatch.setattr(
        jingting,
        "run_agy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgyRunnerError("AGY_QUOTA_EXHAUSTED", "quota", retry_after_seconds=120)
        ),
    )

    def fake_gemini(_media, _draft, out):
        Path(out).write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        return "gemini-job"

    monkeypatch.setattr(jingting, "run_gemini_api", fake_gemini)
    with pytest.raises(AgyRunnerError, match="AGY source-context refinement failed"):
        shadow_pipeline._run_source_context_agy(media, draft, output)

    assert not output.exists()


@pytest.mark.parametrize(
    ("print_timeout", "grace_seconds", "expected"),
    [
        (None, None, {"print_timeout": "10m", "process_timeout_seconds": 660}),
        ("4m", "30", {"print_timeout": "4m", "process_timeout_seconds": 270}),
    ],
)
def test_default_source_context_runner_bounds_hung_agy_without_audio_fallback(
    tmp_path, monkeypatch, print_timeout, grace_seconds, expected
):
    from scripts import gemini_slice_jingting as jingting
    from src.autoslice.source_context_executor import AgyRunnerError

    media = tmp_path / "media.mp4"
    media.write_bytes(b"media")
    draft = tmp_path / "draft.srt"
    draft.write_text("1\n00:00:00,000 --> 00:00:01,000\n旧字\n", encoding="utf-8")
    output = tmp_path / "output.srt"
    if print_timeout is None:
        monkeypatch.delenv("SOURCE_CONTEXT_AGY_PRINT_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("SOURCE_CONTEXT_AGY_PRINT_TIMEOUT", print_timeout)
    if grace_seconds is None:
        monkeypatch.delenv("SOURCE_CONTEXT_AGY_TIMEOUT_GRACE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("SOURCE_CONTEXT_AGY_TIMEOUT_GRACE_SECONDS", grace_seconds)
    captured = {}

    def fake_agy(*_args, **kwargs):
        captured.update(kwargs)
        raise AgyRunnerError("AGY_TIMEOUT", "hung")

    gemini_calls = []

    def fake_gemini(_media, _draft, out):
        gemini_calls.append((_media, _draft, out))
        Path(out).write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        return "gemini-job"

    monkeypatch.setattr(jingting, "run_agy", fake_agy)
    monkeypatch.setattr(jingting, "run_gemini_api", fake_gemini)
    with pytest.raises(AgyRunnerError, match="AGY source-context refinement failed"):
        shadow_pipeline._run_source_context_agy(media, draft, output)

    assert captured == expected
    assert gemini_calls == []
    assert not output.exists()


def test_live_source_backfills_duplicate_and_subtitle_alignment_machine_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shadow_pipeline,
        "analyze_content_evidence",
        lambda *, candidate_id, cues, title: _complete_evidence(
            candidate_id,
            duplicate_similarity=None,
            subtitle_alignment_p95_ms=None,
            actual_cut_error_ms=40.0,
        ),
    )
    monkeypatch.setattr(shadow_pipeline, "apply_style_profile", lambda evidence, profile, **kwargs: evidence)
    source_video, source_srt, _refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n我跟你们说一个事\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n最后大家都笑了\n"
        ),
        refined_srt="unused\n",
    )

    def fake_agy_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> shadow_pipeline.AgyExecutionResult:
        output_srt_path.write_text(draft_srt_path.read_text(encoding="utf-8"), encoding="utf-8")
        return shadow_pipeline.AgyExecutionResult(provider="agy", model="Gemini", agy_rc=0, provider_fallback_used=False)

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=None,
        source_context_job={
            "candidate_id": "machine-evidence",
            "cpa_optional": True,
            "title": "机器证据回灌测试",
            "duplicate_corpus": ["完全不同的旧标题"],
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 4_000, "context_start_ms": 0, "context_duration_ms": 4_000},
        },
        source_context_agy_runner=fake_agy_runner,
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert "DUPLICATE_SIMILARITY_MISSING" not in record["reason_codes"]
    assert "SUBTITLE_ALIGNMENT_MISSING" not in record["reason_codes"]
    assert evidence["metrics"]["duplicate_similarity"] < 0.90
    assert evidence["metrics"]["subtitle_alignment_p95_ms"] == 0.0
    assert any(check["code"] == "DUPLICATE_SIMILARITY" for check in evidence["checks"])
    assert any(check["code"] == "SUBTITLE_ALIGNMENT_P95" for check in evidence["checks"])

def test_live_source_boundary_drop_overrides_missing_render_qa_block(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shadow_pipeline,
        "analyze_content_evidence",
        lambda *, candidate_id, cues, title: _complete_evidence(
            candidate_id,
            duplicate_similarity=None,
            subtitle_alignment_p95_ms=None,
            actual_cut_error_ms=None,
        ),
    )
    monkeypatch.setattr(shadow_pipeline, "apply_style_profile", lambda evidence, profile, **kwargs: evidence)
    source_video, source_srt, _refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n前面已经讲完了哈哈\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n然后这个问题怎么办\n"
        ),
        refined_srt="unused\n",
    )

    def fake_agy_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> shadow_pipeline.AgyExecutionResult:
        output_srt_path.write_text(draft_srt_path.read_text(encoding="utf-8"), encoding="utf-8")
        return shadow_pipeline.AgyExecutionResult(provider="agy", model="Gemini", agy_rc=0, provider_fallback_used=False)

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=None,
        source_context_job={
            "candidate_id": "boundary-drop",
            "cpa_optional": True,
            "title": "边界丢弃测试",
            "duplicate_corpus": [],
            "timeline": {"anchor_start_ms": 3_000, "anchor_end_ms": 4_000, "context_start_ms": 0, "context_duration_ms": 4_000},
        },
        source_context_agy_runner=fake_agy_runner,
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]

    assert record["boundary_resolution"]["action"] == "DROP"
    assert record["decision_action"] == "DROP"
    assert "ACTUAL_CUT_ERROR_MISSING" in record["reason_codes"]
    assert summary["counts"]["drop"] == 1
    assert summary["counts"]["block"] == 0


def test_live_source_dialogue_boundary_auto_recuts_and_records_source_context_metadata(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n我跟你们说一个事\n\n"
            "2\n00:00:22,000 --> 00:00:30,000\n然后她看着我说这事你别外传\n\n"
            "3\n00:01:00,000 --> 00:01:05,000\n最后结果就是大家都笑了\n"
        ),
        refined_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n我跟你们说一个事\n\n"
            "2\n00:00:22,000 --> 00:00:30,000\n然后她看着我说这事你别外传\n\n"
            "3\n00:01:00,000 --> 00:01:05,000\n最后结果就是大家都笑了\n"
        ),
    )
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "dialogue-needs-tail",
            "cpa_optional": True,
            "timeline": {
                "anchor_start_ms": 22_000,
                "anchor_end_ms": 30_000,
                "context_start_ms": 0,
                "context_duration_ms": 65_000,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=output_dir,
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))
    boundary = record["boundary_resolution"]

    assert record["decision_action"] == "AUTO_RECUT"
    assert "END_BOUNDARY_LOW" in record["reason_codes"]
    assert "OPEN_LOOPS_PRESENT" in record["reason_codes"]
    assert boundary["action"] == "AUTO_RECUT"
    assert boundary["next_end_ms"] == 65_000
    assert record["recut_plan"]["status"] == "PLANNED"
    assert record["recut_plan"]["start_ms"] == 0
    assert record["recut_plan"]["end_ms"] == 65_000
    assert record["recut_plan"]["command"][0] == "ffmpeg"
    assert "-ss" in record["recut_plan"]["command"]
    assert "65.000" in record["recut_plan"]["command"]
    materialized = record["materialized_recut"]
    assert materialized["status"] == "MATERIALIZED"
    assert materialized["dry_run_placeholder"] is True
    assert materialized["start_ms"] == 0
    assert materialized["end_ms"] == 65_000
    assert Path(materialized["media_path"]).is_file()
    assert Path(materialized["subtitle_path"]).is_file()
    assert Path(materialized["manifest_path"]).is_file()
    assert materialized["artifact_hashes"]["video_sha256"]
    assert materialized["artifact_hashes"]["subtitle_sha256"]
    assert "最后结果就是大家都笑了" in Path(materialized["subtitle_path"]).read_text(encoding="utf-8")
    materialized_manifest = _load_json(Path(materialized["manifest_path"]))
    assert materialized_manifest["schema_version"] == "materialized-recut.v1"
    assert materialized_manifest["requested_range"] == {"start_ms": 0, "end_ms": 65_000, "duration_ms": 65_000}
    assert evidence["metadata"]["boundary_resolution"]["action"] == "AUTO_RECUT"
    assert evidence["metadata"]["source_context_job"]["timeline"]["anchor_end_ms"] == 30_000


def test_live_source_boundary_uses_selector_closure_markers_for_ok_ending(tmp_path, monkeypatch):
    job = {
        "candidate_id": "ok-ending",
        "timeline": {"anchor_start_ms": 1_000, "anchor_end_ms": 83_000},
    }
    cues = [
        shadow_pipeline.SourceCue("cue-1", 1_000, 6_000, "啊 天不熊怎么这么欢迎"),
        shadow_pipeline.SourceCue("cue-2", 6_000, 11_000, "天不熊怎么总是欺负你都是啊"),
        shadow_pipeline.SourceCue("cue-3", 15_000, 18_000, "诶 骗你的"),
        shadow_pipeline.SourceCue("cue-4", 19_000, 24_000, "骗你的 其实根本就没有哭"),
        shadow_pipeline.SourceCue("cue-5", 24_000, 28_000, "而且只是想要叫天不熊小猪"),
        shadow_pipeline.SourceCue("cue-6", 58_000, 64_000, "好了,这就是本次更新的展示"),
        shadow_pipeline.SourceCue("cue-7", 64_000, 67_000, "最后送你"),
        shadow_pipeline.SourceCue("cue-8", 67_000, 72_000, "送给你一朵花"),
        shadow_pipeline.SourceCue("cue-9", 72_000, 75_000, "希望你可以收下"),
        shadow_pipeline.SourceCue("cue-10", 75_000, 83_000, "OK,于是为母展示环节结束"),
    ]

    boundary = shadow_pipeline._resolve_live_source_boundary(job, cues, output_dir=tmp_path)

    assert boundary is not None
    assert boundary.action == shadow_pipeline.DecisionAction.AUTO_UPLOAD
    assert boundary.reason_codes == ()


def test_live_source_requires_cpa_semantic_response_by_default(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    monkeypatch.setattr(
        shadow_pipeline,
        "_resolve_live_source_boundary",
        lambda job_manifest, cues, **_kwargs: shadow_pipeline.BoundaryResolution(
            candidate_id="missing-cpa",
            action=shadow_pipeline.DecisionAction.AUTO_UPLOAD,
            resolved_start_ms=0,
            resolved_end_ms=5_000,
            start_boundary_score=0.97,
            end_boundary_score=0.98,
            reason_codes=(),
        ),
    )
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt="1\n00:00:00,000 --> 00:00:05,000\n我跟你们说一个事，最后大家都笑了\n",
        refined_srt="1\n00:00:00,000 --> 00:00:05,000\n我跟你们说一个事，最后大家都笑了\n",
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "missing-cpa",
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 5_000, "context_start_ms": 0, "context_duration_ms": 5_000},
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["decision_action"] == "BLOCK"
    assert "CPA_SEMANTIC_QA_REQUIRED" in record["reason_codes"]
    assert evidence["checks"][-1]["code"] == "CPA_SEMANTIC_QA"
    assert evidence["checks"][-1]["severity"] == "BLOCK"
    assert evidence["metadata"]["cpa_semantic_qa"]["error"] == "cpa_semantic_response_path is required unless cpa_optional=true"


def test_live_source_applies_cpa_semantic_review_and_blocks_terminology_failure(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    monkeypatch.setattr(
        shadow_pipeline,
        "_stage_publish_draft",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked semantic review must not enter publish staging")
        ),
    )
    monkeypatch.setattr(
        shadow_pipeline,
        "_resolve_live_source_boundary",
        lambda job_manifest, cues, **_kwargs: shadow_pipeline.BoundaryResolution(
            candidate_id="cpa-terms",
            action=shadow_pipeline.DecisionAction.AUTO_UPLOAD,
            resolved_start_ms=0,
            resolved_end_ms=5_000,
            start_boundary_score=0.97,
            end_boundary_score=0.98,
            reason_codes=(),
        ),
    )
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt="1\n00:00:00,000 --> 00:00:05,000\n天不熊被骗到了\n",
        refined_srt="1\n00:00:00,000 --> 00:00:05,000\n天不熊被骗到了\n",
    )
    request_path = tmp_path / "cpa-terms.cpa.request.json"
    response_path = tmp_path / "cpa-terms.cpa.response.json"
    request = write_cpa_semantic_request_artifact(
        CpaSemanticQaRequest(
            candidate_id="cpa-terms",
            room_id="22966160",
            source=CpaSemanticSourceRef(
                video_path=str(source_video),
                srt_path=str(source_srt),
                start_ms=0,
                end_ms=5_000,
            ),
            candidate_text="天不熊被骗到了",
            normalized_text="天不熊被骗到了",
            response_path=str(response_path),
        ),
        request_path,
    )
    failing_response = dataclasses.replace(
        build_mock_cpa_response(request),
        release_ready=False,
        terminology_ok=False,
        reason_codes=("TERMINOLOGY_QA_FAILED",),
        required_fixes=("字幕必须使用 kmx，不能使用 kimo熊 或天不熊",),
    )
    write_cpa_semantic_response_artifact(failing_response, response_path)

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "cpa-terms",
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 5_000, "context_start_ms": 0, "context_duration_ms": 5_000},
            "cpa_semantic_request_path": str(request_path),
            "cpa_semantic_response_path": str(response_path),
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
        publish_staging=True,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["decision_action"] == "BLOCK"
    assert "TERMINOLOGY_QA_FAILED" in record["reason_codes"]
    assert record["materialized_recut"]["publish_staging"]["status"] == "SKIPPED_RELEASE_GATE"
    assert record["materialized_recut"]["cover_release_gate"]["satisfied"] is False
    assert evidence["checks"][-1]["code"] == "CPA_SEMANTIC_QA"
    assert evidence["checks"][-1]["pass"] is False
    assert evidence["metadata"]["cpa_semantic_qa"]["response_path"] == str(response_path)


def test_live_source_response_without_request_artifact_fails_closed(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt="1\n00:00:00,000 --> 00:00:05,000\n我跟你们说一个事，最后大家都笑了\n",
        refined_srt="1\n00:00:00,000 --> 00:00:05,000\n我跟你们说一个事，最后大家都笑了\n",
    )
    response_path = tmp_path / "orphan.cpa.response.json"
    response_path.write_text(
        json.dumps({"schema_version": "cpa-semantic-review-response.v1", "candidate_id": "orphan", "release_ready": True}),
        encoding="utf-8",
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "orphan",
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 5_000, "context_start_ms": 0, "context_duration_ms": 5_000},
            "cpa_semantic_response_path": str(response_path),
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    assert record["decision_action"] == "BLOCK"
    assert "CPA_SEMANTIC_QA_REQUEST_REQUIRED" in record["reason_codes"]


def test_live_source_closed_boundary_clears_content_open_loop_false_positive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shadow_pipeline,
        "analyze_content_evidence",
        lambda *, candidate_id, cues, title: _complete_evidence(candidate_id, open_loop_count=1),
    )
    monkeypatch.setattr(shadow_pipeline, "apply_style_profile", lambda evidence, profile, **kwargs: evidence)
    monkeypatch.setattr(
        shadow_pipeline,
        "_resolve_live_source_boundary",
        lambda job_manifest, cues, **_kwargs: shadow_pipeline.BoundaryResolution(
            candidate_id="closed-boundary",
            action=shadow_pipeline.DecisionAction.AUTO_UPLOAD,
            resolved_start_ms=0,
            resolved_end_ms=7_500,
            start_boundary_score=0.97,
            end_boundary_score=0.98,
            reason_codes=(),
        ),
    )
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n有没有人写\n\n"
            "2\n00:00:06,000 --> 00:00:07,500\n最后大家都笑了\n"
        ),
        refined_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n有没有人写\n\n"
            "2\n00:00:06,000 --> 00:00:07,500\n最后大家都笑了\n"
        ),
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "closed-boundary",
            "cpa_optional": True,
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 7_500, "context_start_ms": 0, "context_duration_ms": 7_500},
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert "OPEN_LOOPS_PRESENT" not in record["reason_codes"]
    assert evidence["metrics"]["open_loop_count"] == 0


def test_live_source_auto_upload_candidate_materializes_preview_render_qa(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shadow_pipeline,
        "analyze_content_evidence",
        lambda *, candidate_id, cues, title: _complete_evidence(candidate_id, actual_cut_error_ms=None),
    )
    monkeypatch.setattr(shadow_pipeline, "apply_style_profile", lambda evidence, profile, **kwargs: evidence)
    monkeypatch.setattr(
        shadow_pipeline,
        "_resolve_live_source_boundary",
        lambda job_manifest, cues, **_kwargs: shadow_pipeline.BoundaryResolution(
            candidate_id="preview-upload",
            action=shadow_pipeline.DecisionAction.AUTO_UPLOAD,
            resolved_start_ms=0,
            resolved_end_ms=7_500,
            start_boundary_score=0.97,
            end_boundary_score=0.98,
        ),
    )
    source_video = _write_valid_source_video(tmp_path / "source.mp4", duration_seconds=8.0)
    source_srt = _write(
        tmp_path / "source.srt",
        "1\n00:00:00,000 --> 00:00:02,000\n我跟你们说一个事\n\n2\n00:00:06,000 --> 00:00:07,500\n最后大家都笑了\n",
    )
    refined_srt = _write(
        tmp_path / "refined.srt",
        "1\n00:00:00,000 --> 00:00:02,000\n我跟你们说一个事\n\n2\n00:00:06,000 --> 00:00:07,500\n最后大家都笑了\n",
    )
    staged_calls: list[str] = []

    def fake_stage(record, *, candidate_id, **_kwargs):
        gate_path = tmp_path / "output" / f"{candidate_id}.cover-release-gate.json"
        assert gate_path.is_file()
        assert _load_json(gate_path)["satisfied"] is True
        staged_calls.append(candidate_id)
        return {**record, "publish_staging": {"status": "STAGED", "upload_enabled": False}}

    monkeypatch.setattr(shadow_pipeline, "_stage_publish_draft", fake_stage)

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "preview-upload",
            "cpa_optional": True,
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 7_500, "context_start_ms": 0, "context_duration_ms": 7_500},
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=True,
        publish_staging=True,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["materialized_recut"]["status"] == "MATERIALIZED"
    assert record["materialized_recut"]["dry_run_placeholder"] is False
    assert record["materialized_recut"]["publish_staging"]["status"] == "STAGED"
    assert record["materialized_recut"]["cover_release_gate"]["satisfied"] is True
    assert staged_calls == ["preview-upload"]
    assert "ACTUAL_CUT_ERROR_MISSING" not in record["reason_codes"]
    assert evidence["metrics"]["actual_cut_error_ms"] is not None


def test_auto_upload_preview_rerenders_accurately_when_stream_copy_cut_error_is_high(tmp_path, monkeypatch):
    candidate_id = "accurate-preview"
    source_video = _write(tmp_path / "source.mp4", b"source video bytes\n")
    output_dir = tmp_path / "output"
    cues = [
        shadow_pipeline.SourceCue(
            cue_id="cue-1",
            source_start_ms=1_000,
            source_end_ms=2_500,
            text="我跟你们说一个事",
        )
    ]
    boundary = shadow_pipeline.BoundaryResolution(
        candidate_id=candidate_id,
        action=shadow_pipeline.DecisionAction.AUTO_UPLOAD,
        resolved_start_ms=1_000,
        resolved_end_ms=2_500,
        start_boundary_score=0.97,
        end_boundary_score=0.98,
    )
    qa_results = [
        {
            "code": "ACTUAL_CUT_ERROR_HIGH",
            "pass": False,
            "severity": "AUTO_RECUT",
            "evidence": {"actual_cut_error_ms": 1_667},
        },
        {
            "code": "ACTUAL_CUT_ERROR_OK",
            "pass": True,
            "severity": "PASS",
            "evidence": {"actual_cut_error_ms": 0},
        },
    ]

    def fake_render_qa(**kwargs):
        return qa_results.pop(0)

    def fake_run(command, check=False, capture_output=True, text=True):
        Path(command[-1]).write_bytes(b"rendered bytes\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(shadow_pipeline, "_evaluate_materialized_recut_render_qa", fake_render_qa)
    monkeypatch.setattr(shadow_pipeline.subprocess, "run", fake_run)

    materialized = shadow_pipeline._materialize_recut_record(
        source_video=source_video,
        candidate_id=candidate_id,
        boundary_resolution=boundary,
        output_dir=output_dir,
        cues=cues,
        run_ffmpeg=True,
    )

    manifest = _load_json(Path(materialized["manifest_path"]))

    assert materialized["accurate_rerender_used"] is True
    assert materialized["render_qa"]["code"] == "ACTUAL_CUT_ERROR_OK"
    assert materialized["render_qa"]["evidence"]["actual_cut_error_ms"] == 0
    assert manifest["accurate_rerender_used"] is True
    assert "accurate_command" in manifest
    assert not qa_results


def test_live_source_materialized_recut_backfills_actual_cut_error_from_render_qa(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shadow_pipeline,
        "analyze_content_evidence",
        lambda *, candidate_id, cues, title: _complete_evidence(candidate_id, actual_cut_error_ms=None),
    )
    monkeypatch.setattr(shadow_pipeline, "apply_style_profile", lambda evidence, profile, **kwargs: evidence)
    source_video = _write_valid_source_video(tmp_path / "source.mp4", duration_seconds=8.0)
    source_srt = _write(
        tmp_path / "source.srt",
        (
            "1\n00:00:01,000 --> 00:00:01,200\n我跟你们说一个事\n\n"
            "2\n00:00:03,200 --> 00:00:04,000\n然后她突然说\n\n"
            "3\n00:00:07,000 --> 00:00:07,500\n所以最后这事就结束了\n"
        ),
    )
    refined_srt = _write(
        tmp_path / "refined.srt",
        (
            "1\n00:00:01,000 --> 00:00:01,200\n我跟你们说一个事\n\n"
            "2\n00:00:03,200 --> 00:00:04,000\n然后她突然说\n\n"
            "3\n00:00:07,000 --> 00:00:07,500\n所以最后这事就结束了\n"
        ),
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "render-qa-recut",
            "cpa_optional": True,
            "timeline": {
                "anchor_start_ms": 3_200,
                "anchor_end_ms": 4_000,
                "context_start_ms": 0,
                "context_duration_ms": 7_500,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=True,
    )

    record = summary["records"][0]
    materialized = record["materialized_recut"]
    evidence = _load_json(Path(record["evidence_path"]))
    materialized_manifest = _load_json(Path(materialized["manifest_path"]))
    render_qa_manifest = _load_json(Path(materialized["render_qa_path"]))

    assert record["decision_action"] == "AUTO_RECUT"
    assert "ACTUAL_CUT_ERROR_MISSING" not in record["reason_codes"]
    assert materialized["status"] == "MATERIALIZED"
    assert materialized["dry_run_placeholder"] is False
    # Repair-first: the copy-cut landed seconds off, so the pipeline must
    # re-render precisely instead of surfacing ACTUAL_CUT_ERROR_HIGH.
    assert materialized_manifest["accurate_rerender_used"] is True
    assert materialized["render_qa"]["code"] == "ACTUAL_CUT_ERROR"
    assert materialized["render_qa"]["evidence"]["actual_cut_error_ms"] <= 100
    assert materialized_manifest["render_qa"] == materialized["render_qa"]
    assert render_qa_manifest["check"] == materialized["render_qa"]
    assert evidence["metrics"]["actual_cut_error_ms"] == materialized["render_qa"]["evidence"]["actual_cut_error_ms"]
    assert any(check["code"] == materialized["render_qa"]["code"] for check in evidence["checks"])
    assert evidence["metadata"]["materialized_recut"]["render_qa"]["code"] == "ACTUAL_CUT_ERROR"


def test_burn_preview_pillarboxes_vertical_source_to_16_9(tmp_path, monkeypatch):
    media_path = _write(tmp_path / "recuts" / "vertical.mp4", b"fake vertical media\n")
    subtitle_path = _write(tmp_path / "recuts" / "vertical.srt", "1\n00:00:00,000 --> 00:00:02,000\n竖屏测试\n")
    calls: list[list[str]] = []

    def fake_run(command, check=False, capture_output=True, text=True, timeout=None):
        calls.append(command)
        if command and command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, "1080,1920\n", "")  # vertical
        Path(command[-1]).write_bytes(b"burned\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(shadow_pipeline.subprocess, "run", fake_run)
    record = shadow_pipeline._burn_preview_subtitles(
        {"status": "MATERIALIZED", "media_path": str(media_path), "subtitle_path": str(subtitle_path), "artifact_hashes": {}},
        run_ffmpeg=True,
    )
    assert record["burned_preview"]["status"] == "BURNED"
    assert record["burned_preview"]["pillarbox_16_9"] is True
    burn_cmd = [c for c in calls if c and c[0] == "ffmpeg"][0]
    fc = burn_cmd[burn_cmd.index("-filter_complex") + 1]
    assert "overlay=(W-w)/2:0" in fc  # vertical centered
    assert "gblur" in fc  # blurred side fill
    assert "scale=1920:1080" in fc
    assert "subtitles=" in fc  # subtitle burned on the 16:9 frame


def test_burn_preview_uses_lidousha_sapphire_ass_style_not_default_srt_force_style(tmp_path, monkeypatch):
    media_path = _write(tmp_path / "recuts" / "lidousha-song.mp4", b"fake media bytes\n")
    subtitle_path = _write(
        tmp_path / "recuts" / "lidousha-song.srt",
        "1\n00:00:00,000 --> 00:00:03,000\n你看过了许多美景\n\n"
        "2\n00:00:03,200 --> 00:00:06,000\n却说不出旅行的意义\n",
    )
    calls: list[list[str]] = []

    def fake_run(command, check=False, capture_output=True, text=True, timeout=None):
        calls.append(command)
        # _video_dimensions ffprobe → non-vertical (horizontal burn branch)
        if command and command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, "1920,1080\n", "")
        Path(command[-1]).write_bytes(b"burned preview bytes\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(shadow_pipeline.subprocess, "run", fake_run)

    record = shadow_pipeline._burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(media_path),
            "subtitle_path": str(subtitle_path),
            "artifact_hashes": {},
        },
        run_ffmpeg=True,
    )

    ass_path = Path(record["burned_preview"]["ass_path"])
    ass_text = ass_path.read_text(encoding="utf-8")
    burn_cmd = [c for c in calls if c and c[0] == "ffmpeg"][0]
    ffmpeg_filter = burn_cmd[burn_cmd.index("-vf") + 1]

    assert ass_path.name.endswith(".final-sapphire72.ass")
    assert "Style: Default,Microsoft YaHei,72" in ass_text
    assert "&H00BA520F" in ass_text
    # approved sapphire72 spec: 72pt belongs to 1080p PlayRes with 60,60,40
    # margins, Shadow 2 and &H70000000 BackColour (skill write_ass values)
    assert "PlayResX: 1920" in ass_text
    assert "PlayResY: 1080" in ass_text
    assert ",&H70000000," in ass_text
    assert ",3,2,2,60,60,40,1" in ass_text
    assert "subtitles='" in ffmpeg_filter
    assert ".final-sapphire72.ass" in ffmpeg_filter
    assert "fontsdir=" in ffmpeg_filter
    assert "force_style=" not in ffmpeg_filter


def test_burn_preview_uses_hash_bound_prebuilt_speaker_ass_without_rebuilding(tmp_path, monkeypatch):
    media_path = _write(tmp_path / "recuts" / "talk.mp4", b"fake media bytes\n")
    subtitle_path = _write(
        tmp_path / "recuts" / "talk.srt",
        "1\n00:00:00,000 --> 00:00:02,000\n她想问是三个位置哦\n",
    )
    ass_path = _write(
        tmp_path / "recuts" / "talk.speaker-final.ass",
        "[Script Info]\n[V4+ Styles]\nStyle: LDS\nStyle: GUEST\n[Events]\n",
    )
    calls: list[list[str]] = []

    def fake_run(command, check=False, capture_output=True, text=True, timeout=None):
        calls.append(command)
        if command and command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, "1920,1080\n", "")
        Path(command[-1]).write_bytes(b"speaker burned\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(shadow_pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(
        shadow_pipeline,
        "_write_lidousha_sapphire_ass_from_srt",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not rebuild speaker ASS")),
    )
    ass_sha = "sha256:" + shadow_pipeline._sha256(ass_path)
    record = shadow_pipeline._burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(media_path),
            "subtitle_path": str(subtitle_path),
            "subtitle_ass_path": str(ass_path),
            "subtitle_style": "lidousha-speaker-colour-v1",
            "artifact_hashes": {"ass_sha256": ass_sha},
        },
        run_ffmpeg=True,
    )

    assert record["burned_preview"]["status"] == "BURNED"
    assert record["burned_preview"]["ass_path"] == str(ass_path)
    assert record["burned_preview"]["subtitle_style"] == "lidousha-speaker-colour-v1"
    assert record["burned_preview"]["path"].endswith(".burned-final-speaker.mp4")
    ffmpeg = next(command for command in calls if command and command[0] == "ffmpeg")
    assert "speaker-final.ass" in ffmpeg[ffmpeg.index("-vf") + 1]


def test_burn_preview_rejects_prebuilt_speaker_ass_hash_drift(tmp_path):
    media_path = _write(tmp_path / "talk.mp4", b"media")
    subtitle_path = _write(tmp_path / "talk.srt", "1\n00:00:00,000 --> 00:00:01,000\n文本\n")
    ass_path = _write(tmp_path / "talk.ass", "ass")
    record = shadow_pipeline._burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(media_path),
            "subtitle_path": str(subtitle_path),
            "subtitle_ass_path": str(ass_path),
            "artifact_hashes": {"ass_sha256": "sha256:" + "0" * 64},
        },
        run_ffmpeg=False,
    )
    assert record["burned_preview"]["status"] == "FAILED"
    assert record["burned_preview"]["reason_code"] == "PREBUILT_ASS_MISSING_OR_HASH_MISMATCH"


def test_burn_preview_rejects_prebuilt_speaker_ass_without_expected_hash(tmp_path):
    media_path = _write(tmp_path / "talk.mp4", b"media")
    subtitle_path = _write(tmp_path / "talk.srt", "1\n00:00:00,000 --> 00:00:01,000\n文本\n")
    ass_path = _write(tmp_path / "talk.ass", "ass")
    record = shadow_pipeline._burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(media_path),
            "subtitle_path": str(subtitle_path),
            "subtitle_ass_path": str(ass_path),
            "artifact_hashes": {},
        },
        run_ffmpeg=False,
    )
    assert record["burned_preview"]["status"] == "FAILED"
    assert record["burned_preview"]["reason_code"] == "PREBUILT_ASS_MISSING_OR_HASH_MISMATCH"


def test_ass_layout_never_exceeds_two_lines_of_28_chars(tmp_path):
    # Viewability spec from the LLM Multimodal ASR project (polish_srt_for_viewing
    # --max-chars 28) tightened by Ivan 2026-07-03: <=28 chars per visual line,
    # <=2 lines per dialogue (prefer 1), split over-long cues into sequential
    # sub-cues instead of stacking 3-4 lines over the picture.
    long_text = (
        "不是说，不说150赚多少，那150赚，赚的得比100，赚个111，不轻轻松松，"
        "然后我们抽了107，结果全是电影票，我真的要疯了，这不可能是我的问题吧"
    )
    srt_path = _write(
        tmp_path / "clip.srt",
        f"1\n00:00:00,000 --> 00:00:10,000\n{long_text}\n\n"
        "2\n00:00:10,500 --> 00:00:12,000\n短句无需处理\n\n"
        "3\n00:00:12,500 --> 00:00:13,600\n四十个字左右但是持续时间只有一秒钟出头的这种句子应该双行显示\n",
    )
    ass_path = tmp_path / "clip.ass"
    shadow_pipeline._write_lidousha_sapphire_ass_from_srt(srt_path, ass_path)

    dialogues = [line for line in ass_path.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) > 3  # the long cue was split into sequential sub-cues
    for dialogue in dialogues:
        text = dialogue.split(",", 9)[9]
        visual_lines = text.split("\\N")
        assert len(visual_lines) <= 2, dialogue
        for visual_line in visual_lines:
            assert len(visual_line) <= 28, dialogue
    # the short cue stays a single one-line dialogue
    assert any(d.split(",", 9)[9] == "短句无需处理" for d in dialogues)
    # the short-duration 40-char cue stays ONE dialogue with 2 lines (no sub-1s flicker)
    third = [d for d in dialogues if "四十个字左右" in d.split(",", 9)[9]]
    assert len(third) == 1 and "\\N" in third[0].split(",", 9)[9]


def test_ass_time_rounds_to_nearest_centisecond_not_floor():
    # flooring made every cue start up to 9ms early
    assert shadow_pipeline._format_ass_time(19_996) == "0:00:20.00"
    assert shadow_pipeline._format_ass_time(19_994) == "0:00:19.99"
    assert shadow_pipeline._format_ass_time(0) == "0:00:00.00"


def test_song_recut_burns_lyrics_from_lrc_global_shift_and_forces_accurate_rerender(tmp_path):
    source_video = _write_valid_av_source_video(tmp_path / "source.mp4", duration_seconds=8.0)
    boundary = shadow_pipeline.BoundaryResolution(
        candidate_id="song-lrc",
        action=shadow_pipeline.DecisionAction.AUTO_RECUT,
        resolved_start_ms=1_000,
        resolved_end_ms=8_000,
        start_boundary_score=0.98,
        end_boundary_score=0.98,
        reason_codes=("SONG_FULL_BOUNDARY_READY",),
        next_start_ms=1_000,
        next_end_ms=8_000,
    )
    asr_cue = shadow_pipeline.SourceCue("asr-1", 1_200, 3_000, "ASR听岔的歌词版本", kind="singing")

    materialized = shadow_pipeline._materialize_recut_record(
        source_video=source_video,
        candidate_id="song-lrc",
        boundary_resolution=boundary,
        output_dir=tmp_path / "out",
        cues=[asr_cue],
        run_ffmpeg=True,
        lyric_timeline=[(10_000, "第一句真实歌词"), (13_000, "第二句真实歌词"), (16_000, "第三句真实歌词")],
        lyric_offset_ms=-8_500,
        song_output_proof_binding={"post_song_anchor_start_ms": 8_000},
    )

    assert materialized["status"] == "MATERIALIZED"
    assert materialized["subtitle_source"] == "external_lrc_global_shift"
    assert materialized["lyric_offset_ms"] == -8_500
    # song clips must never keep the packet/keyframe-quantized copy cut: the
    # LRC timeline is only valid against a sample-accurate audio start
    assert materialized["accurate_rerender_used"] is True
    srt_text = Path(materialized["subtitle_path"]).read_text(encoding="utf-8")
    assert "ASR听岔的歌词版本" not in srt_text
    assert "第一句真实歌词" in srt_text
    # lrc 10_000 + offset -8_500 - clip start 1_000 = 500ms in-clip
    assert "00:00:00,500 --> 00:00:03,500" in srt_text
    manifest = _load_json(Path(materialized["manifest_path"]))
    assert manifest["subtitle_source"] == "external_lrc_global_shift"


def test_song_recut_and_burn_bind_exact_source_interval_streams_and_final_hashes(tmp_path):
    from scripts import free_session_autoslice as free_runner

    source_video = _write_valid_av_source_video(tmp_path / "source-av.mp4", duration_seconds=8.0)
    proof_files = {}
    for key, filename in (
        ("lyrics_alignment_report", "alignment.json"),
        ("host_vocal_proof", "host-proof.json"),
        ("agy_run_manifest", "agy-run.json"),
    ):
        path = _write(tmp_path / filename, json.dumps({"fixture": key}) + "\n")
        proof_files[f"{key}_path"] = str(path.resolve())
        proof_files[f"{key}_sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    proof_binding = {
        **proof_files,
        "post_song_anchor_start_ms": 6_000,
    }
    boundary = shadow_pipeline.BoundaryResolution(
        candidate_id="bound-song",
        action=shadow_pipeline.DecisionAction.AUTO_RECUT,
        resolved_start_ms=1_000,
        resolved_end_ms=6_000,
        start_boundary_score=0.98,
        end_boundary_score=0.98,
        reason_codes=("SONG_FULL_BOUNDARY_READY",),
        next_start_ms=1_000,
        next_end_ms=6_000,
    )

    materialized = shadow_pipeline._materialize_recut_record(
        source_video=source_video,
        candidate_id="bound-song",
        boundary_resolution=boundary,
        output_dir=tmp_path / "out",
        cues=[shadow_pipeline.SourceCue("asr", 1_000, 2_000, "错误 ASR", kind="singing")],
        run_ffmpeg=True,
        lyric_timeline=[(0, "第一句"), (2_000, "第二句"), (4_000, "第三句")],
        lyric_offset_ms=1_000,
        song_output_proof_binding=proof_binding,
    )
    assert materialized["status"] == "MATERIALIZED"
    burned = shadow_pipeline._burn_preview_subtitles(materialized, run_ffmpeg=True)
    assert burned["status"] == "MATERIALIZED"
    assert burned["burned_preview"]["status"] == "BURNED"

    source_sha = "sha256:" + hashlib.sha256(source_video.read_bytes()).hexdigest()
    manifest_path = Path(burned["manifest_path"])
    manifest = _load_json(manifest_path)
    binding = manifest["verified_output_binding"]
    assert burned["manifest_sha256"] == "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert manifest["schema_version"] == shadow_pipeline.MATERIALIZED_RECUT_SCHEMA_VERSION
    assert binding["candidate_id"] == "bound-song"
    assert binding["source"] == {"canonical_path": str(source_video.resolve()), "sha256": source_sha}
    assert binding["interval"] == {"start_ms": 1_000, "end_ms": 6_000, "duration_ms": 5_000}
    assert binding["post_song_anchor_start_ms"] == 6_000
    assert binding["proofs"] == proof_binding
    assert binding["stream_contract"] == shadow_pipeline._song_stream_contract()

    recut_command = binding["recut_transform"]["command"]
    recut_maps = [recut_command[index + 1] for index, token in enumerate(recut_command[:-1]) if token == "-map"]
    assert recut_maps == ["0:v:0", "0:a:0"]
    for exclusion in ("-sn", "-dn", "-map_metadata", "-map_chapters"):
        assert exclusion in recut_command
    burn_command = binding["burn_transform"]["command"]
    burn_maps = [burn_command[index + 1] for index, token in enumerate(burn_command[:-1]) if token == "-map"]
    assert burn_maps == ["0:v:0", "0:a:0"]
    for exclusion in ("-sn", "-dn", "-map_metadata", "-map_chapters"):
        assert exclusion in burn_command

    artifacts = binding["artifacts"]
    for path_key, hash_key in (
        ("recut_media_path", "recut_media_sha256"),
        ("subtitle_path", "subtitle_sha256"),
        ("burned_media_path", "burned_media_sha256"),
        ("ass_path", "ass_sha256"),
    ):
        artifact_path = Path(artifacts[path_key])
        assert artifact_path == artifact_path.resolve()
        assert artifacts[hash_key] == "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    assert free_runner._has_exact_av_streams(Path(artifacts["recut_media_path"])) is True
    assert free_runner._has_exact_av_streams(Path(artifacts["burned_media_path"])) is True


def test_song_recut_accurate_rerender_failure_is_retry_infra(tmp_path, monkeypatch):
    source_video = _write(tmp_path / "source.mp4", b"source video bytes\n")
    boundary = shadow_pipeline.BoundaryResolution(
        candidate_id="song-rerender-failed",
        action=shadow_pipeline.DecisionAction.AUTO_RECUT,
        resolved_start_ms=1_000,
        resolved_end_ms=8_000,
        start_boundary_score=0.98,
        end_boundary_score=0.98,
        reason_codes=("SONG_FULL_BOUNDARY_READY",),
        next_start_ms=1_000,
        next_end_ms=8_000,
    )

    calls = []

    def fake_run(command, check=False, capture_output=True, text=True):
        calls.append(command)
        if len(calls) == 1:
            Path(command[-1]).write_bytes(b"coarse packet-aligned copy\n")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "accurate render failed")

    monkeypatch.setattr(shadow_pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(
        shadow_pipeline,
        "_evaluate_materialized_recut_render_qa",
        lambda **_kwargs: {
            "code": "ACTUAL_CUT_ERROR_OK",
            "pass": True,
            "severity": "PASS",
            "evidence": {"actual_cut_error_ms": 20, "threshold_ms": 100},
        },
    )

    materialized = shadow_pipeline._materialize_recut_record(
        source_video=source_video,
        candidate_id="song-rerender-failed",
        boundary_resolution=boundary,
        output_dir=tmp_path / "out",
        cues=[shadow_pipeline.SourceCue("asr", 1_000, 2_000, "错听歌词", kind="singing")],
        run_ffmpeg=True,
        lyric_timeline=[(1_000, "真实歌词第一句"), (4_000, "真实歌词第二句")],
        lyric_offset_ms=0,
        song_output_proof_binding={"post_song_anchor_start_ms": 8_000},
    )

    assert materialized["status"] == "RETRY_INFRA"
    assert materialized["accurate_rerender_used"] is False
    assert "FFMPEG_ACCURATE_RECUT_FAILED" in materialized["reason_codes"]
    manifest = _load_json(Path(materialized["manifest_path"]))
    assert manifest["status"] == "RETRY_INFRA"
    assert len(calls) == 2


def test_known_mebukutoki_pin_dispatches_to_lrclib(monkeypatch):
    calls = []

    def fake_fetch(song_ref):
        calls.append(song_ref)
        return shadow_pipeline.LrcResult(
            provider="lrclib",
            song_title="芽吹くとき",
            artist="yonige",
            source_ref="https://lrclib.net/api/get/33542202",
            lines=tuple(LrcLine(index * 1_000, f"歌词{index}") for index in range(8)),
        )

    monkeypatch.setattr(shadow_pipeline, "fetch_lrclib_lrc", fake_fetch)
    cues = [
        shadow_pipeline.SourceCue("a", 0, 4_000, "最初から望んだ未来とは", kind="singing"),
        shadow_pipeline.SourceCue("b", 5_000, 10_000, "少し違うけれど 最後には何もいらない", kind="singing"),
    ]

    pinned = shadow_pipeline._pinned_lrc_for_song(cues)

    assert calls == ["lrclib://track/33542202"]
    assert [(item.provider, item.song_title) for item in pinned] == [("lrclib", "芽吹くとき")]


def test_load_lyric_timeline_requires_verified_proof_and_offset(tmp_path):
    report_path = tmp_path / "song.lyrics-alignment-report.json"
    report_payload = {
        "schema_version": "lyrics-alignment-report.v1",
        "offset_ms": 42_000,
        "lyric_lines": [
            {"lrc_time_ms": 0, "text": "第一句"},
            {"lrc_time_ms": 4_000, "text": "第二句"},
        ],
        "alignment": [],
    }
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False), encoding="utf-8")
    import hashlib

    sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    job = {
        "lyrics_alignment": {
            "status": "READY",
            "provider": "netease",
            "model": "netease-lrc-global-shift-align-v2",
            "source": "song_repair.netease",
            "alignment_report_path": str(report_path),
            "alignment_report_sha256": sha,
        }
    }

    loaded = shadow_pipeline._load_lyric_timeline(job, output_dir=tmp_path)
    assert loaded == ([(0, "第一句"), (4_000, "第二句")], 42_000)

    # tampered report → proof fails → no LRC subtitle timeline
    report_path.write_text(json.dumps({**report_payload, "offset_ms": 0}), encoding="utf-8")
    assert shadow_pipeline._load_lyric_timeline(job, output_dir=tmp_path) is None


def test_publish_staging_release_gate_skips_title_and_cover_side_effects(tmp_path, monkeypatch):
    def must_not_stage(*_args, **_kwargs):
        raise AssertionError("release-gated candidate must not enter publish staging")

    monkeypatch.setattr(shadow_pipeline, "_stage_publish_draft", must_not_stage)
    record = shadow_pipeline._stage_publish_after_release_gate(
        {
            "status": "MATERIALIZED",
            "media_path": str(tmp_path / "blocked.mp4"),
            "artifact_hashes": {"video_sha256": "sha256:blocked"},
        },
        decision=shadow_pipeline.ReviewDecision(
            action=shadow_pipeline.DecisionAction.BLOCK,
            reason_codes=("TERMINOLOGY_QA_FAILED",),
        ),
        candidate_id="blocked-song",
        title="blocked",
        cues=[],
        output_dir=tmp_path,
        run_ffmpeg=False,
        title_llm_call=lambda _prompt: '{"title":"must not run"}',
    )

    staging = record["publish_staging"]
    gate = _load_json(tmp_path / "blocked-song.cover-release-gate.json")
    assert staging["status"] == "SKIPPED_RELEASE_GATE"
    assert staging["decision_action"] == "BLOCK"
    assert staging["reason_codes"] == ["TERMINOLOGY_QA_FAILED"]
    assert gate["satisfied"] is False
    assert gate["artifact_hashes"]["video_sha256"] == "sha256:blocked"


def test_publish_staging_release_gate_runs_only_after_auto_upload_snapshot(tmp_path, monkeypatch):
    observed: dict[str, object] = {}

    def fake_stage(record, **_kwargs):
        gate_path = tmp_path / "ready-song.cover-release-gate.json"
        observed["gate_existed_before_stage"] = gate_path.is_file()
        observed["gate"] = _load_json(gate_path)
        return {**record, "publish_staging": {"status": "STAGED", "upload_enabled": False}}

    monkeypatch.setattr(shadow_pipeline, "_stage_publish_draft", fake_stage)
    record = shadow_pipeline._stage_publish_after_release_gate(
        {
            "status": "MATERIALIZED",
            "media_path": str(tmp_path / "ready.mp4"),
            "artifact_hashes": {"video_sha256": "sha256:ready"},
        },
        decision=shadow_pipeline.ReviewDecision(action=shadow_pipeline.DecisionAction.AUTO_UPLOAD),
        candidate_id="ready-song",
        title="ready",
        cues=[],
        output_dir=tmp_path,
        run_ffmpeg=False,
        title_llm_call=None,
    )

    assert observed["gate_existed_before_stage"] is True
    assert observed["gate"]["satisfied"] is True
    assert record["publish_staging"]["status"] == "STAGED"
    assert record["cover_release_gate"]["decision_action"] == "AUTO_UPLOAD"


def test_shadow_publish_adapter_forwards_recovery_publication_authority(
    monkeypatch,
):
    """Producer 导入的兼容 seam 不得丢失 same-BV 权威参数。"""

    authority = {
        "schema_version": "recovery-same-bv-publication-authority.v1",
        "candidate_id": "auto_test",
        "bvid": "BV1test",
    }
    captured = {}

    def stage_impl(materialized_recut, **kwargs):
        captured["materialized_recut"] = materialized_recut
        captured.update(kwargs)
        return {"status": "MATERIALIZED"}

    monkeypatch.setattr(
        shadow_pipeline,
        "_stage_publish_draft_impl",
        stage_impl,
    )

    result = shadow_pipeline._stage_publish_draft(
        {"status": "MATERIALIZED"},
        candidate_id="auto_test",
        title="【李豆沙】测试",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=None,
        recovery_publication_authority=authority,
    )

    assert result == {"status": "MATERIALIZED"}
    assert captured["recovery_publication_authority"] is authority
    assert (
        captured["stage_cover"]
        is shadow_pipeline._stage_lidousha_ai_cover
    )


def test_publish_staging_blocks_without_cpa_ai_cover_and_never_extracts_frame_cover(tmp_path, monkeypatch):
    media_path = _write(tmp_path / "recuts" / "lidousha-song.mp4", b"fake media bytes\n")
    subtitle_path = _write(tmp_path / "recuts" / "lidousha-song.srt", "1\n00:00:00,000 --> 00:00:03,000\n唱歌\n")
    manifest_path = tmp_path / "recuts" / "lidousha-song.manifest.json"
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    # 2026-07-21 起 auto/screenshot 路线可产出"设计过的截图封面"（Ivan 批准）；
    # 本测试守护的旧保证只属于强制 cpa 模式：无凭据必须在任何抽帧前卡死。
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")

    def fail_if_frame_cover_is_extracted(*_args, **_kwargs):
        raise AssertionError("publish staging must not extract a deterministic frame cover as a finished cover")

    monkeypatch.setattr(shadow_pipeline.subprocess, "run", fail_if_frame_cover_is_extracted)

    record = shadow_pipeline._stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media_path),
            "subtitle_path": str(subtitle_path),
            "manifest_path": str(manifest_path),
            "artifact_hashes": {},
        },
        candidate_id="lidousha-song",
        title="【李豆沙】豆沙歌，《旅行的意义》唱到伴奏卡住像在KTV录的",
        cues=[],
        run_ffmpeg=True,
        title_llm_call=None,
    )

    publish_path = Path(record["publish_staging"]["publish_json_path"])
    publish = _load_json(publish_path)

    assert record["publish_staging"]["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert "CPA_AI_COVER_REQUIRED" in record["publish_staging"]["reason_codes"]
    assert publish["cover_path"] is None
    assert publish["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    # Song titles are a fixed catalog form; the old hook tail must not leak
    # into either the publish title or its coupled cover text.
    assert publish["cover_text"] == "《旅行的意义》"
    assert publish["cover_generation"]["workflow"] == "cpa-openai-compatible-image-edit-cover-plus-approved-local-title-overlay"
    assert publish["cover_generation"]["model"] == "gpt-image-2"
    assert publish["cover_generation"]["method"] == "images.edit"
    assert publish["cover_generation"]["fallback_used"] is False
    assert not media_path.with_suffix(".cover.jpg").exists()


def test_publish_staging_records_cpa_ai_cover_chain_and_embedded_title(tmp_path, monkeypatch):
    from PIL import Image
    from src.autoslice import cover_host_identity_gate

    media_path = _write_valid_source_video(tmp_path / "recuts" / "lidousha-song.mp4", duration_seconds=2.0)
    subtitle_path = _write(tmp_path / "recuts" / "lidousha-song.srt", "1\n00:00:00,000 --> 00:00:01,000\n唱歌\n")
    manifest_path = tmp_path / "recuts" / "lidousha-song.manifest.json"
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")

    def fake_cpa_image_edit(*, output_path: Path, request_path: Path, response_path: Path, **_kwargs):
        request_path.write_text('{"api_key":"<redacted>"}\n', encoding="utf-8")
        Image.new("RGB", (1920, 1080), (30, 80, 140)).save(output_path)
        response_path.write_text('{"status":"ok"}\n', encoding="utf-8")
        return {"status": "AI_BACKGROUND_READY", "output_path": str(output_path)}

    monkeypatch.setattr(shadow_pipeline, "_call_cpa_image_edit", fake_cpa_image_edit)

    def fake_host_identity_verifier(*, final_cover_sha256: str, **_kwargs):
        comparison_hash = "a" * 64
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v2"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
            ),
            "status": "PASS",
            "final_cover_sha256": final_cover_sha256,
            "comparison_sha256": "sha256:" + comparison_hash,
            "witness": {
                "status": "OBSERVED",
                "provider": "cpa",
                "image_sha256": comparison_hash,
            },
        }

    monkeypatch.setattr(
        cover_host_identity_gate,
        "verify_lidousha_final_host_identity",
        fake_host_identity_verifier,
    )

    record = shadow_pipeline._stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media_path),
            "subtitle_path": str(subtitle_path),
            "manifest_path": str(manifest_path),
            "artifact_hashes": {},
        },
        candidate_id="lidousha-song",
        title="【李豆沙】豆沙歌，《旅行的意义》唱到伴奏卡住像在KTV录的",
        cues=[],
        run_ffmpeg=True,
        title_llm_call=None,
    )

    publish = _load_json(Path(record["publish_staging"]["publish_json_path"]))
    generation = publish["cover_generation"]

    assert publish["cover_status"] == "AI_COVER_READY"
    assert Path(publish["cover_path"]).is_file()
    assert Path(generation["ai_background"]).is_file()
    assert Path(generation["reference_image"]).is_file()
    assert generation["workflow"] == "cpa-openai-compatible-image-edit-cover-plus-approved-local-title-overlay"
    assert generation["method"] == "images.edit"
    assert generation["cover_origin"] == "AI_REDRAW"
    assert generation["image_generation_planned"] is True
    assert generation["image_generation_attempted"] is True
    assert generation["image_generation_used"] is True
    assert generation["model"] == "gpt-image-2"
    assert generation["fallback_used"] is False
    assert generation["cover_text"] == "《旅行的意义》"
    assert generation["font"] == "ZCOOLKuaiLe-Regular.ttf"
    assert generation["angle_degrees"] == -4.0
    route = generation["route_decision"]
    assert route["schema_version"] == "lidousha-cover-route-decision.v2"
    assert route["selected_treatment"] == "cpa_redraw"
    assert route["actual_treatment"] == "cpa_redraw"
    assert route["execution_status"] == "READY"
    assert route["image_generation_used"] is True
    assert "cover_sha256" in publish["artifact_hashes"]


def test_cpa_image_edit_falls_back_only_on_explicit_model_unavailable(
    tmp_path, monkeypatch
):
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"reference")
    calls = []
    unavailable = json.dumps(
        {"error": {"code": "client_model_unavailable", "message": "unsupported"}}
    ).encode()
    image_payload = json.dumps(
        {"data": [{"b64_json": base64.b64encode(b"image").decode()}]}
    ).encode()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return image_payload

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "bad request",
                hdrs=None,
                fp=io.BytesIO(unavailable),
            )
        return Response()

    monkeypatch.setattr(shadow_pipeline.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(shadow_pipeline, "_normalize_cover_canvas", lambda _path: (1920, 1080))
    output = tmp_path / "out.png"
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    result = shadow_pipeline._call_cpa_image_edit(
        base_url="https://cpa.example/v1",
        api_key="secret",
        reference_path=reference,
        output_path=output,
        prompt="prompt",
        request_path=request_path,
        response_path=response_path,
        model_candidates=("gpt-image-2", "gpt-image-1.5"),
    )

    assert result["status"] == "AI_BACKGROUND_READY"
    assert result["selected_model"] == "gpt-image-1.5"
    assert result["attempted_models"] == ["gpt-image-2", "gpt-image-1.5"]
    assert result["model_fallback_used"] is True
    assert len(calls) == 2
    assert b'gpt-image-2' in calls[0].data
    assert b'gpt-image-1.5' in calls[1].data
    request_evidence = json.loads(request_path.read_text(encoding="utf-8"))
    response_evidence = json.loads(response_path.read_text(encoding="utf-8"))
    assert request_evidence["api_key"] == "<redacted>"
    assert [row["model"] for row in response_evidence["attempts"]] == [
        "gpt-image-2",
        "gpt-image-1.5",
    ]


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [(400, "model_price_error"), (403, "safety_rejection"), (500, "upstream_error")],
)
def test_cpa_image_edit_does_not_fallback_for_other_failures(
    tmp_path, monkeypatch, status_code, error_code
):
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"reference")
    calls = []
    body = json.dumps({"error": {"code": error_code}}).encode()

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(
            request.full_url,
            status_code,
            "failed",
            hdrs=None,
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(shadow_pipeline.urllib.request, "urlopen", fake_urlopen)
    result = shadow_pipeline._call_cpa_image_edit(
        base_url="https://cpa.example/v1",
        api_key="secret",
        reference_path=reference,
        output_path=tmp_path / "out.png",
        prompt="prompt",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        model_candidates=("gpt-image-2", "gpt-image-1.5"),
    )

    assert result["status"] == "FAILED"
    assert result["attempted_models"] == ["gpt-image-2"]
    assert result["model_fallback_used"] is False
    assert len(calls) == 1


def test_live_source_missing_srt_records_source_integrity_and_replay_plan(tmp_path):
    source_video = _write(tmp_path / "tiny.m4s", b"tiny")
    output_dir = tmp_path / "output"

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=None,
        source_context_job={
            "candidate_id": "tiny-stalled-live",
            "date": "2026-06-29",
            "timeline": {"source_duration_ms": 7_200_000},
            "source_integrity": {
                "session_date": "2026-06-29",
                "expected_start_ms": 0,
                "expected_end_ms": 7_200_000,
                "observed_segments": [
                    {
                        "path": str(source_video),
                        "start_ms": 0,
                        "end_ms": 4_040,
                        "duration_ms": 4_040,
                        "size_bytes": 3_369,
                        "probed_ok": True,
                    }
                ],
                "danmaku_latest_ms": 7_100_000,
                "active_media_size_growth_bytes": 0,
                "bilibili_replay_auth_available": False,
            },
        },
        room_id="22966160",
        output_dir=output_dir,
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    integrity = summary["source_integrity"]
    ledger = integrity["ledger"]
    plan = integrity["compensation_plan"]
    issue_codes = {issue["code"] for issue in ledger["issues"]}

    assert summary["counts"]["retry"] == 1
    assert ledger["compensation_required"] is True
    assert ledger["replay_probe_required"] is True
    assert ledger["can_use_local_source"] is False
    assert "DANMAKU_OUTRUNS_MEDIA" in issue_codes
    assert "MEDIA_STALLED_WHILE_DANMAKU_ADVANCES" in issue_codes
    assert plan["status"] == "BLOCKED"
    assert "BILIBILI_REPLAY_AUTH_REQUIRED" in plan["reason_codes"]
    assert "cookie" not in json.dumps(integrity, ensure_ascii=False).lower()


def test_shadow_pipeline_fails_closed_when_preexisting_marker_exists(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    review_package = _write_review_package(
        tmp_path,
        stem="preexisting-marker",
        jingting_manifest={
            "provider": "agy",
            "agy_rc": 0,
            "model": "Gemini 3.6 Flash (Low)",
            "provider_fallback_used": False,
        },
        review_required={"release_ready": True, "findings": []},
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    stale_marker = output_dir / "preexisting-marker.auto_review.retry"
    stale_marker.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="preexisting-marker\\.auto_review\\.retry"):
        shadow_pipeline.run_shadow_pipeline(review_package=review_package, output_dir=output_dir, no_upload=True)

    assert stale_marker.read_text(encoding="utf-8") == "sentinel\n"
    assert not (output_dir / "preexisting-marker.auto_review.would_upload").exists()



@pytest.mark.parametrize(
    ("audio_provider", "paid_backup"),
    [("agy", False)],
)
def test_live_source_song_repair_earns_proof_and_unblocks(tmp_path, audio_provider, paid_backup):
    from src.autoslice.song_repair import LrcLine, LrcResult

    lyric_lines = [
        "憧憬一生 竹马组你终相守",
        "常叹一生未曾有此从容",
        "江湖难测 侠骨柔情红颜梦",
        "沧桑了谁人的眼眸",
        "还有多少痛 埋藏在心中",
        "只为一人从容 无求孤身闯万重",
        "一壶浊酒笑看风云动",
        "回首江湖仍是少年梦",
    ]
    srt_blocks = []
    cursor_ms = 50_000
    for index, text in enumerate(lyric_lines, start=1):
        start = cursor_ms
        end = cursor_ms + 6_000

        def _ts(ms):
            h, rem = divmod(ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, msec = divmod(rem, 1_000)
            return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"

        srt_blocks.append(f"{index}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
        cursor_ms += 7_000
    srt_text = "\n".join(srt_blocks)
    source_video, source_srt, refined_srt = _write_live_source_inputs(tmp_path, source_srt=srt_text, refined_srt=srt_text)
    cpa_fields = _write_passing_cpa_job_fields(
        tmp_path,
        candidate_id="repairable-song",
        source_video=source_video,
        source_srt=source_srt,
        start_ms=0,
        end_ms=120_000,
        text="唱了一整首侠客行，最后大家都说好听笑了",
    )

    lrc = LrcResult(
        provider="fake-netease",
        song_title="侠客行",
        artist="测试歌手",
        source_ref="fake://song/99",
        # same line pacing as the performance: the strict global-shift model
        # rejects LRCs whose span cannot be one continuous performance
        lines=tuple(LrcLine(time_ms=i * 7_000, text=text) for i, text in enumerate(lyric_lines)),
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "repairable-song",
            **cpa_fields,
            "song_candidate": True,
            "title": "【测试】整首侠客行",
            "timeline": {
                "source_duration_ms": 120_000,
                "anchor_start_ms": 60_000,
                "anchor_end_ms": 80_000,
                "context_start_ms": 0,
                "context_duration_ms": 120_000,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
        lrc_provider=lambda query: lrc,
        audio_lrc_aligner=lambda source, selected_lrc, candidate, artifact_dir: make_ready_audio_alignment_run(
            source_media=source,
            source_duration_ms=120_000,
            lrc=selected_lrc,
            candidate_id=candidate,
            output_dir=artifact_dir,
            provider=audio_provider,
            paid_backup=paid_backup,
        ),
        host_vocal_prover=_ready_host_vocal_prover,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    # repair earned the proof: no SONG_PARTIAL block, full-song boundary honored
    assert record["decision_action"] == "AUTO_RECUT"
    assert "SONG_FULL_BOUNDARY_READY" in record["reason_codes"]
    assert "SONG_PARTIAL" not in record["reason_codes"]
    assert "SONG_PROOF_UNVERIFIED" not in record["reason_codes"]
    # burned subtitle timeline must come from the earned LRC proof, not ASR
    assert record["materialized_recut"]["subtitle_source"] == "external_lrc_global_shift"
    repair = evidence["metadata"]["song_repair"]
    assert repair["repaired"] is True
    assert any(a["step"] == "lrc_discovery" and a["status"] == "SUCCESS" for a in repair["attempts"])

    # Positive-chain regression: AGY, the projected report, CAM++ and the
    # materialized manifest must all name the exact source bytes and interval.
    job = record["source_context_job"]
    alignment_claim = job["lyrics_alignment"]
    report_path = Path(alignment_claim["alignment_report_path"])
    alignment_report = _load_json(report_path)
    agy_manifest = _load_json(Path(alignment_report["audio_alignment_artifacts"]["run_manifest_path"]))
    host_proof = _load_json(Path(job["host_vocal_proof"]["proof_path"]))
    recut = record["materialized_recut"]
    recut_manifest_path = Path(recut["manifest_path"])
    recut_manifest = _load_json(recut_manifest_path)
    source_path = str(source_video.resolve())
    source_sha = hashlib.sha256(source_video.read_bytes()).hexdigest()
    assert {
        recut["source_binding"]["canonical_path"],
        alignment_claim["source_media_path"],
        alignment_report["source_media_path"],
        alignment_report["audio_alignment_artifacts"]["source_origin_path"],
        agy_manifest["artifacts"]["source_origin_path"],
        host_proof["source_media"]["path"],
    } == {source_path}
    assert {
        str(value).removeprefix("sha256:")
        for value in (
            recut["source_binding"]["sha256"],
            alignment_claim["source_media_sha256"],
            alignment_report["source_media_sha256"],
            alignment_report["audio_alignment_artifacts"]["source_sha256"],
            agy_manifest["artifacts"]["source_sha256"],
            host_proof["source_media"]["sha256"],
        )
    } == {source_sha}
    assert recut["end_ms"] == alignment_report["post_song_talk_start_ms"]
    assert recut["end_ms"] == host_proof["session_host_anchor"]["start_ms"]
    assert recut["end_ms"] == job["song_boundary"]["clip_end_ms"]
    assert recut_manifest["requested_range"] == {
        "start_ms": recut["start_ms"],
        "end_ms": recut["end_ms"],
        "duration_ms": recut["duration_ms"],
    }
    assert recut_manifest["verified_output_binding"] == recut["verified_output_binding"]
    assert recut["manifest_sha256"] == "sha256:" + hashlib.sha256(recut_manifest_path.read_bytes()).hexdigest()

    if audio_provider == "gemini_api":
        tampered_report = dict(alignment_report)
        tampered_report["audio_alignment_provider"] = "agy"
        report_path.write_text(json.dumps(tampered_report, ensure_ascii=False) + "\n", encoding="utf-8")
        tampered_claim = dict(alignment_claim)
        tampered_claim["alignment_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        assert shadow_pipeline._verify_live_performance_observation(
            tampered_claim,
            output_dir=report_path.parent,
        ) == "live-performance proof is not an approved production audio alignment"


def test_live_source_song_repair_failure_records_attempts_then_blocks(tmp_path):
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt=(
            "1\n00:00:50,000 --> 00:00:58,000\n憧憬一生追命逐离中相骨\n\n"
            "2\n00:00:58,500 --> 00:01:04,000\n常叹一生未曾有此从容\n\n"
            "3\n00:01:04,500 --> 00:01:08,000\n江湖难测侠骨柔情红颜梦\n"
        ),
        refined_srt=(
            "1\n00:00:50,000 --> 00:00:58,000\n憧憬一生追命逐离中相骨\n\n"
            "2\n00:00:58,500 --> 00:01:04,000\n常叹一生未曾有此从容\n\n"
            "3\n00:01:04,500 --> 00:01:08,000\n江湖难测侠骨柔情红颜梦\n"
        ),
    )
    cpa_fields = _write_passing_cpa_job_fields(
        tmp_path,
        candidate_id="unrepairable-song",
        source_video=source_video,
        source_srt=source_srt,
        start_ms=0,
        end_ms=70_000,
        text="唱歌片段，最后大家都笑了",
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "unrepairable-song",
            **cpa_fields,
            "song_candidate": True,
            "title": "【测试】修不好的歌",
            "timeline": {
                "source_duration_ms": 70_000,
                "anchor_start_ms": 50_000,
                "anchor_end_ms": 68_000,
                "context_start_ms": 0,
                "context_duration_ms": 70_000,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
        lrc_provider=lambda query: None,  # discovery finds nothing
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["decision_action"] in {"BLOCK", "DROP"}
    repair = evidence["metadata"]["song_repair"]
    assert repair["repaired"] is False
    assert any(a["step"] == "lrc_discovery" and a["status"] == "FAILED" for a in repair["attempts"])


def test_burn_preview_subtitles_dry_run_and_non_materialized_passthrough(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试字幕\n", encoding="utf-8")

    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
        "artifact_hashes": {},
    }
    burned = shadow_pipeline._burn_preview_subtitles(record, run_ffmpeg=False)
    assert burned["burned_preview"]["status"] == "DRY_RUN"
    assert Path(burned["burned_preview"]["path"]).is_file()

    untouched = shadow_pipeline._burn_preview_subtitles({"status": "RETRY_INFRA"}, run_ffmpeg=False)
    assert untouched == {"status": "RETRY_INFRA"}
    assert shadow_pipeline._burn_preview_subtitles(None, run_ffmpeg=False) is None


def test_publish_staging_writes_upload_disabled_draft_and_blocks_unfinished_ai_cover(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    # The title sample now prefers the FINAL subtitle file (fresh transcription
    # with glossary corrections) over the context cues.
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n价格有点贵哈哈哈\n", encoding="utf-8")
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
        "artifact_hashes": {"video_sha256": "sha256:x"},
    }
    cues = [shadow_pipeline.SourceCue("c1", 0, 2_000, "粗轴旧文本不应被采样")]

    title_prompts: list[str] = []

    def fake_title_llm(prompt: str) -> str:
        title_prompts.append(prompt)
        assert "价格有点贵哈哈哈" in prompt
        return '{"title": "主播吐槽游戏价格贵，笑场三连"}'

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-1",
        title="原始job标题",
        cues=cues,
        run_ffmpeg=False,
        title_llm_call=fake_title_llm,
    )

    staging = staged["publish_staging"]
    assert len(title_prompts) == 1
    # Prompt freeze includes the current title_style authority.  Keep this
    # outside the fake callback so an asset drift fails as a fingerprint
    # assertion instead of being swallowed by the production LLM error gate.
    assert hashlib.sha256(title_prompts[0].encode()).hexdigest() == (
        "00efda8b6421f3b7ce1ec5e02552f582d94d9db09f0227fbb1f62a19c26685ed"  # title_style +百合作品关联即出 (Ivan 2026-07-25)
    )
    assert staging["status"] == "STAGED"
    assert staging["upload_enabled"] is False
    # Auto title gets the 【李豆沙】 publish prefix forced on (length counted with it).
    assert staging["title"] == "【李豆沙】主播吐槽游戏价格贵，笑场三连"
    assert staging["title_source"] == "llm+lidousha_style_asset"
    assert staging["title_policy_violations"] == []
    draft = _load_json(Path(staging["publish_json_path"]))
    assert draft["upload_enabled"] is False
    assert draft["title"] == "【李豆沙】主播吐槽游戏价格贵，笑场三连"
    assert draft["title_policy_violations"] == []
    assert draft["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert draft["cover_path"] is None
    assert draft["cover_generation"]["fallback_used"] is False


def test_publish_staging_fails_before_cover_when_title_llm_fails(tmp_path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试字幕\n", encoding="utf-8")
    record = {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(srt), "artifact_hashes": {}}

    def broken_llm(prompt: str) -> str:
        raise RuntimeError("bridge down")

    cover_calls = []

    def forbidden_cover_call(*args, **kwargs):
        cover_calls.append((args, kwargs))
        raise AssertionError("cover stage must not run before title authority")

    monkeypatch.setattr(
        shadow_pipeline, "_stage_lidousha_ai_cover", forbidden_cover_call
    )

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-2",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=broken_llm,
    )

    staging = staged["publish_staging"]
    assert staging["title"] == "【李豆沙】原始job标题"
    assert staging["title_source"].startswith("job_title(llm_failed")
    assert staging["status"] == "BLOCKED_TITLE_AUTHORITY"
    assert staging["title_authority_status"] == "UNRESOLVED_AUTO"
    assert staging["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert staging["cover_generation"]["status"] == "NOT_ATTEMPTED"
    assert staging["reason_codes"] == ["TITLE_AUTHORITY_UNRESOLVED"]
    assert cover_calls == []
    assert staging["upload_enabled"] is False


def test_title_policy_helpers_flag_banned_words_and_universal_suffix():
    # Clean concrete title passes.
    assert shadow_pipeline._title_policy_violations("温柔《虫儿飞》清唱甜美哄睡") == []
    # Standalone empty-hype words are caught anywhere in the title.
    for word in ("炸裂", "震惊", "天花板", "绝了", "犯规", "太顶"):
        assert "banned_hype_word" in shadow_pipeline._title_policy_violations(f"李豆沙{word}现场")
    # "X到{banned}" universal suffix trips its own distinct code too.
    both = shadow_pipeline._title_policy_violations("哄睡到犯规")
    assert "banned_universal_suffix" in both and "banned_hype_word" in both
    # 离谱 is dual-use: descriptive "越看越离谱" (Ivan-approved) passes, but the
    # empty "X到离谱" suffix is still banned.
    assert shadow_pipeline._title_policy_violations("结果越看越离谱主播看傻") == []
    assert "banned_universal_suffix" in shadow_pipeline._title_policy_violations("哄睡到离谱")
    # Prefix helper is idempotent and never double-prefixes song titles.
    assert shadow_pipeline._ensure_lidousha_prefix("反沙，不是反李豆沙！") == "【李豆沙】反沙，不是反李豆沙！"
    assert shadow_pipeline._ensure_lidousha_prefix("【李豆沙】豆沙歌，《宝贝》") == "【李豆沙】豆沙歌，《宝贝》"


def test_publish_staging_retries_banned_hype_word_then_accepts_clean_rewrite(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n虫儿飞清唱\n", encoding="utf-8")
    record = {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(srt), "artifact_hashes": {}}

    calls: list[str] = []
    # First response trips the "X到犯规" universal suffix; the retry rewrites concretely.
    responses = ['{"title": "虫儿飞哄睡到犯规"}', '{"title": "温柔《虫儿飞》清唱甜美哄睡观众"}']

    def retry_llm(prompt: str) -> str:
        calls.append(prompt)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="song-retry",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=retry_llm,
    )

    staging = staged["publish_staging"]
    assert len(calls) == 2  # retried exactly once, then accepted the clean rewrite
    assert staging["title"] == "【李豆沙】温柔《虫儿飞》清唱甜美哄睡观众"
    assert staging["title_source"] == "llm+lidousha_style_asset"
    assert staging["title_policy_violations"] == []
    # Retry prompt explicitly flags the prior violation; the first prompt does not.
    assert "已被否决" in calls[1]
    assert "已被否决" not in calls[0]


def test_publish_staging_mechanically_removes_automatic_filler_before_cover(
    tmp_path, monkeypatch
):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n拒绝花钱\n",
        encoding="utf-8",
    )
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
        "artifact_hashes": {},
    }
    calls: list[str] = []

    def filler_llm(prompt: str) -> str:
        calls.append(prompt)
        return (
            '{"title": "观众想让新3D永久保留白色奶龙表情，'
            '小李当场拒绝花钱"}'
        )

    cover_calls: list[str] = []

    def stage_cover(_record, **kwargs):
        cover_calls.append(str(kwargs["title"]))
        cover_path = tmp_path / "cover.png"
        cover_path.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover_path),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    monkeypatch.setattr(
        shadow_pipeline, "_stage_lidousha_ai_cover", stage_cover
    )
    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-filler-self-heal",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=filler_llm,
    )

    staging = staged["publish_staging"]
    assert len(calls) == 1
    assert staging["title"] == (
        "【李豆沙】观众想让新3D永久保留白色奶龙表情，小李拒绝花钱"
    )
    assert staging["title_source"] == (
        "llm+lidousha_style_asset+deterministic_filler_removal"
    )
    assert (
        staging["title_authority_status"]
        == "RESOLVED_DETERMINISTIC_FILLER_REMOVAL"
    )
    assert staging["title_policy_violations"] == []
    assert staging["status"] == "STAGED"
    assert cover_calls == [staging["title"]]


def test_automatic_filler_removal_canonicalizes_selection_hook_anchor():
    from src.autoslice.publish_staging import _resolve_automatic_title

    responses: list[str] = []

    def filler_llm(_prompt: str) -> str:
        responses.append("called")
        return json.dumps(
            {
                "title": (
                    "观众想让新3D永久保留“白色奶龙”表情，"
                    "小李当场拒绝花钱"
                ),
                "selection_hook_anchor": "当场拒绝花钱",
            },
            ensure_ascii=False,
        )

    result = _resolve_automatic_title(
        base_prompt="test",
        title_llm_call=filler_llm,
        selection_hook=(
            "观众想让新3D永久保留“白色奶龙”表情，"
            "李豆沙当场拒绝花钱，还坦白旧模型越看越恐怖。"
        ),
        initial_title_source="job_title",
    )

    assert responses == ["called"]
    assert result.staged_title == (
        "【李豆沙】观众想让新3D永久保留“白色奶龙”表情，小李拒绝花钱"
    )
    assert result.title_authority_status == (
        "RESOLVED_DETERMINISTIC_FILLER_REMOVAL"
    )
    assert result.title_policy_violations == []


def test_publish_choke_repairs_blocked_legacy_automatic_filler_result(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n拒绝花钱\n",
        encoding="utf-8",
    )
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
        "artifact_hashes": {},
    }
    blocked_title = (
        "【李豆沙】观众想让新3D永久保留“白色奶龙”表情，"
        "小李当场拒绝花钱"
    )
    monkeypatch.setattr(
        publish_staging,
        "_resolve_automatic_title",
        lambda **_kwargs: publish_staging._AutomaticTitleResult(
            blocked_title,
            "llm+lidousha_style_asset(title_policy_violation)",
            None,
            "title_policy_violation:banned_filler_word",
            ["banned_filler_word"],
        ),
    )
    cover_calls: list[str] = []

    def stage_cover(_record, **kwargs):
        cover_calls.append(str(kwargs["title"]))
        cover_path = tmp_path / "cover.png"
        cover_path.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover_path),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    monkeypatch.setattr(
        shadow_pipeline, "_stage_lidousha_ai_cover", stage_cover
    )
    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-filler-publish-choke",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=lambda _prompt: "{}",
        selection_hook=(
            "观众想让新3D永久保留“白色奶龙”表情，"
            "李豆沙当场拒绝花钱，还坦白旧模型越看越恐怖。"
        ),
    )

    staging = staged["publish_staging"]
    assert staging["title"] == (
        "【李豆沙】观众想让新3D永久保留“白色奶龙”表情，小李拒绝花钱"
    )
    assert staging["title_source"].endswith(
        "deterministic_filler_removal_at_publish_choke"
    )
    assert (
        staging["title_authority_status"]
        == "RESOLVED_DETERMINISTIC_FILLER_REMOVAL"
    )
    assert staging["title_authority_error"] is None
    assert staging["title_policy_violations"] == []
    assert staging["status"] == "STAGED"
    assert cover_calls == [staging["title"]]


def test_publish_staging_retries_unbalanced_title_before_cover(
    tmp_path, monkeypatch
):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n搭档把椅子让给小李\n",
        encoding="utf-8",
    )
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
        "artifact_hashes": {},
    }
    calls: list[str] = []
    responses = [
        '{"title": "看着像不良帅姐的搭档把大椅子让给小李（误"}',
        '{"title": "看着像不良帅姐的搭档把大椅子让给小李，自己缩角落假哭"}',
    ]

    def retry_llm(prompt: str) -> str:
        calls.append(prompt)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    cover_calls: list[str] = []

    def stage_cover(_record, **kwargs):
        cover_calls.append(str(kwargs["title"]))
        cover_path = tmp_path / "cover.png"
        cover_path.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover_path),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    monkeypatch.setattr(
        shadow_pipeline, "_stage_lidousha_ai_cover", stage_cover
    )

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-unbalanced-title",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=retry_llm,
    )

    staging = staged["publish_staging"]
    assert len(calls) == 2
    assert "成对符号未闭合" in calls[1]
    assert staging["title"] == (
        "【李豆沙】看着像不良帅姐的搭档把大椅子让给小李，自己缩角落假哭"
    )
    assert staging["title_policy_violations"] == []
    assert staging["cover_status"] == "AI_COVER_READY"
    assert cover_calls == [staging["title"]]


def test_publish_staging_binds_title_to_selected_main_hook_and_self_heals_mismatch(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n上下摇以后又聊到熊猫头锤和温柔歌\n",
        encoding="utf-8",
    )
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
        "artifact_hashes": {},
    }
    hook = "弹幕让李豆沙表演上下摇，她先把自己摇晕，又叫熊熊靠近后突然发动熊猫头锤。"
    calls = []

    def wrong_topic_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(
            {
                "title": "熊猫头槌解决kmx，温柔歌后再表演上下摇",
                "selection_hook_anchor": "上下摇",
            },
            ensure_ascii=False,
        )

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-hook-drift",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=wrong_topic_llm,
        selection_hook=hook,
    )

    staging = staged["publish_staging"]
    assert len(calls) == 3
    assert hook in calls[0]
    assert "第一分句" in calls[0]
    assert staging["title"] == "【李豆沙】弹幕让小李表演上下摇，结果先把自己摇晕"
    assert staging["title_source"] == "selection_hook_fallback_after_llm_mismatch"
    assert staging["title_policy_violations"] == []
    assert "熊猫头槌解决" not in staging["title"]


def test_publish_staging_blocks_title_policy_violation_before_cover(tmp_path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n哄睡\n", encoding="utf-8")
    record = {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(srt), "artifact_hashes": {}}

    calls: list[str] = []

    def stubborn_llm(prompt: str) -> str:
        calls.append(prompt)
        return '{"title": "虫儿飞温柔哄睡到犯规了"}'

    cover_calls = []
    monkeypatch.setattr(
        shadow_pipeline,
        "_stage_lidousha_ai_cover",
        lambda *args, **kwargs: cover_calls.append((args, kwargs)),
    )

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="song-stubborn",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=stubborn_llm,
    )

    staging = staged["publish_staging"]
    assert len(calls) == 3  # 1 initial + 2 bounded retries
    # Retained only as audit text; it has no title authority and cannot spend on a cover.
    assert staging["title"] == "【李豆沙】虫儿飞温柔哄睡到犯规了"
    assert staging["title_source"] == "llm+lidousha_style_asset(title_policy_violation)"
    assert "banned_hype_word" in staging["title_policy_violations"]
    assert "banned_universal_suffix" in staging["title_policy_violations"]
    assert staging["status"] == "BLOCKED_TITLE_AUTHORITY"
    assert staging["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert cover_calls == []
    draft = _load_json(Path(staging["publish_json_path"]))
    assert draft["title_policy_violations"] == staging["title_policy_violations"]


def test_publish_staging_length_gate_blocks_before_cover(tmp_path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n短\n", encoding="utf-8")
    record = {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(srt), "artifact_hashes": {}}

    def short_llm(prompt: str) -> str:
        return '{"title": "好听"}'  # prefixed 【李豆沙】好听 = 7 chars < 12 lower bound

    cover_calls = []
    monkeypatch.setattr(
        shadow_pipeline,
        "_stage_lidousha_ai_cover",
        lambda *args, **kwargs: cover_calls.append((args, kwargs)),
    )

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-short",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=short_llm,
    )

    staging = staged["publish_staging"]
    # Length gate rejects the auto title; even the blocked diagnostic fallback
    # is canonicalized so a later stage cannot accidentally publish it bare.
    assert staging["title"] == "【李豆沙】原始job标题"
    assert staging["title_source"].startswith("job_title(llm_length_out_of_bounds")
    assert staging["title_policy_violations"] == []
    assert staging["status"] == "BLOCKED_TITLE_AUTHORITY"
    assert staging["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert cover_calls == []


def test_invalid_first_title_then_retry_error_still_blocks_before_cover(tmp_path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试\n", encoding="utf-8")
    record = {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(srt), "artifact_hashes": {}}
    calls = 0

    def invalid_then_error(prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"title":"测试现场炸裂到犯规"}'
        raise RuntimeError("retry bridge failed")

    cover_calls = []
    monkeypatch.setattr(
        shadow_pipeline,
        "_stage_lidousha_ai_cover",
        lambda *args, **kwargs: cover_calls.append((args, kwargs)),
    )
    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="talk-retry-error",
        title="原始job标题",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=invalid_then_error,
    )

    assert calls == 2
    assert staged["publish_staging"]["status"] == "BLOCKED_TITLE_AUTHORITY"
    assert staged["publish_staging"]["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert cover_calls == []


def test_selection_hook_anchor_rejects_ascii_late_clause_and_generic_fragment():
    assert not shadow_pipeline._selection_hook_anchor_valid(
        anchor="熊猫头槌",
        selection_hook="弹幕让小李表演上下摇, 后来突然发动熊猫头槌",
        title="熊猫头槌终于来了",
    )
    assert not shadow_pipeline._selection_hook_anchor_valid(
        anchor="弹幕让",
        selection_hook="弹幕让李豆沙表演上下摇…后来发动熊猫头槌",
        title="弹幕让熊猫头槌抢戏",
    )


def test_selection_hook_fallback_preserves_terminal_question_mark():
    title = shadow_pipeline._selection_hook_fallback_title("你们还要来找我玩，好不好？")
    assert title == "【李豆沙】你们还要来找我玩，好不好？"


def test_publish_staging_manual_title_keeps_body_but_gets_publish_envelope(tmp_path):
    # Ivan owns the body; the shared publish layer owns the channel prefix.
    # Automatic-style banned words are not applied retroactively to a human
    # title, but structural rules are never bypassed.
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"placeholder video")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n随便\n", encoding="utf-8")
    record = {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(srt), "artifact_hashes": {}}

    staged = shadow_pipeline._stage_publish_draft(
        record,
        candidate_id="manual-1",
        title="李豆沙唱到炸裂现场",  # no 【李豆沙】 prefix, contains banned 炸裂 + 到炸裂 suffix
        cues=[],
        run_ffmpeg=False,
        title_llm_call=None,
    )

    staging = staged["publish_staging"]
    assert staging["title"] == "【李豆沙】李豆沙唱到炸裂现场"
    assert staging["title_source"] == "job_title"
    assert staging["title_policy_violations"] == []


def test_lidousha_cover_prompt_injects_persona_identity_descriptors():
    prompt = shadow_pipeline._lidousha_cover_prompt(
        title="【李豆沙】豆沙歌，《虫儿飞》温柔清唱", cover_text="《虫儿飞》温柔清唱"
    )
    # English identity gloss keeps the panda character stable across AI redraws.
    assert "panda" in prompt.lower()
    assert "小李" in prompt
    # persona.md 身份/形象 descriptors are injected verbatim (熊猫头/熊猫耳/白毛).
    assert "熊猫" in prompt
    # Composition contract is preserved (16:9 protagonist-centered cover).
    assert "16:9" in prompt
    assert "MULTI-PERSON REFERENCE RULE" in prompt
    assert "labelled 李豆沙" in prompt
    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "4426c60cf3745bad16e7bd5b24504942b28fcc22cf3494f9248b3e5358b5dc45"
    )


# ---------------------------------------------------------------------------
# Persona-driven cover art-direction regression tests (Ivan 2026-07-04):
# covers must rotate (anti-monotony) yet stay reproducible, songs stay clean,
# the CPA art-direction judge fails OPEN and is guard-railed (never 吐舌/sexy),
# and the local title overlay renders a real fail-closed cover with the right
# hook/metadata (and none of the reserved image-gen keys leaking through).
# ---------------------------------------------------------------------------

# A talk title (does NOT start with 【李豆沙】豆沙歌) with no role-lexicon keyword,
# so its background_style comes from the deterministic BUSY rotation.
_COVER_TALK_TITLE = "【李豆沙】被网友追问，我家猫到底叫啥"
_COVER_TALK_TEXT = "被网友追问\n我家猫到底叫啥"
# A 豆沙歌 song title → locked to the clean song layout / calm background pool.
_COVER_SONG_TITLE = "【李豆沙】豆沙歌，《旅行的意义》完整清唱"
_COVER_SONG_TEXT = "旅行的意义\n完整清唱"


def test_cover_art_direction_deterministic_rotation():
    # Same candidate_id → byte-identical art direction (reproducible covers).
    first = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7", title=_COVER_TALK_TITLE, cover_text=_COVER_TALK_TEXT
    )
    second = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7", title=_COVER_TALK_TITLE, cover_text=_COVER_TALK_TEXT
    )
    assert first == second

    # A spread of candidate_ids over one talk title must rotate layout, visual
    # family and hook color (anti-monotony) while staying inside approved sets.
    layouts: set[str] = set()
    backgrounds: set[str] = set()
    hook_colors: set[str] = set()
    for i in range(24):
        direction = shadow_pipeline._lidousha_cover_art_direction(
            candidate_id=f"cand-{i}", title=_COVER_TALK_TITLE, cover_text=_COVER_TALK_TEXT
        )
        assert direction.layout in shadow_pipeline._COVER_TALK_LAYOUTS
        assert direction.background_style in shadow_pipeline._COVER_BG_BUSY
        assert direction.hook_color in shadow_pipeline._COVER_HOOK_COLORS
        layouts.add(direction.layout)
        backgrounds.add(direction.background_style)
        hook_colors.add(direction.hook_color)

    assert len(layouts) >= 2
    assert len(backgrounds) >= 4
    assert len(hook_colors) >= 2
    assert layouts <= set(shadow_pipeline._COVER_TALK_LAYOUTS)


def test_cover_art_direction_batch_slots_force_distinct_background_families():
    directions = [
        shadow_pipeline._lidousha_cover_art_direction(
            candidate_id="same-hash-family",
            title=_COVER_TALK_TITLE,
            cover_text=_COVER_TALK_TEXT,
            diversity_slot=slot,
        )
        for slot in range(6)
    ]

    assert [direction.background_style for direction in directions] == list(
        shadow_pipeline._COVER_BG_BUSY
    )
    assert len({direction.hook_color for direction in directions}) == 6
    assert len({direction.layout for direction in directions}) == 3


def test_cover_art_direction_song_uses_clean_layout():
    song = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="song-1", title=_COVER_SONG_TITLE, cover_text=_COVER_SONG_TEXT
    )
    assert song.is_song is True
    assert song.layout == shadow_pipeline._COVER_SONG_LAYOUT == "song-clean"
    assert song.background_style in shadow_pipeline._COVER_BG_CALM

    talk = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="song-1", title=_COVER_TALK_TITLE, cover_text=_COVER_TALK_TEXT
    )
    assert talk.is_song is False
    assert talk.layout in shadow_pipeline._COVER_TALK_LAYOUTS
    assert talk.background_style in shadow_pipeline._COVER_BG_BUSY


def test_cover_art_direction_fail_open_on_llm_error():
    def raising_llm(_prompt: str) -> str:
        raise RuntimeError("art-direction judge unavailable")

    for title, cover_text, expect_song, expect_layouts in (
        (_COVER_TALK_TITLE, _COVER_TALK_TEXT, False, set(shadow_pipeline._COVER_TALK_LAYOUTS)),
        (_COVER_SONG_TITLE, _COVER_SONG_TEXT, True, {shadow_pipeline._COVER_SONG_LAYOUT}),
    ):
        baseline = shadow_pipeline._lidousha_cover_art_direction(
            candidate_id="cand-7", title=title, cover_text=cover_text
        )
        # A raising judge must NOT propagate — it degrades to the deterministic baseline.
        resolved = shadow_pipeline._lidousha_cover_art_direction(
            candidate_id="cand-7",
            title=title,
            cover_text=cover_text,
            art_direction_llm_call=raising_llm,
        )
        assert resolved == baseline
        assert resolved.is_song is expect_song
        assert resolved.layout in expect_layouts


def test_cover_art_direction_llm_guardrail_strips_tongue_and_cannot_override_rotation():
    baseline = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7", title=_COVER_TALK_TITLE, cover_text=_COVER_TALK_TEXT
    )

    # The judge returns a valid layout/hook color but a forbidden 吐舌/sexy face.
    def unsafe_llm(_prompt: str) -> str:
        payload = {
            "role": "witty_smug",
            "expression_en": "tongue out, sexy wink",
            "layout": "banner",
            "hook_color": "pink",
            "background_style": "halftone-dots",
            "hook_word": "我家猫",
        }
        # Wrapped in prose so we also exercise the first-JSON-object extraction.
        return "这是我的封面艺术指导：" + json.dumps(payload, ensure_ascii=False) + " 完毕"

    resolved = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7",
        title=_COVER_TALK_TITLE,
        cover_text=_COVER_TALK_TEXT,
        art_direction_llm_call=unsafe_llm,
    )

    # Guardrail: the forbidden expression is rejected back to the safe baseline.
    assert resolved.expression_en == baseline.expression_en
    assert "tongue out" not in resolved.expression_en.lower()
    assert "sexy" not in resolved.expression_en.lower()
    # Layout/background/color are batch-diversity axes, so even valid values
    # from the semantic judge cannot override the deterministic rotation.
    assert resolved.layout == baseline.layout
    assert resolved.background_style == baseline.background_style
    assert resolved.hook_color == baseline.hook_color


def test_cover_art_direction_degenerate_llm_cannot_collapse_real_batch():
    """2026-07-16 实案：judge 连续八次偏爱 left/pop/pink，成片封面近乎
    同模板。真实 candidate 批次即使遇到这种退化输出也必须保留轮换。"""

    batch = (
        ("auto_162645_264_391", "【李豆沙】看《魔法少女奈叶》求婚片段彻底上头：求AI续上！"),
        ("auto_162645_394_507", "【李豆沙】本想看“难绷小视频”，熊猫头却为小狗操碎了心"),
        ("auto_203003_1644_1737", "【李豆沙】自称喜欢坏姐姐，却被妹妹型坏女人奴役：我不恋姐"),
        ("auto_203003_272_377", "【李豆沙】游戏苦手想通为何接到商单：用豆沙方式攻略妹妹"),
        ("auto_210002_297_513", "【李豆沙】抽卡惩罚被弹幕定成“为礼墨做0.6”"),
        ("auto_213000_116_205", "【李豆沙】刚想把观众拉进公会，喜提一群打灰帕鲁！"),
        ("auto_213000_1218_1328", "【李豆沙】打灰到生命尽头？周次买下打灰工友？这种事情不要啊"),
        ("auto_162645_712_967", "【李豆沙】‘妈感姐’还是‘妈感妹’？小李把女主播分了个遍"),
    )

    def degenerate_llm(_prompt: str) -> str:
        return json.dumps(
            {
                "role": "shy_cute_default",
                "expression_en": "softly surprised face",
                "layout": "left-split",
                "background_style": "pop-art-burst",
                "hook_color": "pink",
            },
            ensure_ascii=False,
        )

    resolved_directions = []
    for candidate_id, title in batch:
        cover_text = shadow_pipeline._lidousha_cover_text(title)
        baseline = shadow_pipeline._lidousha_cover_art_direction(
            candidate_id=candidate_id,
            title=title,
            cover_text=cover_text,
        )
        resolved = shadow_pipeline._lidousha_cover_art_direction(
            candidate_id=candidate_id,
            title=title,
            cover_text=cover_text,
            art_direction_llm_call=degenerate_llm,
        )
        assert (
            resolved.layout,
            resolved.background_style,
            resolved.hook_color,
        ) == (
            baseline.layout,
            baseline.background_style,
            baseline.hook_color,
        )
        resolved_directions.append(resolved)

    assert len({row.layout for row in resolved_directions}) == 3
    # The real July-16 batch spans five genuinely different style/palette
    # families even when the judge asks for the same blue comic template eight
    # times in a row.
    assert len({row.background_style for row in resolved_directions}) == 5
    assert len({row.hook_color for row in resolved_directions}) >= 3


def test_talk_cover_families_are_not_synonyms_for_one_blue_comic_template():
    phrases = [
        shadow_pipeline._COVER_BG_PHRASES[key].lower()
        for key in shadow_pipeline._COVER_BG_BUSY
    ]
    assert len(phrases) == 6
    assert len(set(phrases)) == 6
    for palette_token in ("cobalt", "coral", "plum", "mint", "ivory", "mustard"):
        assert any(palette_token in phrase for phrase in phrases)


# ---------------------------------------------------------------------------
# Word-aware cover line splitting (Ivan 2026-07-06): the balanced-partition
# wrapper is word-BLIND — it split 小皇帝拒/绝更新 and 吵闹熊/猫头的 mid-word on
# the first unattended batch.  The art-direction LLM now proposes a word-aware
# line split; it is accepted ONLY when provably lossless + renderable, else the
# balancer stays as the fallback.
# ---------------------------------------------------------------------------


def test_validated_cover_lines_accepts_lossless_word_split():
    v = shadow_pipeline._validated_cover_lines
    assert v(["电脑要造反？", "小皇帝", "拒绝更新"], "电脑要造反？小皇帝拒绝更新", hook_word="", max_lines=5) == (
        "电脑要造反？",
        "小皇帝",
        "拒绝更新",
    )


def test_validated_cover_lines_rejects_lossy_or_unrenderable():
    v = shadow_pipeline._validated_cover_lines
    ct = "小皇帝拒绝更新"
    assert v(["小皇帝", "拒绝跟新"], ct, hook_word="", max_lines=5) == ()      # a character was changed
    assert v(["小皇帝拒绝"], ct, hook_word="", max_lines=5) == ()             # characters dropped
    assert v(["拒绝更新", "小皇帝"], ct, hook_word="", max_lines=5) == ()      # reordered
    assert v(["小", "皇", "帝", "拒", "绝", "新"], ct, hook_word="", max_lines=5) == ()  # 6 lines > max
    assert v("小皇帝拒绝更新", ct, hook_word="", max_lines=5) == ()           # not a list
    assert v([], ct, hook_word="", max_lines=5) == ()                        # empty
    assert v(["你们还要来找我玩", "好不好"], "你们还要来找我玩，好不好？", hook_word="好不好", max_lines=5) == ()
    assert v(["你们还要来找我玩", "，好不好？"], "你们还要来找我玩，好不好？", hook_word="好不好", max_lines=5) == ()
    long_line = "一二三四五六七八九十甲乙丙"  # 13 chars — over the single-line budget
    assert v([long_line], long_line, hook_word="", max_lines=5) == ()


def test_validated_cover_lines_keeps_song_and_hook_whole():
    v = shadow_pipeline._validated_cover_lines
    ct = "吵闹熊猫头的《嘉宾》"
    # 《song》split across lines → rejected (must stay whole on one line)
    assert v(["吵闹熊猫头的《嘉", "宾》"], ct, hook_word="《嘉宾》", max_lines=5) == ()
    assert v(["吵闹熊猫头的", "《嘉宾》"], ct, hook_word="《嘉宾》", max_lines=5) == ("吵闹熊猫头的", "《嘉宾》")
    # the highlighted hook word broken across lines → rejected (its color would tear)
    assert v(["小李当场反", "杀"], "小李当场反杀", hook_word="反杀", max_lines=5) == ()


def test_cover_wrapping_keeps_short_quoted_catchphrase_whole():
    text = "为什么提到我就要“最最最喜欢”？"
    quoted = "“最最最喜欢”"

    v_lines = shadow_pipeline._validated_cover_lines
    assert v_lines(
        ["为什么提到我就要“最最最喜", "欢”？"],
        text,
        hook_word="",
        max_lines=5,
    ) == ()

    v_words = shadow_pipeline._validated_cover_words
    assert v_words(
        ["为什么", "提到我", "就要“", "最最最喜欢", "”？"],
        text,
        hook_word="",
    ) == ()

    lines = shadow_pipeline._wrap_even(text, 4)
    assert "".join(lines) == text
    assert any(quoted in line for line in lines), lines
    assert shadow_pipeline._split_wide_atom(quoted, 4.0) == [quoted]


def test_validated_cover_words_accepts_lossless_segmentation():
    """Ivan 2026-07-10: the FULL title stays on the cover; the font grows via
    MANY line breaks and a break may fall anywhere EXCEPT inside a word / hook /
    proper noun — so the LLM's word segmentation becomes the wrap atoms."""
    v = shadow_pipeline._validated_cover_words
    words = ["沙豆李", "沉浸在", "指认", "上下左右", "和", "疯狂摇头", "之中，", "完全", "听不到", "礼墨", "的", "声音"]
    text = "沙豆李沉浸在指认上下左右和疯狂摇头之中，完全听不到礼墨的声音"
    assert v(words, text, hook_word="听不到") == tuple(words)


def test_validated_cover_words_rejects_lossy_or_hook_splitting():
    v = shadow_pipeline._validated_cover_words
    text = "沙豆李完全听不到礼墨的声音"
    assert v(["沙豆李", "完全", "听不到", "礼墨的"], text, hook_word="") == ()  # drops 声音
    assert v(["沙豆李", "完全", "听不", "到礼墨的声音"], text, hook_word="听不到") == ()  # hook split
    assert v("not a list", text, hook_word="") == ()
    assert v([], text, hook_word="") == ()
    punctuation_text = "你们还要来找我玩，好不好？"
    assert v(["你们还要来找我玩", "，好不好？"], punctuation_text, hook_word="好不好") == ()


def test_wrap_even_packs_word_atoms_without_splitting():
    lines = shadow_pipeline._wrap_even(
        "沙豆李沉浸在疯狂摇头之中完全听不到礼墨的声音",
        4,
        keep=("沙豆李", "沉浸在", "疯狂摇头", "之中", "完全", "听不到", "礼墨", "的", "声音"),
    )
    assert 2 <= len(lines) <= 4
    assert "".join(lines) == "沙豆李沉浸在疯狂摇头之中完全听不到礼墨的声音"
    for atom in ("沙豆李", "疯狂摇头", "听不到", "礼墨"):
        assert any(atom in line for line in lines), (atom, lines)  # atom never split across lines


def test_wrap_even_preserves_visible_punctuation_and_never_strands_it():
    text = "去彩排前连问三遍你们还要来找我玩，好不好？"
    lines = shadow_pipeline._wrap_even(text, 5, keep=("好不好",))
    assert "".join(lines) == text
    assert all(not line.startswith(tuple("，,、；;！!？?。")) for line in lines)
    assert lines[-1].endswith("？")
    assert any(line.endswith("，") for line in lines)


def test_fit_cover_lines_full_title_grows_with_raised_line_budget():
    """The old max_lines=5 cap pinned a 42-char side-zone title at ~73px; the
    raised budget lets the fitter use more, shorter lines — same FULL text,
    visibly bigger font (Ivan 2026-07-10: 多换行放大, 绝不丢字)."""
    text = "最吵闹的黑白小猪成为了聋人只能不知所措的大喊我聋了进入游戏十分钟都没搞清状况"
    atoms = ("最吵闹的", "黑白小猪", "成为了", "聋人", "只能", "不知所措的", "大喊", "我聋了",
             "进入游戏", "十分钟", "都没", "搞清", "状况")
    font_path = shadow_pipeline._cover_font_for_text(text)
    zone = shadow_pipeline._COVER_LAYOUT_RENDER["left-split"]["zone"]

    def max_font(budget):
        lines = shadow_pipeline._fit_cover_lines(
            text, hook_word="我聋了", base_fill=shadow_pipeline._COVER_BASE_FILL,
            hook_rgb=(255, 82, 82), zone=zone, font_path=font_path,
            max_lines=budget, max_size=360, word_atoms=atoms,
        )
        assert "".join("".join(seg[0] for seg in line["segs"]) for line in lines) == text  # full title kept
        return max(line["size"] for line in lines)

    assert shadow_pipeline._COVER_LAYOUT_RENDER["left-split"]["max_lines"] >= 8
    assert shadow_pipeline._COVER_LAYOUT_RENDER["banner"]["max_lines"] >= 4
    assert max_font(8) > max_font(5) * 1.15  # the raised budget buys real size


def test_fit_cover_lines_resplits_wide_llm_atom_instead_of_pinning_small():
    """7/11 河粉封面实案：LLM 把 “要交780吗”？ 整段当一个词、hook 又是其中的
    780，强调行被 ~7.6em 原子钉在 90px（同批其他封面 146-182px）。fitter 必须
    把超宽原子按词内安全点再分，字号回到可读档，且不丢一个字。"""
    text = "河粉小姐姐合照问“要交780吗”？说完免费她后悔了"
    atoms = ("河粉小姐姐", "合照", "问", "“要交780吗”？", "说完", "免费", "她", "后悔了")
    font_path = shadow_pipeline._cover_font_for_text(text)
    zone = shadow_pipeline._COVER_LAYOUT_RENDER["left-split"]["zone"]
    lines = shadow_pipeline._fit_cover_lines(
        text, hook_word="780", base_fill=shadow_pipeline._COVER_BASE_FILL,
        hook_rgb=(255, 200, 60), zone=zone, font_path=font_path,
        max_lines=8, max_size=360, word_atoms=atoms,
    )
    assert "".join("".join(seg[0] for seg in line["segs"]) for line in lines) == text
    assert max(line["size"] for line in lines) >= shadow_pipeline._COVER_MIN_EMPH
    for line in lines:
        joined = "".join(seg[0] for seg in line["segs"])
        assert joined[0] not in shadow_pipeline._COVER_CLOSING_PUNCT
        assert joined[-1] not in shadow_pipeline._COVER_OPENING_PUNCT


def test_split_wide_atom_binds_punctuation_and_keeps_hook():
    parts = shadow_pipeline._split_wide_atom("“要交780吗”？", 4.6, protect="780")
    assert "".join(parts) == "“要交780吗”？"
    assert len(parts) >= 2
    assert any("780" in p for p in parts)  # hook 不被拆
    for p in parts:
        assert p[0] not in shadow_pipeline._COVER_CLOSING_PUNCT
        assert p[-1] not in shadow_pipeline._COVER_OPENING_PUNCT


def test_split_wide_atom_noop_when_it_fits():
    assert shadow_pipeline._split_wide_atom("宿敌恋人", 5.0) == ["宿敌恋人"]


def test_cover_text_strips_song_parenthetical_qualifier():
    got = shadow_pipeline._lidousha_cover_text(
        "【李豆沙】豆沙歌，直播间唱《恋爱告急 (2021浙江卫视跨年演唱会)》"
    )
    assert "《恋爱告急》" in got
    assert "2021" not in got
    # 非歌名括号不受影响
    plain = shadow_pipeline._lidousha_cover_text("【李豆沙】被问(超小声)为什么")
    assert plain == "被问(超小声)为什么"
    assert "(超小声)" in plain


def test_normalize_cover_art_direction_fills_word_aware_line_breaks():
    title = "【李豆沙】电脑要造反？小皇帝拒绝更新"
    cover_text = "电脑要造反？小皇帝拒绝更新"

    def llm(_prompt: str) -> str:
        return json.dumps(
            {
                "role": "witty_smug",
                "expression_en": "clever pleased grin",
                "layout": "banner",
                "hook_color": "yellow",
                "background_style": "halftone-dots",
                "hook_word": "",
                "lines": ["电脑要造反？", "小皇帝", "拒绝更新"],
            },
            ensure_ascii=False,
        )

    resolved = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-lines", title=title, cover_text=cover_text, art_direction_llm_call=llm
    )
    assert resolved.line_breaks == ("电脑要造反？", "小皇帝", "拒绝更新")

    # No judge → no forced split → the balancer fallback (line_breaks empty).
    baseline = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-lines", title=title, cover_text=cover_text
    )
    assert baseline.line_breaks == ()


def test_normalize_cover_art_direction_rejects_lossy_llm_lines():
    title = "【李豆沙】电脑要造反？小皇帝拒绝更新"
    cover_text = "电脑要造反？小皇帝拒绝更新"

    def llm(_prompt: str) -> str:  # drops 新 → must fall back to the balancer
        return json.dumps(
            {
                "role": "witty_smug",
                "expression_en": "clever pleased grin",
                "layout": "banner",
                "hook_color": "yellow",
                "background_style": "halftone-dots",
                "hook_word": "",
                "lines": ["电脑要造反？", "小皇帝", "拒绝更"],
            },
            ensure_ascii=False,
        )

    resolved = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-lossy", title=title, cover_text=cover_text, art_direction_llm_call=llm
    )
    assert resolved.line_breaks == ()  # lossy split refused; balancer still runs


def test_overlay_honors_word_aware_lines_without_midword_break(tmp_path):
    from PIL import Image

    bg = tmp_path / "bg.png"
    Image.new("RGB", (1920, 1080), (40, 80, 160)).save(bg)
    out = tmp_path / "cover.png"
    art_direction = shadow_pipeline.LidoushaCoverArtDirection(
        role="witty_smug",
        expression_en="clever grin",
        background_style="halftone-dots",
        layout="banner",
        hook_color="yellow",
        is_song=False,
        hook_word="",
        line_breaks=("电脑要造反？", "小皇帝", "拒绝更新"),
    )
    meta = shadow_pipeline._overlay_lidousha_cover_title(
        bg, out, cover_text="电脑要造反？小皇帝拒绝更新", art_direction=art_direction
    )
    assert out.is_file()
    assert meta["line_split"] == "llm_word_aware"
    rendered = meta["rendered_lines"]
    # lossless: the rendered lines concatenate back to the cover text in order
    assert "".join(rendered) == "电脑要造反？小皇帝拒绝更新"
    # NO word is torn across lines — each stays whole within a single line (the
    # fitter may regroup the LLM lines for a bigger font, but never splits a word).
    for word in ("电脑", "造反", "小皇帝", "拒绝", "更新", "拒绝更新"):
        assert any(word in line for line in rendered), f"{word} was split across lines: {rendered}"


def test_overlay_falls_back_to_balancer_without_forced_lines(tmp_path):
    from PIL import Image

    bg = tmp_path / "bg.png"
    Image.new("RGB", (1920, 1080), (40, 80, 160)).save(bg)
    out = tmp_path / "cover.png"
    art_direction = shadow_pipeline.LidoushaCoverArtDirection(
        role="shy_cute_default",
        expression_en="soft smile",
        background_style="halftone-dots",
        layout="banner",
        hook_color="yellow",
        is_song=False,
        hook_word="",
        line_breaks=(),
    )
    meta = shadow_pipeline._overlay_lidousha_cover_title(
        bg, out, cover_text="电脑要造反？小皇帝拒绝更新", art_direction=art_direction
    )
    assert out.is_file()
    assert meta["line_split"] == "balancer"  # no forced split, no colon → balancer path


# ---------------------------------------------------------------------------
# 封面梗字（2026-07-20 B站高播放封面调研）：自动标题封面渲染 2-12 字梗字，
# 不再整条标题上封面；手定标题/歌切保持旧行为。
# ---------------------------------------------------------------------------

_PUNCH_TITLE = "【李豆沙】精心设计MC环节想让kmx介绍自己，是奶P！才，才不是熊猫呢！"
_PUNCH_TEXT = "精心设计MC环节想让kmx介绍自己，是奶P！才，才不是熊猫呢！"


def test_cover_punch_deterministic_baseline_and_gates():
    from src.autoslice import cover_generation

    # 确定性兜底链①：取最后一个 ≤12 字的 ！/？ 完整分句（点睛尾惯例）。
    assert cover_generation._cover_default_punch(_PUNCH_TEXT) == ("才不是熊猫呢！",)
    # 链②：引号内 4-12 字梗词（2026-07-21 二期：LLM 保守给 null 的实测案例）。
    assert cover_generation._cover_default_punch("抽卡惩罚被弹幕定成“为礼墨做0.6”") == ("为礼墨做0.6",)
    assert cover_generation._cover_default_punch(
        "发1支持沙豆李、发0支持李豆沙，“为什么要这样说！”赶紧改成2"
    ) == ("“为什么要这样说！”",)
    # 链③：最后一个 4-12 字普通分句（引号词太短时跳过链②）。
    assert cover_generation._cover_default_punch("‘妈感姐’还是‘妈感妹’？小李把女主播分了个遍") == ("小李把女主播分了个遍",)
    assert cover_generation._cover_default_punch("游戏苦手想通为何接到商单\n用豆沙方式攻略妹妹") == ("用豆沙方式攻略妹妹",)
    # 全链无命中 → () （单一超长分句、无引号、无词库钩子）。
    assert cover_generation._cover_default_punch("温情李姐下播后说了很多很多的心里话啊") == ()

    # allow_punch 默认关（老调用路径字节不变）。
    off = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7", title=_PUNCH_TITLE, cover_text=_PUNCH_TEXT
    )
    assert off.cover_punch == ()
    # 自动标题开启但 CPA 文字裁决不可用 → 不放行确定性碎片，退回完整文案。
    on = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7", title=_PUNCH_TITLE, cover_text=_PUNCH_TEXT, allow_punch=True
    )
    assert on.cover_punch == ()
    assert on.cover_punch_semantic_review["reason_code"] == (
        "CPA_TEXT_REVIEW_UNAVAILABLE"
    )
    # 歌切永远不用梗字（裸《歌名》banner 已是终态）。
    song = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="song-1", title=_COVER_SONG_TITLE, cover_text=_COVER_SONG_TEXT, allow_punch=True
    )
    assert song.cover_punch == ()


def test_cover_punch_llm_pick_is_source_bound():
    from src.autoslice import cover_generation

    # LLM 选中合法梗字（文案逐字片段）→ 采用（main+sub 两行）。
    def judge_ok(_prompt: str) -> str:
        if "最终文字语义裁决者" in _prompt:
            return (
                '{"schema_version":"lidousha-cover-punch-semantic-review.v1",'
                '"status":"PASS",'
                '"final_punch":{"main":"才，才不是熊猫呢！","sub":"是奶P！"},'
                '"stranger_can_infer_event":true,'
                '"contains_concrete_subject":true,'
                '"contains_action_or_conflict":true,'
                '"story_summary":"她嘴硬否认自己是熊猫并自称奶P",'
                '"click_motivation":"身份反差和嘴硬原话让人想看前因后果"}'
            )
        return (
            '{"role":"stubborn_pout","expression_en":"pouty defiant frown",'
            '"hook_word":"熊猫","words":[],"lines":[],'
            '"cover_punch":{"main":"才，才不是熊猫呢！","sub":"是奶P！"}}'
        )

    picked = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7",
        title=_PUNCH_TITLE,
        cover_text=_PUNCH_TEXT,
        art_direction_llm_call=judge_ok,
        allow_punch=True,
    )
    assert picked.cover_punch == ("才，才不是熊猫呢！", "是奶P！")

    # 语义行也必须是可直接渲染的物理行；不得先通过 CPA，
    # 再让 renderer 从词中间二次断行。
    assert cover_generation._validated_cover_punch(
        {"main": "精心设计MC环节想让k", "sub": None}, _PUNCH_TEXT
    ) == ()

    # 编造的字（不在文案里）→ 拒绝 → 回退确定性兜底。
    def judge_fabricated(_prompt: str) -> str:
        if "最终文字语义裁决者" in _prompt:
            return (
                '{"schema_version":"lidousha-cover-punch-semantic-review.v1",'
                '"status":"PASS",'
                '"final_punch":{"main":"才不是熊猫呢！","sub":null},'
                '"stranger_can_infer_event":true,'
                '"contains_concrete_subject":true,'
                '"contains_action_or_conflict":true,'
                '"story_summary":"她用原话嘴硬否认自己是熊猫",'
                '"click_motivation":"强烈否认形成身份反差并引出前因"}'
            )
        return (
            '{"role":"stubborn_pout","expression_en":"pouty defiant frown",'
            '"hook_word":"熊猫","words":[],"lines":[],'
            '"cover_punch":{"main":"你不许玩谐音梗","sub":null}}'
        )

    rejected = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7",
        title=_PUNCH_TITLE,
        cover_text=_PUNCH_TEXT,
        art_direction_llm_call=judge_fabricated,
        allow_punch=True,
    )
    assert rejected.cover_punch == ("才不是熊猫呢！",)

    # 超长（>12 字）同样拒绝 → 兜底。
    assert cover_generation._validated_cover_punch(
        {"main": "精心设计MC环节想让kmx介绍自己", "sub": None}, _PUNCH_TEXT
    ) == ()
    # 未开启 allow_punch 时 LLM 字段被忽略（手定标题防线）。
    ignored = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="cand-7",
        title=_PUNCH_TITLE,
        cover_text=_PUNCH_TEXT,
        art_direction_llm_call=judge_ok,
    )
    assert ignored.cover_punch == ()


def test_cover_punch_cpa_repairs_real_raw_beans_fragmentation():
    from src.autoslice import cover_generation

    title = (
        "【李豆沙】听说安晚也吃了生豆角，熊猫头下播就去暗示礼墨，"
        "三人组必须团结有默契！"
    )
    cover_text = title.removeprefix("【李豆沙】")
    story_hook = (
        "听说安晚也吃了生豆角，李豆沙震惊之余决定下播暗示礼墨也吃，"
        "誓要用集体中招维护三人组的“团结默契”。"
    )

    def judge(prompt: str) -> str:
        if "最终文字语义裁决者" in prompt:
            return (
                '{"schema_version":"lidousha-cover-punch-semantic-review.v1",'
                '"status":"REVISE",'
                '"final_punch":{"main":"安晚也吃了生豆角","sub":"三人组必须团结"},'
                '"stranger_can_infer_event":true,'
                '"contains_concrete_subject":true,'
                '"contains_action_or_conflict":true,'
                '"story_summary":"安晚吃了生豆角后她把集体中招说成三人组团结",'
                '"click_motivation":"食物中毒和团结口号的荒诞反差让人想看她如何圆场"}'
            )
        return (
            '{"role":"witty_smug","expression_en":"mischievous smile",'
            '"hook_word":"生豆角","scene_props":["raw green beans"],'
            '"words":[],"lines":[],'
            '"cover_punch":{"main":"生豆角","sub":"熊猫头下播"}}'
        )

    direction = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="auto_183122_1209_1410",
        title=title,
        cover_text=cover_text,
        story_hook=story_hook,
        art_direction_llm_call=judge,
        allow_punch=True,
    )

    assert direction.cover_punch == (
        "安晚也吃了生豆角",
        "三人组必须团结",
    )
    review = direction.cover_punch_semantic_review
    assert review["status"] == "REVISED"
    assert cover_generation.validate_cover_punch_semantic_review(
        review,
        rendered_lines=list(direction.cover_punch),
        cover_text=cover_text,
        story_hook=story_hook,
    )
    assert not cover_generation.validate_cover_punch_semantic_review(
        review,
        rendered_lines=["生豆角", "熊猫头下播"],
        cover_text=cover_text,
        story_hook=story_hook,
    )
    assert not cover_generation.validate_cover_punch_semantic_review(
        review,
        rendered_lines=list(direction.cover_punch),
        cover_text=cover_text,
        story_hook=story_hook + "（伪造）",
    )


def test_cover_punch_cpa_keeps_pink_sister_phrase_as_one_physical_line(tmp_path):
    from PIL import Image

    from src.autoslice import cover_generation

    title = (
        "【李豆沙】小李被粉色小姐姐布下迷魂阵仍然逞强自己相对礼墨是0.6，"
        "突然想起来绝望大喊「我是侄女啊」"
    )
    cover_text = title.removeprefix("【李豆沙】")
    story_hook = "她被粉色小姐姐迷住后还在算CP数值，想起辈分后绝望大喊。"

    def judge(prompt: str) -> str:
        if "最终文字语义裁决者" in prompt:
            assert "不能依赖渲染器在词中间二次断行" in prompt
            return (
                '{"schema_version":"lidousha-cover-punch-semantic-review.v1",'
                '"status":"REVISE",'
                '"final_punch":{"main":"小姐姐布下迷魂阵","sub":"我是侄女啊"},'
                '"stranger_can_infer_event":true,'
                '"contains_concrete_subject":true,'
                '"contains_action_or_conflict":true,'
                '"story_summary":"小姐姐布下迷魂阵后她想起自己的侄女辈分",'
                '"click_motivation":"迷魂阵和侄女辈分的突然反转值得点开"}'
            )
        return (
            '{"role":"shocked_flustered","expression_en":"shocked face",'
            '"hook_word":"侄女","words":[],"lines":[],'
            '"cover_punch":{"main":"被粉色小姐姐布下迷魂阵","sub":"我是侄女啊"}}'
        )

    direction = shadow_pipeline._lidousha_cover_art_direction(
        candidate_id="auto_162016_20_319",
        title=title,
        cover_text=cover_text,
        story_hook=story_hook,
        art_direction_llm_call=judge,
        allow_punch=True,
    )
    assert direction.cover_punch == ("小姐姐布下迷魂阵", "我是侄女啊")

    bg = tmp_path / "bg.png"
    out = tmp_path / "cover.png"
    Image.new("RGB", (1920, 1080), (40, 80, 160)).save(bg)
    meta = shadow_pipeline._overlay_lidousha_cover_title(
        bg,
        out,
        cover_text=cover_text,
        art_direction=direction,
    )
    assert meta["rendered_lines"] == ["小姐姐布下迷魂阵", "我是侄女啊"]
    assert meta["rendered_lines"] == direction.cover_punch_semantic_review[
        "final_punch"
    ]
    assert cover_generation.validate_cover_punch_semantic_review(
        direction.cover_punch_semantic_review,
        rendered_lines=meta["rendered_lines"],
        cover_text=cover_text,
        story_hook=story_hook,
    )


def test_cover_punch_rejects_fragment_cut_before_quoted_object():
    from src.autoslice import cover_generation

    title = (
        "【李豆沙】观众想让新3D永久保留“白色奶龙”表情，"
        "小李拒绝花钱"
    )
    cover_text = title.removeprefix("【李豆沙】")
    story_hook = "观众要求新3D永久保留白色奶龙表情，她因为要花钱而拒绝。"

    assert cover_generation._validated_cover_punch(
        {"main": "让新3D永久保留", "sub": "小李拒绝花钱"},
        cover_text,
    ) == ()
    assert cover_generation._validated_cover_punch(
        {"main": "“白色奶龙”表情", "sub": "小李拒绝花钱"},
        cover_text,
    ) == ("“白色奶龙”表情", "小李拒绝花钱")

    def judge(prompt: str) -> str:
        assert "不得在左括号前截断" in prompt
        return (
            '{"schema_version":"lidousha-cover-punch-semantic-review.v1",'
            '"status":"REVISE",'
            '"final_punch":{"main":"让新3D永久保留","sub":"小李拒绝花钱"},'
            '"stranger_can_infer_event":true,'
            '"contains_concrete_subject":true,'
            '"contains_action_or_conflict":true,'
            '"story_summary":"观众让新3D保留表情但小李拒绝花钱",'
            '"click_motivation":"永久保留表情和花钱之间的冲突值得点开"}'
        )

    reviewed, proof = cover_generation.review_cover_punch_semantics(
        title=title,
        cover_text=cover_text,
        story_hook=story_hook,
        punch=("小李拒绝花钱",),
        llm_call=judge,
        punch_validator=cover_generation._validated_cover_punch,
    )
    assert reviewed == ()
    assert proof["status"] == "FAILED"
    assert proof["reason_code"] == "CPA_PUNCH_SEMANTIC_REVIEW_REJECTED"


def test_cover_punch_renderer_fails_closed_instead_of_midword_wrap():
    from src.autoslice import cover_generation

    assert cover_generation._punch_wrap("小姐姐布下迷魂阵") == ["小姐姐布下迷魂阵"]
    with pytest.raises(ValueError, match="COVER_PUNCH_LINE_REQUIRES_CPA_REVISE"):
        cover_generation._punch_wrap("被粉色小姐姐布下迷魂阵")


def test_overlay_renders_punch_instead_of_full_text(tmp_path):
    from PIL import Image

    bg = tmp_path / "bg.png"
    Image.new("RGB", (1920, 1080), (40, 80, 160)).save(bg)
    out = tmp_path / "cover.png"
    art_direction = shadow_pipeline.LidoushaCoverArtDirection(
        role="stubborn_pout",
        expression_en="pouty defiant frown",
        background_style="halftone-dots",
        layout="banner",
        hook_color="orange",
        is_song=False,
        hook_word="熊猫",
        cover_punch=("才，才不是熊猫呢！", "是奶P！"),
    )
    meta = shadow_pipeline._overlay_lidousha_cover_title(
        bg, out, cover_text=_PUNCH_TEXT, art_direction=art_direction
    )
    assert out.is_file()
    assert meta["line_split"] == "punch"
    assert meta["cover_text_mode"] == "punch"
    assert meta["cover_punch"] == ["才，才不是熊猫呢！", "是奶P！"]
    assert meta["rendered_lines"] == ["才，才不是熊猫呢！", "是奶P！"]
    rendered = "".join(meta["rendered_lines"])
    # 渲染的只有梗字，绝不是整条文案。
    assert "才不是熊猫呢！" in rendered and "是奶P！" in rendered
    assert "精心设计" not in rendered
    # 梗字必须渲染得大（整段文案时代 banner 里 30+ 字会被压小）。
    assert meta["font_size"] >= 120


def test_rotated_punch_pixels_are_clamped_to_feed_safe_zone(tmp_path):
    """Regression: the 7/22 hotpot cover exceeded x=1660 by one pixel only
    after its -2° title layer was rotated, so staging succeeded but the final
    delivery gate rejected the otherwise valid cover."""

    from PIL import Image

    from src.autoslice.cover_route_evidence import (
        validate_rendered_text_pixel_evidence,
    )
    from src.autoslice.cover_title_rendering import (
        FEED_SAFE_X0,
        FEED_SAFE_X1,
    )

    bg = tmp_path / "bg.png"
    Image.new("RGB", (1920, 1080), (40, 80, 160)).save(bg)
    out = tmp_path / "cover.png"
    art_direction = shadow_pipeline.LidoushaCoverArtDirection(
        role="witty_smug",
        expression_en="cheeky smile",
        background_style="violet-neon-stage",
        layout="banner",
        hook_color="purple",
        is_song=False,
        hook_word="火锅",
        cover_punch=("请南町吃火锅", "刚认识就互相霸凌"),
    )

    meta = shadow_pipeline._overlay_lidousha_cover_title(
        bg,
        out,
        cover_text=(
            "弹幕追问李豆沙为何请南町吃火锅，从“付出劳动”嘴硬到"
            "“最最喜欢”，刚认识就互相霸凌"
        ),
        art_direction=art_direction,
    )

    bbox = meta["rendered_text_pixels"]["text_pixel_bbox"]
    assert bbox[0] >= FEED_SAFE_X0
    assert bbox[2] <= FEED_SAFE_X1
    assert validate_rendered_text_pixel_evidence(
        {
            **meta,
            "final_cover_sha256": meta["rendered_text_pixels"][
                "final_cover_sha256"
            ],
        }
    )


def test_overlay_rejects_talk_title_that_repeats_the_known_91px_failure(tmp_path):
    """2026-07-22 regression: a technically intact 91px split title was still
    unreadably small next to the character and must never become a cover."""
    from PIL import Image

    bg = tmp_path / "bg.png"
    Image.new("RGB", (1920, 1080), (20, 90, 210)).save(bg)
    out = tmp_path / "cover.png"
    art_direction = shadow_pipeline.LidoushaCoverArtDirection(
        role="witty_smug",
        expression_en="mischievous smirk",
        background_style="cobalt-comic-burst",
        layout="left-split",
        hook_color="yellow",
        is_song=False,
        hook_word="最最最喜欢",
    )

    with pytest.raises(ValueError, match="COVER_TITLE_TOO_SMALL"):
        shadow_pipeline._overlay_lidousha_cover_title(
            bg,
            out,
            cover_text="为什么提到我\n就要“最最最喜欢”？",
            art_direction=art_direction,
        )
    assert not out.exists()


def test_screenshot_direct_cover_skips_cpa_and_needs_no_creds(tmp_path, monkeypatch):
    """screenshot 可零 CPA 出图，但无文字裁决时必须渲染完整文案。"""

    from PIL import Image

    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.delenv("AUTOSLICE_COVER_REF_MS", raising=False)
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "screenshot")
    media = _write_synthetic_performance_clip(tmp_path)

    def forbidden_image_edit(**_kwargs):
        raise AssertionError("screenshot mode must not call CPA image edit")

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="shot-1",
        title="【李豆沙】才，才不是熊猫呢！小李被kmx用两个字点名",
        cover_text="才，才不是熊猫呢！小李被kmx用两个字点名",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=forbidden_image_edit,
        punch_allowed=True,
    )
    assert result["status"] == "AI_COVER_READY", result
    generation = result["cover_generation"]
    assert generation["cover_mode"] == "screenshot"
    assert generation["method"] == "screenshot_direct"
    assert generation["cover_text_mode"] == "full"
    assert generation["art_direction"][
        "cover_punch_semantic_review"
    ]["reason_code"] == "CPA_TEXT_REVIEW_UNAVAILABLE"
    assert generation["reference_selection"]["status"] == "SELECTED"
    assert generation["screenshot_graphic_poster"]["status"] == "COMPOSED"
    assert generation["screenshot_graphic_poster"]["background_style"] == generation["background_style"]
    assert Path(str(generation["ai_background"])).name == "shot-1.screenshot-poster.png"
    assert Path(str(result["cover_path"])).is_file()
    assert Image.open(str(result["cover_path"])).size == (1920, 1080)
    route = generation["route_decision"]
    assert route["schema_version"] == "lidousha-cover-route-decision.v2"
    assert route["selected_rationale"] == "mode=screenshot (forced)"
    assert route["required_participant_ids"] == []
    assert route["source_visible_participant_ids"] == []
    assert route["image_generation_planned"] is False
    assert route["image_generation_attempted"] is False
    assert route["image_generation_used"] is False
    assert route["actual_treatment"] == "screenshot_direct"
    assert route["execution_status"] == "READY"
    assert {row["treatment"] for row in route["alternatives"]} == {
        "screenshot_direct",
        "screenshot_polish",
        "cpa_redraw",
    }
    assert len(route["rejected_alternatives"]) == 2
    assert all(
        row["rejected_reason"] for row in route["rejected_alternatives"]
    )
    from src.autoslice.cover_route_evidence import (
        validate_cover_route_decision,
    )

    assert validate_cover_route_decision(generation)


def test_screenshot_materialization_failure_blocks_without_calling_ai(
    tmp_path, monkeypatch
):
    """Once screenshot is selected, its failure cannot authorize a CPA redraw."""

    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "screenshot")
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    media = _write_synthetic_performance_clip(tmp_path)
    calls = {"image_edit": 0}

    def fail_screenshot(*_args, **_kwargs):
        raise RuntimeError("synthetic crop failure")

    def forbidden_image_edit(**_kwargs):
        calls["image_edit"] += 1
        raise AssertionError("screenshot failure must not call CPA")

    monkeypatch.setattr(
        publish_staging, "extract_zoomed_cover_frame", fail_screenshot
    )
    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="shot-fails-closed",
        title="【李豆沙】截图失败不能偷偷改画风",
        cover_text="截图失败不能偷偷改画风",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=forbidden_image_edit,
        punch_allowed=True,
    )

    assert calls["image_edit"] == 0
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED" in result["reason_codes"]
    generation = result["cover_generation"]
    assert generation["screenshot_direct"]["status"] == "BLOCKED"
    assert generation["method"] == "screenshot_direct"
    assert generation["model"] == "none"
    assert generation.get("cover_origin") != "AI_REDRAW"
    route = generation["route_decision"]
    assert route["selected_treatment"] == "screenshot_direct"
    assert route["actual_treatment"] is None
    assert route["execution_status"] == "BLOCKED"
    assert route["image_generation_attempted"] is False
    assert route["image_generation_used"] is False
    assert "synthetic crop failure" in route["execution_detail"]


def test_manual_title_can_finish_on_screenshot_without_hidden_cpa_fallback(
    tmp_path, monkeypatch
):
    """手定标题锁的是完整文字，不得再通过 no-punch 间接强制 AI 重绘。"""

    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "screenshot")
    media = _write_synthetic_performance_clip(tmp_path)
    manual_cover_text = "最包容异性恋的直播间，看到男角色只能说出一句不熟"

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="manual-shot",
        title="【李豆沙】" + manual_cover_text,
        cover_text=manual_cover_text,
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual screenshot must not call CPA redraw")
        ),
        punch_allowed=False,
    )

    assert result["status"] == "AI_COVER_READY", result
    generation = result["cover_generation"]
    assert generation["method"] == "screenshot_direct"
    assert generation["cover_text"] == manual_cover_text
    assert generation["cover_text_mode"] == "full"
    assert generation["route_decision"]["reason"] == "mode=screenshot (forced)"


def test_screenshot_poster_materializes_six_distinct_background_families(tmp_path):
    """The diversity slot must change pixels, not only metadata."""

    from PIL import Image, ImageStat

    from src.autoslice.cover_generation import (
        LidoushaCoverArtDirection,
        _COVER_BG_BUSY,
    )
    from src.autoslice.cover_screenshot_poster import (
        _compose_screenshot_poster_background,
    )

    source = tmp_path / "same-live-frame.png"
    Image.new("RGB", (1920, 1080), (118, 126, 136)).save(source)
    top_band_means = []
    for slot, style in enumerate(_COVER_BG_BUSY):
        output = tmp_path / f"poster-{slot}.png"
        evidence = _compose_screenshot_poster_background(
            source,
            output,
            art_direction=LidoushaCoverArtDirection(
                role="shy_cute_default",
                expression_en="soft smile",
                background_style=style,
                layout="banner",
                hook_color="yellow",
                is_song=False,
                cover_punch=("真实名场面",),
            ),
        )
        assert evidence["status"] == "COMPOSED"
        assert evidence["background_style"] == style
        with Image.open(output) as rendered:
            assert rendered.size == (1920, 1080)
            top_band_means.append(
                tuple(round(value, 1) for value in ImageStat.Stat(rendered.crop((0, 0, 1920, 250))).mean)
            )

    assert len(set(top_band_means)) == len(_COVER_BG_BUSY)


def test_cover_treatment_router_by_moment_strength():
    from src.autoslice import publish_staging

    def sel(score, emo=0.0, subject=True, dispersion=None):
        return {
            "candidates": [{"score": score, "emotion": emo}],
            "subject_confident": subject,
            "motion_dispersion_frac": dispersion,
        }

    decide = publish_staging._decide_cover_treatment
    # 强名场面 → 直出；中等 → 轻微调；弱 → 全图重绘。
    assert decide(cover_mode="auto", is_song=False, punch_allowed=True, frame_selection=sel(5.2))[0] == "screenshot_direct"
    assert decide(cover_mode="auto", is_song=False, punch_allowed=True, frame_selection=sel(3.4, 1.0))[0] == "screenshot_direct"
    assert decide(cover_mode="auto", is_song=False, punch_allowed=True, frame_selection=sel(3.4))[0] == "screenshot_polish"
    assert decide(cover_mode="auto", is_song=False, punch_allowed=True, frame_selection=sel(1.9))[0] == "cpa_redraw"
    assert decide(cover_mode="auto", is_song=False, punch_allowed=True, frame_selection=sel(8.8, subject=False))[0] == "cpa_redraw"
    # A local motion blob can look subject-like while a game UI moves across
    # most of the canvas.  This witnessed 2026-07-22 geometry must redraw a
    # large face instead of preserving a mostly empty game screenshot.
    assert decide(
        cover_mode="auto",
        is_song=False,
        punch_allowed=True,
        frame_selection=sel(4.35, dispersion=0.6033),
    )[0] == "cpa_redraw"
    assert decide(
        cover_mode="auto",
        is_song=False,
        punch_allowed=True,
        frame_selection=sel(4.35, dispersion=0.42),
    )[0] == "screenshot_polish"
    # 歌切 / 无选帧 → 全图重绘；手定标题只锁文字，不得偷偷决定视觉路线。
    assert decide(cover_mode="auto", is_song=True, punch_allowed=True, frame_selection=sel(9.0))[0] == "cpa_redraw"
    assert decide(cover_mode="auto", is_song=False, punch_allowed=False, frame_selection=sel(9.0))[0] == "screenshot_direct"
    assert decide(cover_mode="auto", is_song=False, punch_allowed=True, frame_selection=None)[0] == "cpa_redraw"
    assert decide(cover_mode="screenshot", is_song=False, punch_allowed=True, frame_selection=sel(0.5))[0] == "screenshot_direct"
    assert decide(cover_mode="polish", is_song=False, punch_allowed=True, frame_selection=sel(9.0))[0] == "screenshot_polish"
    assert decide(cover_mode="cpa", is_song=False, punch_allowed=True, frame_selection=sel(9.0))[0] == "cpa_redraw"
    assert decide(
        cover_mode="auto",
        is_song=False,
        punch_allowed=True,
        frame_selection=sel(7.2, subject=False),
        verified_stream_frame=True,
    ) == (
        "screenshot_direct",
        "hash-bound source frame verifies all required participants",
    )


def test_relation_auto_route_blocks_before_host_only_ai_when_participants_unverified(
    tmp_path, monkeypatch
):
    """Regression: 2026-07-22 dual-person hooks became one-person AI covers."""

    from PIL import Image

    from src.autoslice import publish_staging
    from src.autoslice.story_contract import build_story_contract
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "auto")
    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setattr(
        publish_staging,
        "select_expressive_cover_frame",
        lambda *_args, **_kwargs: {
            "schema": "cover-frame-selection.v1",
            "status": "SELECTED",
            "best_ms": 1_000,
            "candidates": [
                {"ms": 1_000, "score": 4.6224, "emotion": 0.0}
            ],
            "subject_confident": False,
            "motion_dispersion_frac": 0.4456,
        },
    )
    contract = build_story_contract(
        candidate_id="relation-no-authority",
        selection_hook=(
            "弹幕追问李豆沙为何请南町吃火锅，"
            "一路嘴硬到最最最最喜欢，刚认识就互相霸凌"
        ),
        transcript_text="为什么请大N老师吃火锅呢？",
        selection_scorecard={"status": "VALID"},
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": [
                {"canonical_id": "lidousha", "display_name": "李豆沙"},
                {"canonical_id": "nancho", "display_name": "南町"},
            ],
        },
    )
    calls = {"image_edit": 0}

    def fake_host_only_ai(**kwargs):
        calls["image_edit"] += 1
        Image.new("RGB", (1920, 1080), (30, 40, 80)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "story_contract": contract,
        },
        media_path=media,
        candidate_id="relation-no-authority",
        title="【李豆沙】为什么请南町吃火锅，刚认识就互相霸凌",
        cover_text="为什么请南町吃火锅",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=fake_host_only_ai,
        punch_allowed=True,
    )

    assert calls["image_edit"] == 0
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert "RELATION_COVER_SOURCE_PARTICIPANTS_UNVERIFIED" in result[
        "reason_codes"
    ]
    route = result["cover_generation"]["route_decision"]
    assert route["relationship_visual_required"] is True
    assert route["source_visible_participant_ids"] == []
    assert route["selected_treatment"] == "screenshot_direct"
    assert route["actual_treatment"] is None
    assert route["execution_status"] == "BLOCKED"


def test_3573_shaped_dual_route_blocks_without_proof_then_passes_no_crop(
    tmp_path, monkeypatch
):
    from PIL import Image

    from src.autoslice import publish_staging
    from src.autoslice.cover_route_evidence import (
        validate_cover_route_decision,
    )
    from src.autoslice.story_contract import build_story_contract

    reference_path = tmp_path / "3573-dual-reference.png"
    Image.new("RGB", (1920, 1080), (52, 88, 126)).save(reference_path)
    reference_sha256 = (
        "sha256:" + hashlib.sha256(reference_path.read_bytes()).hexdigest()
    )
    reference_authority = {
        "candidate_id": "auto_193450_3573_3665r8",
        "content_time_ms": 70_000,
        "source_time_ms": 3_643_070,
        "source_sha256": "sha256:" + "1" * 64,
        "reference_png_sha256": reference_sha256,
        "visible_participant_ids": ["lidousha", "nancho"],
        "required_treatment": "screenshot_direct",
        "authority": "reviewed source frame",
    }
    contract = build_story_contract(
        candidate_id="auto_193450_3573_3665r8",
        selection_hook="李豆沙展示金发有角妹妹",
        transcript_text="看到男角色只能说不熟。",
        selection_scorecard={"status": "VALID"},
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": [
                {
                    "canonical_id": "lidousha",
                    "display_name": "李豆沙",
                },
                {"canonical_id": "nancho", "display_name": "南町"},
            ],
        },
        cover_reference_authority=reference_authority,
    )
    title = "【李豆沙】最包容异性恋的直播间，看到男角色只能说出一句不熟"
    cover_text = "看到男角色只能说不熟"
    frame_selection = {
        "schema": "cover-frame-selection.v1",
        "status": "SELECTED",
        "best_ms": 70_000,
        "candidates": [{"ms": 70_000, "score": 3.2, "emotion": 0.0}],
        "subject_confident": False,
        "motion_dispersion_frac": 0.2,
    }
    art_direction = shadow_pipeline.LidoushaCoverArtDirection(
        role="witty_smug",
        expression_en="mischievous smirk",
        background_style="cobalt-comic-burst",
        layout="left-split",
        hook_color="yellow",
        is_song=False,
    )

    def build_generation() -> dict[str, object]:
        generation: dict[str, object] = {
            "title": title,
            "cover_text": cover_text,
            "story_contract": contract,
            "reference_selection": frame_selection,
        }
        treatment, route = publish_staging._build_lidousha_cover_route(
            cover_generation=generation,
            story_contract=contract,
            title=title,
            cover_text=cover_text,
            cover_mode="auto",
            art_direction=art_direction,
            punch_allowed=False,
            frame_selection=frame_selection,
            reference_authority=reference_authority,
        )
        assert treatment == "screenshot_direct"
        assert route["relationship_semantic_evidence"] == []
        assert route["relationship_visual_required"] is True
        return generation

    def forbidden_crop(*_args, **_kwargs):
        raise AssertionError(
            "confirmed multi-participant cover must preserve the full frame"
        )

    monkeypatch.setattr(
        publish_staging, "extract_zoomed_cover_frame", forbidden_crop
    )
    real_verification_builder = (
        publish_staging.build_no_crop_participant_verification
    )
    monkeypatch.setattr(
        publish_staging,
        "build_no_crop_participant_verification",
        lambda _generation: {},
    )
    blocked_generation = build_generation()
    blocked_ai_dir = tmp_path / "blocked" / "ai"
    blocked_covers_dir = tmp_path / "blocked" / "covers"
    blocked_ai_dir.mkdir(parents=True)
    blocked_covers_dir.mkdir(parents=True)
    blocked = publish_staging._stage_screenshot_direct_cover(
        media_path=tmp_path / "unused.mp4",
        candidate_id="auto_193450_3573_3665r8-blocked",
        cover_text=cover_text,
        art_direction=art_direction,
        frame_selection=frame_selection,
        reference_path=reference_path,
        ai_dir=blocked_ai_dir,
        covers_dir=blocked_covers_dir,
        cover_generation=blocked_generation,
    )
    assert blocked["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert blocked["reason_codes"] == [
        "RELATION_COVER_FINAL_PARTICIPANTS_UNVERIFIED"
    ]
    assert blocked_generation["screenshot_frame"]["crop_applied"] is False
    assert blocked_generation["screenshot_frame"]["frame_ms"] == 70_000
    assert blocked_generation["route_decision"]["execution_status"] == (
        "BLOCKED"
    )

    monkeypatch.setattr(
        publish_staging,
        "build_no_crop_participant_verification",
        real_verification_builder,
    )
    ready_generation = build_generation()
    ready_ai_dir = tmp_path / "ready" / "ai"
    ready_covers_dir = tmp_path / "ready" / "covers"
    ready_ai_dir.mkdir(parents=True)
    ready_covers_dir.mkdir(parents=True)
    ready = publish_staging._stage_screenshot_direct_cover(
        media_path=tmp_path / "unused.mp4",
        candidate_id="auto_193450_3573_3665r8-ready",
        cover_text=cover_text,
        art_direction=art_direction,
        frame_selection=frame_selection,
        reference_path=reference_path,
        ai_dir=ready_ai_dir,
        covers_dir=ready_covers_dir,
        cover_generation=ready_generation,
    )

    assert ready["status"] == "AI_COVER_READY", ready
    assert ready_generation["screenshot_frame"]["crop_applied"] is False
    assert (
        ready_generation["screenshot_frame"]["frame_ms"]
        == ready_generation["reference_selection"]["best_ms"]
        == 70_000
    )
    assert ready_generation["screenshot_frame"]["zoom"] == 1.0
    assert ready_generation["screenshot_graphic_poster"][
        "source_frame_transform"
    ]["full_frame_preserved"] is True
    assert ready_generation["final_participant_verification"]["status"] == (
        "PASS"
    )
    assert ready_generation["route_decision"][
        "final_visible_participant_ids"
    ] == ["lidousha", "nancho"]
    assert validate_cover_route_decision(ready_generation)


def test_screenshot_polish_retouches_cropped_frame(tmp_path, monkeypatch):
    """polish 路线：CPA 以裁切后的截图为参考做保真修图，成品用修图版叠梗字。"""

    from PIL import Image

    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.delenv("AUTOSLICE_COVER_REF_MS", raising=False)
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "polish")
    media = _write_synthetic_performance_clip(tmp_path)
    captured: dict = {}

    def fake_polish_edit(**kwargs):
        captured.update(kwargs)
        Image.new("RGB", (1920, 1080), (90, 120, 40)).save(kwargs["output_path"])
        return {"status": "AI_BACKGROUND_READY", "selected_model": "gpt-image-2", "attempted_models": ["gpt-image-2"]}

    def fake_face_verify(final_cover_path, *, base_url, api_key):
        import hashlib

        sha = hashlib.sha256(Path(final_cover_path).read_bytes()).hexdigest()
        return {
            "schema_version": "lidousha-cover-polish-face-verification.v2",
            "authority": "CPA_PRIMARY_HASH_BOUND_FINAL_FACE_CHECK",
            "status": "PASS",
            "witness": {
                "status": "OBSERVED",
                "provider": "cpa",
                "image_sha256": sha,
            },
        }

    monkeypatch.setattr(
        publish_staging, "_verify_polish_face_integrity", fake_face_verify
    )
    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="polish-1",
        title="【李豆沙】才，才不是熊猫呢！小李被kmx用两个字点名",
        cover_text="才，才不是熊猫呢！小李被kmx用两个字点名",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=fake_polish_edit,
        punch_allowed=True,
    )
    assert result["status"] == "AI_COVER_READY", result
    generation = result["cover_generation"]
    assert generation["cover_treatment"]["treatment"] == "screenshot_polish"
    assert generation["method"] == "screenshot_polish"
    assert generation["screenshot_polish"]["status"] == "POLISHED"
    # 修图参考=裁切后的截图底图；prompt 是保真修图合同，不是重绘。
    assert Path(str(captured["reference_path"])).name == "polish-1.screenshot-base.png"
    assert "RETOUCH" in captured["prompt"] and "remove livestream overlay clutter" in captured["prompt"]
    assert Path(str(result["cover_path"])).is_file()


def test_screenshot_polish_degrades_to_direct_on_cpa_failure(tmp_path, monkeypatch):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "polish")
    media = _write_synthetic_performance_clip(tmp_path)

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="polish-degrade",
        title="【李豆沙】才，才不是熊猫呢！小李被kmx用两个字点名",
        cover_text="才，才不是熊猫呢！小李被kmx用两个字点名",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        punch_allowed=True,
    )
    assert result["status"] == "AI_COVER_READY"
    generation = result["cover_generation"]
    assert generation["method"] == "screenshot_direct"
    assert generation["screenshot_polish"]["status"] == "DEGRADED_TO_DIRECT"
    route = generation["route_decision"]
    assert route["selected_treatment"] == "screenshot_polish"
    assert route["actual_treatment"] == "screenshot_direct"
    assert route["execution_status"] == "READY_DEGRADED"
    assert route["image_generation_planned"] is True
    assert route["image_generation_attempted"] is True
    assert route["image_generation_used"] is False
    assert "RuntimeError: boom" in route["execution_detail"]


def test_screenshot_mode_song_falls_back_to_cpa_gate(tmp_path, monkeypatch):
    """歌切在 screenshot 模式下仍走 CPA；凭据缺失 → 同语义卡死。"""

    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "screenshot")
    media = _write_synthetic_performance_clip(tmp_path)

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="song-shot",
        title="【李豆沙】豆沙歌，《暖暖》",
        cover_text="《暖暖》",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=lambda **kwargs: {"status": "NEVER"},
        punch_allowed=False,
    )
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert "CPA_CREDENTIALS_MISSING" in result["reason_codes"]


def test_stage_publish_draft_gates_punch_by_title_authority(tmp_path):
    captured: list[dict] = []

    def fake_stage_cover(record, **kwargs):
        captured.append(kwargs)
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(tmp_path / "cover.png"),
            "cover_generation": {"status": "OK"},
            "reason_codes": [],
        }

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n才，才不是熊猫呢\n", encoding="utf-8")
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
        "artifact_hashes": {"video_sha256": "sha256:x"},
    }
    cues = [shadow_pipeline.SourceCue("c1", 0, 2_000, "x")]

    # 自动标题（LLM 起题）→ punch_allowed=True。
    shadow_pipeline._stage_publish_draft_impl(
        dict(record),
        candidate_id="talk-auto",
        title="job",
        cues=cues,
        run_ffmpeg=False,
        title_llm_call=lambda prompt: '{"title": "才，才不是熊猫呢！小李被kmx用两个字点名"}',
        stage_cover=fake_stage_cover,
    )
    assert captured[-1]["punch_allowed"] is True

    # 手定标题（title_llm_call=None，一字不改直通）→ punch_allowed=False。
    shadow_pipeline._stage_publish_draft_impl(
        dict(record),
        candidate_id="talk-manual",
        title="【李豆沙】反沙，不是反李豆沙！",
        cues=cues,
        run_ffmpeg=False,
        title_llm_call=None,
        stage_cover=fake_stage_cover,
    )
    assert captured[-1]["punch_allowed"] is False


def test_cover_prompt_layout_and_overlay_hook_metadata(tmp_path):
    from PIL import Image

    # --- Prompt: identity anchors + global never-tongue clause are always present.
    song_prompt = shadow_pipeline._lidousha_cover_prompt(
        title=_COVER_SONG_TITLE, cover_text=_COVER_SONG_TEXT
    )
    assert "panda" in song_prompt.lower()
    assert "小李" in song_prompt
    assert "熊猫" in song_prompt
    assert "16:9" in song_prompt
    assert "tongue" in song_prompt
    assert "PRESERVE THE EXACT OUTFIT" in song_prompt

    # --- Prompt: composition follows the art direction's talk layout.
    left = shadow_pipeline.LidoushaCoverArtDirection(
        role="witty_smug",
        expression_en="clever pleased closed-mouth grin",
        background_style="halftone-dots",
        layout="left-split",
        hook_color="yellow",
        is_song=False,
    )
    right = dataclasses.replace(left, layout="right-split")
    assert "LEFT ~55%" in shadow_pipeline._lidousha_cover_prompt(
        title=_COVER_TALK_TITLE, cover_text=_COVER_TALK_TEXT, art_direction=left
    )
    assert "RIGHT ~55%" in shadow_pipeline._lidousha_cover_prompt(
        title=_COVER_TALK_TITLE, cover_text=_COVER_TALK_TEXT, art_direction=right
    )

    # --- Overlay: render a real 1920x1080 cover on a real background image.
    background_path = tmp_path / "ai_background.png"
    Image.new("RGB", (1920, 1080), (40, 120, 210)).save(background_path)
    final_cover_path = tmp_path / "cover.final.png"
    cover_text = "被弹幕拆台\n当场反杀"
    art_direction = shadow_pipeline.LidoushaCoverArtDirection(
        role="witty_smug",
        expression_en="clever pleased closed-mouth grin",
        background_style="halftone-dots",
        layout="banner",
        hook_color="pink",
        is_song=False,
        hook_word="反杀",  # a verbatim substring of cover_text → highlighted segment
    )

    overlay = shadow_pipeline._overlay_lidousha_cover_title(
        background_path, final_cover_path, cover_text=cover_text, art_direction=art_direction
    )

    assert final_cover_path.is_file()
    with Image.open(final_cover_path) as rendered:
        assert rendered.size == (1920, 1080)

    assert overlay["hook_word"] == "反杀"
    assert overlay["hook_color"] == "pink"
    assert overlay["layout"] == "banner"
    assert overlay["font"] == "ZCOOLKuaiLe-Regular.ttf"

    # Regression: the overlay metadata must NOT leak image-gen/publish reserved keys.
    reserved_keys = {"model", "method", "fallback_used", "ai_background", "workflow", "image_gen_model"}
    assert reserved_keys.isdisjoint(overlay.keys())


def test_title_policy_bans_zhijie_filler_word():
    """Ivan 2026-07-06: 标题里不能出现"直接"（直呼打咩 >> 直接打咩）。"""
    assert "banned_filler_word" in shadow_pipeline._title_policy_violations("【李豆沙】小李直接打咩")
    assert shadow_pipeline._title_policy_violations("【李豆沙】小李直呼打咩") == []


def test_cover_request_size_satisfies_gateway_multiple_of_16():
    """2026-07-06: CPA 图像网关(new_api)开始 400 拒绝非 16 倍数尺寸——1920x1080
    整批封面全灭。请求尺寸必须合规，画布归一由 _normalize_cover_canvas 负责。"""
    w, h = map(int, shadow_pipeline._COVER_REQUEST_SIZE.split("x"))
    assert w % 16 == 0 and h % 16 == 0


def test_normalize_cover_canvas_crops_and_covers(tmp_path):
    from PIL import Image

    canvas = shadow_pipeline._COVER_CANVAS
    # 请求尺寸 1920x1088 回图 → 居中裁回画布
    tall = tmp_path / "tall.png"
    Image.new("RGB", (1920, 1088), (10, 20, 30)).save(tall)
    assert shadow_pipeline._normalize_cover_canvas(tall) == canvas
    with Image.open(tall) as img:
        assert img.size == canvas
    # 模型自作主张回方图 → scale-to-cover + 居中裁，不拉伸不留黑边
    square = tmp_path / "square.png"
    Image.new("RGB", (1024, 1024), (1, 2, 3)).save(square)
    assert shadow_pipeline._normalize_cover_canvas(square) == canvas
    # 已是画布尺寸 → 原样
    exact = tmp_path / "exact.png"
    Image.new("RGB", canvas, (5, 5, 5)).save(exact)
    assert shadow_pipeline._normalize_cover_canvas(exact) == canvas


def test_regroup_lines_merges_contiguously_and_word_safely():
    """_regroup_lines merges adjacent word-safe lines into k balanced groups —
    fewer lines fill wide zones with bigger text and never split a word."""
    lines = ["电脑要造反？", "小皇帝", "拒绝更新"]
    assert shadow_pipeline._regroup_lines(lines, 3) == lines          # k>=len → unchanged
    assert shadow_pipeline._regroup_lines(lines, 1) == ["电脑要造反？小皇帝拒绝更新"]
    two = shadow_pipeline._regroup_lines(lines, 2)                    # balanced 2-group
    assert len(two) == 2 and "".join(two) == "电脑要造反？小皇帝拒绝更新"
    # every group is a run of whole original lines → no word split
    for word in ("小皇帝", "拒绝更新"):
        assert any(word in g for g in two)


def test_cover_font_swaps_whole_cover_on_wrong_shape_glyph():
    """ZCOOLKuaiLe renders 自 as 白 (擅自→擅白) — not tofu, so the missing-glyph
    checker misses it.  A title with 自 must swap the WHOLE cover to the complete
    fallback font (Ivan 2026-07-07, the recurring 自→白 通病)."""
    zcool = shadow_pipeline._find_cover_font()
    fallback = shadow_pipeline._cover_fallback_font_path()
    assert fallback is not None, "fallback font (SmileySans/得意黑) must be present"
    # a title WITH 自 → fallback (whole-cover swap)
    assert shadow_pipeline._cover_font_for_text("电脑擅自更新").path == fallback
    assert shadow_pipeline._cover_font_for_text("cos比sin自私").path == fallback
    # a title WITHOUT 自 → stays ZCOOL
    assert shadow_pipeline._cover_font_for_text("电脑要造反小皇帝拒绝更新").path == zcool


def test_cover_font_checks_every_chain_member_and_blocks_notdef(monkeypatch):
    """2026-07-16 《怪獣の花唄》 published-cover case: ZCOOL lacks 獣, the cover
    swapped to SmileySans — and SmileySans ALSO lacks 獣, so its stylised .notdef
    shipped on a live B站 cover.  Only ZCOOL was ever glyph-checked.  Now every
    chain member is checked; when nothing fully covers, generation must block
    instead of shipping a disclosed-but-still-visible .notdef."""
    zcool = shadow_pipeline._find_cover_font()
    smiley = shadow_pipeline._cover_fallback_font_path()
    assert smiley is not None and "SmileySans" in smiley.name
    # SmileySans really is missing 獣 (raster .notdef probe on the fallback font)
    assert shadow_pipeline._cover_missing_checker(smiley)("獣") is True
    assert shadow_pipeline._cover_missing_checker(smiley)("怪") is False

    # Constrain the chain to the two repo fonts: neither covers 獣, so selection
    # records the evidence and fails closed.
    from src.autoslice import cover_generation

    limited = [
        shadow_pipeline._CoverFontChoice(zcool),
        shadow_pipeline._CoverFontChoice(smiley),
    ]
    monkeypatch.setattr(
        cover_generation, "_cover_font_chain", lambda *, prefer_jp: limited
    )
    audit: dict = {}
    with pytest.raises(
        RuntimeError, match="COVER_FONT_GLYPH_COVERAGE_MISSING"
    ):
        shadow_pipeline._cover_font_for_text(
            "怪獣の花唄", selection_audit=audit
        )
    assert "獣" in audit["glyph_risk"]
    assert len(audit["rejected"]) == 2

    # The real production chain is also committed-only and therefore portable.
    monkeypatch.undo()
    audit2: dict = {}
    with pytest.raises(
        RuntimeError, match="COVER_FONT_GLYPH_COVERAGE_MISSING"
    ):
        shadow_pipeline._cover_font_for_text(
            "怪獣の花唄", selection_audit=audit2
        )
    assert "獣" in audit2["glyph_risk"]


def _fake_polish_image_edit(*, output_path, reference_path, **_kwargs):
    from PIL import Image

    Image.open(reference_path).save(output_path)
    return {
        "status": "AI_BACKGROUND_READY",
        "selected_model": "gpt-image-2",
        "attempted_models": ["gpt-image-2"],
    }


def test_polish_cover_face_gate_retries_contain_then_passes(tmp_path, monkeypatch):
    """FACE_INCOMPLETE on the fit-crop card must retry once with the
    whole-face contain card before any fail-closed decision (424 case)."""

    import hashlib

    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "polish")
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    media = _write_synthetic_performance_clip(tmp_path)
    face_calls: list[str] = []

    def fake_face_verify(final_cover_path, *, base_url, api_key):
        assert base_url and api_key
        face_calls.append(str(final_cover_path))
        if len(face_calls) == 1:
            return {
                "schema_version": "lidousha-cover-polish-face-verification.v2",
                "status": "FAIL",
                "reason_code": "FACE_INCOMPLETE",
                "witness": {"status": "OBSERVED", "image_sha256": "0" * 64},
            }
        sha = hashlib.sha256(Path(final_cover_path).read_bytes()).hexdigest()
        return {
            "schema_version": "lidousha-cover-polish-face-verification.v2",
            "authority": "CPA_PRIMARY_HASH_BOUND_FINAL_FACE_CHECK",
            "status": "PASS",
            "witness": {
                "status": "OBSERVED",
                "provider": "cpa",
                "image_sha256": sha,
            },
        }

    monkeypatch.setattr(
        publish_staging, "_verify_polish_face_integrity", fake_face_verify
    )
    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="polish-face-retry",
        title="【李豆沙】和别的女同一起挖人",
        cover_text="和别的女同一起挖人",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=_fake_polish_image_edit,
        punch_allowed=True,
    )
    assert result["status"] == "AI_COVER_READY", result
    generation = result["cover_generation"]
    assert generation["method"] == "screenshot_polish"
    assert generation["polish_face_verification"]["status"] == "PASS"
    transform = generation["screenshot_graphic_poster"]["source_frame_transform"]
    assert transform["card_fit"] == "contain_face_safe"
    assert transform["crop_applied"] is False
    assert len(face_calls) == 2


def test_polish_cover_face_gate_degrades_to_direct_when_never_complete(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "polish")
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    media = _write_synthetic_performance_clip(tmp_path)
    face_calls: list[str] = []

    def fake_face_verify(final_cover_path, *, base_url, api_key):
        face_calls.append(str(final_cover_path))
        return {
            "schema_version": "lidousha-cover-polish-face-verification.v2",
            "status": "FAIL",
            "reason_code": "FACE_INCOMPLETE",
            "witness": {"status": "OBSERVED", "image_sha256": "0" * 64},
        }

    monkeypatch.setattr(
        publish_staging, "_verify_polish_face_integrity", fake_face_verify
    )
    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="polish-face-blocked",
        title="【李豆沙】脸不完整必须拦下",
        cover_text="脸不完整必须拦下",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=_fake_polish_image_edit,
        punch_allowed=True,
    )
    assert result["status"] == "AI_COVER_READY", result
    assert len(face_calls) == 2
    generation = result["cover_generation"]
    assert generation["method"] == "screenshot_direct"
    assert generation["cover_origin"] == "SOURCE_SCREENSHOT"
    assert generation["image_generation_attempted"] is True
    assert generation["image_generation_used"] is False
    assert generation["screenshot_polish"] == {
        "status": "DEGRADED_TO_DIRECT",
        "reason_code": "POLISH_FACE_GATE_FAILED",
        "detail": (
            "generated polish rejected by final-pixel face gate; "
            "hash-bound source screenshot retained"
        ),
        "image_generation_attempted": True,
        "image_generation_used": False,
    }
    assert generation["rejected_polish_face_verification"]["status"] == "FAIL"
    route = generation["route_decision"]
    assert route["selected_treatment"] == "screenshot_polish"
    assert route["actual_treatment"] == "screenshot_direct"
    assert route["execution_status"] == "READY_DEGRADED"


def test_polish_cover_face_gate_unavailable_degrades_without_retry(
    tmp_path, monkeypatch
):
    """Verifier outage rejects generated pixels and keeps the source screenshot."""

    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import _write_synthetic_performance_clip

    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "polish")
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    media = _write_synthetic_performance_clip(tmp_path)
    face_calls: list[str] = []

    def fake_face_verify(final_cover_path, *, base_url, api_key):
        face_calls.append(str(final_cover_path))
        return {
            "schema_version": "lidousha-cover-polish-face-verification.v2",
            "status": "FAIL",
            "reason_code": "VERIFIER_UNAVAILABLE",
            "witness": {"status": "UNAVAILABLE"},
        }

    monkeypatch.setattr(
        publish_staging, "_verify_polish_face_integrity", fake_face_verify
    )
    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="polish-face-unavailable",
        title="【李豆沙】验证不可用也要拦",
        cover_text="验证不可用也要拦",
        run_ffmpeg=True,
        art_direction_llm_call=None,
        image_edit=_fake_polish_image_edit,
        punch_allowed=True,
    )
    assert result["status"] == "AI_COVER_READY", result
    assert len(face_calls) == 1
    generation = result["cover_generation"]
    assert generation["method"] == "screenshot_direct"
    assert generation["screenshot_polish"]["status"] == "DEGRADED_TO_DIRECT"
    assert generation["rejected_polish_face_verification"][
        "reason_code"
    ] == "VERIFIER_UNAVAILABLE"
    assert generation["route_decision"]["execution_status"] == (
        "READY_DEGRADED"
    )


def test_screenshot_poster_face_safe_contain_keeps_whole_frame(tmp_path):
    from PIL import Image

    from src.autoslice.cover_generation import LidoushaCoverArtDirection
    from src.autoslice.cover_screenshot_poster import (
        _compose_screenshot_poster_background,
    )

    source = tmp_path / "closeup.png"
    Image.new("RGB", (1920, 1080), (200, 180, 170)).save(source)
    direction = LidoushaCoverArtDirection(
        role="shy_cute_default",
        expression_en="soft smile",
        background_style="cobalt-comic-burst",
        layout="banner",
        hook_color="yellow",
        is_song=False,
        cover_punch=("整脸卡",),
    )
    fit = _compose_screenshot_poster_background(
        source, tmp_path / "fit.png", art_direction=direction
    )
    contain = _compose_screenshot_poster_background(
        source,
        tmp_path / "contain.png",
        art_direction=direction,
        face_safe_contain=True,
    )
    assert fit["source_frame_transform"]["card_fit"] == "fit_crop"
    assert fit["source_frame_transform"]["crop_applied"] is True
    assert contain["source_frame_transform"]["card_fit"] == "contain_face_safe"
    assert contain["source_frame_transform"]["crop_applied"] is False
    box = contain["source_frame_transform"]["rendered_content_box"]
    width = box[2] - box[0]
    height = box[3] - box[1]
    # 16:9 preserved inside the 1640×700 card: no vertical decapitation.
    assert abs((width / height) - (16 / 9)) < 0.02
    assert contain["source_frame_transform"]["center_4_3_safe"] is True
