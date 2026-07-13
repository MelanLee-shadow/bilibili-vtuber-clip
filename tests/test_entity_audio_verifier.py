import json
from pathlib import Path

from src.autoslice import entity_audio_verifier as verifier_module


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _request():
    return {
        "schema_version": "chat-entity-verification-request.v1",
        "request_sha256": "a" * 64,
        "evidence_id": "b" * 64,
        "matched_start_ms": 2_000,
        "matched_end_ms": 4_000,
        # These locate/audit the outer decision but must never enter AGY's prompt.
        "exact_text": "还没看，怎么有人说有母鸡卡的风险",
        "matched_audio_text": "还没看怎么有人说有母鸡卡的风险",
        "candidate_entities": [
            {"canonical": "梦限大", "surfaces": ["梦限大", "梦现代"], "readings": ["meng xian da"]},
            {"canonical": "Ave Mujica", "surfaces": ["Mujica", "母鸡卡"], "readings": ["mujica"]},
        ],
    }


def test_audio_verifier_uses_black_frame_clip_and_neutral_prompt(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video pixels and audio")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"black frame plus cropped audio")
            return _Completed()
        job_dir = Path(kwargs["cwd"])
        (job_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "RESOLVED",
                    "canonical_entity": "梦限大",
                    "heard_syllables": "meng xian da",
                    "confidence": 0.98,
                    "reason": "three distinct syllables",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "梦限大"
    ffmpeg = commands[0]
    assert "color=c=black:s=320x240:r=10" in ffmpeg
    assert ffmpeg[ffmpeg.index("-map") + 1] == "1:v:0"
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
    assert "还没看" not in prompt
    assert "怎么有人说" not in prompt
    assert "viewer chat" in prompt
    assert verdict["source_media_sha256"] != verdict["audio_clip_sha256"]


def test_audio_verifier_low_confidence_is_uncertain(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"audio")
            return _Completed()
        Path(kwargs["cwd"], "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "RESOLVED",
                    "canonical_entity": "梦限大",
                    "heard_syllables": "unclear",
                    "confidence": 0.79,
                    "reason": "unclear",
                }
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    assert verify(_request())["status"] == "UNCERTAIN"
