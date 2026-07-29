"""Visual witnesses use AGY; stale CPA image calls fail closed."""

from __future__ import annotations

from pathlib import Path

from src.autoslice import agy_frame_witness
from src.autoslice import screen_read_witness
from src.autoslice.cover_polish_gate import _verify_polish_face_integrity
from src.autoslice.cpa_frame_witness import (
    frame_vision_probe as retired_cpa_frame_probe,
)


def test_agy_observed_receipt_binds_frame_prompt_response(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        agy_frame_witness,
        "_extract_frame_jpeg",
        lambda *args, **kwargs: b"jpegbytes",
    )
    monkeypatch.setenv("AGY_BIN", "/opt/agy")
    monkeypatch.setenv("AGY_VISION_MODEL", "Gemini Vision Test")
    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"texts":["温柔型李豆沙"]}\n'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        job_dir = Path(kwargs["cwd"])
        assert (job_dir / "input.jpg").read_bytes() == b"jpegbytes"
        assert "visible pixels" in (job_dir / "prompt.md").read_text(
            encoding="utf-8"
        )
        return Completed()

    monkeypatch.setattr(agy_frame_witness.subprocess, "run", fake_run)
    receipt = agy_frame_witness.frame_vision_probe(
        tmp_path / "x.mp4",
        10_300,
        "抄录画面ID",
    )

    assert receipt["status"] == "OBSERVED"
    assert receipt["provider"] == "agy"
    assert receipt["model"] == "Gemini Vision Test"
    assert receipt["answer"] == '{"texts":["温柔型李豆沙"]}'
    assert captured["command"][0] == "/opt/agy"
    for key in ("frame_sha256", "prompt_sha256", "response_sha256"):
        assert len(str(receipt[key])) == 64


def test_agy_failures_return_unavailable_receipt_never_raise(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        agy_frame_witness,
        "_extract_frame_jpeg",
        lambda *args, **kwargs: b"jpegbytes",
    )

    def boom(*args, **kwargs):
        raise OSError("agy unavailable")

    monkeypatch.setattr(agy_frame_witness.subprocess, "run", boom)
    receipt = agy_frame_witness.frame_vision_probe(
        tmp_path / "x.mp4",
        0,
        "q",
    )
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "AGY_VISION_CALL_FAILED"


def test_retired_cpa_image_probe_refuses_without_network(tmp_path):
    receipt = retired_cpa_frame_probe(
        tmp_path / "x.mp4",
        0,
        "q",
        api_base="https://cpa.example/v1",
        api_key="secret",
    )

    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "CPA_TEXT_ONLY"
    assert receipt["provider"] == "cpa"


def test_env_screen_probe_requires_agy_not_cpa_credentials(
    monkeypatch,
    tmp_path,
):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    agy = tmp_path / "agy"
    agy.write_bytes(b"binary")
    monkeypatch.setenv("AGY_BIN", str(agy))
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return "probe"

    monkeypatch.setattr(
        screen_read_witness,
        "make_screen_read_probe",
        fake_builder,
    )

    assert screen_read_witness.build_env_screen_read_probe(media) == "probe"
    assert captured == {
        "media_path": media,
        "api_base": "",
        "api_key": "",
    }


def test_polish_face_gate_uses_agy_without_cpa_credentials(
    monkeypatch,
    tmp_path,
):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")

    def fake_probe(image_path, question, **kwargs):
        assert image_path == cover
        assert kwargs["api_base"] == ""
        assert kwargs["api_key"] == ""
        return {
            "schema_version": "agy-frame-witness.v1",
            "status": "OBSERVED",
            "provider": "agy",
            "image_sha256": "abc",
            "answer": (
                '{"face_complete":true,"missing":[],'
                '"tongue_out":false,"reason":"完整"}'
            ),
        }

    monkeypatch.setattr(agy_frame_witness, "image_vision_probe", fake_probe)
    verification = _verify_polish_face_integrity(
        cover,
        base_url="",
        api_key="",
    )

    assert verification["status"] == "PASS"
    assert verification["witness"]["provider"] == "agy"
