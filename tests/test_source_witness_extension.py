"""Durable incremental-acquisition controls; explicit fixtures, zero providers."""
import json
from pathlib import Path
import pytest
from src.autoslice.source_witness_extension import _stage, _write_new, account_root


def test_completed_incremental_stage_cold_replay_never_calls_provider(tmp_path):
    request = {"source_media": {"sha256": "fixture"}, "duration_ms": 12010}
    result = {"raw": {"provider": "EXPLICIT_STUB", "utterances": []}}
    assert _stage(tmp_path, "asr", request, lambda: result) == result
    def forbidden():
        raise AssertionError("cold replay attempted a new provider")
    assert _stage(tmp_path, "asr", request, forbidden) == result


def test_dispatched_unknown_incremental_stage_cannot_retry_or_clear_claim(tmp_path):
    request = {"fixture": "unknown"}
    def lost_transport():
        raise OSError("EXPLICIT_STUB_LOST_RETURN")
    with pytest.raises(OSError):
        _stage(tmp_path, "asr", request, lost_transport)
    before = (tmp_path / "asr.claim.json").read_bytes()
    with pytest.raises(ValueError, match="OUTCOME_UNKNOWN_ASR"):
        _stage(tmp_path, "asr", request, lambda: pytest.fail("repeated unknown provider"))
    assert (tmp_path / "asr.claim.json").read_bytes() == before
    assert not (tmp_path / "asr.result.json").exists()


@pytest.mark.parametrize("damage", ["request", "claim", "result", "missing_seal"])
def test_cold_incremental_stage_rejects_scope_and_observation_drift(tmp_path, damage):
    request = {"source_sha256": "fixture-a", "window": [1000, 2000]}
    _stage(tmp_path, "asr", request, lambda: {"fixture": "complete"})
    if damage == "request":
        request = {**request, "window": [1000, 3000]}
    elif damage == "missing_seal":
        (tmp_path / "asr.binding.json").unlink()
    else:
        path = tmp_path / ("asr." + damage + ".json")
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        _stage(tmp_path, "asr", request, lambda: pytest.fail("drift triggered provider"))


def test_output_directory_does_not_reset_incremental_claim_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    plan = {"candidate_id": "fixture", "source": {"sha256": "fixture-hash"},
            "tail_source_start_ms": 1597990, "tail_source_end_ms": 1610000}
    first = {"runtime_root": str(tmp_path), "candidate_id": "fixture", "output_root": "one"}
    second = {**first, "output_root": "two"}
    assert account_root(first, plan) == account_root(second, plan)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "reset"))
    with pytest.raises(ValueError, match="ACCOUNT_NAMESPACE_DRIFT"):
        account_root(second, plan)
