from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_title_cover_joint_qc import build_joint_qc_receipt
from src.autoslice import qixi_screenshot_direct_cover_repair as repair


def _sha(value: object) -> str:
    return repair.canonical_sha256(value)


def _authority(generation: dict[str, object], cover: bytes, qc: bytes) -> dict[str, object]:
    subtrees = {key: _sha(generation.get(key)) for key in (
        "route_decision", "source_composition_receipt", "source_composition_verification",
        "screenshot_frame", "screenshot_graphic_poster", "cover_treatment",
        "final_host_identity_verification", "rendered_text_pixels", "art_direction",
        "font_selection", "overlay_position", "rendered_lines", "thumbnail_text_gate",
    )}
    result: dict[str, object] = {
        "schema_version": "qixi-screenshot-direct-cover-repair-authority.v1",
        "candidate_id": repair.CANDIDATE_ID, "recording_date": repair.RECORDING_DATE,
        "runtime_root": "/runtime", "upload_enabled": False,
        "title": {"value": "title", "sha256": "sha256:" + hashlib.sha256(b"title").hexdigest()},
        "punch_candidates": list(repair.PUNCH_CANDIDATES),
        "terminal_refresh_authority": {"relative_path": "assets/x.json", "authority_sha256": "sha256:" + "1" * 64},
        "legacy_cover": {
            "final_cover": {"path": "/runtime/cover.png", "sha256": "sha256:" + hashlib.sha256(cover).hexdigest(), "bytes": len(cover)},
            "failed_joint_qc": {"path": "/runtime/fail.json", "sha256": "sha256:" + hashlib.sha256(qc).hexdigest(), "bytes": len(qc)},
            "generation_sha256": _sha(generation), "subtree_sha256": subtrees,
        },
        "immutable_media": {"srt": "sha256:" + "2" * 64, "ass": "sha256:" + "3" * 64, "burn": "sha256:" + "4" * 64},
        "title_projection_sha256": "sha256:" + "5" * 64,
        "allowed_mutations": {
            role: {"allowed": [], "required": []}
            for role in ("record", "delivery_record", "publish", "state")
        },
    }
    result["authority_sha256"] = _sha(result)
    return result


def _generation() -> dict[str, object]:
    return {
        "method": "screenshot_direct", "image_generation_used": False,
        "route_decision": {"selected_treatment": "screenshot_direct"},
        "source_composition_receipt": {"sha256": "sha256:x"},
        "source_composition_verification": {"status": "PASS"},
        "screenshot_frame": {"frame_ms": 1}, "screenshot_graphic_poster": {"path": "/p"},
        "cover_treatment": {"treatment": "screenshot_direct"},
        "final_host_identity_verification": {"status": "PASS"},
        "rendered_text_pixels": {"status": "PASS"}, "art_direction": {"layout": "banner"},
        "font_selection": {"font": "ZCOOL"}, "overlay_position": {"x": 1},
        "rendered_lines": ["有女友感吗？"], "thumbnail_text_gate": {"status": "PASS"},
    }


def test_legacy_screenshot_evidence_is_subtree_bound() -> None:
    generation, cover, qc = _generation(), b"old-cover", b"old-failed-qc"
    authority = _authority(generation, cover, qc)
    repair.validate_legacy_cover_inputs(authority, generation=generation, old_cover=cover, old_qc=qc)
    changed = copy.deepcopy(generation)
    changed["rendered_lines"] = ["宿敌有点亲密"]
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="SUBTREE_DRIFT"):
        repair.validate_legacy_cover_inputs(authority, generation=changed, old_cover=cover, old_qc=qc)


def test_repaired_punch_is_only_from_fixed_candidate_pool() -> None:
    assert repair.require_repaired_punch(["有女友感吗？", "宿敌有点亲密"]) == repair.PUNCH_CANDIDATES
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="OUTSIDE_POOL"):
        repair.require_repaired_punch(["任意新梗"])


def test_joint_qc_callable_binds_staged_bytes_to_intended_logical_path(tmp_path) -> None:
    cover = tmp_path / "private-stage.png"
    cover.write_bytes(b"private-stage-cover")

    def probe(path, _prompt):
        assert path == cover
        return {
            "status": "OBSERVED",
            "answer": '{"lidousha_primary":true,"thumbnail_readable":true,'
            '"single_clear_hook":true,"text_overcrowded":false,'
            '"title_cover_aligned":true,"physical_text_line_count":2,'
            '"unrelated_or_misleading_elements":[],"reason":"符合标题","pass":true}',
        }

    receipt = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】小李有女友感吗？宿敌是否有点亲密了",
        candidate_id=repair.CANDIDATE_ID,
        image_probe=probe,
        logical_cover_path="/runtime/intended.cover.png",
    )
    assert receipt["pass"] is True
    assert receipt["cover_path"] == "/runtime/intended.cover.png"
    assert receipt["witness"]["image_path"] == "/runtime/intended.cover.png"
    assert receipt["cover_sha256"] == "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()


def test_preflight_is_authority_bound_and_contains_no_provider_replay() -> None:
    generation, cover, qc = _generation(), b"new-cover", b"old-failed-qc"
    generation["final_cover_sha256"] = "sha256:" + hashlib.sha256(cover).hexdigest()
    authority = _authority(_generation(), b"old-cover", qc)
    receipt = {
        "schema_version": "lidousha-title-cover-joint-qc.v1", "status": "PASS", "pass": True,
        "cover_path": "/runtime/new.cover.png", "cover_sha256": generation["final_cover_sha256"],
    }
    qc_bytes = json.dumps(receipt, sort_keys=True).encode()
    manifest = repair.build_preflight_manifest(
        authority=authority, cover_bytes=cover, qc_bytes=qc_bytes, generation=generation,
        logical_cover_path="/runtime/new.cover.png", logical_qc_path="/runtime/new.qc.json",
    )
    assert repair.validate_preflight_manifest(manifest, authority=authority)["route"] == "screenshot_direct"
    tampered = copy.deepcopy(manifest)
    tampered["route"] = "cpa_redraw"
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="PREFLIGHT_DRIFT"):
        repair.validate_preflight_manifest(tampered, authority=authority)


def test_private_preflight_writes_no_official_target_and_cleans_stage(tmp_path) -> None:
    old = _generation()
    authority = _authority(old, b"old-cover", b"old-qc")
    official = tmp_path / "official.record.json"
    official.write_bytes(b"sealed-before")

    def stage(root):
        cover = root / "cover.png"
        cover.write_bytes(b"new-cover")
        generation = _generation()
        generation["final_cover_sha256"] = "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
        qc = {
            "schema_version": "lidousha-title-cover-joint-qc.v1", "status": "PASS", "pass": True,
            "cover_path": "/runtime/new.cover.png", "cover_sha256": generation["final_cover_sha256"],
        }
        return {"generation": generation, "cover_path": cover, "qc_bytes": json.dumps(qc).encode(),
                "logical_cover_path": "/runtime/new.cover.png", "logical_qc_path": "/runtime/new.qc.json"}

    result = repair.run_private_preflight(authority=authority, stage_root_parent=tmp_path, stage=stage)
    assert result["status"] == "FULL_DRY_RUN_PASS"
    assert result["target_writes"] == 0
    assert official.read_bytes() == b"sealed-before"
    assert not list(tmp_path.glob(".qixi-screenshot-cover-stage-*"))
    store = tmp_path / "qixi_screenshot_direct_cover_preflights" / authority["authority_sha256"][7:23]
    assert (store / result["manifest"]["preflight_sha256"][7:] / "cover.png").read_bytes() == b"new-cover"


def test_production_punch_fails_closed_without_credentials_before_provider(tmp_path) -> None:
    env = tmp_path / "cpa.env"
    env.write_text("CPA_BASE_URL=https://example.invalid\n", encoding="utf-8")
    called = False

    def factory(_config):
        nonlocal called
        called = True
        raise AssertionError("provider factory must not run")

    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="CREDENTIALS_MISSING"):
        repair._production_review_punch(
            story_hook="小李和宿敌关系梗", cover_text="小李有女友感吗？宿敌是否有点亲密了",
            llm_factory=factory, env_path=env,
        )
    assert called is False


def test_production_joint_qc_binds_fixed_title_candidate_and_logical_path(tmp_path) -> None:
    cover = tmp_path / "stage.png"
    cover.write_bytes(b"cover")
    seen = {}

    def probe(path, prompt):
        seen["path"], seen["prompt"] = path, prompt
        return {"status": "OBSERVED", "answer": '{"lidousha_primary":true,"thumbnail_readable":true,"single_clear_hook":true,"text_overcrowded":false,"title_cover_aligned":true,"physical_text_line_count":1,"unrelated_or_misleading_elements":[],"reason":"符合标题","pass":true}'}

    receipt = repair._production_joint_qc(
        cover_path=cover, logical_cover_path="/runtime/final.cover.png", image_probe=probe
    )
    assert receipt["candidate_id"] == repair.CANDIDATE_ID
    assert receipt["cover_path"] == "/runtime/final.cover.png"
    assert "女友感" in seen["prompt"]


def test_apply_preflight_targets_is_cas_replayable_without_provider(tmp_path) -> None:
    authority = _authority(_generation(), b"old-cover", b"old-qc")
    authority["runtime_root"] = str(tmp_path)
    authority["authority_sha256"] = _sha({key: value for key, value in authority.items() if key != "authority_sha256"})
    target = tmp_path / "official.json"
    target.write_bytes(b"before")
    result = repair.apply_preflight_targets(
        authority=authority,
        stage_root_parent=tmp_path,
        preflight_sha256="sha256:" + "a" * 64,
        targets={target: b"after"},
        apply=True,
    )
    assert result["status"] == "COMMITTED"
    assert target.read_bytes() == b"after"
    assert repair.apply_preflight_targets(
        authority=authority,
        stage_root_parent=tmp_path,
        preflight_sha256="sha256:" + "a" * 64,
        targets={target: b"after"},
        apply=True,
    )["status"] == "ALREADY_COMMITTED"


def test_private_preflight_seals_relocated_sidecars(tmp_path) -> None:
    authority = _authority(_generation(), b"old-cover", b"old-qc")
    relocated = Path("/opt/bilive/autoslice/out/qixi-sidecar.png")

    def stage(root):
        cover = root / "cover.png"
        sidecar = root / "pre-overlay.png"
        cover.write_bytes(b"new-cover")
        sidecar.write_bytes(b"pre-overlay")
        generation = _generation()
        generation["final_cover_sha256"] = "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
        qc = {
            "schema_version": "lidousha-title-cover-joint-qc.v1", "status": "PASS", "pass": True,
            "cover_path": "/runtime/new.cover.png", "cover_sha256": generation["final_cover_sha256"],
        }
        return {
            "generation": generation, "cover_path": cover, "qc_bytes": json.dumps(qc).encode(),
            "logical_cover_path": "/runtime/new.cover.png", "logical_qc_path": "/runtime/new.qc.json",
            "sidecars": {relocated: sidecar.read_bytes()},
        }

    result = repair.run_private_preflight(authority=authority, stage_root_parent=tmp_path, stage=stage)
    assert result["manifest"]["sidecars"][str(relocated)]["sha256"] == "sha256:" + hashlib.sha256(b"pre-overlay").hexdigest()
    stored = repair._load_preflight_store(Path(result["preflight_store"]), authority=authority)
    assert stored["sidecars"] == {relocated: b"pre-overlay"}


def test_mutation_contract_rejects_neighbor_leaf() -> None:
    authority = _authority(_generation(), b"old-cover", b"old-qc")
    authority["allowed_mutations"]["record"] = {
        "allowed": ["/publish_staging/cover_path"],
        "required": ["/publish_staging/cover_path"],
    }
    authority["authority_sha256"] = _sha({key: value for key, value in authority.items() if key != "authority_sha256"})
    before = {role: {} for role in ("record", "delivery_record", "publish", "state")}
    after = copy.deepcopy(before)
    before["record"] = {"publish_staging": {"cover_path": "before", "cover_text": "same"}}
    after["record"] = {"publish_staging": {"cover_path": "after", "cover_text": "drift"}}
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="ALLOWLIST_DRIFT"):
        repair._assert_projection_mutation_contract(authority, before=before, after=after)
