"""Bind the existing final-audio sidecars without changing verdict semantics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import audit_review_package as auditor


SIDECARS = (
    "candidate.final.subtitle-audio-witness.srt",
    "candidate.final.subtitle-audio-provenance.json",
    "candidate.final.subtitle-audio-correspondence.json",
    "candidate.final.subtitle-audio-bcut.raw.json",
)


def _fixture(root: Path) -> None:
    (root / "review_manifest.json").write_text(json.dumps({"items": []}))
    for name in SIDECARS:
        (root / name).write_bytes(b"SYNTHETIC_INVENTORY_TEST_NOT_A_VALID_RECEIPT")


def _inputs(root: Path) -> dict[str, dict]:
    return {row["path"]: row for row in auditor.audit_package(root)["audited_inputs"]}


def test_canonical_audit_hashes_all_four_audio_sidecars_without_writes(tmp_path):
    _fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    actual = _inputs(tmp_path)
    for name in SIDECARS:
        assert actual[name]["sha256"] == hashlib.sha256(before[name]).hexdigest()
        assert actual[name]["bytes"] == len(before[name])
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    # Input presence/hash is NOT a claim that these synthetic receipts pass.


@pytest.mark.parametrize("sidecar", SIDECARS)
def test_changed_audio_sidecar_invalidates_audited_inputs(tmp_path, sidecar):
    _fixture(tmp_path)
    previous = _inputs(tmp_path)
    target = tmp_path / sidecar
    target.write_bytes(target.read_bytes() + b" ")
    current = _inputs(tmp_path)
    assert previous != current
    assert previous[sidecar]["sha256"] != current[sidecar]["sha256"]
    assert {k: v for k, v in previous.items() if k != sidecar} == {
        k: v for k, v in current.items() if k != sidecar
    }


@pytest.mark.parametrize("sidecar", SIDECARS)
def test_missing_audio_sidecar_invalidates_audited_inputs(tmp_path, sidecar):
    _fixture(tmp_path)
    previous = _inputs(tmp_path)
    (tmp_path / sidecar).unlink()
    current = _inputs(tmp_path)
    assert sidecar in previous and sidecar not in current
    assert previous != current


def test_operational_json_is_not_added_to_delivery_input_closure(tmp_path):
    _fixture(tmp_path)
    before = _inputs(tmp_path)
    (tmp_path / "operational-note.json").write_text('{"not_delivery_evidence":true}')
    assert before == _inputs(tmp_path)


@pytest.mark.parametrize("sidecar", SIDECARS)
def test_symlink_is_not_accepted_as_a_portable_sidecar(tmp_path, sidecar):
    _fixture(tmp_path)
    previous = _inputs(tmp_path)
    target = tmp_path / sidecar
    outside = tmp_path / "not-a-sidecar.bin"
    target.rename(outside)
    target.symlink_to(outside)
    current = _inputs(tmp_path)
    assert sidecar in previous and sidecar not in current
