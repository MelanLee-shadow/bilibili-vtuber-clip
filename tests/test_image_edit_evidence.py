"""Offline adapter tests: synthetic pixels/HTTP replies, no provider requests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice import cover_generation as cover


def _png(size=(48, 32)):
    output = io.BytesIO()
    Image.new("RGB", size, (18, 46, 70)).save(output, format="PNG")
    return output.getvalue()


class Response(io.BytesIO):
    status = 200

    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _run(tmp_path, monkeypatch, *, metadata=None, model="gpt-image-2.5-flare"):
    image = _png()
    reference = tmp_path / "ref.png"
    reference.write_bytes(image)
    calls = []

    def respond(request, *, timeout):
        calls.append(request)
        return Response(
            {"data": [{"b64_json": base64.b64encode(image).decode()}], **(metadata or {})},
            {"x-request-id": "req_synthetic_42"},
        )

    monkeypatch.setattr(cover.urllib.request, "urlopen", respond)
    result = cover._call_cpa_image_edit.__wrapped__(
        base_url="https://synthetic.invalid/v1",
        api_key="SYNTHETIC_PRIVATE_KEY",
        reference_path=reference,
        output_path=tmp_path / "background.png",
        prompt="Synthetic fixture; not a real character or a production cover.",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        model_candidates=(model,),
    )
    return result, image, calls


def test_real_adapter_keeps_usage_reported_identity_and_pre_crop_bytes(tmp_path, monkeypatch):
    usage = {
        "input_tokens": 30,
        "input_tokens_details": {"text_tokens": 10, "image_tokens": 20},
        "output_tokens": 40,
        "total_tokens": 70,
    }
    result, original, calls = _run(
        tmp_path,
        monkeypatch,
        metadata={
            "model": "gpt-image-2.5-flare-2026-09-08",
            "quality": "high",
            "size": "48x32",
            "output_format": "png",
            "usage": usage,
            "private_field": "DO_NOT_PERSIST",
        },
    )
    assert result["status"] == "AI_BACKGROUND_READY" and len(calls) == 1
    evidence = json.loads((tmp_path / "response.json").read_text())["attempts"][0]
    observed = evidence["service_observation"]
    assert observed["requested_model"] == "gpt-image-2.5-flare"
    assert observed["reported_model"] == "gpt-image-2.5-flare-2026-09-08"
    assert observed["upstream_identity_verified"] is False
    assert observed["usage"] == usage and observed["usage_status"] == "REPORTED_VALID"
    assert observed["request_id"] == "req_synthetic_42"
    assert observed["reported_quality"] == "high"
    raw = evidence["raw_image"]
    assert Path(raw["path"]).read_bytes() == original
    assert raw["sha256"] == "sha256:" + hashlib.sha256(original).hexdigest()
    assert raw["dimensions"] == [48, 32]
    assert evidence["canvas"] == [1920, 1080]
    assert (tmp_path / "background.png").read_bytes() != original
    assert 0 <= observed["request_elapsed_seconds"] <= evidence["attempt_elapsed_seconds"]
    combined = (tmp_path / "request.json").read_text() + (tmp_path / "response.json").read_text()
    assert "SYNTHETIC_PRIVATE_KEY" not in combined and "DO_NOT_PERSIST" not in combined


def test_missing_usage_and_upstream_model_remain_unknown(tmp_path, monkeypatch):
    result, _, _ = _run(tmp_path, monkeypatch)
    evidence = json.loads((tmp_path / "response.json").read_text())["attempts"][0]
    observed = evidence["service_observation"]
    assert result["selected_model"] == "gpt-image-2.5-flare"
    assert observed["reported_model"] is None and observed["upstream_identity_verified"] is False
    assert observed["usage"] is None and observed["usage_status"] == "NOT_REPORTED"
    assert observed["cost_usd"] is None and observed["cost_status"] == "NOT_MEASURED"
    assert set(observed["requested_parameters"]) == {"model", "size"}
    assert observed["reported_quality"] is None and observed["reported_size"] is None


@pytest.mark.parametrize(
    "usage",
    [
        [],
        "unknown",
        {},
        {"input_tokens": -1},
        {"input_tokens": True},
        {"output_tokens": 1.5},
        {"input_tokens_details": {"cached_tokens": False}},
        {"input_tokens_details": []},
    ],
)
def test_invalid_usage_is_not_billing_or_a_reason_to_regenerate(tmp_path, monkeypatch, usage):
    result, _, calls = _run(tmp_path, monkeypatch, metadata={"usage": usage})
    observed = json.loads((tmp_path / "response.json").read_text())["attempts"][0][
        "service_observation"
    ]
    assert result["status"] == "AI_BACKGROUND_READY" and len(calls) == 1
    assert observed["usage"] is None and observed["usage_status"] == "REPORTED_INVALID"
    assert observed["cost_usd"] is None


@pytest.mark.parametrize(
    ("usage", "status"),
    [
        ({"input_tokens": 2}, "REPORTED_PARTIAL"),
        ({"input_tokens": 2, "output_tokens": 3, "total_tokens": 100}, "REPORTED_INCONSISTENT"),
        ({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "REPORTED_VALID"),
    ],
)
def test_reported_usage_kept_but_not_silently_reconciled(tmp_path, monkeypatch, usage, status):
    _, _, _ = _run(tmp_path, monkeypatch, metadata={"usage": usage})
    observed = json.loads((tmp_path / "response.json").read_text())["attempts"][0][
        "service_observation"
    ]
    assert observed["usage"] == usage and observed["usage_status"] == status
    assert observed["cost_usd"] is None


@pytest.mark.parametrize(
    "model",
    [
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
        "gpt-image-2.5-flare-2026-09-08",
        "gpt-image-2.5-sunburst-2026-09-08",
    ],
)
def test_explicit_candidate_is_not_implicitly_rerouted(tmp_path, monkeypatch, model):
    result, _, calls = _run(tmp_path, monkeypatch, model=model, metadata={"model": "gpt-image-1.5"})
    observed = json.loads((tmp_path / "response.json").read_text())["attempts"][0][
        "service_observation"
    ]
    assert len(calls) == 1 and result["attempted_models"] == [model]
    assert model.encode() in calls[0].data
    assert observed["reported_model"] == "gpt-image-1.5"
    assert observed["requested_model"] == model and observed["upstream_identity_verified"] is False


def test_default_selection_and_downstream_allowlist_unchanged(monkeypatch):
    monkeypatch.delenv("CPA_IMAGE_MODEL", raising=False)
    assert cover._cpa_image_model_candidates() == ("gpt-image-2", "gpt-image-1.5")
    # This change does not authorize model migration or modify the repair gate.
    source = Path(__file__).resolve().parents[1] / "src/autoslice/cover_repair.py"
    assert 'if model not in {"gpt-image-2", "gpt-image-1.5"}' in source.read_text()


@pytest.mark.parametrize("value", [None, "[]", "not-json"])
def test_malformed_envelope_stays_typed_failure(tmp_path, monkeypatch, value):
    reference = tmp_path / "ref.png"
    reference.write_bytes(_png())
    calls = []

    def fake(request, **kwargs):
        calls.append(request)
        result = Response(None)
        result.seek(0)
        result.truncate()
        result.write(("null" if value is None else value).encode())
        result.seek(0)
        return result

    monkeypatch.setattr(cover.urllib.request, "urlopen", fake)
    result = cover._call_cpa_image_edit.__wrapped__(
        base_url="https://test.invalid",
        api_key="private",
        reference_path=reference,
        output_path=tmp_path / "out.png",
        prompt="test",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        model_candidates=("gpt-image-2", "gpt-image-1.5"),
    )
    assert result["reason_code"] == "CPA_IMAGE_EDIT_BAD_JSON" and len(calls) == 1
    assert (
        json.loads((tmp_path / "response.json").read_text())["attempts"][0][
            "attempt_elapsed_seconds"
        ]
        >= 0
    )


def test_invalid_image_bytes_are_evidence_not_a_success(tmp_path, monkeypatch):
    reference = tmp_path / "ref.png"
    reference.write_bytes(_png())

    def fake(*_args, **_kwargs):
        return Response({"data": [{"b64_json": base64.b64encode(b"not-an-image").decode()}]})

    monkeypatch.setattr(cover.urllib.request, "urlopen", fake)
    result = cover._call_cpa_image_edit.__wrapped__(
        base_url="https://test.invalid",
        api_key="private",
        reference_path=reference,
        output_path=tmp_path / "out.png",
        prompt="test",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )
    evidence = json.loads((tmp_path / "response.json").read_text())["attempts"][0]
    assert result["reason_code"] == "CPA_IMAGE_EDIT_BAD_IMAGE"
    assert Path(evidence["raw_image"]["path"]).read_bytes() == b"not-an-image"
    assert evidence["raw_image"]["dimensions"] is None


def test_raw_evidence_is_idempotent_and_never_overwrites_a_conflict(tmp_path):
    from src.autoslice.cpa_image_edit import preserve_response_image

    output = tmp_path / "out.png"
    output.write_bytes(_png())
    response = tmp_path / "response.json"
    result = preserve_response_image(output, response)
    assert preserve_response_image(output, response) == result
    raw = Path(result["path"])
    raw.write_bytes(b"other earlier evidence")
    with pytest.raises(ValueError, match="conflict"):
        preserve_response_image(output, response)
    assert raw.read_bytes() == b"other earlier evidence"


def test_arbitrary_metadata_is_not_serialized():
    from src.autoslice.cpa_image_edit import service_observation

    result = service_observation(
        {
            "model": "Bearer SECRET\nTOKEN",
            "quality": ["high"],
            "size": "https://unsafe.invalid/token",
            "output_format": {},
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "api_key": "HIDDEN",
            },
        },
        requested_model="gpt-image-2",
        requested_size="1920x1088",
        headers={"x-request-id": "Bearer SECRET\nTOKEN"},
        request_elapsed_seconds=0.5,
    )
    assert all(
        result[key] is None
        for key in [
            "reported_model",
            "reported_quality",
            "reported_size",
            "reported_output_format",
            "request_id",
        ]
    )
    assert "HIDDEN" not in json.dumps(result) and "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_raw_image_evidence_does_not_accept_aliases(tmp_path, kind):
    import os
    from src.autoslice.cpa_image_edit import preserve_response_image

    output = tmp_path / "image.png"
    output.write_bytes(_png())
    response = tmp_path / "response.json"
    expected = tmp_path / (
        response.stem + ".raw-image-" + hashlib.sha256(output.read_bytes()).hexdigest() + ".bin"
    )
    output.chmod(0o600)
    if kind == "symlink":
        expected.symlink_to(output)
    else:
        os.link(output, expected)
    with pytest.raises(ValueError, match="conflict"):
        preserve_response_image(output, response)
    assert output.read_bytes() == _png()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_failure_time_recorded_without_second_model_attempt(tmp_path, monkeypatch, status):
    import urllib.error

    reference = tmp_path / "ref.png"
    reference.write_bytes(_png())
    calls = []

    def fake(request, **_kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(
            request.full_url, status, "fixture", {}, io.BytesIO(b'{"error":{"code":"test"}}')
        )

    monkeypatch.setattr(cover.urllib.request, "urlopen", fake)
    result = cover._call_cpa_image_edit.__wrapped__(
        base_url="https://test.invalid",
        api_key="fixture",
        reference_path=reference,
        output_path=tmp_path / "out.png",
        prompt="fixture",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        model_candidates=("gpt-image-2", "gpt-image-1.5"),
    )
    assert (
        len(calls) == 1
        and result["status"] == "FAILED"
        and result["attempted_models"] == ["gpt-image-2"]
    )
    evidence = json.loads((tmp_path / "response.json").read_text())["attempts"][0]
    assert evidence["status_code"] == status and evidence["attempt_elapsed_seconds"] >= 0
    assert not list(tmp_path.glob("*.raw-image-*.bin"))
