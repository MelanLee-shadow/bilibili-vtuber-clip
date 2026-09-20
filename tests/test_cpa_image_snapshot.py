"""Real image conversion controls; every HTTP response is explicitly synthetic."""

from __future__ import annotations

import base64
from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess

from PIL import Image
import pytest

from src.autoslice import cpa_frame_witness as cpa


def _png(color: tuple[int, int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (96, 64), color).save(stream, format="PNG")
    return stream.getvalue()


def _legacy_jpeg(path: Path, output: Path, *, max_width: int = 1280) -> bytes:
    # Exact old file-input conversion, retained as the ordinary-format control.
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-vf",
            f"scale=min({max_width}\\,iw):-2",
            "-q:v",
            "4",
            str(output),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    return output.read_bytes()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("This test must not contact a real model or network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(cpa.urllib.request, "urlopen", denied)
    monkeypatch.setattr(cpa, "runtime_provider_slot", lambda **_: nullcontext())


def _capture_http(monkeypatch) -> dict:
    captured = {"calls": 0}
    response_bytes = json.dumps({"output_text": "synthetic transport answer"}).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return response_bytes

    def fake_urlopen(request, **kwargs):
        captured["calls"] += 1
        captured["body"] = json.loads(request.data)
        captured["timeout"] = kwargs["timeout"]
        data_uri = captured["body"]["input"][0]["content"][1]["image_url"]
        captured["jpeg"] = base64.b64decode(data_uri.split(",", 1)[1])
        return Response()

    monkeypatch.setattr(cpa.urllib.request, "urlopen", fake_urlopen)
    captured["response_bytes"] = response_bytes
    return captured


@pytest.mark.parametrize("replacement", ["atomic", "in_place"])
def test_path_replacement_cannot_change_the_hashed_image_sent_to_cpa(
    monkeypatch,
    tmp_path,
    replacement,
):
    path = tmp_path / "cover.png"
    original = _png((210, 20, 30))
    different = _png((20, 30, 210))
    path.write_bytes(original)
    expected_jpeg = _legacy_jpeg(path, tmp_path / "expected.jpg")
    captured = _capture_http(monkeypatch)
    real_run = subprocess.run
    conversions = []

    def swap_before_conversion(argv, **kwargs):
        # This point is after Python's original-byte read but before FFmpeg opens
        # its input. Only our synthetic input is replaced; production files are not.
        if argv[0] == "ffmpeg":
            conversions.append(argv)
            if replacement == "atomic":
                successor = tmp_path / "new.png"
                successor.write_bytes(different)
                os.replace(successor, path)
            else:
                path.write_bytes(different)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cpa.subprocess, "run", swap_before_conversion)
    receipt = cpa.image_vision_probe(
        path,
        "synthetic question",
        api_base="https://test.invalid/v1",
        api_key="synthetic-key-not-a-credential",
    )
    assert receipt["status"] == "OBSERVED"
    assert receipt["image_sha256"] == hashlib.sha256(original).hexdigest()
    assert captured["jpeg"] == expected_jpeg, "A's hash was bound to B's actual wire image"
    assert receipt["frame_sha256"] == hashlib.sha256(expected_jpeg).hexdigest()
    assert path.read_bytes() == different
    assert captured["calls"] == len(conversions) == 1


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "BMP", "TIFF"])
@pytest.mark.parametrize("max_width", [1280, 48])
def test_snapshot_conversion_preserves_ordinary_format_and_scaling_bytes(
    monkeypatch,
    tmp_path,
    fmt,
    max_width,
):
    path = tmp_path / ("input." + fmt.lower())
    im = Image.new("RGB", (96, 64), (45, 90, 135))
    for x in range(15, 60):
        for y in range(10, 48):
            im.putpixel((x, y), (x * 3, y * 4, (x + y) * 2))
    im.save(path, format=fmt)
    original = path.read_bytes()
    expected_jpeg = _legacy_jpeg(path, tmp_path / "legacy.jpg", max_width=max_width)
    captured = _capture_http(monkeypatch)
    receipt = cpa.image_vision_probe(
        path,
        "same question",
        api_base="https://test.invalid/v1",
        api_key="synthetic-key",
        max_width=max_width,
        model="synthetic-model",
        timeout_seconds=12,
        max_tokens=83,
    )
    assert receipt["status"] == "OBSERVED"
    assert captured["jpeg"] == expected_jpeg
    assert path.read_bytes() == original
    assert receipt["schema_version"] == "cpa-frame-witness.v1"
    assert receipt["image_sha256"] == hashlib.sha256(original).hexdigest()
    assert receipt["frame_sha256"] == hashlib.sha256(expected_jpeg).hexdigest()
    assert captured["body"]["model"] == "synthetic-model"
    assert captured["body"]["reasoning"] == {"effort": "low"}
    assert captured["body"]["max_output_tokens"] == 83
    assert captured["timeout"] == 12
    assert captured["body"]["input"][0]["content"][0]["text"] == "same question"
    assert receipt["response_sha256"] == hashlib.sha256(captured["response_bytes"]).hexdigest()
    prompt_identity = {
        "question": "same question",
        "frame_sha256": receipt["frame_sha256"],
        "model": "synthetic-model",
    }
    assert (
        receipt["prompt_sha256"]
        == hashlib.sha256(json.dumps(prompt_identity, sort_keys=True).encode()).hexdigest()
    )


@pytest.mark.parametrize("bad_bytes", [b"", b"not an image", b"\x89PNG\r\n\x1a\n"])
def test_bad_frozen_input_never_reaches_http(monkeypatch, tmp_path, bad_bytes):
    path = tmp_path / "bad.png"
    path.write_bytes(bad_bytes)
    captured = _capture_http(monkeypatch)
    receipt = cpa.image_vision_probe(
        path,
        "q",
        api_base="https://test.invalid/v1",
        api_key="synthetic-key",
    )
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "IMAGE_READ_FAILED"
    assert captured["calls"] == 0
    assert "frame_sha256" not in receipt
    assert "answer" not in receipt


def test_missing_credentials_do_not_read_image_or_encode(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("No image read or encoder without configuration")

    monkeypatch.setattr(Path, "read_bytes", denied)
    monkeypatch.setattr(cpa.subprocess, "run", denied)
    receipt = cpa.image_vision_probe(tmp_path / "missing.png", "q", api_base="", api_key="")
    assert receipt["reason_code"] == "CPA_CREDENTIALS_MISSING"
    assert receipt["status"] == "UNAVAILABLE"


def test_conversion_failure_retains_existing_error_contract(monkeypatch, tmp_path):
    path = tmp_path / "valid.png"
    path.write_bytes(_png((30, 60, 90)))
    captured = _capture_http(monkeypatch)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "synthetic ffmpeg failure")

    monkeypatch.setattr(cpa.subprocess, "run", fail)
    receipt = cpa.image_vision_probe(
        path,
        "q",
        api_base="https://test.invalid/v1",
        api_key="synthetic-key",
    )
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "IMAGE_READ_FAILED"
    assert captured["calls"] == 0


@pytest.mark.parametrize("encoding_fails", [False, True])
def test_owned_input_snapshot_is_private_seekable_and_removed(
    monkeypatch, tmp_path, encoding_fails
):
    path = tmp_path / "source.png"
    original = _png((45, 90, 135))
    path.write_bytes(original)
    captured = _capture_http(monkeypatch)
    real_run = subprocess.run
    frozen = []

    def inspect_snapshot(argv, **kwargs):
        if argv[0] == "ffmpeg":
            snapshot = Path(argv[argv.index("-i") + 1])
            assert snapshot != path
            assert snapshot.suffix == path.suffix
            assert snapshot.read_bytes() == original
            assert snapshot.stat().st_mode & 0o777 == 0o600
            with snapshot.open("rb") as reader:
                assert reader.seekable()
            frozen.append(snapshot)
            if encoding_fails:
                raise subprocess.CalledProcessError(1, "synthetic conversion failure")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cpa.subprocess, "run", inspect_snapshot)
    receipt = cpa.image_vision_probe(
        path,
        "q",
        api_base="https://test.invalid/v1",
        api_key="synthetic-key",
    )
    assert len(frozen) == 1
    assert all(not p.exists() for p in frozen)
    assert path.read_bytes() == original
    assert receipt["status"] == ("UNAVAILABLE" if encoding_fails else "OBSERVED")
    assert captured["calls"] == int(not encoding_fails)
