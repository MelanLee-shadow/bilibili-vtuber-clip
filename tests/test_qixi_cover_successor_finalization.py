"""Focused tests for the single Qixi CPA cover-successor lane."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import build_manual_review_manifest as manual_manifest
from src.autoslice import qixi_cover_successor_finalization as successor
from src.autoslice import qixi_review_package_owner_bridge as owner_bridge


CID = successor.CANDIDATE_ID


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _authority() -> dict[str, object]:
    return {
        "authority_sha256": "sha256:" + "a" * 64,
        "predecessor": {
            "previous_cover_sha256": "",
            "failed_joint_qc_sha256": "sha256:" + "b" * 64,
        },
        "route_preserving_attempt": {"status": "REPAIRED"},
        "replacement": {
            "final_cover_sha256": "sha256:" + "1" * 64,
            "route_background_sha256": "sha256:" + "2" * 64,
            "pre_overlay_sha256": "sha256:" + "3" * 64,
            "title_mask_sha256": "sha256:" + "4" * 64,
            "required_title_lines": ["甲", "乙"],
        },
    }


def _generation() -> dict[str, object]:
    return {
        "cover_text": "甲\n乙",
        "pre_overlay_sha256": "sha256:" + "3" * 64,
        "rendered_text_pixels": {"mask_sha256": "sha256:" + "4" * 64},
    }


def test_finalize_apply_is_create_only_and_freezes_noncover_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preimage = tmp_path / "preimage"
    package = preimage / "replacement_recuts"
    old_cover = _write(package / f"{CID}.cover.png", b"old-cover")
    authority = _authority()
    authority["predecessor"]["previous_cover_sha256"] = _sha(old_cover)
    record = {
        "story_contract": {"candidate_id": CID},
        "publish_staging": {"cover_generation": {"title": "frozen", "art_direction": {}}},
        "artifact_hashes": {},
    }
    _write(package / f"{CID}.record.json", json.dumps(record).encode())
    _write(package / f"{CID}.publish.json", b"{}")
    _write(preimage / "frozen.bin", b"must-not-change")
    trial = tmp_path / "trial"
    files = {
        key: _write(trial / f"{key}.bin", key.encode())
        for key in ("final", "background", "pre", "mask")
    }
    output_parent = tmp_path / "private"
    output_parent.mkdir(mode=0o700)
    os.chmod(output_parent, 0o700)

    monkeypatch.setattr(successor, "load_authority", lambda _repo: authority)
    monkeypatch.setattr(successor, "validate_provider_evidence", lambda **_kwargs: {})
    monkeypatch.setattr(successor, "_trial_files", lambda _root, _authority: (files, {}))
    monkeypatch.setattr(successor, "_write_portable_provenance", lambda **_kwargs: {})
    monkeypatch.setattr(successor, "_generation", lambda **_kwargs: _generation())

    target = output_parent / CID
    result = successor.finalize(
        repo_root=tmp_path,
        preimage=preimage,
        trial_root=trial,
        target=target,
        apply=True,
        punch_response="observed",
    )

    assert result["mode"] == "APPLIED"
    assert (target / "frozen.bin").read_bytes() == b"must-not-change"
    assert (target / "replacement_recuts" / successor.RECEIPT).is_file()
    with pytest.raises(successor.QixiCoverSuccessorError, match="create-only"):
        successor.finalize(
            repo_root=tmp_path,
            preimage=preimage,
            trial_root=trial,
            target=target,
            apply=False,
        )


@pytest.mark.parametrize("bad", ["/tmp/cover.png", "../escape.png"])
def test_portable_locator_rejects_absolute_and_escape_paths(bad: str) -> None:
    generation = {
        "final_cover": bad,
        "ai_background": "cover.ai-bg.png",
        "pre_overlay_path": "cover.pre.png",
        "reference_image": "evidence/ref.png",
        "request_path": "evidence/request.json",
        "response_path": "evidence/response.json",
        "rendered_text_pixels": {"mask_path": "cover.mask.png", "pre_overlay_path": "cover.pre.png"},
        "final_host_identity_verification": {
            "final_cover_path": "cover.png",
            "comparison_path": "evidence/identity.png",
            "reference_path": "evidence/ref.png",
            "witness": {"image_path": "evidence/identity.png"},
        },
    }
    with pytest.raises(successor.QixiCoverSuccessorError, match="package-relative"):
        successor.validate_portable_cover_locators(generation)


def test_portable_locator_recursively_rejects_absolute_provenance() -> None:
    with pytest.raises(successor.QixiCoverSuccessorError, match="absolute locator"):
        successor._assert_no_absolute_locator(
            {"document": {"request_path": "/opt/bilive/private.json"}},
            label="test",
        )


def test_private_output_parent_rejects_symlink_and_wrong_mode(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    with pytest.raises(successor.QixiCoverSuccessorError, match="owner-private"):
        successor._private_output_parent(unsafe)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(successor.QixiCoverSuccessorError, match="unsafe"):
        successor._private_output_parent(link)


def test_receipt_replay_rejects_path_hash_schema_candidate_and_upload_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    paths = [
        successor._PORTABLE_PATHS["final"],
        successor._PORTABLE_PATHS["background"],
        successor._PORTABLE_PATHS["pre"],
        successor._PORTABLE_PATHS["mask"],
        f"{CID}.record.json",
        f"{CID}.publish.json",
    ]
    for name in paths:
        _write(package / name, name.encode())
    receipt = {
        "schema_version": successor.SCHEMA,
        "mode": "APPLIED",
        "candidate_id": CID,
        "status": "finished_review_package_no_upload_pending_human_review",
        "upload_allowed": False,
        "ready_for_serial_upload": "NO_WAITING_FINAL_HUMAN_REVIEW",
        "authority_sha256": "sha256:" + "a" * 64,
        "preimage_noncover_sha256": "sha256:" + "b" * 64,
        "final_cover_sha256": _sha(package / paths[0]),
        "route_background_sha256": _sha(package / paths[1]),
        "pre_overlay_sha256": _sha(package / paths[2]),
        "title_mask_sha256": _sha(package / paths[3]),
        "record_sha256": _sha(package / paths[4]),
        "publish_sha256": _sha(package / paths[5]),
    }
    monkeypatch.setattr(successor, "_validate_deep_package", lambda **_kwargs: None)
    assert successor.validate_applied_receipt(receipt, package_root=package, repo_root=tmp_path)["candidate_id"] == CID
    for key, value in (
        ("candidate_id", "other"),
        ("upload_allowed", True),
        ("schema_version", "wrong"),
        ("final_cover_sha256", "sha256:" + "0" * 64),
    ):
        drifted = dict(receipt)
        drifted[key] = value
        with pytest.raises(successor.QixiCoverSuccessorError):
            successor.validate_applied_receipt(drifted, package_root=package, repo_root=tmp_path)


def test_manual_builder_prepares_successor_gate_with_deep_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = {"schema_version": successor.SCHEMA}
    receipt_path = _write(tmp_path / successor.RECEIPT, json.dumps(receipt).encode())
    monkeypatch.setattr(manual_manifest, "validate_qixi_successor_receipt", lambda *_a, **_kw: receipt)
    monkeypatch.setattr(manual_manifest, "validate_recovery_publication_authority", lambda value, **_kw: value)
    monkeypatch.setattr(
        manual_manifest,
        "_sync_record_bound_candidate_artifacts",
        lambda **_kw: {"chat_authority": "chat.json", "clip_context": "clip.json"},
    )
    record = {"recovery_publication_authority": {"sealed": True}, "publish_staging": {"recovery_publication_authority": {"sealed": True}}}
    publish = {"recovery_publication_authority": {"sealed": True}}
    gate = manual_manifest._prepare_qixi_gate(
        package_root=tmp_path,
        stem=CID,
        candidate_id=CID,
        title="frozen",
        record_doc=record,
        publish_doc=publish,
        qixi_repo_root=tmp_path,
    )
    assert gate.successor_receipt is True
    assert gate.receipt_sha256 == "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()


def test_owner_bridge_requires_successor_receipt_hash_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b'{"mode":"APPLIED"}'
    _write(tmp_path / successor.RECEIPT, payload)
    monkeypatch.setattr(owner_bridge, "validate_cover_successor_receipt", lambda *_a, **_kw: {})
    monkeypatch.setattr(owner_bridge, "validate_projection_assets", lambda _root: {"terminal": "ok"})
    item = {
        "candidate_id": CID,
        "qixi_cover_successor_finalization": successor.RECEIPT,
        "qixi_cover_successor_finalization_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }
    assert owner_bridge.manifest_bound_terminal_projection_authority(
        package_root=tmp_path, item=item, qixi_repo_root=tmp_path
    ) == {"terminal": "ok"}
    item["qixi_cover_successor_finalization_sha256"] = "sha256:" + "0" * 64
    assert owner_bridge.manifest_bound_terminal_projection_authority(
        package_root=tmp_path, item=item, qixi_repo_root=tmp_path
    ) is None
