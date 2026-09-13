"""Relocate audio evidence locators without rewriting the historical witness."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import package_import as pi
from src.autoslice.package_relocation_contract import project_uniform_host_locators
from tests.test_import_external_package import (
    CANDIDATE, SRC_PACKAGE, _build_external_package, _run, _step, _write_json,
)

PATH_FIELDS = ("output_dir", "witness_srt_path", "provenance_path", "correspondence_path", "raw_result_path")


def _audio_fixture(tmp_path):
    fixture = _build_external_package(tmp_path)
    record_path = fixture.staging_package / f"{CANDIDATE}.record.json"
    record = json.loads(record_path.read_bytes())
    evidence = {
        "schema_version": "final-subtitle-audio-gate.v1",
        "candidate_id": CANDIDATE, "provider": "bcut", "output_dir": SRC_PACKAGE,
        "status": "PASS", "timing_status": "PASS", "text_correctness_status": "UNASSESSED",
        "receipt": {"inputs": {"actual_media": {"path": f"{SRC_PACKAGE}/original.mp4"}}, "anchor_count": 3},
    }
    for field, suffix in zip(PATH_FIELDS[1:], ("witness.srt", "provenance.json", "correspondence.json", "raw.json")):
        name = f"{CANDIDATE}.{suffix}"
        payload = ("synthetic evidence: " + suffix).encode()
        (fixture.staging_package / name).write_bytes(payload)
        evidence[field] = f"{SRC_PACKAGE}/{name}"
        evidence[field.removesuffix("_path") + "_sha256"] = hashlib.sha256(payload).hexdigest()
    evidence["receipt_sha256"] = hashlib.sha256(json.dumps(evidence["receipt"], sort_keys=True).encode()).hexdigest()
    record["subtitle_audio_correspondence"] = evidence
    _write_json(record_path, record)
    return fixture, record


def _plan(fixture):
    return pi.plan_import(source_package_dir=fixture.staging_package,
                          destination_package_root=fixture.destination_package,
                          destination_repo_root=fixture.repo_root, candidate_id=CANDIDATE)


def test_native_import_rebases_audio_locators_but_preserves_witness(tmp_path):
    fixture, record = _audio_fixture(tmp_path)
    original = copy.deepcopy(record["subtitle_audio_correspondence"])
    before = {p: p.read_bytes() for p in fixture.staging_package.rglob("*") if p.is_file()}
    receipt, code = _run(fixture)
    assert code == 0, receipt
    after = json.loads((fixture.destination_package / f"{CANDIDATE}.record.json").read_bytes())["subtitle_audio_correspondence"]
    for field in PATH_FIELDS:
        assert after[field] == original[field].replace(SRC_PACKAGE, str(fixture.destination_package), 1)
    assert all(after[k] == v for k, v in original.items() if k not in PATH_FIELDS)
    assert all(p.read_bytes() == data for p, data in before.items())
    for field in PATH_FIELDS[1:]:
        assert Path(after[field]).read_bytes() == (fixture.staging_package / Path(original[field]).name).read_bytes()


def test_uniform_projection_keeps_nested_receipt_and_its_hash(tmp_path):
    fixture, record = _audio_fixture(tmp_path)
    plan = _plan(fixture)
    after = project_uniform_host_locators(record, kind="record", mappings=plan.roots.mappings(),
                                         source_workspace_root=plan.roots.source_workspace_root)
    evidence = after["subtitle_audio_correspondence"]
    assert evidence["output_dir"] == str(fixture.destination_package)
    assert evidence["receipt"] == record["subtitle_audio_correspondence"]["receipt"]
    assert evidence["receipt_sha256"] == record["subtitle_audio_correspondence"]["receipt_sha256"]


@pytest.mark.parametrize("field", PATH_FIELDS)
def test_audio_locator_cannot_move_to_repo_role(tmp_path, field):
    fixture, record = _audio_fixture(tmp_path)
    record["subtitle_audio_correspondence"][field] = str(fixture.repo_root / "assets" / "wrong.json")
    _write_json(fixture.staging_package / f"{CANDIDATE}.record.json", record)
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "LOCATOR_ROOT_ROLE_VIOLATION"
    assert not fixture.destination_package.exists()


def test_unknown_audio_path_is_not_hidden_by_receipt_compatibility(tmp_path):
    fixture, record = _audio_fixture(tmp_path)
    record["subtitle_audio_correspondence"]["unrecognized_path"] = SRC_PACKAGE + "/unrecognized.json"
    _write_json(fixture.staging_package / f"{CANDIDATE}.record.json", record)
    receipt, code = _run(fixture)
    assert code == 2
    assert any(s.get("code") == "UNRELOCATED_EXTERNAL_PATH" for s in receipt["steps"])


@pytest.mark.parametrize("field", ["receipt", "receipt_sha256"])
def test_receipt_or_receipt_hash_is_never_mutable(tmp_path, field):
    _fixture, record = _audio_fixture(tmp_path)
    after = copy.deepcopy(record)
    after["subtitle_audio_correspondence"][field] = "tampered"
    with pytest.raises(pi.PackageImportError):
        pi._guard_immutable("record", record, after, source_workspace_root=SRC_PACKAGE)
