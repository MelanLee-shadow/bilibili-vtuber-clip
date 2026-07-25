"""CPA 画面见证收据契约（网络层 mock，真调用由 free 金丝雀验证）。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from src.autoslice import cpa_frame_witness
from src.autoslice.cpa_frame_witness import frame_vision_probe


def _fake_frame(monkeypatch, payload: bytes = b"jpegbytes") -> None:
    monkeypatch.setattr(
        cpa_frame_witness, "_extract_frame_jpeg", lambda *a, **k: payload
    )


def test_observed_receipt_binds_frame_prompt_response(monkeypatch, tmp_path):
    _fake_frame(monkeypatch)
    raw = json.dumps({"output_text": "温柔型李豆沙"}).encode("utf-8")

    class _Resp:
        def read(self):
            return raw
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", fake_urlopen)
    receipt = frame_vision_probe(
        tmp_path / "x.mp4", 10_300, "抄录画面ID",
        api_base="https://cpa.example/v1", api_key="k", model="gpt-5.5",
    )
    assert receipt["status"] == "OBSERVED"
    assert receipt["answer"] == "温柔型李豆沙"
    assert captured["url"].endswith("/responses")
    content = captured["body"]["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    for key in ("frame_sha256", "prompt_sha256", "response_sha256"):
        assert len(str(receipt[key])) == 64


def test_failures_return_unavailable_receipt_never_raise(monkeypatch, tmp_path):
    _fake_frame(monkeypatch)

    def boom(request, timeout):
        raise OSError("gateway down")

    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", boom)
    receipt = frame_vision_probe(
        tmp_path / "x.mp4", 0, "q",
        api_base="https://cpa.example/v1", api_key="k", model="gpt-5.5",
    )
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "VISION_CALL_FAILED"

    def no_frame(*a, **k):
        raise RuntimeError("ffmpeg missing")

    monkeypatch.setattr(cpa_frame_witness, "_extract_frame_jpeg", no_frame)
    receipt2 = frame_vision_probe(
        tmp_path / "x.mp4", 0, "q",
        api_base="https://cpa.example/v1", api_key="k", model="gpt-5.5",
    )
    assert receipt2["status"] == "UNAVAILABLE"
    assert receipt2["reason_code"] == "FRAME_EXTRACT_FAILED"
