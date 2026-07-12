import json
from pathlib import Path

import pytest

from scripts import gemini_slice_jingting as jingting


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_agy_subprocess_env_sets_home_when_daemon_environment_omits_it(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)

    env = jingting.agy_subprocess_env()

    assert env["HOME"]
    assert env["HOME"].startswith("/")


def test_pending_slices_skip_retry_marker_by_default(tmp_path):
    room = "22966160"
    date_dir = tmp_path / room / "2026-06-29"
    stem = "1s_test_22966160_2026-06-29-22-05-02-"
    video = _write(date_dir / f"{stem}.flv", b"fake video")
    _write(date_dir / "subtitles" / f"{stem}.srt", "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
    _write(date_dir / f"{stem}.jingting.retry.json", json.dumps({"error": "agy failed"}) + "\n")

    pending = jingting.pending_slices(str(tmp_path), room, "2026-06-29", False)
    retry_pending = jingting.pending_slices(str(tmp_path), room, "2026-06-29", False, retry_failed=True)

    assert str(video) not in pending
    assert str(video) in retry_pending


def test_directory_lock_is_exclusive_and_released(tmp_path):
    lock = tmp_path / "slice.jingting.lock"

    assert jingting.acquire_directory_lock(lock) is True
    assert jingting.acquire_directory_lock(lock) is False
    assert lock.joinpath("pid").exists()

    jingting.release_directory_lock(lock)

    assert not lock.exists()
    assert jingting.acquire_directory_lock(lock) is True
    jingting.release_directory_lock(lock)


def test_run_agy_recovers_structurally_complete_output_after_nonzero_exit(tmp_path, monkeypatch):
    agy = _write(tmp_path / "agy", b"binary")
    source = _write(tmp_path / "source.mp4", b"source")
    draft_text = "1\n00:00:00,000 --> 00:00:01,000\n旧字\n"
    draft = _write(tmp_path / "draft.srt", draft_text)
    output = tmp_path / "refined.srt"
    monkeypatch.setattr(jingting, "AGY_BIN", str(agy))
    monkeypatch.setattr(jingting, "JINGTING_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(
        jingting,
        "remux_for_agy",
        lambda _source, job_dir: _write(job_dir / "input.mp4", b"prepared"),
    )

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "Error: Agent execution terminated due to error."

    def fake_run(_command, *, cwd, **_kwargs):
        _write(Path(cwd) / "output.srt", draft_text.replace("旧字", "新字"))
        return Completed()

    monkeypatch.setattr(jingting.subprocess, "run", fake_run)
    job_dir = Path(jingting.run_agy(str(source), str(draft), str(output)))

    assert "新字" in output.read_text(encoding="utf-8")
    manifest = json.loads(output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["agy_rc"] == 1
    assert manifest["accepted_valid_output_after_nonzero"] is True
    assert Path(manifest["job_dir"]) == job_dir


def test_run_agy_keeps_nonzero_partial_output_fail_closed(tmp_path, monkeypatch):
    agy = _write(tmp_path / "agy", b"binary")
    source = _write(tmp_path / "source.mp4", b"source")
    draft = _write(
        tmp_path / "draft.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n一\n\n2\n00:00:01,000 --> 00:00:02,000\n二\n",
    )
    monkeypatch.setattr(jingting, "AGY_BIN", str(agy))
    monkeypatch.setattr(jingting, "JINGTING_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(
        jingting,
        "remux_for_agy",
        lambda _source, job_dir: _write(job_dir / "input.mp4", b"prepared"),
    )

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "terminated"

    def fake_run(_command, *, cwd, **_kwargs):
        _write(Path(cwd) / "output.srt", "1\n00:00:00,000 --> 00:00:01,000\n一\n")
        return Completed()

    monkeypatch.setattr(jingting.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="cue/timing mismatch"):
        jingting.run_agy(str(source), str(draft), str(tmp_path / "refined.srt"))


def test_run_agy_restores_draft_timing_when_all_cue_indexes_survive(tmp_path, monkeypatch):
    agy = _write(tmp_path / "agy", b"binary")
    source = _write(tmp_path / "source.mp4", b"source")
    draft = _write(
        tmp_path / "draft.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n旧一\n\n2\n00:00:01,000 --> 00:00:02,000\n旧二\n",
    )
    monkeypatch.setattr(jingting, "AGY_BIN", str(agy))
    monkeypatch.setattr(jingting, "JINGTING_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(
        jingting,
        "remux_for_agy",
        lambda _source, job_dir: _write(job_dir / "input.mp4", b"prepared"),
    )

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(_command, *, cwd, **_kwargs):
        _write(
            Path(cwd) / "output.srt",
            "1\n00:00:00,120 --> 00:00:01,120\n新一\n\n2\n00:00:01,200 --> 00:00:02,200\n新二\n",
        )
        return Completed()

    monkeypatch.setattr(jingting.subprocess, "run", fake_run)
    output = tmp_path / "refined.srt"
    jingting.run_agy(str(source), str(draft), str(output))

    assert "新一" in output.read_text(encoding="utf-8")
    assert jingting.srt_signature(output.read_text(encoding="utf-8")) == jingting.srt_signature(
        draft.read_text(encoding="utf-8")
    )


def test_run_agy_classifies_quota_and_preserves_retry_after(tmp_path, monkeypatch):
    from src.autoslice.source_context_executor import AgyRunnerError

    agy = _write(tmp_path / "agy", b"binary")
    source = _write(tmp_path / "source.mp4", b"source")
    draft = _write(tmp_path / "draft.srt", "1\n00:00:00,000 --> 00:00:01,000\n旧字\n")
    monkeypatch.setattr(jingting, "AGY_BIN", str(agy))
    monkeypatch.setattr(jingting, "JINGTING_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(
        jingting,
        "remux_for_agy",
        lambda _source, job_dir: _write(job_dir / "input.mp4", b"prepared"),
    )

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "Error: Individual quota reached. Resets in 40m58s."

    monkeypatch.setattr(jingting.subprocess, "run", lambda *_args, **_kwargs: Completed())
    with pytest.raises(AgyRunnerError) as caught:
        jingting.run_agy(str(source), str(draft), str(tmp_path / "refined.srt"))

    assert caught.value.reason_code == "AGY_QUOTA_EXHAUSTED"
    assert caught.value.retry_after_seconds == 40 * 60 + 58


def test_gemini_api_fallback_rotates_keys_and_restores_draft_timing(tmp_path, monkeypatch):
    source = _write(tmp_path / "source.mp4", b"source")
    draft = _write(tmp_path / "draft.srt", "1\n00:00:00,000 --> 00:00:01,000\n旧字\n")
    output = tmp_path / "refined.srt"
    monkeypatch.setattr(jingting, "JINGTING_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(jingting, "extract_audio", lambda _source, target: bool(_write(Path(target), b"mp3")))
    monkeypatch.setattr(jingting, "gemini_keys", lambda: ["key-1", "key-2"])
    calls = []

    def fake_correct(_audio, _srt, key, **_kwargs):
        calls.append(key)
        if key == "key-1":
            raise RuntimeError("quota")
        return "1\n00:00:00,200 --> 00:00:01,200\n新字\n"

    monkeypatch.setattr(jingting, "gemini_correct", fake_correct)
    job_dir = Path(jingting.run_gemini_api(str(source), str(draft), str(output)))

    assert calls == ["key-1", "key-2"]
    assert "新字" in output.read_text(encoding="utf-8")
    assert jingting.srt_signature(output.read_text(encoding="utf-8")) == jingting.srt_signature(
        draft.read_text(encoding="utf-8")
    )
    manifest = json.loads(output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider"] == "gemini_api"
    assert manifest["accepted_key_ordinal"] == 2
    assert "key-2" not in json.dumps(manifest)
    assert job_dir.is_dir()
