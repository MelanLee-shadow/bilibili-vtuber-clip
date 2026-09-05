"""Focused tests for the single Qixi CPA cover-successor lane."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from scripts import build_manual_review_manifest as manual_manifest
from scripts import finalize_qixi_cover_successor_review_package as successor_review_package
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
    monkeypatch.setattr(successor, "load_channel_profile", lambda _repo: object())
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
    for item in [target, *target.rglob("*")]:
        expected_mode = 0o700 if item.is_dir() else 0o600
        assert stat.S_IMODE(item.lstat().st_mode) == expected_mode
        assert item.lstat().st_uid == os.geteuid()
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


@pytest.mark.parametrize(
    "relative",
    [
        "replacement_recuts/verification/final-human-review-evidence.v2.json",
        "replacement_recuts/verification/final-human-review.json",
        "replacement_recuts/verification/.final-human-review-evidence.v2.json.tmp.1234."
        + "a" * 32,
        "replacement_recuts/verification/.final-human-review.json.tmp.9876."
        + "b" * 32,
    ],
)
def test_snapshot_ignores_only_canonical_final_human_sidecars_and_temps(
    tmp_path: Path, relative: str
) -> None:
    _write(tmp_path / "replacement_recuts" / "frozen.bin", b"frozen")
    baseline = successor._snapshot(tmp_path)
    _write(tmp_path / relative, b"dynamic final human review output")
    assert successor._snapshot(tmp_path) == baseline


@pytest.mark.parametrize(
    "relative",
    [
        "replacement_recuts/verification/unrelated.json",
        "replacement_recuts/verification/.final-human-review.json.tmp.0." + "a" * 32,
        "replacement_recuts/verification/.final-human-review.json.tmp.12." + "A" * 32,
        "replacement_recuts/verification/.final-human-review-evidence.v2.json.tmp.12."
        + "a" * 31,
        "replacement_recuts/verification/.other.json.tmp.12." + "a" * 32,
    ],
)
def test_snapshot_keeps_unknown_or_malformed_final_human_files_in_closure(
    tmp_path: Path, relative: str
) -> None:
    _write(tmp_path / "replacement_recuts" / "frozen.bin", b"frozen")
    baseline = successor._snapshot(tmp_path)
    _write(tmp_path / relative, b"must change closure")
    assert successor._snapshot(tmp_path) != baseline


def test_snapshot_rejects_final_human_symlink_and_special_file(tmp_path: Path) -> None:
    verification = tmp_path / "replacement_recuts" / "verification"
    verification.mkdir(parents=True)
    (verification / "final-human-review.json").symlink_to(tmp_path / "missing.json")
    with pytest.raises(successor.QixiCoverSuccessorError, match="symlink"):
        successor._snapshot(tmp_path)

    (verification / "final-human-review.json").unlink()
    os.mkfifo(verification / "final-human-review-evidence.v2.json")
    with pytest.raises(successor.QixiCoverSuccessorError, match="non-regular"):
        successor._snapshot(tmp_path)


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


@pytest.mark.parametrize(
    ("candidate_id", "receipt_name"),
    [
        ("other-candidate", successor.RECEIPT),
        (CID, "../" + successor.RECEIPT),
        (CID, "/private/tmp/" + successor.RECEIPT),
        (CID, "another-receipt.json"),
    ],
)
def test_owner_bridge_rejects_wrong_successor_candidate_or_receipt_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_id: str,
    receipt_name: str,
) -> None:
    payload = b'{"mode":"APPLIED"}'
    _write(tmp_path / successor.RECEIPT, payload)
    monkeypatch.setattr(owner_bridge, "validate_cover_successor_receipt", lambda *_a, **_kw: {})
    item = {
        "candidate_id": candidate_id,
        "qixi_cover_successor_finalization": receipt_name,
        "qixi_cover_successor_finalization_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }
    assert owner_bridge.manifest_bound_terminal_projection_authority(
        package_root=tmp_path,
        item=item,
        qixi_repo_root=tmp_path,
    ) is None


def test_manual_builder_passes_explicit_repo_to_successor_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / successor.RECEIPT, b'{"schema_version":"receipt"}')
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        manual_manifest,
        "validate_qixi_successor_receipt",
        lambda *_a, **kwargs: calls.append(kwargs) or {},
    )
    monkeypatch.setattr(manual_manifest, "validate_recovery_publication_authority", lambda value, **_kw: value)
    monkeypatch.setattr(
        manual_manifest,
        "_sync_record_bound_candidate_artifacts",
        lambda **_kw: {"chat_authority": "chat.json", "clip_context": "clip.json"},
    )
    record = {"recovery_publication_authority": {"sealed": True}, "publish_staging": {"recovery_publication_authority": {"sealed": True}}}
    manual_manifest._prepare_qixi_gate(
        package_root=tmp_path,
        stem=CID,
        candidate_id=CID,
        title="frozen",
        record_doc=record,
        publish_doc={"recovery_publication_authority": {"sealed": True}},
        qixi_repo_root=tmp_path,
    )
    assert calls == [{"package_root": tmp_path, "repo_root": tmp_path}]


def test_deep_replay_has_independent_route_identity_pixel_qc_and_text_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every semantic gate remains active after receipt hash verification."""

    package = tmp_path / "candidate" / "replacement_recuts"
    final = _write(package / successor._PORTABLE_PATHS["final"], b"final")
    background = _write(package / successor._PORTABLE_PATHS["background"], b"background")
    pre = _write(package / successor._PORTABLE_PATHS["pre"], b"pre")
    mask = _write(package / successor._PORTABLE_PATHS["mask"], b"mask")
    identity = _write(package / successor._PORTABLE_PATHS["identity"], b"identity")
    lines = ["甲", "乙"]
    generation = {
        "rendered_lines": lines,
        "cover_text": "甲\n乙",
        "final_cover": successor._PORTABLE_PATHS["final"],
        "ai_background": successor._PORTABLE_PATHS["background"],
        "pre_overlay_path": successor._PORTABLE_PATHS["pre"],
        "reference_image": "evidence/ref.png",
        "request_path": "evidence/request.json",
        "response_path": "evidence/response.json",
        "rendered_text_pixels": {
            "mask_path": successor._PORTABLE_PATHS["mask"],
            "pre_overlay_path": successor._PORTABLE_PATHS["pre"],
        },
        "final_host_identity_verification": {
            "comparison_sha256": _sha(identity),
            "final_cover_path": successor._PORTABLE_PATHS["final"],
            "comparison_path": successor._PORTABLE_PATHS["identity"],
            "reference_path": "evidence/ref.png",
            "witness": {"image_path": successor._PORTABLE_PATHS["identity"]},
        },
        "art_direction": {"cover_punch_semantic_review": {}},
    }
    record = {
        "story_contract": {"candidate_id": CID, "selection_hook": "可验证的七夕直播安排"},
        "publish_staging": {"cover_generation": generation},
        "cover_generation": generation,
        "qixi_cover_successor": {"authority_sha256": "sha256:" + "a" * 64},
    }
    _write(package / f"{CID}.record.json", json.dumps(record).encode())
    _write(package / f"{CID}.publish.json", json.dumps({"cover_generation": generation}).encode())
    replacement = {
        "final_cover_sha256": _sha(final),
        "route_background_sha256": _sha(background),
        "pre_overlay_sha256": _sha(pre),
        "title_mask_sha256": _sha(mask),
        "required_title_lines": lines,
    }
    authority = {"authority_sha256": "sha256:" + "a" * 64, "replacement": replacement}
    provenance = {
        "generation": {
            "final_cover_sha256": _sha(final), "ai_background_sha256": _sha(background),
            "pre_overlay_sha256": _sha(pre), "rendered_lines": lines,
        },
        "request": {"method": "images.edit", "image_gen_model": "cpa"},
        "response": {"attempts": [{"output_sha256": _sha(background)}]},
        "no_text": {"status": "OBSERVED", "image_sha256": _sha(pre)[7:], "answer": '{"has_readable_text":false,"text_fragments":[]}'},
        "joint": {"status": "PASS", "pass": True, "cover_sha256": _sha(final), "verdict": {"unrelated_or_misleading_elements": []}},
        "identity": {"image_sha256": _sha(identity)},
    }
    monkeypatch.setattr(successor, "load_authority", lambda _repo: authority)
    monkeypatch.setattr(successor, "validate_provider_evidence", lambda **_kwargs: {})
    monkeypatch.setattr(successor, "load_channel_profile", lambda _repo: object())
    monkeypatch.setattr(successor, "resolve_trusted_cover_font", lambda **_kwargs: tmp_path / "font.ttf")
    monkeypatch.setattr(successor, "validate_cover_route_decision", lambda *_a, **_kw: True)
    monkeypatch.setattr(successor, "validate_final_host_identity_verification", lambda *_a: True)
    monkeypatch.setattr(successor, "verify_rendered_text_pixel_artifacts", lambda *_a, **_kw: True)
    monkeypatch.setattr(successor, "verify_pre_overlay_route_background", lambda **_kw: True)
    monkeypatch.setattr(successor, "validate_cover_punch_semantic_review", lambda *_a, **_kw: True)
    monkeypatch.setattr(successor, "_provenance_document", lambda _p, key, **_kw: provenance[key])
    receipt = {"preimage_noncover_sha256": successor._canonical_sha(successor._snapshot(package.parent))}

    successor._validate_deep_package(package=package, receipt=receipt, repo=tmp_path)
    monkeypatch.setattr(successor, "validate_cover_route_decision", lambda *_a, **_kw: False)
    with pytest.raises(successor.QixiCoverSuccessorError, match="route"):
        successor._validate_deep_package(package=package, receipt=receipt, repo=tmp_path)
    monkeypatch.setattr(successor, "validate_cover_route_decision", lambda *_a, **_kw: True)
    monkeypatch.setattr(successor, "validate_final_host_identity_verification", lambda *_a: False)
    with pytest.raises(successor.QixiCoverSuccessorError, match="identity"):
        successor._validate_deep_package(package=package, receipt=receipt, repo=tmp_path)
    monkeypatch.setattr(successor, "validate_final_host_identity_verification", lambda *_a: True)
    monkeypatch.setattr(successor, "verify_rendered_text_pixel_artifacts", lambda *_a, **_kw: False)
    with pytest.raises(successor.QixiCoverSuccessorError, match="pixel recomposition"):
        successor._validate_deep_package(package=package, receipt=receipt, repo=tmp_path)
    monkeypatch.setattr(successor, "verify_rendered_text_pixel_artifacts", lambda *_a, **_kw: True)
    monkeypatch.setattr(successor, "verify_pre_overlay_route_background", lambda **_kw: False)
    with pytest.raises(successor.QixiCoverSuccessorError, match="background recomposition"):
        successor._validate_deep_package(package=package, receipt=receipt, repo=tmp_path)
    monkeypatch.setattr(successor, "verify_pre_overlay_route_background", lambda **_kw: True)
    provenance["no_text"] = {"status": "OBSERVED", "image_sha256": _sha(pre)[7:], "answer": '{"has_readable_text":true,"text_fragments":["x"]}'}
    with pytest.raises(successor.QixiCoverSuccessorError, match="no-text/joint-QC"):
        successor._validate_deep_package(package=package, receipt=receipt, repo=tmp_path)


def test_portable_provenance_replay_binds_every_source_hash_and_document(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = tmp_path / "package"
    source_documents = {
        "generation": {"kind": "generation", "value": "sealed"},
        "request": {"kind": "request", "value": "sealed"},
        "response": {"kind": "response", "value": "sealed"},
        "joint": {"kind": "joint", "value": "sealed"},
    }
    replacement: dict[str, object] = {}
    for key, document in source_documents.items():
        source = _write(repo / "sealed" / f"{key}.json", json.dumps(document).encode())
        if key == "joint":
            continue
        replacement[f"provider_{key}_relative_path"] = f"sealed/{key}.json"
        replacement[f"provider_{key}_sha256"] = _sha(source)
    joint_path = repo / "sealed" / "joint.json"
    identity_sha = "sha256:" + "1" * 64
    no_text_source_sha = "sha256:" + "2" * 64
    identity_document = {
        "image_path": successor._PORTABLE_PATHS["identity"],
        "image_sha256": identity_sha,
    }
    no_text_document = {"status": "OBSERVED", "answer": "sealed"}
    replacement.update(
        {
            "identity_witness_sha256": identity_sha,
            "identity_portable_document_sha256": successor._canonical_sha(identity_document),
            "no_model_text_witness_sha256": no_text_source_sha,
            "no_model_text_portable_document_sha256": successor._canonical_sha(no_text_document),
        }
    )
    authority = {
        "replacement": replacement,
        "joint_qc": {"relative_path": "sealed/joint.json", "sha256": _sha(joint_path)},
    }
    documents = {**source_documents, "identity": identity_document, "no_text": no_text_document}
    evidence = package / "evidence" / "qixi-cover-successor"
    for key, document in documents.items():
        payload = {
            "schema_version": successor.PORTABLE_PROVENANCE_SCHEMA,
            "kind": key,
            "source_sha256": successor._expected_provenance_source_hashes(authority)[key],
            "document": document,
        }
        _write(evidence / successor._PROVENANCE_NAMES[key], json.dumps(payload).encode())
    for key in documents:
        assert successor._provenance_document(package, key, authority=authority, repo=repo) == documents[key]

    generation_path = evidence / successor._PROVENANCE_NAMES["generation"]
    payload = json.loads(generation_path.read_text())
    payload["source_sha256"] = "sha256:" + "0" * 64
    generation_path.write_text(json.dumps(payload))
    with pytest.raises(successor.QixiCoverSuccessorError, match="source hash drifts"):
        successor._provenance_document(package, "generation", authority=authority, repo=repo)

    payload["source_sha256"] = successor._expected_provenance_source_hashes(authority)["generation"]
    payload["document"]["value"] = "tampered"
    generation_path.write_text(json.dumps(payload))
    with pytest.raises(successor.QixiCoverSuccessorError, match="document drifts"):
        successor._provenance_document(package, "generation", authority=authority, repo=repo)


def test_current_successor_audit_rejects_stale_root_or_input_hash(tmp_path: Path) -> None:
    package = tmp_path / "replacement_recuts"
    required = [
        successor._PORTABLE_PATHS["final"],
        f"{CID}.record.json",
        f"{CID}.publish.json",
        "review_manifest.json",
        successor.RECEIPT,
    ]
    rows = []
    for relative in required:
        path = _write(package / relative, relative.encode())
        rows.append({"path": relative, "sha256": _sha(path)[7:], "bytes": path.stat().st_size})
    audit = {
        "schema_version": "lidousha-review-package-audit.v2",
        "passed": True,
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
        "root": str(package.resolve()),
        "audited_inputs": rows,
    }
    audit_path = _write(package / successor.PACKAGE_AUDIT, json.dumps(audit).encode())
    assert successor.validate_current_successor_audit(package_root=package)["passed"] is True

    audit["root"] = "/opt/bilive/qixi-successor-preimage/stale"
    audit_path.write_text(json.dumps(audit))
    with pytest.raises(successor.QixiCoverSuccessorError, match="stale or failed"):
        successor.validate_current_successor_audit(package_root=package)

    audit["root"] = str(package.resolve())
    audit["audited_inputs"][0]["sha256"] = "0" * 64
    audit_path.write_text(json.dumps(audit))
    with pytest.raises(successor.QixiCoverSuccessorError, match="input hash drifts"):
        successor.validate_current_successor_audit(package_root=package)


def test_review_orchestrator_reseals_manifest_and_current_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / CID
    package = target / "replacement_recuts"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    receipt = _write(package / successor.RECEIPT, b"receipt")
    required = [
        successor._PORTABLE_PATHS["final"],
        f"{CID}.record.json",
        f"{CID}.publish.json",
        "review_manifest.json",
        successor.RECEIPT,
    ]
    for relative in required[:-1]:
        _write(package / relative, relative.encode())
    _write(package / successor.PACKAGE_AUDIT, b"{}")
    monkeypatch.setattr(
        successor_review_package,
        "finalize",
        lambda **_kwargs: {"mode": "APPLIED", "target": str(target)},
    )
    monkeypatch.setattr(
        successor_review_package,
        "build_manual",
        lambda *_args, **_kwargs: {"items": [{"candidate_id": CID}]},
    )

    def audit(_package: Path, **_kwargs: object) -> dict[str, object]:
        inputs = []
        for relative in required:
            path = package / relative
            inputs.append({"path": relative, "sha256": _sha(path)[7:], "bytes": path.stat().st_size})
        return {
            "schema_version": "lidousha-review-package-audit.v2",
            "passed": True,
            "issues": [],
            "issue_count": 0,
            "blocking_issue_count": 0,
            "root": str(package.resolve()),
            "audited_inputs": inputs,
        }

    monkeypatch.setattr(successor_review_package, "audit_package", audit)
    result = successor_review_package.build_review_package(
        preimage=tmp_path / "preimage",
        trial_root=tmp_path / "trial",
        target=target,
        punch_response="observed",
        operator="operator",
        note="pending human review",
    )
    assert result["audit_passed"] is True
    assert receipt.is_file()
    for item in [target, *target.rglob("*")]:
        expected_mode = 0o700 if item.is_dir() else 0o600
        assert stat.S_IMODE(item.lstat().st_mode) == expected_mode
