from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import host_only_v4_successor as successor
from src.autoslice.cover_host_identity_gate import (
    HOST_ONLY_AUTHORITY,
    HOST_ONLY_SCHEMA_VERSION,
)


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _fixture(tmp_path: Path) -> dict[str, object]:
    candidate = "auto_010203_4_5"
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence").mkdir()
    cover_bytes = b"cover-pixels"
    video_bytes = b"video-bytes"
    reference_bytes = b"reference-pixels"
    comparison_bytes = b"comparison-pixels"
    cover = source / "cover.png"
    video = source / "video.mp4"
    reference = source / "evidence" / "cover-ref.png"
    cover.write_bytes(cover_bytes)
    video.write_bytes(video_bytes)
    reference.write_bytes(reference_bytes)
    generation = {
        "candidate_id": candidate,
        "method": "screenshot_polish",
        "final_cover_sha256": _sha(cover_bytes),
        "reference_sha256": _sha(reference_bytes),
        "reference_image": "/legacy/cover-ref.png",
        "story_contract": {"cover_fallback_mode": "HOST_ONLY_GENERIC"},
        "source_composition_verification": {"scene_kind": "talk"},
        "route_decision": {"schema_version": "lidousha-cover-route-decision.v2"},
        "final_host_identity_verification": {
            "schema_version": "lidousha-cover-final-host-identity-verification.v3",
            "status": "PASS",
        },
    }
    publish = {"cover_generation": generation, "title": "title"}
    publish_bytes = _json_bytes(publish)
    publish_path = source / "candidate.publish.json"
    publish_path.write_bytes(publish_bytes)
    record = {
        "artifact_hashes": {"publish_draft_sha256": _sha(publish_bytes)},
        "publish_staging": {"cover_generation": generation},
    }
    evidence_path = source / "candidate.record.json"
    record_path = source / "candidate.delivery.record.json"
    evidence_path.write_bytes(_json_bytes(record))
    record_path.write_bytes(_json_bytes(record))
    manifest = {
        "items": [
            {
                "candidate_id": candidate,
                "cover": cover.name,
                "video": video.name,
                "evidence_json": evidence_path.name,
                "record": record_path.name,
                "publish_json": publish_path.name,
                "sha256": {
                    "cover": _sha(cover_bytes).removeprefix("sha256:"),
                    "video": _sha(video_bytes).removeprefix("sha256:"),
                    "evidence_json": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                    "publish_json": hashlib.sha256(publish_path.read_bytes()).hexdigest(),
                },
            }
        ],
        "cover_route_attestations": [
            {
                "candidate_id": candidate,
                "route_decision": generation["route_decision"],
                "final_cover_sha256": generation["final_cover_sha256"],
                "reference_sha256": generation["reference_sha256"],
                "method": generation["method"],
            }
        ],
    }
    (source / "review_manifest.json").write_bytes(_json_bytes(manifest))
    (source / "package_audit.json").write_text("{}\n", encoding="utf-8")
    comparison = tmp_path / "comparison.png"
    comparison.write_bytes(comparison_bytes)
    receipt = {
        "schema_version": HOST_ONLY_SCHEMA_VERSION,
        "authority": HOST_ONLY_AUTHORITY,
        "status": "PASS",
        "host_only_required": True,
        "final_cover_path": "/legacy/cover.png",
        "final_cover_sha256": _sha(cover_bytes),
        "reference_path": "/legacy/cover-ref.png",
        "reference_sha256": _sha(reference_bytes),
        "comparison_path": "/legacy/comparison.png",
        "comparison_sha256": _sha(comparison_bytes),
        "witness": {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_path": "/legacy/comparison.png",
            "image_sha256": _sha(comparison_bytes).removeprefix("sha256:"),
            "answer": "{}",
        },
    }
    witness = tmp_path / "witness.json"
    witness.write_bytes(_json_bytes(receipt))
    provider = tmp_path / "provider.json"
    provider.write_text("{}\n", encoding="utf-8")
    return {
        "candidate": candidate,
        "source": source,
        "destination": tmp_path / "run" / "package",
        "comparison": comparison,
        "witness": witness,
        "provider": provider,
        "cover_bytes": cover_bytes,
        "video_bytes": video_bytes,
    }


def _patch_current_contract(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    safety = {
        "schema_version": "lidousha-cover-host-only-visual-safety.v1",
        "status": "REQUIRED",
        "scene_kind": "talk",
        "cover_fallback_mode": "HOST_ONLY_GENERIC",
        "forbidden_visual_classes": ["NON_HOST_PERSON"],
        "requirement_basis": ["STORY_CONTRACT_HOST_ONLY_FALLBACK"],
    }
    monkeypatch.setattr(
        successor, "host_only_identity_route_blocker_detail", lambda _generation: "stale"
    )
    monkeypatch.setattr(
        successor, "source_composition_scene_kind", lambda _evidence: "talk"
    )
    monkeypatch.setattr(
        successor,
        "host_only_visual_safety_evidence",
        lambda _story, *, scene_kind: safety if scene_kind == "talk" else {},
    )
    monkeypatch.setattr(
        successor,
        "validate_final_host_identity_verification",
        lambda generation: generation["final_host_identity_verification"]["status"]
        == "PASS",
    )
    monkeypatch.setattr(
        successor,
        "validate_cover_route_decision",
        lambda generation, *, allow_legacy_v1: (
            allow_legacy_v1 is False
            and generation["route_decision"]["host_only_visual_safety_evidence"]
            == safety
        ),
    )
    return safety


def test_successor_rebinds_all_surfaces_and_keeps_media_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    safety = _patch_current_contract(monkeypatch)
    destination = fixture["destination"]
    assert isinstance(destination, Path)
    destination.parent.mkdir()
    source = fixture["source"]
    assert isinstance(source, Path)
    source_before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    def audit_package(root: Path) -> dict[str, object]:
        manifest = json.loads((root / "review_manifest.json").read_text())
        item = manifest["items"][0]
        evidence = json.loads((root / item["evidence_json"]).read_text())
        record = json.loads((root / item["record"]).read_text())
        publish = json.loads((root / item["publish_json"]).read_text())
        generations = [
            evidence["publish_staging"]["cover_generation"],
            record["publish_staging"]["cover_generation"],
            publish["cover_generation"],
        ]
        assert generations[0] == generations[1] == generations[2]
        generation = generations[0]
        assert generation["final_host_identity_verification"]["schema_version"] == (
            HOST_ONLY_SCHEMA_VERSION
        )
        assert generation["route_decision"]["host_only_visual_safety_evidence"] == safety
        assert manifest["cover_route_attestations"][0]["route_decision"] == generation[
            "route_decision"
        ]
        assert item["sha256"]["publish_json"] == hashlib.sha256(
            (root / item["publish_json"]).read_bytes()
        ).hexdigest()
        assert item["sha256"]["evidence_json"] == hashlib.sha256(
            (root / item["evidence_json"]).read_bytes()
        ).hexdigest()
        return {
            "schema_version": "lidousha-review-package-audit.v2",
            "root": str(root),
            "passed": True,
            "issues": [],
            "issue_count": 0,
            "blocking_issue_count": 0,
            "audited_inputs": [],
        }

    result = successor.build_host_only_v4_successor(
        source_package=source,
        destination_package=destination,
        candidate_id=str(fixture["candidate"]),
        witness_receipt=fixture["witness"],
        comparison_image=fixture["comparison"],
        provider_receipt=fixture["provider"],
        audit_package=audit_package,
    )

    assert result["status"] == "PASS"
    assert result["upload_allowed"] is False
    assert result["upload_calls"] == 0
    assert (destination / "cover.png").read_bytes() == fixture["cover_bytes"]
    assert (destination / "video.mp4").read_bytes() == fixture["video_bytes"]
    assert (destination / "package_audit.json").is_file()
    assert (destination.parent / "SUCCESSOR-BUILD.json").is_file()
    assert not (destination.parent / "FAILURE.json").exists()
    assert source_before == {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }


def test_failed_or_stale_witness_is_rejected_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _patch_current_contract(monkeypatch)
    destination = fixture["destination"]
    assert isinstance(destination, Path)
    destination.parent.mkdir()
    witness = fixture["witness"]
    assert isinstance(witness, Path)
    payload = json.loads(witness.read_text())
    payload["status"] = "FAIL"
    witness.write_bytes(_json_bytes(payload))

    with pytest.raises(successor.HostOnlyV4SuccessorError, match="failed, stale"):
        successor.build_host_only_v4_successor(
            source_package=fixture["source"],
            destination_package=destination,
            candidate_id=str(fixture["candidate"]),
            witness_receipt=witness,
            comparison_image=fixture["comparison"],
            provider_receipt=fixture["provider"],
            audit_package=lambda _root: {},
        )

    assert not destination.exists()
    assert not (destination.parent / "PREIMAGE-MANIFEST.json").exists()


def test_audit_failure_is_preserved_without_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _patch_current_contract(monkeypatch)
    destination = fixture["destination"]
    assert isinstance(destination, Path)
    destination.parent.mkdir()

    def failed_audit(root: Path) -> dict[str, object]:
        return {
            "schema_version": "lidousha-review-package-audit.v2",
            "root": str(root),
            "passed": False,
            "issues": [{"code": "TEST_BLOCK"}],
            "issue_count": 1,
            "blocking_issue_count": 1,
            "audited_inputs": [],
        }

    with pytest.raises(successor.HostOnlyV4SuccessorError, match="audit did not pass"):
        successor.build_host_only_v4_successor(
            source_package=fixture["source"],
            destination_package=destination,
            candidate_id=str(fixture["candidate"]),
            witness_receipt=fixture["witness"],
            comparison_image=fixture["comparison"],
            provider_receipt=fixture["provider"],
            audit_package=failed_audit,
        )

    assert destination.is_dir()
    assert (destination / "package_audit.json").is_file()
    assert (destination.parent / "FAILURE.json").is_file()
    assert not (destination.parent / "SUCCESSOR-BUILD.json").exists()


def test_destination_inside_source_is_rejected_before_sidecar_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _patch_current_contract(monkeypatch)
    source = fixture["source"]
    assert isinstance(source, Path)
    parent = source / "successor-run"
    parent.mkdir()
    destination = parent / "package"

    with pytest.raises(
        successor.HostOnlyV4SuccessorError,
        match="outside the immutable source package",
    ):
        successor.build_host_only_v4_successor(
            source_package=source,
            destination_package=destination,
            candidate_id=str(fixture["candidate"]),
            witness_receipt=fixture["witness"],
            comparison_image=fixture["comparison"],
            provider_receipt=fixture["provider"],
            audit_package=lambda _root: {},
        )

    assert not destination.exists()
    assert not (parent / "PREIMAGE-MANIFEST.json").exists()
    assert not (parent / "FAILURE.json").exists()


def test_symlinked_witness_receipt_is_rejected_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _patch_current_contract(monkeypatch)
    destination = fixture["destination"]
    assert isinstance(destination, Path)
    destination.parent.mkdir()
    witness = fixture["witness"]
    assert isinstance(witness, Path)
    linked_witness = tmp_path / "linked-witness.json"
    linked_witness.symlink_to(witness)

    with pytest.raises(
        successor.HostOnlyV4SuccessorError,
        match="witness receipt is unavailable or contains a symlink component",
    ):
        successor.build_host_only_v4_successor(
            source_package=fixture["source"],
            destination_package=destination,
            candidate_id=str(fixture["candidate"]),
            witness_receipt=linked_witness,
            comparison_image=fixture["comparison"],
            provider_receipt=fixture["provider"],
            audit_package=lambda _root: {},
        )

    assert not destination.exists()
    assert not (destination.parent / "PREIMAGE-MANIFEST.json").exists()



def test_comparison_path_replacement_after_freeze_cannot_change_packaged_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _patch_current_contract(monkeypatch)
    destination = fixture["destination"]
    comparison = fixture["comparison"]
    assert isinstance(destination, Path)
    assert isinstance(comparison, Path)
    destination.parent.mkdir()
    frozen_bytes = comparison.read_bytes()
    replacement_bytes = b"replacement after secure one-time read"
    original_copytree = successor.shutil.copytree

    replaced = False

    def copytree_after_replacement(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            comparison.write_bytes(replacement_bytes)
            replaced = True
        monkeypatch.setattr(successor.shutil, "copytree", original_copytree)
        return original_copytree(*args, **kwargs)

    monkeypatch.setattr(successor.shutil, "copytree", copytree_after_replacement)

    result = successor.build_host_only_v4_successor(
        source_package=fixture["source"],
        destination_package=destination,
        candidate_id=str(fixture["candidate"]),
        witness_receipt=fixture["witness"],
        comparison_image=comparison,
        provider_receipt=fixture["provider"],
        audit_package=lambda root: {
            "schema_version": "lidousha-review-package-audit.v2",
            "root": str(root),
            "passed": True,
            "issues": [],
            "issue_count": 0,
            "blocking_issue_count": 0,
            "audited_inputs": [],
        },
    )

    manifest = json.loads((destination / "review_manifest.json").read_text())
    binding = manifest["items"][0]["host_only_v4_binding"]
    packaged = destination / binding["comparison_path"]
    assert comparison.read_bytes() == replacement_bytes
    assert packaged.read_bytes() == frozen_bytes
    assert binding["comparison_sha256"] == _sha(frozen_bytes)
    assert result["external_inputs_frozen_once"] is True
