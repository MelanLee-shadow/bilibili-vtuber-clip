from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.verify_private_boundary_package import main as cli_main
from src.autoslice.boundary_semantic_review import build_boundary_search_scope
from src.autoslice.clip_context import ClipContextError
from src.autoslice.private_boundary_package import (
    PrivateBoundaryPackageError,
    verify_private_boundary_package,
    write_verification_receipt,
)
from src.autoslice.producer_boundary_owner_contract import (
    frozen_boundary_owner_contract_sha256,
)
from src.autoslice.structured_chat_payoff import assessment_sha256


def _canonical(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _owner_set_sha(owners: list[dict[str, object]]) -> str:
    normalized = []
    for owner in owners:
        normalized.append(
            {
                "owner_kind": owner["owner_kind"],
                "owner_id": owner["owner_id"],
                "required": True,
                "source_start_ms": None,
                "source_end_ms": None,
                "owner_scope_sha256": None,
                "local_windows": sorted(owner["local_windows"], key=lambda row: (row["start_ms"], row["end_ms"])),
            }
        )
    normalized.sort(key=lambda owner: (owner["owner_kind"], owner["owner_id"], json.dumps(owner["local_windows"], sort_keys=True)))
    return _canonical(normalized)


def _fixture(tmp_path: Path) -> dict[str, object]:
    candidate = "auto_test_100_200"
    date = "2026-01-02"
    package_root = tmp_path / "private-package"
    package_root.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    source = tmp_path / "source"
    source.mkdir()

    source_media = source / "source.mp4"
    source_media.write_bytes(b"synthetic-private-media")
    media = package_root / "clip.mp4"
    os.link(source_media, media)
    source_srt = source / "source.srt"
    source_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n你好呀\n\n"
        "2\n00:00:01,100 --> 00:00:02,000\n手术故事收束\n"
    )
    subtitle = package_root / "clip.srt"
    os.link(source_srt, subtitle)
    replay = evidence / "replay.json"
    replay.write_text('{"status":"PASS"}\n')
    e256 = evidence / "result.json"
    e256.write_text('{"status":"PRIVATE_PREVIEW"}\n')

    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": candidate,
        "recording_date": date,
        "pieces": [{
            "start_ms": 0,
            "end_ms": 2400,
            "recording_basename": "source.mp4",
            "source_media_sha256": _sha(source_media),
        }],
        "whole_clip_draft_srt": source_srt.read_text(),
        "whole_clip_draft_srt_sha256": _sha(source_srt),
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    context["context_sha256"] = _canonical(context)
    context_path = evidence / "clip-context.json"
    _write(context_path, context)
    context_binding = {
        "path": str(context_path), "sha256": _sha(context_path),
        "bytes": context_path.stat().st_size,
    }
    source_identity = {
        "schema_version": "private-boundary-source-identity.v1",
        "candidate_id": candidate,
        "recording_date": date,
        "clip_context_file_sha256": _sha(context_path),
        "context_sha256": context["context_sha256"],
        "pieces": context["pieces"],
    }
    source_identity["source_identity_sha256"] = _canonical(source_identity)

    manifest = {
        "schema_version": "surgery-private-boundary-package.v1",
        "status": "PRIVATE_BOUNDARY_PACKAGE_READY",
        "candidate_id": candidate,
        "recording_date": date,
        "large_media_copied": False,
        "source_identity_sha256": source_identity["source_identity_sha256"],
        "media_semantics": "DIAGNOSTIC_PREVIEW_PROMOTED_ONLY_AS_PRIVATE_BOUNDARY_CONSUMER_INPUT",
        "production_adopted": False,
        "public": False,
        "upload_allowed": False,
        "artifacts": {
            "media": {
                "path": str(media),
                "hardlinked_from": str(source_media),
                "sha256": _sha(media),
                "bytes": media.stat().st_size,
            },
            "subtitle": {
                "path": str(subtitle),
                "hardlinked_from": str(source_srt),
                "sha256": _sha(subtitle),
                "bytes": subtitle.stat().st_size,
                "cue_count": 2,
            },
        },
    }
    manifest_path = package_root / "package-manifest.json"
    _write(manifest_path, manifest)

    assessment = {
        "schema_version": "structured-chat-payoff-assessment.v1",
        "semantic_target_ms": 2100,
        "mode": "effective_story",
        "scope_basis": "effective_story_ms",
        "observed_ms": 2300,
        "effective_story_ms": None,
        "scope_ms": None,
        "observed_row_refs": [
            {
                "row_id": "test-row",
                "kind": "danmaku",
                "source_offset_ms": 2000,
                "matched_start_ms": 2200,
                "matched_end_ms": 2300,
            }
        ],
        "effective_row_refs": [],
        "excluded_row_refs": [
            {
                "row_id": "test-row",
                "kind": "danmaku",
                "source_offset_ms": 2000,
                "matched_start_ms": 2200,
                "matched_end_ms": 2300,
                "reason_code": "OUTSIDE_IMMUTABLE_STORY_SCOPE",
            }
        ],
    }
    assessment["assessment_sha256"] = assessment_sha256(assessment)
    owners = [
        {
            "owner_kind": "entity_repair",
            "owner_id": "entity_repair:test",
            "required": True,
            "local_windows": [{"start_ms": 1800, "end_ms": 1900}],
        }
    ]
    owner_scope = {
        "schema_version": "candidate-boundary-owner-scope.v1",
        "candidate_id": candidate,
        "story_start_ms": 0,
        "story_end_ms": 2100,
    }
    owner_scope["scope_sha256"] = _canonical(owner_scope)
    scope = build_boundary_search_scope(
        semantic_target_ms=2100,
        repair_cap_ms=60_000,
        structured_payoff_ms=None,
        required_owner_end_ms=1900,
        last_piece_start_ms=0,
        prior_piece_duration_ms=0,
        witness_reserve_ms=15_000,
        boundary_end_mode="semantic_lower_bound",
        semantic_tail_trim_cap_ms=15_000,
    )
    contract = {
        "schema_version": "frozen-boundary-owner-contract.v1",
        "status": "FROZEN",
        "owners": owners,
        "required_owner_count": 1,
        "owner_set_sha256": _owner_set_sha(owners),
        "owner_eligibility_scope": owner_scope,
        "structured_chat_payoff_assessment": assessment,
        "boundary_search_scope": scope,
        "story_start_ms": 0,
        "story_end_ms": 2100,
        "owner_discovery_end_ms": 2100,
        "boundary_review_target_ms": 2100,
    }
    contract["contract_sha256"] = frozen_boundary_owner_contract_sha256(contract)

    authority = {
        "schema_version": "surgery-boundary-package-authority.v1",
        "status": "PASS",
        "candidate_id": candidate,
        "recording_date": date,
        "source_identity": source_identity,
        "authority": "HASH_BOUND_FROZEN_OWNER_CONTRACT_AND_PRIVATE_PACKAGE_BYTES",
        "timeline": {
            "source_local_start_ms": 100,
            "formal_boundary_source_local_ms": 2100,
            "package_subtitle_end_ms": 2000,
            "package_media_end_ms": 2400,
            "delivery_tail_pad_ms": 400,
            "mapping": "source_local_ms = package_ms + 100",
        },
        "frozen_boundary_owner_contract": contract,
        "acceptance": {
            "all_required_owners_covered": True,
            "formal_boundary_matches_subtitle_end": True,
            "media_tail_pad_400ms": True,
            "out_of_story_chat_payoff_excluded": True,
            "required_owner_count": 1,
            "required_owner_end_ms": 1900,
            "provider_calls": 0,
            "media_mutations": 0,
            "subtitle_mutations": 0,
            "upload_allowed": False,
        },
        "artifacts": {
            "clip_context": context_binding,
            "boundary_contract_replay": {
                "path": str(replay),
                "sha256": _sha(replay),
                "bytes": replay.stat().st_size,
            },
            "e256_result": {
                "path": str(e256),
                "sha256": _sha(e256),
                "bytes": e256.stat().st_size,
            },
            "media": manifest["artifacts"]["media"],
            "subtitle": manifest["artifacts"]["subtitle"],
            "package_manifest": {
                "path": str(manifest_path),
                "sha256": _sha(manifest_path),
                "bytes": manifest_path.stat().st_size,
            },
        },
    }
    authority["authority_sha256"] = _canonical(authority)
    authority_path = package_root / "boundary-authority.json"
    _write(authority_path, authority)

    consumer = {
        "schema_version": "surgery-boundary-package-consumer.v1",
        "status": "PASS",
        "candidate_id": candidate,
        "authority_path": str(authority_path),
        "authority_file_sha256": _sha(authority_path),
        "authority_sha256": authority["authority_sha256"],
        "verified_source_identity_sha256": source_identity["source_identity_sha256"],
        "verified_clip_context_file_sha256": _sha(context_path),
        "verified_context_sha256": context["context_sha256"],
        "verified_frozen_contract_sha256": contract["contract_sha256"],
        "verified_owner_set_sha256": contract["owner_set_sha256"],
        "verified_media_sha256": _sha(media),
        "verified_subtitle_sha256": _sha(subtitle),
        "verified_package_manifest_sha256": _sha(manifest_path),
        "verified_timeline": authority["timeline"],
        "provider_calls": 0,
        "media_mutations": 0,
        "subtitle_mutations": 0,
        "upload_allowed": False,
    }
    consumer_path = package_root / "consumer-receipt.json"
    _write(consumer_path, consumer)

    result = {
        "schema_version": "surgery-formal-boundary-result.v1",
        "status": "PASS_FORMAL_TYPED_BOUNDARY_AUTHORITY_CONSUMED_BY_PRIVATE_PACKAGE",
        "candidate_id": candidate,
        "recording_date": date,
        "acceptance": {
            "typed_authority": True,
            "actual_private_package_consumer": True,
            "large_media_copied": False,
            "required_owner_count": 1,
            "required_owner_end_ms": 1900,
            "formal_boundary_source_local_ms": 2100,
            "package_subtitle_end_ms": 2000,
            "package_media_end_ms": 2400,
            "delivery_tail_pad_ms": 400,
            "structured_payoff_ms": None,
            "provider_calls": 0,
            "media_mutations": 0,
            "subtitle_mutations": 0,
        },
        "delivery_level": {
            "formal_boundary_authority": True,
            "private_package_consumer": True,
            "complete_publication_package": False,
            "managed_code_integration": False,
            "production_adopted": False,
            "public": False,
            "deployed": False,
        },
        "publication": {
            "upload_attempted": False,
            "public_release": False,
            "production_state_mutated": False,
        },
        "inputs": {
            "clip_context": context_binding,
            "contract_replay": {"path": str(replay), "sha256": _sha(replay)},
            "e256_result": {"path": str(e256), "sha256": _sha(e256)},
        },
        "outputs": {
            "boundary_authority": {
                "path": str(authority_path),
                "sha256": _sha(authority_path),
                "authority_sha256": authority["authority_sha256"],
            },
            "consumer_receipt": {"path": str(consumer_path), "sha256": _sha(consumer_path)},
            "package_manifest": {"path": str(manifest_path), "sha256": _sha(manifest_path)},
        },
    }
    result_path = package_root.parent / "RESULT.json"
    _write(result_path, result)

    return {
        "root": package_root,
        "context_path": context_path,
        "source_identity": source_identity,
        "result_path": result_path,
        "authority_path": authority_path,
        "consumer_path": consumer_path,
        "manifest_path": manifest_path,
        "media": media,
        "source_media": source_media,
        "probe": lambda _: {
            "duration_ms": 2400,
            "size_bytes": media.stat().st_size,
            "video_stream_count": 1,
            "audio_stream_count": 1,
            "streams": [],
        },
    }


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def _reseal_authority(fx: dict[str, object], authority: dict[str, object]) -> None:
    authority["authority_sha256"] = _canonical({k: v for k, v in authority.items() if k != "authority_sha256"})
    _write(fx["authority_path"], authority)
    consumer = _load(fx["consumer_path"])
    consumer["authority_file_sha256"] = _sha(fx["authority_path"])
    consumer["authority_sha256"] = authority["authority_sha256"]
    contract = authority["frozen_boundary_owner_contract"]
    consumer["verified_frozen_contract_sha256"] = contract["contract_sha256"]
    consumer["verified_owner_set_sha256"] = contract["owner_set_sha256"]
    consumer["verified_timeline"] = authority["timeline"]
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["boundary_authority"]["sha256"] = _sha(fx["authority_path"])
    result["outputs"]["boundary_authority"]["authority_sha256"] = authority["authority_sha256"]
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    _write(fx["result_path"], result)


def test_verifies_private_boundary_package(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    receipt = verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])
    assert receipt["status"] == "PASS_PRIVATE_BOUNDARY_PACKAGE_VERIFIED"
    assert receipt["source_identity"] == fx["source_identity"]
    assert receipt["source_identity_sha256"] == fx["source_identity"]["source_identity_sha256"]
    assert receipt["clip_context"]["sha256"] == _sha(fx["context_path"])
    assert receipt["verification_sha256"] == _canonical({
        k: v for k, v in receipt.items() if k != "verification_sha256"
    })
    assert receipt["boundary"]["delivery_tail_pad_ms"] == 400
    assert receipt["subtitle"]["cue_count"] == 2
    assert receipt["delivery_level"]["canonical_review_package_integrated"] is False
    assert receipt["delivery_level"]["upload_authorized"] is False


def test_rejects_authority_self_seal_drift(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    authority = _load(fx["authority_path"])
    authority["timeline"]["formal_boundary_source_local_ms"] += 1
    _write(fx["authority_path"], authority)
    result = _load(fx["result_path"])
    result["outputs"]["boundary_authority"]["sha256"] = _sha(fx["authority_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="AUTHORITY_SELF_SEAL_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_consumer_manifest_binding_drift(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    consumer = _load(fx["consumer_path"])
    consumer["verified_package_manifest_sha256"] = "sha256:" + "0" * 64
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="CONSUMER_BINDING_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_candidate_mismatch(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    consumer = _load(fx["consumer_path"])
    consumer["candidate_id"] = "auto_other"
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="CANDIDATE_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_owner_beyond_formal_boundary(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    authority = _load(fx["authority_path"])
    contract = authority["frozen_boundary_owner_contract"]
    contract["owners"][0]["local_windows"] = [{"start_ms": 2050, "end_ms": 2200}]
    contract["owner_set_sha256"] = _owner_set_sha(contract["owners"])
    contract["boundary_search_scope"] = build_boundary_search_scope(
        semantic_target_ms=2100,
        repair_cap_ms=60_000,
        required_owner_end_ms=2200,
        last_piece_start_ms=0,
        prior_piece_duration_ms=0,
        witness_reserve_ms=15_000,
        boundary_end_mode="semantic_lower_bound",
        semantic_tail_trim_cap_ms=15_000,
    )
    contract["contract_sha256"] = frozen_boundary_owner_contract_sha256(contract)
    authority["acceptance"]["required_owner_end_ms"] = 2200
    _reseal_authority(fx, authority)
    with pytest.raises(PrivateBoundaryPackageError, match="OWNER_EXCEEDS_FORMAL_END"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_invalid_search_scope(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    authority = _load(fx["authority_path"])
    contract = authority["frozen_boundary_owner_contract"]
    contract["boundary_search_scope"]["max_recommended_end_ms"] += 1
    contract["contract_sha256"] = frozen_boundary_owner_contract_sha256(contract)
    _reseal_authority(fx, authority)
    with pytest.raises(PrivateBoundaryPackageError, match="SEARCH_SCOPE_INVALID"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_effective_out_of_story_payoff(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    authority = _load(fx["authority_path"])
    contract = authority["frozen_boundary_owner_contract"]
    assessment = contract["structured_chat_payoff_assessment"]
    row = {k: v for k, v in assessment["excluded_row_refs"][0].items() if k != "reason_code"}
    assessment["effective_row_refs"] = [row]
    assessment["effective_story_ms"] = 2300
    assessment["scope_ms"] = 2300
    assessment["excluded_row_refs"] = []
    assessment["assessment_sha256"] = assessment_sha256(assessment)
    contract["boundary_search_scope"] = build_boundary_search_scope(
        semantic_target_ms=2100,
        repair_cap_ms=60_000,
        structured_payoff_ms=2300,
        required_owner_end_ms=1900,
        last_piece_start_ms=0,
        prior_piece_duration_ms=0,
        witness_reserve_ms=15_000,
        boundary_end_mode="semantic_lower_bound",
        semantic_tail_trim_cap_ms=15_000,
    )
    contract["contract_sha256"] = frozen_boundary_owner_contract_sha256(contract)
    _reseal_authority(fx, authority)
    with pytest.raises(PrivateBoundaryPackageError, match="PAYOFF_EXCLUSION_INVALID"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_tail_pad_drift(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    authority = _load(fx["authority_path"])
    authority["timeline"]["delivery_tail_pad_ms"] = 399
    authority["timeline"]["package_media_end_ms"] = 2399
    _reseal_authority(fx, authority)
    consumer = _load(fx["consumer_path"])
    consumer["verified_timeline"] = authority["timeline"]
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="TAIL_PAD_INVALID"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=lambda _: {"duration_ms": 2399, "size_bytes": fx["media"].stat().st_size, "video_stream_count": 1, "audio_stream_count": 1})


def test_rejects_srt_end_drift(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    subtitle = fx["root"] / "clip.srt"
    subtitle.write_text(subtitle.read_text().replace("00:00:02,000", "00:00:01,900"))
    manifest = _load(fx["manifest_path"])
    manifest["artifacts"]["subtitle"]["sha256"] = _sha(subtitle)
    manifest["artifacts"]["subtitle"]["bytes"] = subtitle.stat().st_size
    _write(fx["manifest_path"], manifest)
    authority = _load(fx["authority_path"])
    authority["artifacts"]["subtitle"] = manifest["artifacts"]["subtitle"]
    authority["artifacts"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    authority["artifacts"]["package_manifest"]["bytes"] = fx["manifest_path"].stat().st_size
    _reseal_authority(fx, authority)
    consumer = _load(fx["consumer_path"])
    consumer["verified_subtitle_sha256"] = _sha(subtitle)
    consumer["verified_package_manifest_sha256"] = _sha(fx["manifest_path"])
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    result["outputs"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="SRT_END_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_missing_audio_stream(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    def probe(_: Path) -> dict[str, int]:
        return {
            "duration_ms": 2400,
            "size_bytes": fx["media"].stat().st_size,
            "video_stream_count": 1,
            "audio_stream_count": 0,
        }

    with pytest.raises(PrivateBoundaryPackageError, match="AUDIO_STREAM_MISSING"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=probe)


def test_rejects_symlinked_artifact(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    subtitle = fx["root"] / "clip.srt"
    subtitle.unlink()
    subtitle.symlink_to(fx["source_media"])
    with pytest.raises(PrivateBoundaryPackageError, match="ARTIFACT_UNSAFE"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_public_or_upload_flags(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    manifest = _load(fx["manifest_path"])
    manifest["public"] = True
    _write(fx["manifest_path"], manifest)
    authority = _load(fx["authority_path"])
    authority["artifacts"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    authority["artifacts"]["package_manifest"]["bytes"] = fx["manifest_path"].stat().st_size
    _reseal_authority(fx, authority)
    consumer = _load(fx["consumer_path"])
    consumer["verified_package_manifest_sha256"] = _sha(fx["manifest_path"])
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    result["outputs"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="RELEASE_AUTHORITY_FORBIDDEN"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_hardlink_provenance_mismatch(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(fx["media"].read_bytes())
    manifest = _load(fx["manifest_path"])
    manifest["artifacts"]["media"]["hardlinked_from"] = str(replacement)
    _write(fx["manifest_path"], manifest)
    authority = _load(fx["authority_path"])
    authority["artifacts"]["media"] = manifest["artifacts"]["media"]
    authority["artifacts"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    authority["artifacts"]["package_manifest"]["bytes"] = fx["manifest_path"].stat().st_size
    _reseal_authority(fx, authority)
    consumer = _load(fx["consumer_path"])
    consumer["verified_package_manifest_sha256"] = _sha(fx["manifest_path"])
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    result["outputs"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="HARDLINK_PROVENANCE_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_writes_create_only_receipt(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    receipt = verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])
    output = tmp_path / "verification.json"
    write_verification_receipt(output, receipt)
    assert json.loads(output.read_text())["verification_sha256"] == receipt["verification_sha256"]
    with pytest.raises(PrivateBoundaryPackageError, match="RECEIPT_CREATE_FAILED"):
        write_verification_receipt(output, receipt)


def test_rejects_package_path_traversal(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    manifest = _load(fx["manifest_path"])
    manifest["artifacts"]["media"]["path"] = "../source/source.mp4"
    _write(fx["manifest_path"], manifest)
    authority = _load(fx["authority_path"])
    authority["artifacts"]["media"] = manifest["artifacts"]["media"]
    authority["artifacts"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    authority["artifacts"]["package_manifest"]["bytes"] = fx["manifest_path"].stat().st_size
    _reseal_authority(fx, authority)
    consumer = _load(fx["consumer_path"])
    consumer["verified_package_manifest_sha256"] = _sha(fx["manifest_path"])
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    result["outputs"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    _write(fx["result_path"], result)
    with pytest.raises(PrivateBoundaryPackageError, match="ARTIFACT_UNSAFE"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_missing_video_stream(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)

    def probe(_: Path) -> dict[str, int]:
        return {
            "duration_ms": 2400,
            "size_bytes": fx["media"].stat().st_size,
            "video_stream_count": 0,
            "audio_stream_count": 1,
        }

    with pytest.raises(PrivateBoundaryPackageError, match="VIDEO_STREAM_MISSING"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=probe)


def _refresh_package_bindings(fx: dict[str, object]) -> None:
    """Reseal outer documents while preserving the binding under test."""
    authority = _load(fx["authority_path"])
    authority["artifacts"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    authority["artifacts"]["package_manifest"]["bytes"] = fx["manifest_path"].stat().st_size
    _reseal_authority(fx, authority)
    consumer = _load(fx["consumer_path"])
    consumer["verified_package_manifest_sha256"] = _sha(fx["manifest_path"])
    _write(fx["consumer_path"], consumer)
    result = _load(fx["result_path"])
    result["outputs"]["package_manifest"]["sha256"] = _sha(fx["manifest_path"])
    result["outputs"]["consumer_receipt"]["sha256"] = _sha(fx["consumer_path"])
    _write(fx["result_path"], result)


@pytest.mark.parametrize("field,value,error", [
    ("candidate_id", "auto_other", "CLIP_CONTEXT_CANDIDATE_MISMATCH"),
    ("recording_date", "2026-01-03", "CLIP_CONTEXT_DATE_MISMATCH"),
])
def test_rejects_context_scope_mismatch(tmp_path: Path, field: str, value: str, error: str) -> None:
    fx = _fixture(tmp_path)
    context = _load(fx["context_path"])
    context[field] = value
    context["context_sha256"] = _canonical({k: v for k, v in context.items() if k != "context_sha256"})
    _write(fx["context_path"], context)
    with pytest.raises(PrivateBoundaryPackageError, match=error) as caught:
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])
    assert caught.value.code == "PRIVATE_BOUNDARY_CLIP_CONTEXT_INVALID"
    assert isinstance(caught.value.__cause__, ClipContextError)


def test_rejects_tampered_context(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    context = _load(fx["context_path"])
    context["pieces"][0]["source_media_sha256"] = "sha256:" + "0" * 64
    _write(fx["context_path"], context)
    with pytest.raises(PrivateBoundaryPackageError, match="CLIP_CONTEXT_DIGEST_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_context_file_byte_drift(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    with fx["context_path"].open("a") as stream:
        stream.write("\n")
    with pytest.raises(PrivateBoundaryPackageError, match="CLIP_CONTEXT_BINDING_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


@pytest.mark.parametrize("document,keys,error", [
    ("authority_path", ("artifacts", "clip_context"), "OBJECT_INVALID"),
    ("result_path", ("inputs", "clip_context"), "OBJECT_INVALID"),
    ("authority_path", ("artifacts", "clip_context", "path"), "CLIP_CONTEXT_BINDING_MISMATCH"),
    ("result_path", ("inputs", "clip_context", "path"), "CLIP_CONTEXT_BINDING_MISMATCH"),
    ("authority_path", ("artifacts", "clip_context", "sha256"), "CLIP_CONTEXT_BINDING_MISMATCH"),
    ("result_path", ("inputs", "clip_context", "sha256"), "CLIP_CONTEXT_BINDING_MISMATCH"),
    ("authority_path", ("source_identity",), "SOURCE_IDENTITY_MISMATCH"),
    ("authority_path", ("source_identity", "candidate_id"), "SOURCE_IDENTITY_MISMATCH"),
    ("authority_path", ("source_identity", "recording_date"), "SOURCE_IDENTITY_MISMATCH"),
    ("authority_path", ("source_identity", "clip_context_file_sha256"), "SOURCE_IDENTITY_MISMATCH"),
    ("authority_path", ("source_identity", "context_sha256"), "SOURCE_IDENTITY_MISMATCH"),
    ("authority_path", ("source_identity", "pieces"), "SOURCE_IDENTITY_MISMATCH"),
    ("authority_path", ("source_identity", "source_identity_sha256"), "SOURCE_IDENTITY_MISMATCH"),
    ("manifest_path", ("source_identity_sha256",), "SOURCE_IDENTITY_MISMATCH"),
    ("consumer_path", ("verified_source_identity_sha256",), "CONSUMER_BINDING_MISMATCH"),
    ("consumer_path", ("verified_clip_context_file_sha256",), "CONSUMER_BINDING_MISMATCH"),
    ("consumer_path", ("verified_context_sha256",), "CONSUMER_BINDING_MISMATCH"),
])
@pytest.mark.parametrize("mutation", ["missing", "drift"])
def test_requires_explicit_source_bindings(
    tmp_path: Path, document: str, keys: tuple[str, ...], error: str, mutation: str,
) -> None:
    fx = _fixture(tmp_path)
    value = _load(fx[document])
    target = value
    for key in keys[:-1]:
        target = target[key]
    if mutation == "missing":
        target.pop(keys[-1])
    elif keys[-1] == "path":
        alternate = tmp_path / "other-context.json"
        alternate.write_bytes(fx["context_path"].read_bytes())
        target[keys[-1]] = str(alternate)
    else:
        target[keys[-1]] = "sha256:" + "0" * 64
    _write(fx[document], value)
    _refresh_package_bindings(fx)
    with pytest.raises(PrivateBoundaryPackageError, match=error):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_rejects_legacy_package_without_source_bindings(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    for document, keys in (
        ("authority_path", ("source_identity",)),
        ("authority_path", ("artifacts", "clip_context")),
        ("result_path", ("inputs", "clip_context")),
        ("manifest_path", ("source_identity_sha256",)),
        ("consumer_path", ("verified_source_identity_sha256",)),
        ("consumer_path", ("verified_clip_context_file_sha256",)),
        ("consumer_path", ("verified_context_sha256",)),
    ):
        value = _load(fx[document])
        target = value
        for key in keys[:-1]:
            target = target[key]
        target.pop(keys[-1])
        _write(fx[document], value)
    _refresh_package_bindings(fx)
    with pytest.raises(PrivateBoundaryPackageError, match="OBJECT_INVALID"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


@pytest.mark.parametrize("surface", ["input", "authority", "result"])
def test_rejects_symlinked_clip_context(tmp_path: Path, surface: str) -> None:
    fx = _fixture(tmp_path)
    link = tmp_path / "context-link.json"
    link.symlink_to(fx["context_path"])
    supplied = fx["context_path"]
    if surface == "input":
        supplied = link
    else:
        document = f"{surface}_path"
        value = _load(fx[document])
        value["artifacts" if surface == "authority" else "inputs"]["clip_context"]["path"] = str(link)
        _write(fx[document], value)
        _refresh_package_bindings(fx)
    with pytest.raises(PrivateBoundaryPackageError, match="SYMLINK_FORBIDDEN"):
        verify_private_boundary_package(fx["root"], clip_context_path=supplied, probe_media=fx["probe"])


def test_api_requires_clip_context(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="clip_context_path"):
        verify_private_boundary_package(tmp_path)


def test_cli_requires_clip_context(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli_main(["--package-root", str(tmp_path)])
    assert caught.value.code == 2
    assert "--clip-context" in capsys.readouterr().err


def test_cli_verifies_context_and_emits_typed_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fx = _fixture(tmp_path)
    monkeypatch.setattr("src.autoslice.private_boundary_package._ffprobe_media", fx["probe"])
    output = tmp_path / "receipt.json"
    args = ["--package-root", str(fx["root"]), "--clip-context", str(fx["context_path"])]
    assert cli_main([*args, "--output", str(output)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["source_identity"] == fx["source_identity"]
    assert _load(output) == receipt
    context = _load(fx["context_path"])
    context["candidate_id"] = "tampered"
    _write(fx["context_path"], context)
    blocked_output = tmp_path / "blocked.json"
    assert cli_main([*args, "--output", str(blocked_output)]) == 2
    block = json.loads(capsys.readouterr().out)
    assert block["status"] == "BLOCK"
    assert block["code"] == "PRIVATE_BOUNDARY_CLIP_CONTEXT_INVALID"
    assert "CLIP_CONTEXT_DIGEST_MISMATCH" in block["detail"]
    assert not blocked_output.exists()


@pytest.mark.parametrize("field,value", [
    ("start_ms", 1),
    ("recording_basename", "different.mp4"),
    ("source_media_sha256", "sha256:" + "0" * 64),
])
def test_resealed_context_cannot_reuse_old_source_identity(
    tmp_path: Path, field: str, value: object,
) -> None:
    fx = _fixture(tmp_path)
    context = _load(fx["context_path"])
    context["pieces"][0][field] = value
    context["context_sha256"] = _canonical({k: v for k, v in context.items() if k != "context_sha256"})
    _write(fx["context_path"], context)
    for document, key in (("authority_path", "artifacts"), ("result_path", "inputs")):
        payload = _load(fx[document])
        payload[key]["clip_context"]["sha256"] = _sha(fx["context_path"])
        payload[key]["clip_context"]["bytes"] = fx["context_path"].stat().st_size
        _write(fx[document], payload)
    _refresh_package_bindings(fx)
    with pytest.raises(PrivateBoundaryPackageError, match="SOURCE_IDENTITY_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


@pytest.mark.parametrize("field,value,error", [
    ("start_ms", True, "INTEGER_INVALID"),
    ("start_ms", -1, "INTEGER_INVALID"),
    ("end_ms", 0, "INTEGER_INVALID"),
    ("recording_basename", "", "SOURCE_PIECE_INVALID"),
    ("recording_basename", "../source.mp4", "SOURCE_PIECE_INVALID"),
    ("source_media_sha256", "invalid", "CLIP_CONTEXT_SOURCE_BINDING_INVALID"),
])
def test_rejects_invalid_context_piece(
    tmp_path: Path, field: str, value: object, error: str,
) -> None:
    fx = _fixture(tmp_path)
    context = _load(fx["context_path"])
    context["pieces"][0][field] = value
    context["context_sha256"] = _canonical({k: v for k, v in context.items() if k != "context_sha256"})
    _write(fx["context_path"], context)
    with pytest.raises(PrivateBoundaryPackageError, match=error):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])


def test_source_identity_requires_canonical_type_match(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    authority = _load(fx["authority_path"])
    authority["source_identity"]["pieces"][0]["start_ms"] = 0.0
    _reseal_authority(fx, authority)
    with pytest.raises(PrivateBoundaryPackageError, match="SOURCE_IDENTITY_MISMATCH"):
        verify_private_boundary_package(fx["root"], clip_context_path=fx["context_path"], probe_media=fx["probe"])
