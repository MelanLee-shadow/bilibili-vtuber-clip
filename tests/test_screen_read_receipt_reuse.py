import hashlib
import json
from copy import deepcopy

import pytest

from src.autoslice import cpa_frame_witness, screen_read_witness, visual_witness


def receipt(media, frame_ms):
    frame_sha = hashlib.sha256(f"pixels-{frame_ms}".encode()).hexdigest()
    return {
        "schema_version": "cpa-frame-witness.v1", "status": "OBSERVED",
        "provider": "cpa", "model": cpa_frame_witness.DEFAULT_FRAME_MODEL,
        "media_path": str(media), "frame_ms": frame_ms,
        "question": screen_read_witness.SCREEN_TEXT_QUESTION,
        "frame_sha256": frame_sha,
        "prompt_sha256": hashlib.sha256(json.dumps({
            "question": screen_read_witness.SCREEN_TEXT_QUESTION,
            "frame_sha256": frame_sha,
            "model": cpa_frame_witness.DEFAULT_FRAME_MODEL,
        }, sort_keys=True).encode()).hexdigest(),
        "answer": '{"texts":["原画面文字"]}',
        "response_sha256": "a" * 64,
    }


def setup_probe(monkeypatch, tmp_path, prior):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source fixture")
    calls = []
    monkeypatch.setattr(cpa_frame_witness, "_extract_frame_jpeg",
                        lambda path, ms: f"pixels-{ms}".encode())

    def fresh(path, ms, question, **kwargs):
        calls.append(ms)
        return receipt(path, ms)

    monkeypatch.setattr(visual_witness, "frame_vision_probe", fresh)
    probe = screen_read_witness.make_screen_read_probe(
        media_path=media, api_base="fixture", api_key="fixture", prior_receipts=prior)
    return media, probe, calls


def test_same_pixels_prompt_provider_model_reuse_original_receipts(monkeypatch, tmp_path):
    media = tmp_path / "source.mp4"
    prior = [receipt(media, ms) for ms in [100, 200]]
    original = deepcopy(prior)
    _, probe, calls = setup_probe(monkeypatch, tmp_path, prior)
    result = probe(0, 300)
    assert calls == []
    assert result["reused_frame_ms"] == [100, 200]
    assert result["receipts"] == original == prior
    result["receipts"][0]["answer"] = "consumer mutation"
    assert prior == original


@pytest.mark.parametrize("field,value", [
    ("model", "older-model"), ("provider", "agy"),
    ("question", "another question"), ("frame_sha256", "b" * 64),
    ("prompt_sha256", "b" * 64), ("status", "UNAVAILABLE"),
    ("media_path", "/different/source.mp4"), ("answer", "invalid answer"),
])
def test_drift_calls_normal_provider_without_rebinding_old_receipt(monkeypatch, tmp_path, field, value):
    media = tmp_path / "source.mp4"
    prior = [receipt(media, ms) for ms in [100, 200]]
    prior[0][field] = value
    original = deepcopy(prior)
    _, probe, calls = setup_probe(monkeypatch, tmp_path, prior)
    result = probe(0, 300)
    assert calls == [100]
    assert result["reused_frame_ms"] == [200]
    assert prior == original


def test_current_audit_pool_and_fresh_receipts_survive_repeated_probe(monkeypatch, tmp_path):
    media, _, calls = setup_probe(monkeypatch, tmp_path, [])
    monkeypatch.setenv("CPA_BASE_URL", "fixture")
    monkeypatch.setenv("CPA_API_KEY", "fixture")
    audit = {"findings": [{"screen_read_witness": {"receipts": [receipt(media, 100)]}}]}
    original = deepcopy(audit)
    probe = screen_read_witness.build_env_screen_read_probe(media, prior_observations=audit)
    first = probe(0, 300)
    second = probe(0, 300)
    assert calls == [200]
    assert first["reused_frame_ms"] == [100]
    assert second["reused_frame_ms"] == [100, 200]
    assert first["receipts"] == second["receipts"]
    assert audit == original
