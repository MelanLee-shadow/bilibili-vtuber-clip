"""Portable mechanical-review regression cases, using synthetic package bytes.

The production canonical auditor is only replaced in explicit unit seams.
The unpatched negative case verifies that fake media cannot pass real audit.
No private review contract, real candidate, media, or publication authority is
required. The paired private suite retains its historical coverage.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import build_final_human_review as builder
from src.autoslice import final_human_review as human_review

CANDIDATE_ID = "auto_193450_1000_1020"
STEM = "reviewed-clip"
TITLE = "【主播】测试片完整标题"

def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def receipt_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    root = tmp_path / "package"
    root.mkdir()
    verification = root / "verification"
    verification.mkdir()
    paths = {
        "video": root / f"{STEM}.mp4",
        "subtitle": root / f"{STEM}.srt",
        "cover": root / f"{STEM}.cover.png",
        "record": root / f"{STEM}.record.json",
    }
    for kind in ("video", "subtitle", "cover"):
        paths[kind].write_bytes(f"final {kind} bytes".encode())

    source_claims = [
        "最终源帧左侧清楚可见主播",
        "最终源帧右侧清楚可见乙乙",
    ]
    narrative = "封面文字表达两人围绕测试问题争论的故事"
    authority = {
        "candidate_id": CANDIDATE_ID,
        "observed_public_title": TITLE,
        "bvid": "BV1234567890",
        "aid": 123,
        "cid": 456,
        "authority_sha256": "sha256:" + "a" * 64,
    }
    record = {
        "burned_preview": {"branding_intro": {"verification": {"duration_ms": 20_000}}},
        "story_contract": {
            "candidate_id": CANDIDATE_ID,
            "cover_reference_authority": {
                "source_visible_claims": source_claims,
                "narrative_presentation": narrative,
            },
        },
        "publish_staging": {"title": TITLE},
        "recovery_publication_authority": authority,
    }
    _write_json(paths["record"], record)
    manifest = {
        "status": ("finished_review_package_no_upload_pending_human_review"),
        "upload_allowed": False,
        "exact_candidate_ids": [CANDIDATE_ID],
        "selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [CANDIDATE_ID],
        },
        "items": [
            {
                "candidate_id": CANDIDATE_ID,
                "title": TITLE,
                "video": paths["video"].name,
                "mp4": paths["video"].name,
                "subtitle_srt": paths["subtitle"].name,
                "cover": paths["cover"].name,
                "record": paths["record"].name,
                "recovery_publication_authority": authority,
            }
        ],
    }
    review_path = root / "review_manifest.json"
    _write_json(review_path, manifest)

    audit = {
        "schema_version": builder.AUDIT_SCHEMA_VERSION,
        "policy_epoch": builder.AUDIT_POLICY_EPOCH,
        "policy_fingerprint": "sha256:" + "b" * 64,
        "auditor_source_sha256": "sha256:" + "c" * 64,
        "passed": True,
        "root": str(root.resolve()),
        "audited_inputs": [],
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }
    audit_path = verification / "package-audit.json"
    _write_json(audit_path, audit)
    monkeypatch.setattr(builder, "audit_package", lambda _root: copy.deepcopy(audit))

    # Empty placeholders test that the mechanical route needs no manual
    # contract or human statements. They are never passed off as real reviews.
    contract_path = tmp_path / "unused-human-contract.json"
    evidence_path = verification / "unused-human-evidence.json"
    _write_json(contract_path, {})
    _write_json(evidence_path, {})
    monkeypatch.setattr(human_review, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract_path)
    return {
        "root": root, "paths": paths, "manifest": manifest,
        "review_path": review_path, "audit": audit, "audit_path": audit_path,
        "contract_path": contract_path, "evidence_path": evidence_path,
        "output": verification / "mechanical-review.json", "authority": authority,
    }

@pytest.fixture
def mechanical_package(receipt_package, monkeypatch):
    from src.autoslice import mechanical_delivery_review as mechanical
    # Unit seam only: production always invokes the actual canonical auditor.
    # All real paths, bytes, target identities and create-only writes stay real.
    monkeypatch.setattr(mechanical, "audit_package", lambda root: copy.deepcopy(receipt_package["audit"]))
    ass = receipt_package["root"] / "burned.ass"
    ass.write_bytes(b"synthetic ASS for identity-only unit test")
    receipt_package["paths"]["ass"] = ass
    path = receipt_package["paths"]["record"]
    record = json.loads(path.read_text())
    video = receipt_package["paths"]["video"]
    record["burned_preview"].update(
        status="BURNED", path=str(video), ass_path=str(ass), burned_sha256=_sha256(video),
    )
    record["artifact_hashes"] = {
        "burned_video_sha256": _sha256(video), "ass_sha256": _sha256(ass),
    }
    _write_json(path, record)
    return receipt_package, mechanical


def test_mechanical_receipt_does_not_require_or_claim_human_viewing(mechanical_package):
    fixture, mechanical = mechanical_package
    fixture["evidence_path"].unlink()
    fixture["contract_path"].unlink()  # no new mandatory manual point checklist
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    assert value["fresh_human_full_playback_claimed"] is False
    assert value["new_upload_authorized"] is False
    assert "reviewed_by" not in value and "checks" not in value
    assert mechanical.validate_mechanical_receipt(
        value, fixture["root"], fixture["audit_path"],
        publication_authority=fixture["authority"],
    ) == value


def test_mechanical_cli_create_only_and_existing_uploader_replay(mechanical_package):
    fixture, _ = mechanical_package
    path = fixture["output"].with_name("mechanical-review.json")
    args = [str(fixture["root"]), "--package-audit", str(fixture["audit_path"]),
            "--mechanical", "--out", str(path)]
    assert builder.main(args) == 0
    before = path.read_bytes()
    assert builder.main(args) == 2
    assert path.read_bytes() == before

    manifest = {
        "package_attestation": {
            "package_root": str(fixture["root"].resolve()),
            "review_manifest": builder._absolute_attestation(fixture["review_path"]),
            "package_audit": builder._absolute_attestation(fixture["audit_path"]),
        },
        "recovery_publication_authority": fixture["authority"],
        "season": {"lane": "talk"},
    }
    assert human_review.attach_final_human_review(
        manifest, path, season_ids={"talk": {"season_id": 123, "section_id": 456}},
    ) == []
    assert human_review.final_human_review_attestation_problems(manifest) == []
    assert human_review.ordinary_upload_problems(manifest)  # never a new BV permission
    manifest["recovery_publication_authority"] = {**fixture["authority"], "cid": 999}
    assert human_review.final_human_review_attestation_problems(manifest)


@pytest.mark.parametrize("kind", ["video", "subtitle", "cover", "record", "ass"])
def test_mechanical_receipt_rejects_artifact_drift(mechanical_package, kind):
    fixture, mechanical = mechanical_package
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    with fixture["paths"][kind].open("ab") as handle:
        handle.write(b"\n ")
    with pytest.raises((ValueError, human_review.FinalHumanReviewError)):
        mechanical.validate_mechanical_receipt(value, fixture["root"], fixture["audit_path"])


def test_mechanical_receipt_never_overrides_a_fresh_audit_failure(mechanical_package):
    fixture, mechanical = mechanical_package
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    fixture["audit"]["passed"] = False
    fixture["audit"]["blocking_issue_count"] = 1
    with pytest.raises(ValueError, match="current canonical audit rejected"):
        mechanical.validate_mechanical_receipt(value, fixture["root"], fixture["audit_path"])


@pytest.mark.parametrize("key", ["fresh_human_full_playback_claimed", "new_upload_authorized"])
def test_mechanical_receipt_rejects_false_claims(mechanical_package, key):
    fixture, mechanical = mechanical_package
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    value[key] = True
    with pytest.raises(ValueError, match="overclaims"):
        mechanical.validate_mechanical_receipt(value, fixture["root"], fixture["audit_path"])


def test_mechanical_path_runs_real_canonical_auditor_for_invalid_package(receipt_package):
    from src.autoslice import mechanical_delivery_review as mechanical
    # Deliberately do NOT patch mechanical.audit_package: synthetic MP4 bytes
    # and a self-reported PASS cannot authorize a real delivery.
    with pytest.raises((ValueError, human_review.FinalHumanReviewError)):
        mechanical.build_mechanical_receipt(receipt_package["root"], receipt_package["audit_path"])


def test_mechanical_writer_rechecks_inputs_before_final_link(mechanical_package, monkeypatch):
    fixture, _ = mechanical_package
    original = builder._secure_json_create_only

    def drift(**kwargs):
        fixture["paths"]["video"].write_bytes(b"replaced while preparing output")
        return original(**kwargs)

    monkeypatch.setattr(builder, "_secure_json_create_only", drift)
    path = fixture["output"].with_name("mechanical-race.json")
    assert builder.main([str(fixture["root"]), "--package-audit", str(fixture["audit_path"]),
                         "--mechanical", "--out", str(path)]) == 2
    assert not path.exists()


@pytest.mark.parametrize("defect", ["not_burned", "video_hash", "ass_hash"])
def test_mechanical_review_reuses_producer_burn_identity_gate(mechanical_package, defect):
    fixture, mechanical = mechanical_package
    path = fixture["paths"]["record"]
    record = json.loads(path.read_text())
    if defect == "not_burned":
        record["burned_preview"]["status"] = "PLANNED"
    elif defect == "video_hash":
        record["artifact_hashes"]["burned_video_sha256"] = "sha256:" + "0" * 64
    else:
        record["artifact_hashes"]["ass_sha256"] = "sha256:" + "0" * 64
    _write_json(path, record)
    with pytest.raises(ValueError, match="burn output binding invalid"):
        mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
