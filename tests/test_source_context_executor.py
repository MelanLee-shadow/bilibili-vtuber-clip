import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import src.autoslice.source_context_executor as source_context_executor

from src.autoslice.source_context_executor import (
    AgyExecutionResult,
    AgyRunnerError,
    build_ffmpeg_context_clip_command,
    execute_source_context_job,
)


def job_manifest(**overrides):
    data = {
        "schema_version": "source-context-jingting-job.v1",
        "job_kind": "SOURCE_CONTEXT_JINGTING",
        "job_id": "scj_test",
        "candidate_id": "clip-a",
        "provider": "agy",
        "local_only": True,
        "source_offset_ms": 1_000,
        "timeline": {
            "source_duration_ms": 10_000,
            "anchor_start_ms": 2_000,
            "anchor_end_ms": 3_000,
            "context_start_ms": 1_000,
            "context_end_ms": 4_000,
            "context_duration_ms": 3_000,
            "context_anchor_start_ms": 1_000,
            "context_anchor_end_ms": 2_000,
        },
        "input": {
            "source_uri": "file:///tmp/source.mp4",
            "source_sha256": "sha256:source",
            "source_offset_ms": 1_000,
            "duration_ms": 3_000,
            "write_outputs": False,
        },
        "outputs": {"jingting_srt": None, "jingting_manifest": None, "review_required": None},
    }
    data.update(overrides)
    return data


def job_manifest_for_source(source: Path, **overrides):
    data = job_manifest(**overrides)
    data["input"] = {
        **data["input"],
        "source_sha256": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    return data


def write_srt(path: Path) -> None:
    path.write_text(
        "1\n00:00:01,500 --> 00:00:02,500\n第一句\n\n"
        "2\n00:00:03,800 --> 00:00:04,500\n第二句跨出窗口\n\n",
        encoding="utf-8",
    )


def test_context_window_clip_command_is_inspectable_and_not_shell_injected(tmp_path):
    source = tmp_path / "source;rm -rf nope.mp4"
    output = tmp_path / "context.mp4"

    cmd = build_ffmpeg_context_clip_command(
        source_video_path=source,
        output_media_path=output,
        context_start_ms=1_000,
        context_duration_ms=3_000,
    )

    assert isinstance(cmd, list)
    assert cmd[0] == "ffmpeg"
    assert str(source) in cmd
    assert str(output) in cmd
    assert ";" in str(source)  # preserved as one argv, not interpreted by a shell
    assert all("rm -rf" not in part or part == str(source) for part in cmd)


def test_missing_draft_srt_does_not_create_fake_refined_srt_or_done(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not a real mp4; dry-run test")

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        run_ffmpeg=False,
    )

    assert result.decision == "RETRY"
    assert "DRAFT_SRT_MISSING" in result.reason_codes
    assert result.context_refined_srt_path is None
    assert result.jingting_done is False
    assert not (tmp_path / "out" / "context.refined.srt").exists()
    review_required = json.loads(Path(result.review_required_path).read_text(encoding="utf-8"))
    assert review_required["release_ready"] is False
    assert "DRAFT_SRT_MISSING" in review_required["findings"]


def test_source_context_job_runs_agy_runner_when_refined_srt_not_provided(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)
    calls: list[tuple[Path, Path, Path]] = []

    def fake_agy_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
        calls.append((media_path, draft_srt_path, output_srt_path))
        assert media_path.exists()
        assert draft_srt_path.exists()
        output_srt_path.write_text(
            "1\n00:00:00,500 --> 00:00:01,500\n第一句精修\n\n"
            "2\n00:00:02,800 --> 00:00:03,000\n第二句精修\n",
            encoding="utf-8",
        )
        return AgyExecutionResult(provider="agy", model="Gemini", agy_rc=0, provider_fallback_used=False, provider_request_id="job-123")

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        agy_runner=fake_agy_runner,
        run_ffmpeg=False,
    )

    assert result.decision == "READY"
    assert result.jingting_done is True
    assert len(calls) == 1
    assert Path(result.context_refined_srt_path).read_text(encoding="utf-8").startswith("1\n00:00:00,500")
    manifest = json.loads(Path(result.jingting_manifest_path).read_text(encoding="utf-8"))
    assert manifest["provider_request_id"] == "job-123"
    assert manifest["output_sha256"].startswith("sha256:")
    assert not Path(result.review_required_path).exists()


def test_song_proof_context_bypasses_talk_refinement_provider(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)
    calls = []

    def forbidden_agy_runner(*args):
        calls.append(args)
        raise AssertionError("song proof context must not call talk AGY")

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        agy_runner=forbidden_agy_runner,
        refinement_required=False,
        run_ffmpeg=False,
    )

    assert result.decision == "READY"
    assert result.reason_codes == ()
    assert calls == []
    assert Path(result.context_refined_srt_path).read_text(encoding="utf-8") == Path(
        result.context_draft_srt_path
    ).read_text(encoding="utf-8")
    manifest = json.loads(Path(result.jingting_manifest_path).read_text(encoding="utf-8"))
    assert manifest["provider"] == "source_draft_context"
    assert manifest["refinement_required"] is False
    assert manifest["subtitle_authority_scope"] == "proof_context_only_external_lrc_required"


def test_agy_runner_error_reason_code_is_distinguishable(tmp_path):
    """rc=0-but-empty (AGY_EMPTY_OUTPUT) must not be conflated with a timeout:
    the 7/2 whole-session BLOCK was misdiagnosed as a timeout exactly because
    the evidence only said AGY_SOURCE_CONTEXT_RUNNER_FAILED."""

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)

    def empty_output_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
        raise AgyRunnerError("AGY_EMPTY_OUTPUT", "remote agy exited rc=0 but produced no valid output.srt")

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        agy_runner=empty_output_runner,
        run_ffmpeg=False,
    )

    assert result.decision == "RETRY_INFRA"
    assert "AGY_SOURCE_CONTEXT_RUNNER_FAILED" in result.reason_codes
    assert "AGY_EMPTY_OUTPUT" in result.reason_codes
    review_required = json.loads(Path(result.review_required_path).read_text(encoding="utf-8"))
    assert "AGY_EMPTY_OUTPUT" in review_required["findings"]


def test_agy_retry_after_is_persisted_for_autonomous_resume(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)

    def quota_runner(*_args) -> AgyExecutionResult:
        raise AgyRunnerError(
            "AGY_QUOTA_EXHAUSTED",
            "individual quota exhausted",
            retry_after_seconds=2458,
        )

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        agy_runner=quota_runner,
        run_ffmpeg=False,
    )

    review_required = json.loads(Path(result.review_required_path).read_text(encoding="utf-8"))
    assert "AGY_QUOTA_EXHAUSTED" in result.reason_codes
    assert review_required["metadata"]["retry_after_seconds"] == 2458


def test_gemini_api_fallback_without_recorded_agy_outcome_is_rejected(tmp_path):
    """改判：拒绝理由从「provider 不是 agy」改成「没记录 AGY 那条腿
    怎么退出的」。

    旧断言是一个**按 provider 层拒证据的门**。维护者 拍板（项目
    memory）：AGY 订阅 / 免费 3key / 付费 backup 是同一个 Gemini 模型的配额
    顺序，「按 provider 层拒证据的门 = 过度限制」，处方是「任一层证据有效 +
    按层钉模型串」。歌lane 的音频/LRC 证明链
    （``song_common.validate_audio_lrc_execution_metadata``）早就是这么做的，
    source-context 这一层是唯一还没跟上的。

    ``agy_rc=None`` 仍然 RETRY_INFRA —— fail-closed 保留，理由变准确。
    """

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)

    def gemini_fallback_runner(_media, draft, output) -> AgyExecutionResult:
        output.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        return AgyExecutionResult(
            provider="gemini_api",
            model="gemini-3.6-flash",
            agy_rc=None,
            provider_fallback_used=True,
            provider_request_id="fallback-job",
        )

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        agy_runner=gemini_fallback_runner,
        run_ffmpeg=False,
    )

    assert result.decision == "RETRY_INFRA"
    assert "AGY_FAILED" in result.reason_codes
    assert "JINGTING_PROVIDER_NOT_AGY" not in result.reason_codes
    assert "JINGTING_PROVIDER_FALLBACK_USED" not in result.reason_codes
    manifest = json.loads(Path(result.jingting_manifest_path).read_text(encoding="utf-8"))
    assert manifest["provider"] == "gemini_api"
    assert manifest["provider_fallback_used"] is True


def test_fully_typed_gemini_api_fallback_is_accepted_as_source_context_provider(tmp_path):
    """按层钉模型串齐全（模型串 + fallback=True + AGY 退出码）→ READY。"""

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)

    def gemini_fallback_runner(_media, draft, output) -> AgyExecutionResult:
        output.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        return AgyExecutionResult(
            provider="gemini_api",
            model="gemini-3.6-flash",
            agy_rc=1,
            provider_fallback_used=True,
            provider_request_id="fallback-job",
        )

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        agy_runner=gemini_fallback_runner,
        run_ffmpeg=False,
    )

    assert result.decision == "READY"
    assert result.reason_codes == ()
    assert result.jingting_done is True
    manifest = json.loads(Path(result.jingting_manifest_path).read_text(encoding="utf-8"))
    assert manifest["provider"] == "gemini_api"
    assert manifest["model"] == "gemini-3.6-flash"
    assert manifest["provider_fallback_used"] is True


def test_agy_failure_or_fallback_unknown_is_not_release_ready(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        agy_result=AgyExecutionResult(provider="agy", model="Gemini", agy_rc=1, provider_fallback_used=None),
        run_ffmpeg=False,
    )

    assert result.decision == "RETRY_INFRA"
    assert "AGY_FAILED" in result.reason_codes
    assert "JINGTING_PROVIDER_FALLBACK_UNKNOWN" in result.reason_codes
    assert result.jingting_done is False
    manifest = json.loads(Path(result.jingting_manifest_path).read_text(encoding="utf-8"))
    assert manifest["provider_fallback_used"] is None
    review_required = json.loads(Path(result.review_required_path).read_text(encoding="utf-8"))
    assert review_required["release_ready"] is False


def test_success_path_records_hash_provenance_and_source_time_cues(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)
    refined = tmp_path / "refined.srt"
    refined.write_text("1\n00:00:00,500 --> 00:00:01,500\n第一句精修\n\n", encoding="utf-8")

    result = execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        refined_srt_path=refined,
        agy_result=AgyExecutionResult(provider="agy", model="Gemini", agy_rc=0, provider_fallback_used=False),
        run_ffmpeg=False,
    )

    assert result.decision == "READY"
    assert result.jingting_done is True
    assert Path(result.context_refined_srt_path).exists()
    cues = json.loads(Path(result.source_cues_path).read_text(encoding="utf-8"))
    assert cues[0]["source_start_ms"] == 1_500
    assert cues[0]["source_end_ms"] == 2_500
    draft_text = Path(result.context_draft_srt_path).read_text(encoding="utf-8")
    assert "00:00:00,500 --> 00:00:01,500" in draft_text
    manifest = json.loads(Path(result.jingting_manifest_path).read_text(encoding="utf-8"))
    assert manifest["provider"] == "agy"
    assert manifest["agy_rc"] == 0
    assert manifest["provider_fallback_used"] is False
    assert manifest["prompt_sha256"].startswith("sha256:")
    assert manifest["input_sha256"].startswith("sha256:")
    assert manifest["output_sha256"].startswith("sha256:")
    assert not Path(result.review_required_path).exists()


def test_malformed_source_sha256_blocks_instead_of_skipping_integrity(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)

    result = execute_source_context_job(
        job_manifest(),  # fixture declares malformed "sha256:source"
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        refined_srt_path=srt,
        run_ffmpeg=False,
    )

    assert result.decision == "RETRY_INFRA"
    assert "SOURCE_SHA256_MALFORMED" in result.reason_codes
    assert result.jingting_done is False
    review_required = json.loads(Path(result.review_required_path).read_text(encoding="utf-8"))
    assert review_required["release_ready"] is False
    assert "SOURCE_SHA256_MALFORMED" in review_required["findings"]


def test_mismatched_source_sha256_still_blocks(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    srt = tmp_path / "full.srt"
    write_srt(srt)
    manifest = job_manifest()
    manifest["input"] = {**manifest["input"], "source_sha256": "sha256:" + "0" * 64}

    result = execute_source_context_job(
        manifest,
        source_video_path=source,
        output_dir=tmp_path / "out",
        full_source_srt_path=srt,
        refined_srt_path=srt,
        run_ffmpeg=False,
    )

    assert result.decision == "RETRY"
    assert "SOURCE_SHA256_MISMATCH" in result.reason_codes


def _fake_ffmpeg(calls: list, payload: bytes = b"encoded-context-clip"):
    """Stand in for ffmpeg: record the call and write the output it promised."""

    def run(cmd, **_kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(payload)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_second_attempt_reuses_the_first_context_clip_instead_of_re_cutting(
    tmp_path, monkeypatch
):
    """Retries share one clip.

    Each song retry used to cut its own copy of the same window into a fresh
    attempt dir — that was 1.27 GiB per attempt, byte-identical
    every time. Reuse is keyed on the source hash and the window, so a genuine
    change still re-cuts.
    """
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not a real mp4; reuse test")
    srt = tmp_path / "source.srt"
    write_srt(srt)
    calls: list = []
    monkeypatch.setattr(source_context_executor.subprocess, "run", _fake_ffmpeg(calls))

    outputs = []
    for attempt in ("attempt-one", "attempt-two"):
        result = execute_source_context_job(
            job_manifest_for_source(source),
            source_video_path=source,
            output_dir=tmp_path / attempt,
            full_source_srt_path=srt,
            refinement_required=False,
        )
        outputs.append(Path(result.context_media_path))

    assert len(calls) == 1, "the second attempt must not re-run ffmpeg"
    assert outputs[0] != outputs[1], "each attempt keeps its own path"
    assert outputs[0].stat().st_ino == outputs[1].stat().st_ino, "both must share one inode"
    assert outputs[1].read_bytes() == b"encoded-context-clip"


def test_a_changed_source_re_cuts_rather_than_serving_a_stale_clip(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"first source")
    srt = tmp_path / "source.srt"
    write_srt(srt)
    calls: list = []
    monkeypatch.setattr(source_context_executor.subprocess, "run", _fake_ffmpeg(calls))

    execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "attempt-one",
        full_source_srt_path=srt,
        refinement_required=False,
    )
    source.write_bytes(b"second source, different bytes")
    execute_source_context_job(
        job_manifest_for_source(source),
        source_video_path=source,
        output_dir=tmp_path / "attempt-two",
        full_source_srt_path=srt,
        refinement_required=False,
    )

    assert len(calls) == 2, "a different source hash must produce a fresh cut"


def test_cache_is_skipped_when_source_and_output_are_on_different_filesystems(
    tmp_path, monkeypatch
):
    """Talk sources are read off the CloudFS mount; never cache onto it."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"remote-ish source")
    srt = tmp_path / "source.srt"
    write_srt(srt)
    calls: list = []
    monkeypatch.setattr(source_context_executor.subprocess, "run", _fake_ffmpeg(calls))

    real_stat = Path.stat

    class _OtherDevice:
        """Everything the real stat says, except it lives on another device."""

        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            return getattr(self._info, name)

        @property
        def st_dev(self):
            return self._info.st_dev + 1

    def split_devices(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        return _OtherDevice(info) if self == source else info

    monkeypatch.setattr(Path, "stat", split_devices)
    for attempt in ("attempt-one", "attempt-two"):
        execute_source_context_job(
            job_manifest_for_source(source),
            source_video_path=source,
            output_dir=tmp_path / attempt,
            full_source_srt_path=srt,
            refinement_required=False,
        )

    assert len(calls) == 2, "no cross-device cache, so each attempt cuts its own"
    assert not (tmp_path / ".context_clip_cache").exists()
