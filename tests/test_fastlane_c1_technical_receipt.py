import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import fastlane_c1_technical_receipt as c1_receipt
from src.autoslice.fastlane_c1_formal_adapter import CID, SIX_NAMED_POINTS, TITLE
from src.autoslice.final_human_review import replay_final_human_review_attestation


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def package(tmp_path, monkeypatch):
    """Build the smallest self-contained package needed by receipt semantics."""
    root = tmp_path / "c1-package"
    root.mkdir()
    authority = copy.deepcopy(c1_receipt.load_formal_authority())
    names = authority["output_names"]
    evidence = {
        "candidate_id": CID,
        "title": TITLE,
        "ivan_rereview_required": False,
        "points": [
            {"point_id": point["point_id"], "expected": point["expected"]}
            for point in SIX_NAMED_POINTS
        ],
    }
    evidence_path = root / c1_receipt.ROOT_EVIDENCE
    _write_json(evidence_path, evidence)
    monkeypatch.setattr(
        c1_receipt,
        "ROOT_SHA",
        "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    )
    _write_json(root / names["public_identity"], {
        "identity": {"bvid": "BV1os8q61Eya", "aid": 117132650155234,
                     "cid": 41126267272, "title": TITLE}
    })
    for name in ("burned_final", "successor_srt", "cover"):
        (root / names[name]).write_bytes(name.encode("ascii"))
    _write_json(root / "review_manifest.json", {
        "schema_version": "fastlane-c1-formal-private-review-manifest.v1"
    })
    audit = root / "package_audit.json"
    _write_json(audit, {"synthetic": True})

    # Receipt tests own receipt semantics, not the independently-tested formal
    # package/audit builders. Their deterministic seams keep this fixture tiny.
    monkeypatch.setattr(c1_receipt, "validate_formal_package", lambda value: None)
    monkeypatch.setattr(c1_receipt, "load_formal_authority", lambda: authority)
    monkeypatch.setattr(c1_receipt, "_audit", lambda value, path: {"policy_fingerprint": "synthetic"})
    monkeypatch.setattr(c1_receipt, "_direct_authority", lambda value: {
        "ruling_document_sha256": c1_receipt.RULING_SHA,
        "seals": copy.deepcopy(c1_receipt.SEALS),
    })
    return root, audit


def test_fixture_is_repository_portable_and_has_no_private_source(tmp_path, monkeypatch):
    root, _ = package(tmp_path, monkeypatch)
    assert root.is_relative_to(tmp_path)
    assert ".pri" + "vate" not in Path(__file__).read_text(encoding="utf-8")


def test_c1_template_is_six_point_no_grant(tmp_path, monkeypatch):
    root, audit = package(tmp_path, monkeypatch)
    value = c1_receipt.template(root, audit)
    assert value["accepted"] is False and value["upload_allowed"] is False
    assert len(value["six_named_points"]) == 6


def test_c1_template_rejects_root_evidence_drift(tmp_path, monkeypatch):
    root, audit = package(tmp_path, monkeypatch)
    (root / c1_receipt.ROOT_EVIDENCE).write_text("{}", encoding="utf-8")
    with pytest.raises(c1_receipt.C1TechnicalReceiptError, match="ROOT_EVIDENCE"):
        c1_receipt.template(root, audit)


def _accepted_receipt(root, audit):
    value = c1_receipt.template(root, audit)
    value.update(status="ACCEPTED_FOR_SAME_BV_TECHNICAL", accepted=True,
                 reviewed_by="Codex root", reviewed_at="2026-08-24T00:00:00Z")
    for row in value["six_named_points"]:
        row.update(verdict="PASS", evidence=row["root_evidence_point_sha256"])
    return value


def test_completed_requires_exact_six_passes_and_root_identity(tmp_path, monkeypatch):
    root, audit = package(tmp_path, monkeypatch)
    value = _accepted_receipt(root, audit)
    assert c1_receipt.validate_completed(value, root, audit)["accepted"] is True
    value["six_named_points"][0]["verdict"] = "FAIL"
    with pytest.raises(c1_receipt.C1TechnicalReceiptError, match="POINT"):
        c1_receipt.validate_completed(value, root, audit)


def test_completed_requires_timezone_aware_iso8601(tmp_path, monkeypatch):
    root, audit = package(tmp_path, monkeypatch)
    value = _accepted_receipt(root, audit)
    value["reviewed_at"] = "2026-08-24 00:00:00"
    with pytest.raises(c1_receipt.C1TechnicalReceiptError, match="REVIEWED_AT"):
        c1_receipt.validate_completed(value, root, audit)


def test_completed_rejects_direct_upload_seal_drift(tmp_path, monkeypatch):
    root, audit = package(tmp_path, monkeypatch)
    value = _accepted_receipt(root, audit)
    value["bindings"]["direct_upload_authority"]["seals"]["line1643"]["raw_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(c1_receipt.C1TechnicalReceiptError, match="BINDING"):
        c1_receipt.validate_completed(value, root, audit)


def test_same_bv_attestation_dispatches_only_c1_formal_receipt(tmp_path, monkeypatch):
    root, audit = package(tmp_path, monkeypatch)
    receipt = _accepted_receipt(root, audit)
    receipt_path = root / "c1.receipt.json"
    _write_json(receipt_path, receipt)

    def bind(path):
        return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}

    manifest = {"recovery_publication_authority": {}, "package_attestation": {
        "package_root": str(root.resolve()), "review_manifest": bind(root / "review_manifest.json"),
        "package_audit": bind(audit), "c1_technical_receipt": bind(receipt_path),
    }}
    assert replay_final_human_review_attestation(manifest)["c1_technical_receipt"]["path"] == str(receipt_path.resolve())
