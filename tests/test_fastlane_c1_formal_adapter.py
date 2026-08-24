from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import audit_lidousha_review_package as package_audit
from scripts.build_fastlane_c1_formal_private_package import build_parser
from scripts.build_fastlane_c1_private_successor import (
    parse_srt,
    project_cues,
    validate_projection,
)
from scripts.build_fastlane_c1_root_review_evidence import (
    C1RootReviewEvidenceError,
    build_parser as evidence_build_parser,
    build_review_plan,
)
from src.autoslice.fastlane_c1_formal_adapter import (
    CID,
    FORMAL_MANIFEST_SCHEMA,
    ROOT_TECHNICAL_STATUS,
    ROOT,
    TITLE,
    FastlaneC1FormalAdapterError,
    _build_manifest,
    _public_identity_binding,
    _validate_formal_authority,
    _validate_manifest_shape,
    _validate_ruling_inputs,
    _verify_input,
    load_formal_authority,
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _reseal(value: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(value)
    unsigned = dict(result)
    unsigned.pop("authority_sha256", None)
    result["authority_sha256"] = "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    return result


def _source_srt() -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},500\ncue-{index}"
        for index in range(1, 37)
    ) + "\n"


def test_formal_authority_is_closed_and_has_no_markdown_line_binding() -> None:
    authority = load_formal_authority()
    assert _validate_formal_authority(authority, repo_root=ROOT) == authority
    rendered = json.dumps(authority, ensure_ascii=False)
    assert "line 38" not in rendered
    assert authority["operator_ruling"]["candidate_scope_ordinal"] == 1
    assert authority["operator_ruling"]["ivan_rereview_required"] is False
    assert authority["operator_ruling"]["raw_line947_content_sha256"] == (
        "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b"
    )


def test_formal_authority_is_a_fixed_reviewed_baseline_recovery_adapter() -> None:
    authority = load_formal_authority()
    assert TITLE == "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"
    assert "上头" not in TITLE
    grid = authority["successor_grid"]
    assert grid["source_cue_count"] == 36
    assert grid["retained_live_cue_count"] == 25
    assert grid["watched_video_drop_count"] == 11
    assert grid["inserted_live_cue_count"] == 1
    assert grid["release_cue_count"] == 26
    assert authority["same_bv_delivery_constraint"] == {
        "requires_same_bv_repair": True,
        "forbid_new_bv": True,
        "preserve_public_identity_and_metadata": True,
    }


def test_formal_manifest_rejects_the_stale_ivan_rereview_status() -> None:
    authority = load_formal_authority()
    manifest = _build_manifest(authority=authority)
    assert manifest["status"] == ROOT_TECHNICAL_STATUS
    manifest["status"] = "PENDING_ROOT_AND_IVAN_REVIEW"
    with pytest.raises(FastlaneC1FormalAdapterError, match="C1_FORMAL_MANIFEST_BINDING_DRIFT"):
        _validate_manifest_shape(manifest, authority)


def test_root_review_evidence_maps_public_points_through_the_z1_offset() -> None:
    plan = build_review_plan(intro_offset_ms=5_749)
    assert len(plan) == 6
    assert plan[0]["public_anchor_ms"] == 46_000
    assert plan[0]["source_anchor_ms"] == 40_251
    assert plan[3]["proof_frame_source_ms"] == 91_251
    assert plan[5]["public_anchor_ms"] == 146_000
    assert plan[5]["proof_frame_public_ms"] == 147_800
    assert plan[5]["proof_frame_source_ms"] == 142_051
    with pytest.raises(C1RootReviewEvidenceError, match="C1_REVIEW_INTRO_OFFSET_DRIFT"):
        build_review_plan(intro_offset_ms=0)


def test_root_review_evidence_cli_has_no_free_candidate_or_text_flags() -> None:
    option_names = set(evidence_build_parser()._option_string_actions)
    assert option_names == {"-h", "--help", "--package", "--out"}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update(candidate_id="other_candidate"), "C1_FORMAL_AUTHORITY_BINDING_INVALID"),
        (
            lambda value: value["correction_authority"].update(sha256="sha256:" + "0" * 64),
            "C1_CORRECTION_AUTHORITY_HASH_DRIFT",
        ),
        (
            lambda value: value["successor_grid"].update(release_cue_count=27),
            "C1_SUCCESSOR_GRID_AUTHORITY_INVALID",
        ),
        (
            lambda value: value.update(title="【李豆沙】薇欧拉对阿拉蕾上头了！"),
            "C1_FORMAL_AUTHORITY_BINDING_INVALID",
        ),
    ],
)
def test_formal_authority_canaries_fail_closed(mutate, code: str) -> None:
    authority = copy.deepcopy(load_formal_authority())
    mutate(authority)
    authority = _reseal(authority)
    with pytest.raises(FastlaneC1FormalAdapterError, match=code):
        _validate_formal_authority(authority, repo_root=ROOT)


def test_cue_scope_canary_rejects_dropping_a_live_cue() -> None:
    authority_path = ROOT / "assets/lidousha/fastlane_c1_private/auto_173005_934_1166.subtitle-correction.v1.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    authority["mutations"]["drop"] = [1]
    source = parse_srt(_source_srt())
    projected = project_cues(source, authority)
    with pytest.raises(ValueError, match="may drop only perceptually classified watched-video cues"):
        validate_projection(source, projected, authority)


def test_intro_hash_canary_rejects_drift(tmp_path: Path) -> None:
    intro = tmp_path / "intro.mp4"
    intro.write_bytes(b"approved intro")
    expected = "sha256:" + hashlib.sha256(intro.read_bytes()).hexdigest()
    _verify_input(intro, expected, label="BRANDING_INTRO")
    intro.write_bytes(b"drifted intro")
    with pytest.raises(FastlaneC1FormalAdapterError, match="C1_BRANDING_INTRO_HASH_DRIFT"):
        _verify_input(intro, expected, label="BRANDING_INTRO")


def test_public_identity_canary_rejects_identity_drift(tmp_path: Path) -> None:
    snapshot = {
        "schema_version": "c1-same-bv-public-binding.v1",
        "response_sha256": "response-bytes-hash",
        "constraints": {
            "new_bv_forbidden": True,
            "preserve_all_live_metadata_unless_ivan_changed": True,
            "same_bv_required": True,
        },
        "identity": {
            "bvid": "BV1os8q61Eya",
            "aid": 117132650155234,
            "cid": 41126267272,
            "title": TITLE,
            "desc": "preserve me",
        },
    }
    path = tmp_path / "public.json"
    path.write_bytes(_canonical(snapshot))
    authority = copy.deepcopy(load_formal_authority())
    authority["public_identity"] = {
        "snapshot_schema_version": snapshot["schema_version"],
        "snapshot_sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "response_sha256": snapshot["response_sha256"],
        "bvid": snapshot["identity"]["bvid"],
        "aid": snapshot["identity"]["aid"],
        "cid": snapshot["identity"]["cid"],
    }
    _public_identity_binding(path, authority)
    snapshot["identity"]["cid"] = 999
    path.write_bytes(_canonical(snapshot))
    authority["public_identity"]["snapshot_sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(FastlaneC1FormalAdapterError, match="C1_PUBLIC_IDENTITY_CONTENT_DRIFT"):
        _public_identity_binding(path, authority)


def test_ruling_scope_reseals_document_but_binds_raw_line_and_candidate_scope(tmp_path: Path) -> None:
    authority = copy.deepcopy(load_formal_authority())
    raw_fragments = authority["operator_ruling"]["raw_payload_required_fragments"]
    document_fragments = authority["operator_ruling"]["ruling_document_required_fragments"]
    raw = _canonical(
        {
            "type": "user",
            "uuid": "555195ed-ec18-418d-a311-558f7e54291f",
            "timestamp": "2026-08-19T00:08:52.249Z",
            "message": {"content": " ".join(raw_fragments)},
        }
    )
    authority["operator_ruling"]["raw_line947_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    authority["operator_ruling"]["raw_line947_content_sha256"] = (
        "sha256:" + hashlib.sha256(" ".join(raw_fragments).encode("utf-8")).hexdigest()
    )
    ruling_document = tmp_path / "ruling.md"
    ruling_document.write_text("\n".join(document_fragments), encoding="utf-8")
    seal, _ = _validate_ruling_inputs(
        raw_line=raw,
        ruling_document=ruling_document,
        authority=authority,
    )
    assert seal["candidate_id"] == CID
    assert seal["ivan_rereview_required"] is False
    ruling_document.write_text("candidate scope only", encoding="utf-8")
    with pytest.raises(FastlaneC1FormalAdapterError, match="C1_RULING_DOCUMENT_SCOPE_DRIFT"):
        _validate_ruling_inputs(raw_line=raw, ruling_document=ruling_document, authority=authority)


def test_ruling_scope_rejects_resealed_raw_line_with_content_hash_drift(tmp_path: Path) -> None:
    authority = copy.deepcopy(load_formal_authority())
    raw_fragments = authority["operator_ruling"]["raw_payload_required_fragments"]
    document_fragments = authority["operator_ruling"]["ruling_document_required_fragments"]
    content = " ".join(raw_fragments)
    raw = _canonical(
        {
            "type": "user",
            "uuid": "555195ed-ec18-418d-a311-558f7e54291f",
            "timestamp": "2026-08-19T00:08:52.249Z",
            "message": {"content": content},
        }
    )
    authority["operator_ruling"]["raw_line947_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    authority["operator_ruling"]["raw_line947_content_sha256"] = "sha256:" + "0" * 64
    ruling_document = tmp_path / "ruling.md"
    ruling_document.write_text("\n".join(document_fragments), encoding="utf-8")
    with pytest.raises(FastlaneC1FormalAdapterError, match="C1_CLAUDE_LINE947_CONTENT_HASH_DRIFT"):
        _validate_ruling_inputs(raw_line=raw, ruling_document=ruling_document, authority=authority)


def test_formal_cli_does_not_offer_free_candidate_or_text_flags() -> None:
    option_names = set(build_parser()._option_string_actions)
    assert "--candidate" not in option_names
    assert "--text" not in option_names
    assert "--upload" not in option_names


def test_current_package_audit_rejects_an_arbitrary_c1_manifest(tmp_path: Path) -> None:
    (tmp_path / "review_manifest.json").write_text(
        json.dumps({"schema_version": FORMAL_MANIFEST_SCHEMA, "candidate_id": "other"}),
        encoding="utf-8",
    )
    result = package_audit.audit_package(tmp_path)
    assert result["passed"] is False
    assert result["issues"][0]["code"] == "C1_FORMAL_MANIFEST_FIELDS_INVALID"
