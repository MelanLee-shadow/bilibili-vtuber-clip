"""Model-upgrade regressions; all provider responses here are synthetic."""

from __future__ import annotations

import base64
from contextlib import nullcontext
import io
import json
import socket
import urllib.error
import urllib.request

from PIL import Image
import pytest

from scripts import regenerate_channel_cover as cover_only
from scripts import run_auto_review_shadow_pipeline as shadow
from src.autoslice import cover_generation, cover_repair, cover_repair_route_lineage
from src.autoslice import cpa_image_edit, final_human_review, provider_slots, publish_staging
from src.autoslice.cover_polish_gate import _materialize_screenshot_polish
from tests.cover_binding_test_support import (
    _cover_binding_fixture,
    _screenshot_polish_binding_fixture,
)


@pytest.fixture(autouse=True)
def no_provider_or_network(monkeypatch):
    monkeypatch.delenv("CPA_IMAGE_MODEL", raising=False)
    monkeypatch.setattr(provider_slots, "runtime_provider_slot", lambda **_: nullcontext())

    def denied(*_args, **_kwargs):
        raise AssertionError("model-default regression must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, ("gpt-image-2.5", "gpt-image-2")),
        ("", ("gpt-image-2.5", "gpt-image-2")),
        (" \t ", ("gpt-image-2.5", "gpt-image-2")),
        ("gpt-image-2.5", ("gpt-image-2.5", "gpt-image-2")),
        ("gpt-image-2", ("gpt-image-2",)),
        ("gpt-image-1.5", ("gpt-image-1.5",)),
        (" custom-image ", ("custom-image",)),
    ],
)
def test_ordinary_entrypoints_share_default_and_explicit_override(monkeypatch, override, expected):
    if override is not None:
        monkeypatch.setenv("CPA_IMAGE_MODEL", override)
    for module in (cover_generation, publish_staging, shadow, cover_only):
        assert module._cpa_image_model_candidates() == expected


class Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


@pytest.fixture
def edit_inputs(tmp_path):
    reference = tmp_path / "reference.png"
    Image.new("RGB", (160, 90), (100, 130, 180)).save(reference)
    return {
        "base_url": "https://synthetic.invalid/v1",
        "api_key": "synthetic-test-key",
        "reference_path": reference,
        "output_path": tmp_path / "output.png",
        "prompt": "synthetic cover model regression",
        "request_path": tmp_path / "request.json",
        "response_path": tmp_path / "response.json",
    }


def image_response():
    stream = io.BytesIO()
    Image.new("RGB", (160, 90), (90, 120, 170)).save(stream, format="PNG")
    return Response({"model": "", "data": [{"b64_json": base64.b64encode(stream.getvalue()).decode()}]})


@pytest.mark.parametrize("entrypoint", [cover_generation._call_cpa_image_edit, shadow._call_cpa_image_edit])
@pytest.mark.parametrize("unavailable_status", [None, 400, 404])
def test_actual_edit_request_uses_new_default_and_only_explicit_fallback(
    monkeypatch, edit_inputs, entrypoint, unavailable_status,
):
    calls = []

    def reply(request, **_kwargs):
        calls.append(request)
        if unavailable_status is not None and len(calls) == 1:
            body = json.dumps({"error": {"code": "client_model_unavailable"}}).encode()
            raise urllib.error.HTTPError(
                request.full_url, unavailable_status, "unavailable", None, io.BytesIO(body)
            )
        return image_response()

    monkeypatch.setattr(urllib.request, "urlopen", reply)
    result = entrypoint(**edit_inputs)
    expected = ["gpt-image-2.5"] + (["gpt-image-2"] if unavailable_status else [])
    evidence = json.loads(edit_inputs["response_path"].read_text())
    assert result["status"] == "AI_BACKGROUND_READY"
    assert result["selected_model"] == expected[-1]
    assert result["attempted_models"] == expected
    assert result["model_fallback_used"] is bool(unavailable_status)
    assert [row["model"] for row in evidence["attempts"]] == expected
    assert len(calls) == len(expected)
    for request, model in zip(calls, expected):
        assert (b'name="model"\r\n\r\n' + model.encode() + b"\r\n") in request.data
    observation = evidence["attempts"][-1]["service_observation"]
    assert observation["requested_model"] == expected[-1]
    assert observation["reported_model"] == ""
    assert observation["reported_model_present"] is True
    assert observation["upstream_identity_verified"] is False
    assert evidence["attempts"][-1]["raw_image"]["dimensions"] == [160, 90]


@pytest.mark.parametrize(
    "failure",
    [400, 401, 403, 404, 429, 500, 503, "timeout", "empty", "bad_pixels"],
)
def test_default_never_falls_back_for_ordinary_failure(monkeypatch, edit_inputs, failure):
    calls = []

    def reply(request, **_kwargs):
        calls.append(request)
        if isinstance(failure, int):
            body = json.dumps({"error": {"code": "ordinary_error"}}).encode()
            raise urllib.error.HTTPError(request.full_url, failure, "failed", None, io.BytesIO(body))
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        if failure == "empty":
            return Response({"data": []})
        return Response({"data": [{"b64_json": base64.b64encode(b"invalid image").decode()}]})

    monkeypatch.setattr(urllib.request, "urlopen", reply)
    result = cover_generation._call_cpa_image_edit(**edit_inputs)
    assert result["status"] == "FAILED"
    assert result["attempted_models"] == ["gpt-image-2.5"]
    assert result["model_fallback_used"] is False
    assert len(calls) == 1


def test_screenshot_polish_uses_same_default_transport(monkeypatch, edit_inputs, tmp_path):
    calls = []

    def reply(request, **_kwargs):
        calls.append(request)
        return image_response()

    monkeypatch.setattr(urllib.request, "urlopen", reply)
    generation = {}
    output, method, model, attempted, used = _materialize_screenshot_polish(
        screenshot_base=edit_inputs["reference_path"], candidate_id="model-upgrade-test",
        ai_dir=tmp_path, evidence_dir=tmp_path, polish=True,
        image_edit=publish_staging._cover_call_cpa_image_edit,
        base_url=edit_inputs["base_url"], api_key=edit_inputs["api_key"],
        cover_generation=generation,
    )
    assert output.is_file()
    assert (method, model, attempted, used) == (
        "screenshot_polish", "gpt-image-2.5", ["gpt-image-2.5"], True
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("payload", "reported", "present"),
    [({}, None, False), ({"model": None}, None, True), ({"model": ""}, "", True),
     ({"model": "gpt-image-2.5"}, "gpt-image-2.5", True)],
)
def test_service_report_keeps_blank_and_missing_distinct(payload, reported, present):
    observation = cpa_image_edit.service_observation(
        payload, requested_model="gpt-image-2.5", requested_size="1920x1088",
        headers={}, request_elapsed_seconds=1,
    )
    assert observation["reported_model"] == reported
    assert observation["reported_model_present"] is present
    assert observation["upstream_identity_verified"] is False


@pytest.mark.parametrize("route", ["redraw", "polish"])
@pytest.mark.parametrize(
    ("model", "attempted", "valid"),
    [
        ("gpt-image-2.5", ["gpt-image-2.5"], True),
        ("gpt-image-2", ["gpt-image-2.5", "gpt-image-2"], True),
        ("gpt-image-1.5", ["gpt-image-2", "gpt-image-1.5"], True),
        ("gpt-image-1.5", ["gpt-image-2.5", "gpt-image-2", "gpt-image-1.5"], False),
        ("gpt-image-2.5", ["gpt-image-2.5", "gpt-image-2"], False),
        ("gpt-image-2.5", ["gpt-image-2.5", "gpt-image-2.5"], False),
    ],
)
def test_current_and_historical_repair_model_receipts(
    tmp_path, monkeypatch, route, model, attempted, valid,
):
    fixture = _cover_binding_fixture if route == "redraw" else _screenshot_polish_binding_fixture
    fx = fixture(tmp_path, monkeypatch)
    path = fx["generation_path"]
    document = json.loads(path.read_text())
    document.update(model=model, attempted_models=attempted, model_fallback_used=len(attempted) > 1)
    if route == "polish":
        document["image_gen_model"] = model
    path.write_text(json.dumps(document))

    def validate():
        kwargs = {"cover": fx["generated_cover"], "title": fx["title"], "candidate_id": fx["cid"]}
        if route == "redraw":
            return cover_repair._validate_repaired_cover_generation(**kwargs)
        return cover_repair_route_lineage.validate_screenshot_generation_document(
            document, method="screenshot_polish", **kwargs
        )

    if valid:
        validate()
    else:
        with pytest.raises(ValueError):
            validate()


@pytest.mark.parametrize("model", ["gpt-image-2.5", "gpt-image-2", "gpt-image-1.5"])
@pytest.mark.parametrize("pixel_hash_matches", [True, False])
def test_host_only_review_accepts_new_model_without_relaxing_pixel_binding(model, pixel_hash_matches):
    digest = "sha256:" + "a" * 64
    record = {
        "story_contract": {
            "cover_reference_authority": None,
            "cover_counterpart_reference_available": False,
            "relation_claim_allowed": False,
            "cover_fallback_mode": "HOST_ONLY_GENERIC",
        },
        "artifact_hashes": {"cover_sha256": digest},
        "publish_staging": {"cover_generation": {
            "cover_origin": "AI_REDRAW", "image_generation_used": True,
            "image_gen_model": "cpa", "method": "images.edit", "model": model,
            "final_cover_sha256": digest, "rendered_lines": ["刚想再聊", "已经下播"],
            "rendered_text_pixels": {
                "status": "PASS", "rendered_text": "刚想再聊已经下播",
                "final_cover_sha256": digest if pixel_hash_matches else "sha256:" + "b" * 64,
            },
            "route_decision": {
                "execution_status": "READY", "actual_treatment": "cpa_redraw",
                "selected_treatment": "cpa_redraw", "relationship_visual_required": False,
                "required_participant_ids": [], "source_visible_participant_ids": [],
                "image_generation_used": True, "source_visibility_authority": "NO_IDENTITY_AUTHORITY",
                "final_visibility_authority": "NOT_REQUIRED",
            },
        }},
    }
    if model == "gpt-image-1.5" or not pixel_hash_matches:
        with pytest.raises(final_human_review.FinalHumanReviewError):
            final_human_review._cover_story_claim_authority(record, candidate_id="model-upgrade-test")
    else:
        assert final_human_review._cover_story_claim_authority(
            record, candidate_id="model-upgrade-test"
        ) == (("封面文字呈现“刚想再聊 / 已经下播”", "COVER_TEXT"),)
