import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import fastlane_c2_release_bridge as bridge
from src.autoslice.fastlane_c2_legacy_recovery import make_accepted_execution_contract, validate_accepted_execution_contract
from src.autoslice.fastlane_c2_formal_adapter import NAMES


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _formal(tmp_path: Path) -> Path:
    root = tmp_path / "formal"
    root.mkdir()
    for role, name in NAMES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((role + "\n").encode())
    (root / "review_manifest.json").write_text("{}", encoding="utf-8")
    (root / "package_audit.json").write_text("{}", encoding="utf-8")
    return root


def _receipt(path: Path, formal: Path) -> Path:
    proposal = formal / "ready-proposal.json"
    proposal.write_text("{}", encoding="utf-8")
    payload = {
        "candidate_id": bridge.CID,
        "title": bridge.TITLE,
        "accepted": True,
        "reviewer": "Codex root",
        "reviewed_at": "2026-08-24T20:00:00+00:00",
        "proposal": {"path": proposal.name, "bytes": proposal.stat().st_size, "sha256": "sha256:" + _sha(proposal)},
        "bindings": {
            "review_manifest_sha256": "sha256:" + _sha(formal / "review_manifest.json"),
            "package_audit_sha256": "sha256:" + _sha(formal / "package_audit.json"),
            "artifacts": {role: {"sha256": "sha256:" + _sha(formal / name)} for role, name in NAMES.items()},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return proposal


def _authorization(path: Path) -> None:
    payload = {
        "schema_version": bridge.AUTH_SCHEMA,
        "candidate_id": bridge.CID,
        "recording_date": bridge.DATE,
        "title": bridge.TITLE,
        "direct_ivan_lines": [
            {
                "line": line,
                "uuid": uuid,
                "timestamp": timestamp,
                "raw_line_sha256": raw_sha,
                "content_sha256": content_sha,
                "quote": quote,
            }
            for line, (uuid, timestamp, raw_sha, content_sha, quote) in bridge.DIRECT_IVAN_LINES.items()
        ],
        "remaining_machine_gates": [
            "accepted C2 root technical receipt bound to current formal audit and artifacts",
            "current C2 release-package audit",
            "CPA title-cover joint QC for exact final title and cover",
            "authorized-upload manifest verify",
            "single serialized upload and public Creator section reconciliation",
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["self_seal"] = {"canonical_json_without_self_seal_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _tags(*_args, **_kwargs):
    return {
        "engine": "suggest-upload-tags.v2",
        "status": "OK",
        "final_tags": ["李豆沙", "虚拟主播", "虚拟UP主", "直播切片", "吐槽"],
        "final_tag_line": "李豆沙,虚拟主播,虚拟UP主,直播切片,吐槽",
        "proper_noun_tags": [], "important_content_ips": [], "content_tags": [], "warnings": [],
    }


def _receipt_binding(path: Path, proposal: Path) -> None:
    path.write_text(json.dumps({
        "proposal": {"path": proposal.name, "bytes": proposal.stat().st_size, "sha256": "sha256:" + _sha(proposal)},
    }), encoding="utf-8")


def test_c2_receipt_requires_its_exact_self_bound_proposal(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    proposal = formal / "exact-proposal.json"
    proposal.write_text("{}", encoding="utf-8")
    other = formal / "other-proposal.json"
    other.write_text("{}", encoding="utf-8")
    receipt = tmp_path / bridge.ROOT_RECEIPT_NAME
    _receipt_binding(receipt, proposal)
    assert bridge._receipt_bound_proposal(formal, proposal, receipt) == proposal
    with pytest.raises(bridge.C2ReleaseBridgeError, match="not receipt-bound"):
        bridge._receipt_bound_proposal(formal, other, receipt)
    proposal.write_text('{"drift":true}', encoding="utf-8")
    with pytest.raises(bridge.C2ReleaseBridgeError, match="binding drift"):
        bridge._receipt_bound_proposal(formal, proposal, receipt)


def test_c2_authorization_asset_is_self_sealed_and_registry_hash_bound():
    repo = Path(__file__).resolve().parents[1]
    authorization = repo / "assets/lidousha/fastlane_c2_private/auto_203011_328_389.release-authorization.v1.json"
    bridge._validate_authorization(json.loads(authorization.read_text(encoding="utf-8")))
    registry = json.loads((repo / "assets/lidousha/publication_registry.v1.json").read_text(encoding="utf-8"))
    row = next(item for item in registry["entries"] if item.get("candidate_id") == bridge.CID)
    assert row["status"] == "released_for_upload"
    assert row["release_authorization"] == {
        "path": "assets/lidousha/fastlane_c2_private/auto_203011_328_389.release-authorization.v1.json",
        "bytes": authorization.stat().st_size,
        "sha256": "sha256:" + _sha(authorization),
        "direct_ivan_lines": [947, 1643, 1745],
        "remaining_machine_gates": [
            "accepted C2 root technical receipt bound to current formal audit and artifacts",
            "current C2 release-package audit",
            "CPA title-cover joint QC for exact final title and cover",
            "authorized-upload manifest verify",
            "single serialized upload and public Creator section reconciliation",
        ],
    }


def test_c2_legacy_execution_envelope_rejects_any_signature_surface_drift(tmp_path):
    proposal = tmp_path / "proposal.json"
    proposal.write_text("{}", encoding="utf-8")
    accepted = make_accepted_execution_contract(proposal=proposal, reviewed_at="2026-08-24T20:00:00+00:00", decision_basis="root acceptance fixture")
    validate_accepted_execution_contract(accepted, proposal=proposal)
    for key, value in (("reviewed_by", "other"), ("reviewed_at", "2026-08-24T20:00:00"), ("decision_basis", "")):
        drift = dict(accepted); drift[key] = value
        with pytest.raises(ValueError):
            validate_accepted_execution_contract(drift, proposal=proposal)
    proposal.write_text('{"drift":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="proposal binding"):
        validate_accepted_execution_contract(accepted, proposal=proposal)


def test_c2_bridge_projects_strict_same_stem_package(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(
        formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization,
        legacy_proposal=receipt, legacy_execution_contract=receipt,
        out=out, tag_generator=_tags,
    )
    record = json.loads((out / bridge.RECORD_NAME).read_text())
    review = json.loads((out / "review_manifest.json").read_text())
    assert record["upload_tags"]["final_tags"] == _tags()["final_tags"]
    assert review["items"] == [bridge._release_item()]
    assert (out / bridge.VIDEO_NAME).read_bytes() == (formal / NAMES["video"]).read_bytes()
    assert bridge.audit_fastlane_c2_release_package(out) == []


@pytest.mark.parametrize("target", [bridge.VIDEO_NAME, bridge.SRT_NAME, bridge.COVER_NAME])
def test_c2_bridge_rejects_final_artifact_drift(monkeypatch, tmp_path, target):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=out, tag_generator=_tags)
    (out / target).write_bytes(b"drift")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


def test_c2_bridge_rejects_authority_receipt_and_tag_drift(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=out, tag_generator=_tags)
    record_path = out / bridge.RECORD_NAME
    record = json.loads(record_path.read_text())
    record["legacy_execution_contract"]["sha256"] = "sha256:" + "0" * 64
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


@pytest.mark.parametrize("path_name, mutate", [
    (bridge.ROOT_RECEIPT_NAME, lambda value: value.__setitem__("accepted", False)),
    (bridge.AUTH_NAME, lambda value: value["direct_ivan_lines"][0].__setitem__("quote", "drift")),
    (bridge.TAG_RECEIPT_NAME, lambda value: value.__setitem__("final_tags", ["drift"])),
])
def test_c2_bridge_rejects_receipt_authority_and_tags_drift(monkeypatch, tmp_path, path_name, mutate):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=out, tag_generator=_tags)
    path = out / path_name
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


def test_c2_bridge_rejects_non_c2_authorization(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    payload = json.loads(authorization.read_text())
    payload["candidate_id"] = "other"
    authorization.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    with pytest.raises(bridge.C2ReleaseBridgeError, match="authorization identity drift"):
        bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=tmp_path / "out", tag_generator=_tags)
