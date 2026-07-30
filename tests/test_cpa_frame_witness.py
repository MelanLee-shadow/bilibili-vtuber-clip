"""CPA is the primary visual witness; AGY is a bounded fallback."""

from __future__ import annotations

import json
from pathlib import Path

from src.autoslice import agy_frame_witness
from src.autoslice import cpa_frame_witness
from src.autoslice import screen_read_witness
from src.autoslice import visual_witness
from src.autoslice.cover_polish_gate import _verify_polish_face_integrity


def test_cpa_observed_receipt_binds_frame_prompt_response(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        cpa_frame_witness,
        "_extract_frame_jpeg",
        lambda *args, **kwargs: b"jpegbytes",
    )
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"status": "completed", "output_text": "屏幕文字"}
            ).encode()

    def fake_urlopen(request, **kwargs):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = kwargs["timeout"]
        return Response()

    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", fake_urlopen)
    receipt = cpa_frame_witness.frame_vision_probe(
        tmp_path / "x.mp4",
        10_300,
        "抄录画面ID",
        api_base="https://cpa.example/v1",
        api_key="secret",
    )

    assert receipt["status"] == "OBSERVED"
    assert receipt["provider"] == "cpa"
    assert receipt["answer"] == "屏幕文字"
    content = captured["body"]["input"][0]["content"]
    assert [item["type"] for item in content] == ["input_text", "input_image"]
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    for key in ("frame_sha256", "prompt_sha256", "response_sha256"):
        assert len(str(receipt[key])) == 64


def test_cpa_missing_credentials_fails_closed_without_extract(
    monkeypatch,
    tmp_path,
):
    def should_not_run(*_args, **_kwargs):
        raise AssertionError("frame extraction should not run")

    monkeypatch.setattr(
        cpa_frame_witness,
        "_extract_frame_jpeg",
        should_not_run,
    )
    receipt = cpa_frame_witness.frame_vision_probe(
        tmp_path / "x.mp4",
        0,
        "q",
        api_base="",
        api_key="",
    )

    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "CPA_CREDENTIALS_MISSING"


def test_visual_router_prefers_cpa_and_does_not_spend_agy():
    calls = []

    def cpa(*_args, **_kwargs):
        calls.append("cpa")
        return {"status": "OBSERVED", "provider": "cpa", "answer": "ok"}

    def agy(*_args, **_kwargs):
        calls.append("agy")
        return {"status": "OBSERVED", "provider": "agy", "answer": "wrong"}

    receipt = visual_witness.frame_vision_probe(
        Path("x.mp4"),
        0,
        "q",
        api_base="https://cpa.example/v1",
        api_key="secret",
        cpa_probe=cpa,
        agy_probe=agy,
    )

    assert calls == ["cpa"]
    assert receipt["provider"] == "cpa"
    assert receipt["routing"]["fallback_used"] is False


def test_visual_router_uses_agy_only_after_cpa_unavailable():
    calls = []

    def cpa(*_args, **_kwargs):
        calls.append("cpa")
        return {
            "status": "UNAVAILABLE",
            "provider": "cpa",
            "model": "gpt-5.6-sol",
            "reason_code": "VISION_CALL_FAILED",
        }

    def agy(*_args, **_kwargs):
        calls.append("agy")
        return {"status": "OBSERVED", "provider": "agy", "answer": "ok"}

    receipt = visual_witness.frame_vision_probe(
        Path("x.mp4"),
        0,
        "q",
        api_base="https://cpa.example/v1",
        api_key="secret",
        cpa_probe=cpa,
        agy_probe=agy,
    )

    assert calls == ["cpa", "agy"]
    assert receipt["provider"] == "agy"
    routing = receipt["routing"]
    assert routing["preferred_provider"] == "cpa"
    assert routing["selected_provider"] == "agy"
    assert routing["fallback_used"] is True
    assert routing["primary_status"] == "UNAVAILABLE"
    assert routing["primary_reason_code"] == "VISION_CALL_FAILED"
    assert routing["primary_model"] == "gpt-5.6-sol"
    assert routing["primary_receipt"] == {
        "status": "UNAVAILABLE",
        "provider": "cpa",
        "model": "gpt-5.6-sol",
        "reason_code": "VISION_CALL_FAILED",
    }


def test_visual_router_falls_back_when_cpa_answer_breaks_contract():
    calls = []

    def cpa(*_args, **_kwargs):
        calls.append("cpa")
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "model": "gpt-5.6-sol",
            "answer": "not json",
        }

    def agy(*_args, **_kwargs):
        calls.append("agy")
        return {
            "status": "OBSERVED",
            "provider": "agy",
            "answer": '{"ok":true}',
        }

    receipt = visual_witness.image_vision_probe(
        Path("x.png"),
        "q",
        cpa_probe=cpa,
        agy_probe=agy,
        answer_validator=lambda answer: answer.startswith("{"),
    )

    assert calls == ["cpa", "agy"]
    assert receipt["provider"] == "agy"
    assert receipt["routing"]["primary_status"] == "OBSERVED_UNUSABLE"
    assert receipt["routing"]["primary_reason_code"] == (
        "PRIMARY_ANSWER_CONTRACT_INVALID"
    )


def test_agy_observed_receipt_still_binds_frame_prompt_response(
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

    class Completed:
        returncode = 0
        stdout = '{"texts":["温柔型李豆沙"]}\n'
        stderr = ""

    monkeypatch.setattr(
        agy_frame_witness.subprocess,
        "run",
        lambda *_args, **_kwargs: Completed(),
    )
    receipt = agy_frame_witness.frame_vision_probe(
        tmp_path / "x.mp4",
        10_300,
        "抄录画面ID",
    )

    assert receipt["status"] == "OBSERVED"
    assert receipt["provider"] == "agy"
    for key in ("frame_sha256", "prompt_sha256", "response_sha256"):
        assert len(str(receipt[key])) == 64


def test_env_screen_probe_uses_cpa_without_agy(monkeypatch, tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    monkeypatch.setenv("AGY_BIN", str(tmp_path / "missing-agy"))
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example/v1")
    monkeypatch.setenv("CPA_API_KEY", "secret")
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return "probe"

    monkeypatch.setattr(screen_read_witness, "make_screen_read_probe", fake_builder)

    assert screen_read_witness.build_env_screen_read_probe(media) == "probe"
    assert captured == {
        "media_path": media,
        "api_base": "https://cpa.example/v1",
        "api_key": "secret",
    }


def test_polish_face_gate_uses_cpa_primary(monkeypatch, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")

    def fake_probe(image_path, _question, **kwargs):
        assert image_path == cover
        assert kwargs["api_base"] == "https://cpa.example/v1"
        assert kwargs["api_key"] == "secret"
        return {
            "schema_version": "cpa-frame-witness.v1",
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": "abc",
            "answer": (
                '{"face_complete":true,"missing":[],'
                '"tongue_out":false,"reason":"完整"}'
            ),
        }

    monkeypatch.setattr(cpa_frame_witness, "image_vision_probe", fake_probe)
    verification = _verify_polish_face_integrity(
        cover,
        base_url="https://cpa.example/v1",
        api_key="secret",
    )

    assert verification["status"] == "PASS"
    assert verification["witness"]["provider"] == "cpa"
    assert verification["preferred_witness_provider"] == "cpa"
