"""Ordinary narrow writer → GC → shared cold consumers, without live providers."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from src.autoslice import entity_audio_verifier as audio
from src.autoslice import targeted_relisten as policy
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.entity_witness_cache_probe import validate_relisten_witness_artifacts
from test_targeted_relisten_public_contract import (
    acquire_narrow, acquire_range, build_offline_rig, no_gain_plan, three_observations,
)


@pytest.fixture
def generated(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    witness = acquire_narrow(rig)
    # The actual writer, persisted as the real consumer will reopen it.
    saved = tmp_path / "saved-witness.json"
    saved.write_text(json.dumps(witness))
    return rig, json.loads(saved.read_text())


@pytest.mark.parametrize("route", ["rebuild", "copy"])
@pytest.mark.parametrize("start_ms,offset", [(2200, 0), (1700, 500)])
def test_missing_narrow_writer_bytes_reach_cold_consumers(tmp_path, monkeypatch, route, start_ms, offset):
    # Vary the ordinary cue clock, rather than preparing an artificial binding.
    from test_targeted_relisten_public_contract import SRT
    srt = SRT.replace("00:00:02,000 --> 00:00:04,000",
                      f"00:00:0{start_ms / 1000:.3f} --> 00:00:04,000".replace(".", ","))
    rig = build_offline_rig(tmp_path, monkeypatch, srt=srt, offset_ms=offset)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    witness = acquire_narrow(rig)
    request = build_witness_request(rig.check)
    original = deepcopy(witness)
    projection = policy.evidence_projection(rig.check, witness=witness)
    assert projection["effective_distinct_observations"] == 1
    path = Path(witness["relisten_artifacts"]["audio_path"])
    immutable = {p: p.read_bytes() for p in rig.output.rglob("*") if p.is_file() and p != path}
    if route == "copy":
        copy = tmp_path / "another-run" / path.name
        copy.parent.mkdir()
        shutil.copyfile(path, copy)
        monkeypatch.setattr(audio, "_crop_black_frame_audio",
                            lambda **kw: pytest.fail("same-hash copy must avoid recrop"))
    path.unlink()
    rig.transport.failure = AssertionError("no provider during recovery")
    recovered = validate_relisten_witness_artifacts(request=request, witness=witness)
    physical = Path(recovered["audio_path"])
    assert physical != path and audio._sha256(physical) == witness["audio_clip_sha256"]
    assert policy.evidence_projection(rig.check, witness=witness) == projection
    policy.protect_clear_audio(rig.check, witness, "我听到了，哼唱")
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        policy.protect_clear_audio(rig.check, witness, "你听到了，哼唱")
    replay = rig.local.probe_witness_cache(request)
    assert replay["served_from_cache"] and replay["relisten_artifacts"]["audio_path"] == str(path)
    validate_relisten_witness_artifacts(request=request, witness=replay)
    assert witness == original and not path.exists() and len(rig.transport.calls) == 1
    assert all(p.read_bytes() == raw for p, raw in immutable.items())


@pytest.mark.parametrize("damage,code", [
    ("rebuild_hash", "AUDIO"), ("bytes", "AUDIO"), ("symlink", "AUDIO"),
    ("source", "SOURCE"), ("window", "CLOCK"), ("prompt", "PROMPT"),
    ("response", "RESPONSE"), ("raw_rebound", "RAW_VERDICT"),
])
def test_narrow_recovery_preserves_binding_rejections(generated, tmp_path, monkeypatch, damage, code):
    rig, witness = generated
    request = build_witness_request(rig.check)
    binding = witness["relisten_artifacts"]
    path = Path(binding["audio_path"])
    if damage == "bytes":
        path.write_bytes(b"changed existing bytes")
    elif damage == "symlink":
        copy = tmp_path / "retained.mp4"
        shutil.copyfile(path, copy)
        path.unlink(); path.symlink_to(copy)
    else:
        path.unlink()
        if damage == "rebuild_hash":
            def wrong(**kw):
                kw["audio_path"].write_bytes(b"different reconstruction")
                return True, ""
            monkeypatch.setattr(audio, "_crop_black_frame_audio", wrong)
        elif damage == "source":
            rig.source.write_bytes(b"wrong source")
        elif damage == "window":
            witness["timeline_binding"]["source_media"]["crop_end_ms"] += 100
        elif damage in {"prompt", "response"}:
            Path(binding[damage + "_path"]).write_text("{}")
        elif damage == "raw_rebound":
            witness["heard_pinyin"] = "other words"
    with pytest.raises(ValueError, match="RELISTEN_NARROW_" + code + "_DRIFT"):
        validate_relisten_witness_artifacts(request=request, witness=witness)
    assert len(rig.transport.calls) == 1


def test_generated_no_gain_proof_cold_reopens_missing_narrow(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    witness, check, finding, _ = three_observations(rig)
    _, audit, _ = acquire_range(rig, finding, check, no_gain_plan)
    proof, terminal = audit["no_gain_proof"], audit["terminal_check_request"]
    # Persist and reopen the writer's complete proof, without changing observations.
    saved = tmp_path / "no-gain.json"
    saved.write_text(json.dumps([proof, terminal]))
    Path(witness["relisten_artifacts"]["audio_path"]).unlink()
    rig.transport.failure = AssertionError("no new provider hearing")
    proof, terminal = json.loads(saved.read_text())
    policy.validate_no_gain(proof, terminal)
    assert len(rig.transport.calls) == 3
    Path(witness["relisten_artifacts"]["response_path"]).write_text("{}")
    with pytest.raises(ValueError, match="RELISTEN_NO_GAIN_OBSERVATION_DRIFT"):
        policy.validate_no_gain(proof, terminal)
