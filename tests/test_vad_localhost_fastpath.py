"""VAD provider 的 localhost 快路径：免 scp/免 self-ssh，远端语义不变。"""

from pathlib import Path

import src.autoslice.subtitle_timing_qa as timing_qa
from src.autoslice.subtitle_timing_qa import SpeechSpan


def _fake_run_factory(calls):
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"RIFF-fake-wav")
        else:
            Result.stdout = (
                '{"frame_ms": 32, "spans": [{"start_ms": 100, "end_ms": 300}]}'
            )
        return Result()

    return fake_run


def test_localhost_provider_never_shells_out_to_ssh(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(timing_qa.subprocess, "run", _fake_run_factory(calls))
    provider = timing_qa.build_ssh_silero_vad_provider("localhost")

    spans = provider(tmp_path / "src.mp4", 1_000, 5_000)

    assert spans == [SpeechSpan(start_ms=1_100, end_ms=1_300)]
    assert not any(cmd[0] in {"ssh", "scp"} for cmd in calls)
    python_calls = [cmd for cmd in calls if cmd[0] == "python3"]
    assert len(python_calls) == 1
    # 本地直跑仍然用同一个 spans 脚本（env/默认解析不因快路径改变）
    assert python_calls[0][1].endswith("silero_vad_spans.py")


def test_remote_provider_still_ships_wav_over_ssh(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(timing_qa.subprocess, "run", _fake_run_factory(calls))
    provider = timing_qa.build_ssh_silero_vad_provider("free")

    spans = provider(tmp_path / "src.mp4", 0, 2_000)

    assert spans == [SpeechSpan(start_ms=100, end_ms=300)]
    assert any(cmd[0] == "scp" for cmd in calls)
    assert any(cmd[0] == "ssh" for cmd in calls)
    assert not any(cmd[0] == "python3" for cmd in calls)


def test_env_override_steers_localhost_script_path(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(timing_qa.subprocess, "run", _fake_run_factory(calls))
    monkeypatch.setenv("AUTOSLICE_VAD_SCRIPT", "/custom/spans.py")
    provider = timing_qa.build_ssh_silero_vad_provider("127.0.0.1")

    provider(tmp_path / "src.mp4", 0, 1_000)

    python_calls = [cmd for cmd in calls if cmd[0] == "python3"]
    assert python_calls and python_calls[0][1] == "/custom/spans.py"
