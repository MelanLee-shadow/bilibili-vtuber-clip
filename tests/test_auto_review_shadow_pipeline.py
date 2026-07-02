import json
import subprocess
from pathlib import Path

import pytest

from scripts import run_auto_review_shadow_pipeline as shadow_pipeline
from src.autoslice.review_evidence import ReviewEvidence


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
            "model": "Gemini 3.5 Flash (Low)",
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
            "model": "Gemini 3.5 Flash (Low)",
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
            "model": "Gemini 3.5 Flash (Low)",
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
            "model": "Gemini 3.5 Flash (Low)",
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
        source_context_job={"candidate_id": "anchor-planned", "anchor_start_ms": 120_000, "anchor_end_ms": 150_000},
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (Low)",
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
            model="Gemini 3.5 Flash (Low)",
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

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "travel-meaning-anchor",
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
            "lyrics_alignment": {
                "status": "READY",
                "provider": "agy",
                "model": "Gemini 3.5 Flash (High)",
                "external_lrc": "kugeci://travel-meaning",
                "chunked_probe_count": 3,
                "spectrogram_verified": True,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (High)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["decision_action"] == "AUTO_RECUT"
    assert "SONG_PARTIAL" not in record["reason_codes"]
    assert "LYRICS_ALIGNMENT_REQUIRED" not in record["reason_codes"]
    assert "SONG_FULL_BOUNDARY_READY" in record["reason_codes"]
    assert record["decision_action"] == "AUTO_RECUT"
    assert record["recut_plan"]["start_ms"] == 0
    assert record["recut_plan"]["end_ms"] == 280_000
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
            "timeline": {
                "anchor_start_ms": 22_000,
                "anchor_end_ms": 30_000,
                "context_start_ms": 0,
                "context_duration_ms": 65_000,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (Low)",
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

    boundary = shadow_pipeline._resolve_live_source_boundary(job, cues)

    assert boundary is not None
    assert boundary.action == shadow_pipeline.DecisionAction.AUTO_UPLOAD
    assert boundary.reason_codes == ()


def test_live_source_applies_cpa_semantic_review_and_blocks_terminology_failure(tmp_path, monkeypatch):
    _patch_complete_evidence(monkeypatch)
    monkeypatch.setattr(
        shadow_pipeline,
        "_resolve_live_source_boundary",
        lambda job_manifest, cues: shadow_pipeline.BoundaryResolution(
            candidate_id="cpa-terms",
            action=shadow_pipeline.DecisionAction.AUTO_UPLOAD,
            resolved_start_ms=0,
            resolved_end_ms=5_000,
            start_boundary_score=0.97,
            end_boundary_score=0.98,
            reason_codes=(),
        ),
    )
    response_path = tmp_path / "cpa-semantic.json"
    response_path.write_text(
        json.dumps(
            {
                "schema_version": "cpa-semantic-review-response.v1",
                "candidate_id": "cpa-terms",
                "release_ready": False,
                "semantic_complete": True,
                "terminology_ok": False,
                "reason_codes": ["TERMINOLOGY_QA_FAILED"],
                "required_fixes": ["字幕必须使用 kmx，不能使用 kimo熊 或天不熊"],
                "evidence": {"terminology_findings": [{"canonical": "kmx", "alias": "天不熊"}]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source_video, source_srt, refined_srt = _write_live_source_inputs(
        tmp_path,
        source_srt="1\n00:00:00,000 --> 00:00:05,000\n天不熊被骗到了\n",
        refined_srt="1\n00:00:00,000 --> 00:00:05,000\n天不熊被骗到了\n",
    )

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "cpa-terms",
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 5_000, "context_start_ms": 0, "context_duration_ms": 5_000},
            "cpa_semantic_response_path": str(response_path),
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (Low)",
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
    assert "TERMINOLOGY_QA_FAILED" in record["reason_codes"]
    assert evidence["checks"][-1]["code"] == "CPA_SEMANTIC_QA"
    assert evidence["checks"][-1]["pass"] is False
    assert evidence["metadata"]["cpa_semantic_review"]["response_path"] == str(response_path)


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
        lambda job_manifest, cues: shadow_pipeline.BoundaryResolution(
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
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 7_500, "context_start_ms": 0, "context_duration_ms": 7_500},
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (Low)",
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
        lambda job_manifest, cues: shadow_pipeline.BoundaryResolution(
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

    summary = shadow_pipeline.run_shadow_pipeline(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        source_context_job={
            "candidate_id": "preview-upload",
            "timeline": {"anchor_start_ms": 0, "anchor_end_ms": 7_500, "context_start_ms": 0, "context_duration_ms": 7_500},
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (Low)",
            agy_rc=0,
            provider_fallback_used=False,
        ),
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=True,
    )

    record = summary["records"][0]
    evidence = _load_json(Path(record["evidence_path"]))

    assert record["materialized_recut"]["status"] == "MATERIALIZED"
    assert record["materialized_recut"]["dry_run_placeholder"] is False
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
            "timeline": {
                "anchor_start_ms": 3_200,
                "anchor_end_ms": 4_000,
                "context_start_ms": 0,
                "context_duration_ms": 7_500,
            },
        },
        agy_result=shadow_pipeline.AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (Low)",
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
    assert materialized["render_qa"]["code"] == "ACTUAL_CUT_ERROR_HIGH"
    assert materialized["render_qa"]["evidence"]["actual_cut_error_ms"] >= 1000
    assert materialized_manifest["render_qa"] == materialized["render_qa"]
    assert render_qa_manifest["check"] == materialized["render_qa"]
    assert evidence["metrics"]["actual_cut_error_ms"] == materialized["render_qa"]["evidence"]["actual_cut_error_ms"]
    assert any(check["code"] == materialized["render_qa"]["code"] for check in evidence["checks"])
    assert evidence["metadata"]["materialized_recut"]["render_qa"]["code"] == "ACTUAL_CUT_ERROR_HIGH"


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
            "model": "Gemini 3.5 Flash (Low)",
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

