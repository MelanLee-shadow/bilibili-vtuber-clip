import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import fastlane_c1_technical_receipt as c1_receipt
import src.autoslice.same_bv_repair as same_bv
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


def _c1_plan(root, audit, receipt_path, authority):
    """A minimal real-shaped C1 plan for same-BV attestation replay."""
    def entry(path):
        return {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }

    names = c1_receipt.load_formal_authority()["output_names"]
    bound_manifest = root / "bound-manifest.json"
    _write_json(bound_manifest, {})
    return {
        "schema_version": same_bv.PLAN_SCHEMA,
        "plan_id": "c1-plan",
        "bvid": authority["bvid"],
        "manifest": entry(bound_manifest),
        "replacement": {
            "video": entry(root / names["burned_final"]),
            "cover": entry(root / names["cover"]),
        },
        "recovery_publication_authority": copy.deepcopy(authority),
        "package_attestation": {
            "package_root": str(root.resolve()),
            "review_manifest": entry(root / "review_manifest.json"),
            "package_audit": entry(audit),
            "c1_technical_receipt": entry(receipt_path),
        },
        "season": {"season_id": 8383206, "section_id": 9320779},
        "target_metadata": {"title": TITLE},
        "before": {"creator": {"videos": [{"cid": authority["cid"]}]}},
    }


def test_c1_plan_attestation_replay_keeps_canonical_plan_authority(
    tmp_path, monkeypatch
):
    root, audit = package(tmp_path, monkeypatch)
    receipt_path = root / "c1.receipt.json"
    _write_json(receipt_path, _accepted_receipt(root, audit))
    authority = {
        "candidate_id": CID,
        "bvid": "BV1os8q61Eya",
        "cid": 41126267272,
    }

    def validate(value, *, candidate_id, expected_final_title=None):
        if value != authority or candidate_id != CID or expected_final_title != TITLE:
            raise same_bv.RecoveryTitleAuthorityError("C1 plan authority drift")
        return copy.deepcopy(authority)

    monkeypatch.setattr(same_bv, "validate_recovery_publication_authority", validate)
    plan = _c1_plan(root, audit, receipt_path, authority)
    same_bv.validate_plan(plan)


@pytest.mark.parametrize(
    "mutation",
    ["missing_authority", "non_mapping_authority", "authority_drift"],
)
def test_c1_plan_attestation_rejects_missing_non_mapping_or_drifted_authority(
    tmp_path, monkeypatch, mutation
):
    root, audit = package(tmp_path, monkeypatch)
    receipt_path = root / "c1.receipt.json"
    _write_json(receipt_path, _accepted_receipt(root, audit))
    authority = {"candidate_id": CID, "bvid": "BV1os8q61Eya", "cid": 41126267272}
    monkeypatch.setattr(
        same_bv,
        "validate_recovery_publication_authority",
        lambda value, **_kwargs: (_ for _ in ()).throw(
            same_bv.RecoveryTitleAuthorityError("C1 plan authority invalid")
        ) if value != authority else copy.deepcopy(authority),
    )
    plan = _c1_plan(root, audit, receipt_path, authority)
    if mutation == "missing_authority":
        plan.pop("recovery_publication_authority")
    elif mutation == "non_mapping_authority":
        plan["recovery_publication_authority"] = "not-a-mapping"
    else:
        plan["recovery_publication_authority"]["bvid"] = "BV1tTg46UE3y"
    with pytest.raises(same_bv.PlanInvalid, match="recovery_publication_authority|FINAL_HUMAN_REVIEW_C1_RECOVERY_REQUIRED"):
        same_bv.validate_plan(plan)


@pytest.mark.parametrize("drifted", ["receipt", "audit"])
def test_c1_plan_attestation_rejects_receipt_or_audit_drift(
    tmp_path, monkeypatch, drifted
):
    root, audit = package(tmp_path, monkeypatch)
    receipt_path = root / "c1.receipt.json"
    _write_json(receipt_path, _accepted_receipt(root, audit))
    authority = {"candidate_id": CID, "bvid": "BV1os8q61Eya", "cid": 41126267272}
    monkeypatch.setattr(
        same_bv,
        "validate_recovery_publication_authority",
        lambda value, **_kwargs: copy.deepcopy(authority) if value == authority else (_ for _ in ()).throw(same_bv.RecoveryTitleAuthorityError("C1 plan authority invalid")),
    )
    plan = _c1_plan(root, audit, receipt_path, authority)
    (receipt_path if drifted == "receipt" else audit).write_text("{}", encoding="utf-8")
    with pytest.raises(same_bv.PlanInvalid, match="FINAL_HUMAN_REVIEW_ATTESTED_FILE_(HASH|SIZE)_DRIFT"):
        same_bv.validate_plan(plan)
