"""Synthetic byte/coordinate controls; these are not perceived-media verdicts."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.autoslice import new_bv_repair_authority as authority_module
from src.autoslice.producer_boundary_resolution import _manual_end_contract
from src.autoslice.producer_boundary_owner_contract import freeze_required_boundary_owner_contract
from src.autoslice.recovery_title_authority import validate_recovery_publication_authority
from src.autoslice.source_subtitle_truth import candidate_boundary_owner_scope
from tests.test_publication_reconciliation import (
    CANDIDATE, DATE, _entry, _new_bv_fixture, _reconcile_new, _sha, _write_json,
)
from src.autoslice.publication_reconciliation import authority_sidecar_path


def published_fixture(tmp_path):
    fixture = _new_bv_fixture(tmp_path)
    root = tmp_path / "source"
    root.mkdir()
    source = root / "recording.mp4"
    source.write_bytes(b"explicit synthetic recording bytes: no media quality claim")
    piece = root / "piece.mp4"
    padded = root / "padded.mp4"
    piece.write_bytes(b"explicit synthetic padded bytes")
    padded.write_bytes(piece.read_bytes())
    recut = root / "recut.mp4"
    recut.write_bytes(b"explicit synthetic recut bytes")
    piece_proof = {
        "source_path": str(source), "source_sha256": _sha(source),
        "source_media_binding": "sha256:" + _sha(source),
        "start_ms": 1_000_000, "end_ms": 1_200_000,
        "output_path": str(piece), "output_sha256": _sha(piece),
    }
    padded_proof = {"inputs": [{"path": str(piece), "sha256": _sha(piece)}],
                    "output_path": str(padded), "output_sha256": _sha(padded)}
    _write_json(piece.with_suffix(".provenance.json"), piece_proof)
    _write_json(padded.with_suffix(".provenance.json"), padded_proof)
    recut_proof = {
        "schema_version": "lidousha-speaker-recut-provenance.v1",
        "source_piece": piece_proof, "padded": padded_proof,
        "final_recut": {
            "source_path": str(padded), "source_sha256": _sha(padded),
            "start_ms": 9500, "end_ms": 136740,
            "absolute_source_start_ms": 1_009_500,
            "absolute_source_end_ms": 1_136_740,
            "output_path": str(recut), "output_sha256": _sha(recut),
        },
    }
    _write_json(recut.with_suffix(".provenance.json"), recut_proof)
    context = root / "context.json"
    _write_json(context, {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": CANDIDATE, "recording_date": DATE,
        "pieces": [{"source_media_sha256": "sha256:" + _sha(source),
                    "recording_basename": source.name, "start_ms": 1_000_000,
                    "end_ms": 1_200_000}],
    })
    record_path = Path(fixture["manifest"]["package_attestation"]["record"]["path"])
    record = json.loads(record_path.read_text())
    record.update({
        "candidate_id": CANDIDATE, "media_path": str(recut),
        "clip_context_path": str(context), "start_ms": 0,
        "end_ms": 127240, "duration_ms": 127240,
        "boundary_audit": {"final_start_ms": 9500, "final_end_ms": 136740},
        "artifact_hashes": {
            "video_sha256": "sha256:" + _sha(recut),
            "burned_video_sha256": "sha256:" + fixture["manifest"]["video"]["sha256"],
            "clip_context_file_sha256": "sha256:" + _sha(context),
        },
    })
    _write_json(record_path, record)
    fixture["manifest"]["package_attestation"]["record"] = _entry(record_path)
    _write_json(fixture["manifest_path"], fixture["manifest"])
    fixture["uploaded"]["manifest_sha256"] = _sha(fixture["manifest_path"])
    fixture["uploaded"]["record_sha256"] = _sha(record_path)
    _write_json(fixture["uploaded_path"], fixture["uploaded"])
    _reconcile_new(fixture)
    spec = {
        "candidate_id": CANDIDATE, "date": DATE,
        "semantic_start_ms": 1_009_750, "semantic_end_ms": 1_120_130,
        "pieces": [{"remote_media": str(source), "source_media_sha256": "sha256:" + _sha(source),
                    "start_ms": 1_000_000, "end_ms": 1_200_000}],
        "given_title": fixture["manifest"]["title"],
    }
    fixture.update(source=source, recut=recut, context=context, record=record_path,
                   publication=authority_sidecar_path(fixture["manifest_path"]), spec=spec)
    return fixture


def recall_authority(fixture):
    return authority_module.build_new_bv_repair_authority(
        fixture["publication"], candidate_id=CANDIDATE,
        title=fixture["spec"]["given_title"], published_recall_anchor=True,
    )


def test_existing_title_only_authority_stays_title_only(tmp_path):
    fixture = published_fixture(tmp_path)
    original = authority_module.build_new_bv_repair_authority(
        fixture["publication"], candidate_id=CANDIDATE, title=fixture["spec"]["given_title"])
    assert "boundary_end_mode" not in original
    assert "required_given_end_ms" not in original


def test_normal_builder_validator_and_resolver_derive_actual_source_end(tmp_path):
    fixture = published_fixture(tmp_path)
    authority = recall_authority(fixture)
    assert authority["boundary_end_mode"] == "published_recall_anchor"
    assert authority["required_given_end_ms"] == 1_136_740
    assert authority["required_given_end_ms"] not in (127240, 136740)
    assert validate_recovery_publication_authority(authority, candidate_id=CANDIDATE) == authority
    spec = dict(fixture["spec"], recovery_publication_authority=authority)
    reason, local_end, mode = _manual_end_contract(
        spec, prior_piece_duration_ms=0, last_piece_start_ms=1_000_000)
    assert (local_end, mode) == (136740, "published_recall_anchor")
    assert reason == "verified-first-publication:" + authority["authority_sha256"]


@pytest.mark.parametrize("field,value", [
    ("candidate_id", "other"), ("bvid", "BV0000000000"),
    ("required_given_end_ms", 127240), ("boundary_end_mode", "exact_source_pin"),
    ("boundary_end_mode", "semantic_lower_bound"),
])
def test_rehashed_authority_cannot_override_public_source(tmp_path, field, value):
    fixture = published_fixture(tmp_path)
    authority = recall_authority(fixture)
    authority[field] = value
    authority.pop("authority_sha256")
    authority["authority_sha256"] = authority_module._digest(authority)
    with pytest.raises(ValueError):
        validate_recovery_publication_authority(authority, candidate_id=CANDIDATE)


@pytest.mark.parametrize("role", ["record", "context", "source", "recut", "publication"])
def test_original_bytes_are_replayed_not_just_caller_hashes(tmp_path, role):
    fixture = published_fixture(tmp_path)
    authority = recall_authority(fixture)
    path = fixture[role]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        validate_recovery_publication_authority(authority, candidate_id=CANDIDATE)


@pytest.mark.parametrize("damage", ["source_hash", "date", "mode", "clock", "authority", "candidate"])
def test_normal_boundary_consumer_rejects_spec_drift(tmp_path, damage):
    fixture = published_fixture(tmp_path)
    spec = copy.deepcopy(fixture["spec"])
    spec["recovery_publication_authority"] = recall_authority(fixture)
    if damage == "source_hash":
        spec["pieces"][0]["source_media_sha256"] = "sha256:" + "0" * 64
    elif damage == "date":
        spec["date"] = "2026-01-01"
    elif damage == "mode":
        spec["given_end_mode"] = "exact_source_pin"
    elif damage == "clock":
        spec["given_end_ms"] = 136740
    elif damage == "authority":
        spec["given_end_authority"] = "self-signed"
    else:
        spec["candidate_id"] = "other"
    with pytest.raises(SystemExit, match="AUTHORITY"):
        _manual_end_contract(spec, prior_piece_duration_ms=0, last_piece_start_ms=1_000_000)


def test_recall_scope_preserves_owner_identity_and_bounds_search(tmp_path):
    from src.autoslice.first_publication_boundary import bind_published_recall_to_spec
    fixture = published_fixture(tmp_path)
    spec = copy.deepcopy(fixture["spec"])
    before = candidate_boundary_owner_scope(spec=spec, durations=[200000])
    spec["recovery_publication_authority"] = recall_authority(fixture)
    assert bind_published_recall_to_spec(spec)
    assert candidate_boundary_owner_scope(spec=spec, durations=[200000]) == before
    audit = {}
    freeze_required_boundary_owner_contract(
        spec=spec, durations=[200000], chat_authority_audit=audit, required_boundary_owners=[])
    contract = audit["frozen_boundary_owner_contract"]
    scope = spec["boundary_search_scope"]
    assert scope["boundary_end_mode"] == "published_recall_anchor"
    assert scope["published_recall_anchor_ms"] == 136740
    assert scope["manual_lower_bound_ms"] is None
    assert scope["recommendation_backward_ms"] == 15000
    assert scope["max_recommended_end_ms"] == 166740
    assert scope["delivery_lower_bound_ms"] < 136740
    assert contract["owner_eligibility_scope"] == before


def test_changed_padded_provenance_source_clock_is_rejected(tmp_path):
    fixture = published_fixture(tmp_path)
    authority = recall_authority(fixture)
    path = fixture["recut"].with_suffix(".provenance.json")
    proof = json.loads(path.read_text())
    proof["final_recut"]["absolute_source_end_ms"] += 1
    _write_json(path, proof)
    with pytest.raises(ValueError, match="provenance"):
        validate_recovery_publication_authority(authority, candidate_id=CANDIDATE)
