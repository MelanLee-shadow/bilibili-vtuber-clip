from __future__ import annotations

import copy
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from scripts.run_title_cover_joint_qc import build_joint_qc_receipt
from src.autoslice import qixi_screenshot_direct_cover_repair as repair
from src.autoslice import qixi_post_correction_projection_paths as projection_paths
from src.autoslice.cover_generation import LidoushaCoverArtDirection
from src.autoslice.fixed_cover_stage import FixedCoverStageOptions, apply_approved_punch


def _sha(value: object) -> str:
    return repair.canonical_sha256(value)


def _sealed_punch_receipt(
    *, cover_text: str = "小李有女友感吗？宿敌是否有点亲密了", story_hook: str = "小李和宿敌有点亲密",
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": "lidousha-cover-punch-semantic-review.v2",
        "status": "REVISED", "reason_code": None,
        "original_punch": list(repair.PUNCH_ORIGINAL),
        "final_punch": list(repair.PUNCH_CANDIDATES),
        "stranger_can_infer_event": True, "contains_concrete_subject": True,
        "contains_action_or_conflict": True, "no_fabricated_fact": True,
        "story_summary": "小李把女友感关系说成宿敌又有点亲密。",
        "click_motivation": "女友感与宿敌亲密的反差值得点开。",
        "cover_text_sha256": hashlib.sha256(cover_text.encode()).hexdigest(),
        "story_hook_sha256": hashlib.sha256(story_hook.encode()).hexdigest(),
        "request_sha256": "1" * 64, "response_sha256": "2" * 64,
        "attempt_count": 1,
        "attempts": [{
            "attempt": 1, "request_sha256": "1" * 64, "response_sha256": "2" * 64,
            "model_status": "REVISE", "validated_final_punch": list(repair.PUNCH_CANDIDATES),
            "status": "ACCEPTED",
        }],
    }
    return {**body, "receipt_sha256": _sha(body)}


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
        "punch_semantic_receipt": _sealed_punch_receipt(),
        "identity_landmark_title_exclusion": {
            "schema_version": "lidousha-cover-identity-landmark-title-exclusion.v1",
            "landmark": "lidousha_panda_ears",
            "background_sha256": "sha256:" + "6" * 64,
            "protected_bbox": [700, 120, 1230, 310],
            "title_zone": [260, 640, 1660, 1070],
        },
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
    assert repair.require_repaired_punch(list(repair.PUNCH_CANDIDATES)) == repair.PUNCH_CANDIDATES
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="OUTSIDE_POOL"):
        repair.require_repaired_punch(["任意新梗"])


def test_identity_landmark_exclusion_is_hash_bound_and_replayed() -> None:
    exclusion = {
        "schema_version": "lidousha-cover-identity-landmark-title-exclusion.v1",
        "landmark": "lidousha_panda_ears",
        "background_sha256": "sha256:" + "a" * 64,
        "protected_bbox": [700, 120, 1230, 310],
        "title_zone": [260, 640, 1660, 1070],
    }
    generation = {
        "ai_background_sha256": exclusion["background_sha256"],
        "identity_landmark_title_exclusion": {
            **exclusion, "status": "PASS", "text_pixel_bbox": [270, 690, 1600, 980],
        },
        "rendered_text_pixels": {
            "pre_overlay_sha256": exclusion["background_sha256"],
            "text_pixel_bbox": [270, 690, 1600, 980],
        },
    }
    assert repair._validate_identity_landmark_title_exclusion(
        exclusion, generation=generation,
    ) == exclusion
    generation["rendered_text_pixels"]["text_pixel_bbox"] = [700, 120, 1230, 310]
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="IDENTITY_LANDMARK_DRIFT"):
        repair._validate_identity_landmark_title_exclusion(exclusion, generation=generation)
    generation["rendered_text_pixels"]["text_pixel_bbox"] = [270, 690, 1600, 980]
    generation["identity_landmark_title_exclusion"]["text_pixel_bbox"] = [100, 690, 1600, 980]
    generation["rendered_text_pixels"]["text_pixel_bbox"] = [100, 690, 1600, 980]
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="IDENTITY_LANDMARK_DRIFT"):
        repair._validate_identity_landmark_title_exclusion(exclusion, generation=generation)


def test_identity_landmark_exclusion_moves_fixed_title_below_ears(tmp_path) -> None:
    from PIL import Image

    from src.autoslice.cover_generation import (
        LidoushaCoverArtDirection,
        _overlay_lidousha_cover_title,
    )

    background = tmp_path / "background.png"
    Image.new("RGB", (1920, 1080), (210, 120, 120)).save(background)
    background_sha = "sha256:" + hashlib.sha256(background.read_bytes()).hexdigest()
    exclusion = {
        "schema_version": "lidousha-cover-identity-landmark-title-exclusion.v1",
        "landmark": "lidousha_panda_ears",
        "background_sha256": background_sha,
        "protected_bbox": [700, 120, 1230, 310],
        "title_zone": [260, 640, 1660, 1070],
    }
    direction = LidoushaCoverArtDirection(
        role="shy_cute_default", expression_en="soft smile",
        background_style="coral-checker-pop", layout="banner", hook_color="purple",
        is_song=False, cover_punch=repair.PUNCH_CANDIDATES,
    )
    evidence = _overlay_lidousha_cover_title(
        background, tmp_path / "cover.png", cover_text="小李有女友感吗？宿敌是否有点亲密了",
        art_direction=direction, identity_landmark_title_exclusion=exclusion,
    )
    identity = evidence["identity_landmark_title_exclusion"]
    assert identity["status"] == "PASS"
    assert identity["text_pixel_bbox"][1] >= exclusion["title_zone"][1]
    assert identity["text_pixel_bbox"][1] >= exclusion["protected_bbox"][3]
    broken = dict(exclusion)
    broken["background_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="IDENTITY_LANDMARK_EXCLUSION_INVALID"):
        _overlay_lidousha_cover_title(
            background, tmp_path / "broken.png", cover_text="x",
            art_direction=direction, identity_landmark_title_exclusion=broken,
        )


def test_sealed_punch_receipt_is_hash_bound_and_revalidated_against_runtime_text() -> None:
    cover_text = "小李有女友感吗？宿敌是否有点亲密了"
    story_hook = "小李和宿敌有点亲密"
    authority = _authority(_generation(), b"old-cover", b"old-qc")
    assert repair.sealed_punch_semantic_receipt(
        authority, cover_text=cover_text, story_hook=story_hook,
    )["receipt_sha256"] == authority["punch_semantic_receipt"]["receipt_sha256"]
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="PUNCH_REVIEW_INVALID"):
        repair.sealed_punch_semantic_receipt(
            authority, cover_text=cover_text + "漂移", story_hook=story_hook,
        )
    tampered = copy.deepcopy(authority)
    receipt = tampered["punch_semantic_receipt"]
    assert isinstance(receipt, dict)
    receipt["story_summary"] = "伪造摘要"
    tampered["authority_sha256"] = _sha(
        {key: value for key, value in tampered.items() if key != "authority_sha256"}
    )
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="PUNCH_RECEIPT_INVALID"):
        repair.validate_authority(tampered)


def test_fixed_stage_accepts_only_valid_revised_approved_punch() -> None:
    cover_text = "小李有女友感吗？宿敌是否有点亲密了"
    story_hook = "小李和宿敌有点亲密"
    receipt = _sealed_punch_receipt(
        cover_text=cover_text, story_hook=story_hook,
    )
    direction = LidoushaCoverArtDirection(
        role="shy_cute_default", expression_en="soft smile",
        background_style="coral-checker-pop", layout="banner",
        hook_color="purple", is_song=False,
    )
    options = FixedCoverStageOptions(
        approved_punch=repair.PUNCH_CANDIDATES,
        approved_punch_receipt=receipt,
    )
    accepted = apply_approved_punch(
        direction, options=options, punch_allowed=True,
        cover_text=cover_text, story_hook=story_hook,
    )
    assert accepted is not None
    assert accepted.cover_punch == repair.PUNCH_CANDIDATES
    assert accepted.cover_punch_semantic_review == receipt

    for change in (
        lambda value: value.update(no_fabricated_fact=False),
        lambda value: value.update(final_punch=["伪造梗字"]),
        lambda value: value.update(status="FAILED"),
        lambda value: value.update(status="REJECT"),
    ):
        rejected_receipt = copy.deepcopy(receipt)
        change(rejected_receipt)
        assert apply_approved_punch(
            direction,
            options=FixedCoverStageOptions(
                approved_punch=repair.PUNCH_CANDIDATES,
                approved_punch_receipt=rejected_receipt,
            ),
            punch_allowed=True, cover_text=cover_text, story_hook=story_hook,
        ) is None
    assert apply_approved_punch(
        direction, options=options, punch_allowed=False,
        cover_text=cover_text, story_hook=story_hook,
    ) is None


def test_fixed_full_dry_reuses_sealed_punch_without_punch_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cover_text = "小李有女友感吗？宿敌是否有点亲密了"
    story_hook = "小李和宿敌有点亲密"
    authority = _authority(_generation(), b"old-cover", b"old-qc")
    authority["runtime_root"] = str(tmp_path)
    authority["authority_sha256"] = _sha(
        {key: value for key, value in authority.items() if key != "authority_sha256"}
    )
    runtime = {
        "record": json.dumps({
            "story_contract": {"selection_hook": story_hook},
            "publish_staging": {"cover_text": cover_text},
        }).encode()
    }
    monkeypatch.setattr(repair, "load_authority", lambda _root: authority)
    monkeypatch.setattr(repair, "snapshot_fixed_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(repair, "_scoped_cpa_environment", lambda: nullcontext())
    monkeypatch.setattr(
        repair,
        "_production_review_punch",
        lambda **_kwargs: pytest.fail("sealed full-dry must not call punch provider"),
    )

    def canonical(**kwargs):
        review = kwargs["review_punch"](
            title=authority["title"]["value"],
            story={"selection_hook": story_hook},
            cover_text=cover_text,
            candidates=repair.PUNCH_CANDIDATES,
        )
        assert review["status"] == "REVISED"
        return {"status": "FULL_DRY_RUN_PASS"}

    monkeypatch.setattr(repair, "run_canonical_full_dry", canonical)
    assert repair.run_fixed_full_dry(repo_root=tmp_path, stage_root_parent=tmp_path) == {
        "status": "FULL_DRY_RUN_PASS"
    }


def test_relocated_gate_seam_uses_projection_authority_and_reports_all_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert repair.replayed_cover_gate_callables is projection_paths.replayed_cover_gate_callables
    called: list[str] = []

    def fail(name: str):
        def check() -> None:
            called.append(name)
            raise RuntimeError("private stage path must not escape")
        return check

    predicate_ids = tuple(repair._RELOCATED_GATE_REASON_CODES)
    monkeypatch.setattr(
        repair,
        "replayed_cover_gate_callables",
        lambda *_args, **_kwargs: tuple((name, fail(name)) for name in predicate_ids),
    )
    with pytest.raises(
        repair.QixiScreenshotDirectCoverRepairError,
        match="cover_route,cover_rendered_text_pixels,cover_final_host_identity,"
        "cover_final_participant_identity,cover_punch_semantics,cover_materialized_hashes",
    ) as raised:
        repair._replay_relocated_cover_gates(
            generation={}, story={}, package_root=Path("/official"), after={},
        )
    assert called == list(predicate_ids)
    assert "private" not in str(raised.value)
    assert "COVER_REPAIR_RELOCATED_MATERIALIZED_INVALID" in "".join(raised.value.__notes__)


def test_relocated_generation_replays_materialized_paths_after_private_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private-stage"
    root.mkdir()
    final, pre_overlay = root / "final.png", root / "pre.png"
    final.write_bytes(b"final-cover")
    pre_overlay.write_bytes(b"pre-overlay")
    authority = _authority(_generation(), b"old-cover", b"old-qc")
    legacy = authority["legacy_cover"]
    assert isinstance(legacy, dict)
    legacy["final_cover"] = {
        "path": "/opt/bilive/autoslice/out/final.cover.png",
        "sha256": "sha256:" + hashlib.sha256(b"old-cover").hexdigest(),
        "bytes": len(b"old-cover"),
    }
    authority["authority_sha256"] = _sha(
        {key: value for key, value in authority.items() if key != "authority_sha256"}
    )
    generation = {
        "method": "screenshot_direct",
        "final_cover": str(final),
        "final_cover_sha256": "sha256:" + hashlib.sha256(final.read_bytes()).hexdigest(),
        "pre_overlay_path": str(pre_overlay),
        "pre_overlay_sha256": "sha256:" + hashlib.sha256(pre_overlay.read_bytes()).hexdigest(),
        "ai_background": str(pre_overlay),
        "ai_background_sha256": "sha256:" + hashlib.sha256(pre_overlay.read_bytes()).hexdigest(),
        "route_decision": {"required_participant_ids": []},
        "cover_text_mode": "full",
    }
    relocated, sidecars = repair._relocate_private_generation(
        authority=authority, root=root, generation=generation, cover_path=final,
    )
    monkeypatch.setattr(projection_paths, "validate_cover_route_decision", lambda *_a, **_k: True)
    monkeypatch.setattr(projection_paths, "validate_rendered_text_pixel_evidence", lambda *_a, **_k: True)
    monkeypatch.setattr(projection_paths, "validate_final_host_identity_verification", lambda *_a, **_k: True)
    after = dict(sidecars)
    after[Path(str(relocated["final_cover"]))] = final.read_bytes()
    matrix = repair._replay_relocated_cover_gates(
        generation=relocated,
        story={},
        package_root=Path(str(relocated["final_cover"])).parent,
        after=after,
    )
    assert all(row["status"] == "PASS" for row in matrix)
    assert Path(str(relocated["pre_overlay_path"])) in sidecars
    assert Path(str(relocated["ai_background"])) in sidecars


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


def test_background_reuse_keeps_old_locator_and_rejects_new_bytes(tmp_path) -> None:
    old_background = tmp_path / "old-poster.png"
    staged_background = tmp_path / "stage-poster.png"
    old_background.write_bytes(b"immutable-poster")
    staged_background.write_bytes(b"immutable-poster")
    digest = "sha256:" + hashlib.sha256(b"immutable-poster").hexdigest()
    prior = {"ai_background": str(old_background), "ai_background_sha256": digest}
    staged = {"ai_background": str(staged_background), "ai_background_sha256": digest}
    normalized = repair._preserve_immutable_background(
        prior_generation=prior, staged_generation=staged,
    )
    assert normalized["ai_background"] == str(old_background)
    assert normalized["ai_background_sha256"] == digest
    staged_background.write_bytes(b"new-background")
    with pytest.raises(repair.QixiScreenshotDirectCoverRepairError, match="AI_BACKGROUND_DRIFT"):
        repair._preserve_immutable_background(
            prior_generation=prior, staged_generation=staged,
        )
