import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import fastlane_c2_release_bridge as bridge
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


def _receipt(path: Path, formal: Path) -> None:
    payload = {
        "candidate_id": bridge.CID,
        "title": bridge.TITLE,
        "accepted": True,
        "reviewer": "Codex root",
        "reviewed_at": "2026-08-24T20:00:00+00:00",
        "bindings": {
            "review_manifest_sha256": "sha256:" + _sha(formal / "review_manifest.json"),
            "package_audit_sha256": "sha256:" + _sha(formal / "package_audit.json"),
            "artifacts": {role: {"sha256": "sha256:" + _sha(formal / name)} for role, name in NAMES.items()},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _authorization(path: Path) -> None:
    payload = {
        "schema_version": bridge.AUTH_SCHEMA,
        "candidate_id": bridge.CID,
        "recording_date": bridge.DATE,
        "title": bridge.TITLE,
        "direct_ivan_lines": [
            {"line": line, "quote": quote, "raw_line_sha256": digest}
            for line, (digest, quote) in bridge.DIRECT_IVAN_LINES.items()
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _tags(*_args, **_kwargs):
    return {
        "engine": "suggest-upload-tags.v2",
        "status": "OK",
        "final_tags": ["李豆沙", "虚拟主播", "虚拟UP主", "直播切片", "吐槽"],
        "final_tag_line": "李豆沙,虚拟主播,虚拟UP主,直播切片,吐槽",
        "proper_noun_tags": [], "important_content_ips": [], "content_tags": [], "warnings": [],
    }


def test_c2_bridge_projects_strict_same_stem_package(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / "receipt.json", tmp_path / "auth.json", tmp_path / "out"
    _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    bridge.build_release_package(
        formal_package=formal, root_receipt=receipt, authorization=authorization,
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
    receipt, authorization, out = tmp_path / "receipt.json", tmp_path / "auth.json", tmp_path / "out"
    _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    bridge.build_release_package(formal_package=formal, root_receipt=receipt, authorization=authorization, out=out, tag_generator=_tags)
    (out / target).write_bytes(b"drift")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


def test_c2_bridge_rejects_authority_receipt_and_tag_drift(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / "receipt.json", tmp_path / "auth.json", tmp_path / "out"
    _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    bridge.build_release_package(formal_package=formal, root_receipt=receipt, authorization=authorization, out=out, tag_generator=_tags)
    record_path = out / bridge.RECORD_NAME
    record = json.loads(record_path.read_text())
    record["story_contract"]["candidate_id"] = "other"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


@pytest.mark.parametrize("path_name, mutate", [
    (bridge.ROOT_RECEIPT_NAME, lambda value: value.__setitem__("accepted", False)),
    (bridge.AUTH_NAME, lambda value: value["direct_ivan_lines"][0].__setitem__("quote", "drift")),
    (bridge.TAG_RECEIPT_NAME, lambda value: value.__setitem__("final_tags", ["drift"])),
])
def test_c2_bridge_rejects_receipt_authority_and_tags_drift(monkeypatch, tmp_path, path_name, mutate):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / "receipt.json", tmp_path / "auth.json", tmp_path / "out"
    _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    bridge.build_release_package(formal_package=formal, root_receipt=receipt, authorization=authorization, out=out, tag_generator=_tags)
    path = out / path_name
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


def test_c2_bridge_rejects_non_c2_authorization(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization = tmp_path / "receipt.json", tmp_path / "auth.json"
    _receipt(receipt, formal)
    _authorization(authorization)
    payload = json.loads(authorization.read_text())
    payload["candidate_id"] = "other"
    authorization.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    with pytest.raises(bridge.C2ReleaseBridgeError, match="authorization identity drift"):
        bridge.build_release_package(formal_package=formal, root_receipt=receipt, authorization=authorization, out=tmp_path / "out", tag_generator=_tags)
