"""CPA is the primary visual witness; AGY is a bounded fallback."""

from __future__ import annotations

import base64
import hashlib
from email.message import Message
import io
import json
import os
from contextlib import nullcontext
from pathlib import Path
import urllib.error

from src.autoslice import agy_frame_witness
from src.autoslice import cpa_frame_witness
from src.autoslice import screen_read_witness
from src.autoslice import visual_witness
from src.autoslice.cover_polish_gate import _verify_polish_face_integrity


def test_cpa_batch_jpeg_probe_binds_ordered_exact_images_and_one_slot(
    monkeypatch,
    tmp_path,
):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first_bytes = b"persisted-jpeg-one"
    second_bytes = b"persisted-jpeg-two"
    first.write_bytes(first_bytes)
    second.write_bytes(second_bytes)
    captured = {}
    slot_calls = []
    response_bytes = json.dumps(
        {"status": "completed", "output_text": "批量结果"}
    ).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return response_bytes

    def fake_urlopen(request, **kwargs):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = kwargs["timeout"]
        return Response()

    def fake_slot(**kwargs):
        slot_calls.append(kwargs)
        return nullcontext()

    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cpa_frame_witness, "runtime_provider_slot", fake_slot)
    receipt = cpa_frame_witness.batch_jpeg_vision_probe(
        [first, second],
        "比较这两张图",
        api_base="https://cpa.example/v1",
        api_key="secret",
        timeout_seconds=17,
        max_tokens=4096,
    )

    assert receipt["status"] == "OBSERVED"
    assert receipt["answer"] == "批量结果"
    assert receipt["images"] == [
        {
            "image_path": str(first.absolute()),
            "image_sha256": hashlib.sha256(first_bytes).hexdigest(),
        },
        {
            "image_path": str(second.absolute()),
            "image_sha256": hashlib.sha256(second_bytes).hexdigest(),
        },
    ]
    content = captured["body"]["input"][0]["content"]
    assert [item["type"] for item in content] == [
        "input_text",
        "input_image",
        "input_image",
    ]
    assert content[0]["text"] == "比较这两张图"
    assert [
        base64.b64decode(item["image_url"].split(",", 1)[1])
        for item in content[1:]
    ] == [first_bytes, second_bytes]
    assert captured["timeout"] == 17
    assert len(slot_calls) == 1
    assert receipt["response_sha256"] == hashlib.sha256(response_bytes).hexdigest()
    prompt_identity = {
        "question": "比较这两张图",
        "image_sha256s": [item["image_sha256"] for item in receipt["images"]],
        "model": "gpt-6-sol",
    }
    assert receipt["prompt_sha256"] == hashlib.sha256(
        json.dumps(prompt_identity, sort_keys=True).encode()
    ).hexdigest()
    assert "secret" not in json.dumps(receipt)


def test_cpa_batch_jpeg_probe_rejects_bounds_and_read_fail_without_dispatch(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fail_urlopen(*_args, **_kwargs):
        calls.append("urlopen")
        raise AssertionError("provider dispatch is forbidden")

    def fail_slot(*_args, **_kwargs):
        calls.append("slot")
        raise AssertionError("provider slot is forbidden")

    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr(cpa_frame_witness, "runtime_provider_slot", fail_slot)
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")

    for paths in ([], [image] * 13):
        receipt = cpa_frame_witness.batch_jpeg_vision_probe(
            paths,
            "q",
            api_base="https://cpa.example/v1",
            api_key="secret",
        )
        assert receipt["status"] == "UNAVAILABLE"
        assert receipt["reason_code"] == "BATCH_IMAGE_COUNT_INVALID"

    missing = cpa_frame_witness.batch_jpeg_vision_probe(
        [image, tmp_path / "missing.jpg"],
        "q",
        api_base="https://cpa.example/v1",
        api_key="secret",
    )
    assert missing["status"] == "UNAVAILABLE"
    assert missing["reason_code"] == "IMAGE_READ_FAILED"
    assert calls == []


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


def _http_error(code: int, *, retry_after: str | None = None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://cpa.example/v1/responses",
        code,
        "Too Many Requests" if code == 429 else "Internal Server Error",
        headers,
        io.BytesIO(b"provider response body must not enter the receipt"),
    )


def test_cpa_http_429_is_capacity_without_persisting_provider_body(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        cpa_frame_witness,
        "_extract_frame_jpeg",
        lambda *args, **kwargs: b"jpegbytes",
    )
    monkeypatch.setattr(
        cpa_frame_witness,
        "runtime_provider_slot",
        lambda **_kwargs: nullcontext(),
    )
    error = _http_error(429, retry_after="120")
    monkeypatch.setattr(
        cpa_frame_witness.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    receipt = cpa_frame_witness.frame_vision_probe(
        tmp_path / "x.mp4",
        0,
        "q",
        api_base="https://cpa.example/v1",
        api_key="secret-not-for-receipts",
    )

    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "VISION_PROVIDER_CAPACITY"
    assert receipt["http_status"] == 429
    assert receipt["retry_after"] == "120"
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "provider response body" not in serialized
    assert "secret-not-for-receipts" not in serialized
    assert "response_sha256" not in receipt
    assert error.closed is True


def test_cpa_non_capacity_http_error_keeps_call_failed_contract(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        cpa_frame_witness,
        "_extract_frame_jpeg",
        lambda *args, **kwargs: b"jpegbytes",
    )
    monkeypatch.setattr(
        cpa_frame_witness,
        "runtime_provider_slot",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        cpa_frame_witness.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(500)),
    )

    receipt = cpa_frame_witness.frame_vision_probe(
        tmp_path / "x.mp4",
        0,
        "q",
        api_base="https://cpa.example/v1",
        api_key="secret-not-for-receipts",
    )

    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "VISION_CALL_FAILED"
    assert receipt["http_status"] == 500
    assert "retry_after" not in receipt
    assert "provider response body" not in json.dumps(receipt, ensure_ascii=False)


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
        stdout = '{"texts":["温柔型甲甲"]}\n'
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


def test_env_screen_probe_reads_private_runtime_cpa_without_ambient(monkeypatch, tmp_path):
    from src.autoslice import llm_client

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = runtime / "cpa.env"
    env_file.write_text("CPA_BASE_URL=https://runtime.example/v1\nCPA_API_KEY=runtime-secret\n")
    env_file.chmod(0o600)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("AGY_BIN", str(tmp_path / "missing-agy"))
    original = llm_client.runtime_cpa_command_environment
    monkeypatch.setattr(
        llm_client,
        "runtime_cpa_command_environment",
        lambda path: original(path, _owner_uid=os.getuid()),
    )
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return "runtime-probe"

    monkeypatch.setattr(screen_read_witness, "make_screen_read_probe", fake_builder)
    assert screen_read_witness.build_env_screen_read_probe(media) == "runtime-probe"
    assert captured == {
        "media_path": media,
        "api_base": "https://runtime.example/v1",
        "api_key": "runtime-secret",
    }


def test_env_screen_probe_missing_runtime_credentials_stays_unavailable(monkeypatch, tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("AGY_BIN", str(tmp_path / "missing-agy"))
    assert screen_read_witness.build_env_screen_read_probe(media) is None


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
