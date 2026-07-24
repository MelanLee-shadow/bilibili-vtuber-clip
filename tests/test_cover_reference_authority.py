import json
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.cover_reference_authority import (
    CoverReferenceAuthorityError,
    load_candidate_cover_reference,
)
from src.autoslice.story_contract import (
    build_story_contract,
    cover_relation_prompt,
)
from src.autoslice.cover_route_evidence import (
    build_no_crop_participant_verification,
    build_cover_route_decision,
    record_cover_route_execution,
    validate_cover_route_decision,
)
from src.autoslice.cover_text_pixel_evidence import (
    materialize_rendered_text_pixel_evidence,
)
from src.autoslice.cover_title_rendering import (
    SCHEMA_VERSION as COVER_TITLE_RENDER_SPEC_SCHEMA,
    render_spec_sha256,
    render_title_layer,
    sha256_file,
)
from src.autoslice.producer_package_finalization import _audit_story_bound_cover


REPO_ROOT = Path(__file__).resolve().parents[1]
COVER_FONT = (
    REPO_ROOT / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"
)


def _structural_pixel_evidence(
    text: str,
    *,
    final_cover_sha256: str,
) -> dict[str, object]:
    render_spec: dict[str, object] = {
        "schema_version": COVER_TITLE_RENDER_SPEC_SCHEMA,
        "font_file_name": COVER_FONT.name,
        "font_file_sha256": sha256_file(COVER_FONT),
        "font_face_index": 0,
        "layer_size": [1300, 410],
        "rendered_layer_size": [1300, 410],
        "angle_degrees": 0,
        "lines": [
            {
                "x": 30,
                "y": 30,
                "font_size": 180,
                "segment_pad": 10,
                "segments": [{"text": text, "fill": [255, 198, 41]}],
                "outlines": [
                    {"width": 15, "color": [18, 36, 79]},
                    {"width": 8, "color": [255, 255, 255]},
                ],
            }
        ],
    }
    return {
        "schema_version": "lidousha-cover-rendered-text-pixels.v3",
        "status": "PASS",
        "final_cover_sha256": final_cover_sha256,
        "pre_overlay_sha256": "sha256:" + "4" * 64,
        "canvas_size": [1920, 1080],
        "text_pixel_bbox": [300, 20, 1600, 430],
        "text_pixel_width": 1300,
        "text_pixel_height": 410,
        "font_size": 180,
        "font_file_name": COVER_FONT.name,
        "font_file_sha256": sha256_file(COVER_FONT),
        "rendered_text": text,
        "render_spec": render_spec,
        "render_spec_sha256": render_spec_sha256(render_spec),
        "overlay_position": {"x": 300, "y": 20},
        "mask_sha256": "sha256:" + "5" * 64,
        "mask_nonzero_pixels": 1000,
        "changed_pixel_count": 900,
        "changed_pixel_ratio": 0.9,
        "masked_final_pixels_sha256": "sha256:" + "6" * 64,
    }


def _materialize_pixel_evidence(
    tmp_path: Path,
    text: str,
) -> tuple[Path, Path, dict[str, object]]:
    pre_overlay = tmp_path / "pre-overlay.png"
    final_cover = tmp_path / "cover.png"
    Image.new("RGB", (1920, 1080), (244, 238, 220)).save(
        pre_overlay
    )
    render_spec = _structural_pixel_evidence(
        text, final_cover_sha256="sha256:" + "0" * 64
    )["render_spec"]
    assert isinstance(render_spec, dict)
    layer = render_title_layer(render_spec, font_path=COVER_FONT)
    with Image.open(pre_overlay) as source:
        final = source.convert("RGB")
    final.paste(layer, (300, 20), layer)
    final.save(final_cover)
    evidence = materialize_rendered_text_pixel_evidence(
        final_cover_path=final_cover,
        pre_overlay_path=pre_overlay,
        font_path=COVER_FONT,
        render_spec=render_spec,
        paste_xy=(300, 20),
        font_size=180,
        rendered_text=text,
    )
    return pre_overlay, final_cover, evidence


def _relation():
    return {
        "state": "CONFIRMED",
        "participants": [
            {"canonical_id": "lidousha", "display_name": "李豆沙"},
            {"canonical_id": "nancho", "display_name": "南町"},
        ],
    }


def _hash_bound_dual_reference(
    *, required_treatment: str = "screenshot_direct"
) -> dict[str, object]:
    return {
        "candidate_id": "candidate",
        "content_time_ms": 1_000,
        "source_time_ms": 2_000,
        "source_sha256": "sha256:" + "1" * 64,
        "reference_png_sha256": "sha256:" + "2" * 64,
        "visible_participant_ids": ["lidousha", "nancho"],
        "required_treatment": required_treatment,
        "authority": "reviewed source frame",
    }


def _source_visual_contract_row() -> dict[str, object]:
    row = _hash_bound_dual_reference()
    claims = [
        "李豆沙与南町同时出现在源画面中",
        "画面中央可见男性角色基利安",
    ]
    row.update(
        {
            "relation_action": "用双人同框和男性角色呈现不熟反转",
            "source_visible_claims": claims,
            "narrative_presentation": ("封面可借这些可见元素表达看到男角色后改口不熟的反转"),
            "source_visual_verification": {
                "schema_version": ("lidousha-cover-source-visual-verification.v1"),
                "status": "PASS",
                "reference_png_sha256": row["reference_png_sha256"],
                "visible_participant_ids": row["visible_participant_ids"],
                "verified_claims": claims,
                "authority": "independent source-frame pixel review",
            },
        }
    )
    return row


def _write_reference_ledger(tmp_path: Path, row: dict[str, object]) -> Path:
    path = tmp_path / "cover-references.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": ("lidousha-cover-reference-overrides.v1"),
                "overrides": [row],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_committed_chair_reference_is_hash_bound_and_survives_recut_suffix():
    reference = load_candidate_cover_reference(
        "auto_193450_1573_1672r4",
        ledger_path=(
            REPO_ROOT
            / "assets/lidousha/cover_reference_overrides.v1.json"
        ),
    )

    assert reference is not None
    assert reference["content_time_ms"] == 21_500
    assert reference["visible_participant_ids"] == ["lidousha", "nancho"]
    assert reference["required_treatment"] == "screenshot_direct"
    assert str(reference["ledger_sha256"]).startswith("sha256:")
    assert all(
        "椅子" not in claim and "霸凌" not in claim
        for claim in reference["source_visible_claims"]
    )
    assert "不声称画面里出现实体椅子或霸凌动作" in (
        reference["narrative_presentation"]
    )

    contract = build_story_contract(
        candidate_id="auto_193450_1573_1672",
        selection_hook="搭档把大椅子让给李豆沙",
        transcript_text="她把大椅子给我了，像被李豆沙霸凌。",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority=reference,
        source_media_sha256s=[reference["source_sha256"]],
    )
    assert contract["cover_counterpart_reference_available"] is True
    assert contract["cover_fallback_mode"] == "VERIFIED_DUAL_STREAM_FRAME"
    assert contract["source_media_sha256s"] == [reference["source_sha256"]]
    prompt = cover_relation_prompt(contract)
    assert "hash-bound source frame" in prompt
    assert "Preserve both" in prompt
    assert "HOST-ONLY" not in prompt


def test_committed_brainflick_reference_binds_readable_chat_without_fake_action():
    reference = load_candidate_cover_reference(
        "auto_193450_1475_1543r2",
        ledger_path=(
            REPO_ROOT
            / "assets/lidousha/cover_reference_overrides.v1.json"
        ),
    )

    assert reference is not None
    assert "左侧直播弹幕清楚出现“别管，先弹了再说”" in (
        reference["source_visible_claims"]
    )
    assert "不虚构手部动作" in reference["narrative_presentation"]
    assert "不虚构脑瓜崩手部动作" in reference["relation_action"]


def test_committed_sumi_reference_uses_story_aligned_real_frame():
    reference = load_candidate_cover_reference(
        "auto_193450_3573_3665r8",
        ledger_path=(
            REPO_ROOT
            / "assets/lidousha/cover_reference_overrides.v1.json"
        ),
    )

    assert reference is not None
    assert reference["content_time_ms"] == 70_000
    assert reference["source_time_ms"] == 3_643_070
    assert reference["visible_participant_ids"] == ["lidousha", "nancho"]
    assert reference["required_treatment"] == "screenshot_direct"
    assert "男性角色基利安" in reference["relation_action"]
    assert reference["reference_png_sha256"] == (
        "sha256:53d32dd63928c800458cd020ab988703bf13c585f41b56789efa715d6b693b8c"
    )


def test_reference_visible_participants_must_belong_to_story_contract():
    contract = build_story_contract(
        candidate_id="x",
        selection_hook="联动",
        transcript_text="文本",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority={
            "visible_participant_ids": ["lidousha", "unknown_guest"]
        },
    )
    assert contract["cover_counterpart_reference_available"] is False
    assert contract["cover_fallback_mode"] == "HOST_ONLY_RELATION_EXPLICIT"


def test_route_v2_binds_required_and_source_visible_participants():
    reference = _hash_bound_dual_reference()
    contract = build_story_contract(
        candidate_id="candidate",
        selection_hook="搭档把大椅子让给李豆沙",
        transcript_text="她把大椅子给我了。",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority=reference,
    )
    generation = {
        "story_contract": contract,
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="hash-bound source frame verifies all required participants",
        story_contract=contract,
        reference_authority=reference,
        decision_inputs={"cover_mode": "auto"},
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )

    route = generation["route_decision"]
    assert route["required_participant_ids"] == ["lidousha", "nancho"]
    assert route["source_visible_participant_ids"] == ["lidousha", "nancho"]
    assert route["source_visibility_authority"] == "HASH_BOUND_COVER_REFERENCE"
    assert not validate_cover_route_decision(generation)

    generation.update(
        {
            "image_generation_used": False,
            "reference_sha256": reference["reference_png_sha256"],
            "final_cover_sha256": "sha256:" + "3" * 64,
            "font_size": 180,
            "font_selection": {
                "font": COVER_FONT.name,
                "glyph_risk": [],
            },
            "angle_degrees": 0,
            "overlay_position": {"x": 300, "y": 20},
            "rendered_lines": ["真实联动画面"],
            "rendered_text_pixels": _structural_pixel_evidence(
                "真实联动画面",
                final_cover_sha256="sha256:" + "3" * 64,
            ),
            "screenshot_graphic_poster": {
                "schema_version": "screenshot-graphic-poster.v2",
                "status": "COMPOSED",
                "source_frame_transform": {
                    "input_sha256": reference[
                        "reference_png_sha256"
                    ],
                    "rendered_content_box": [480, 520, 1440, 1060],
                    "crop_applied": False,
                    "full_frame_preserved": True,
                    "ai_modified": False,
                    "center_4_3_safe": True,
                },
            },
        }
    )
    verification = build_no_crop_participant_verification(generation)
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
        final_participant_verification=verification,
    )
    assert validate_cover_route_decision(generation)


def test_3573_shaped_story_forces_typed_visual_safety_without_relation_words():
    reference = _hash_bound_dual_reference()
    contract = build_story_contract(
        candidate_id="auto_193450_3573_3665r8",
        selection_hook="李豆沙展示金发有角妹妹",
        transcript_text="看到男角色只能说不熟。",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority=reference,
    )
    generation = {
        "title": "最包容异性恋的直播间，看到男角色只能说出一句不熟",
        "cover_text": "看到男角色只能说不熟",
        "story_contract": contract,
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale=(
            "hash-bound source frame verifies all required participants"
        ),
        story_contract=contract,
        reference_authority=reference,
        decision_inputs={"cover_mode": "auto"},
        title=generation["title"],
        cover_text=generation["cover_text"],
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )

    route = generation["route_decision"]
    assert route["required_participant_ids"] == ["lidousha", "nancho"]
    assert route["relationship_semantic_evidence"] == []
    assert route["relationship_visual_required"] is True
    assert route["relationship_visual_safety_evidence"] == {
        "schema_version": (
            "lidousha-cover-relationship-visual-safety.v1"
        ),
        "status": "REQUIRED",
        "relation_state": "CONFIRMED",
        "required_participant_ids": ["lidousha", "nancho"],
        "requirement_basis": [
            "CONFIRMED_MULTI_PARTICIPANT_STORY_CONTRACT"
        ],
    }
    assert route["final_visibility_authority"] == (
        "PENDING_RELATION_VISUAL_VERIFICATION"
    )
    assert not validate_cover_route_decision(generation)


def test_route_v2_does_not_upgrade_unhashed_participant_list_to_authority():
    reference = {
        "candidate_id": "candidate",
        "visible_participant_ids": ["lidousha", "nancho"],
    }
    contract = build_story_contract(
        candidate_id="candidate",
        selection_hook="两人为了左右争论半天",
        transcript_text="左边是我，右边是你。",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority=reference,
    )
    generation = {
        "story_contract": contract,
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="claimed dual frame",
        story_contract=contract,
        reference_authority=reference,
        decision_inputs={"cover_mode": "auto"},
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )

    route = generation["route_decision"]
    assert route["source_visible_participant_ids"] == []
    assert route["source_visibility_authority"] == "NO_IDENTITY_AUTHORITY"
    assert not validate_cover_route_decision(generation)


def test_route_v2_rejects_dual_relation_ai_without_final_pixel_verification():
    """A redraw may not erase the named counterpart and still report READY."""

    reference = _hash_bound_dual_reference(required_treatment="cpa_redraw")
    contract = build_story_contract(
        candidate_id="candidate",
        selection_hook="被南町当面追问为什么最最最最喜欢",
        transcript_text="南町问她为什么最最最最喜欢。",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority=reference,
        source_media_sha256s=[reference["source_sha256"]],
    )
    generation = {
        "title": "被坏女人南町问到最最最最喜欢的原因",
        "cover_text": "最最最最喜欢？",
        "story_contract": contract,
        "method": "images.edit",
        "cover_origin": "AI_REDRAW",
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="cpa_redraw",
        selected_rationale="forced redraw for regression reproduction",
        story_contract=contract,
        reference_authority=reference,
        decision_inputs={"cover_mode": "cpa"},
        title=generation["title"],
        cover_text=generation["cover_text"],
    )
    record_cover_route_execution(
        generation,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
    )

    route = generation["route_decision"]
    assert route["relationship_visual_required"] is True
    assert route["source_visible_participant_ids"] == [
        "lidousha",
        "nancho",
    ]
    assert route["final_visible_participant_ids"] == []
    assert not validate_cover_route_decision(generation)

    generation["final_cover_sha256"] = "sha256:" + "3" * 64
    verification = {
        "schema_version": (
            "lidousha-cover-final-participant-verification.v1"
        ),
        "status": "PASS",
        "authority": "independent final-pixel review",
        "final_cover_sha256": generation["final_cover_sha256"],
        "visible_participant_ids": ["lidousha", "nancho"],
    }
    wrong_hash_verification = {
        **verification,
        "final_cover_sha256": "sha256:" + "4" * 64,
    }
    record_cover_route_execution(
        generation,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
        final_participant_verification=wrong_hash_verification,
    )
    assert not validate_cover_route_decision(generation)

    record_cover_route_execution(
        generation,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
        final_participant_verification=verification,
    )
    assert validate_cover_route_decision(generation)


def test_route_v2_rejects_incomplete_new_evidence_but_keeps_v1_read_compatibility():
    incomplete_v2 = {
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v2",
            "selected_treatment": "screenshot_direct",
            "reason": "real frame",
        }
    }
    legacy_v1 = {
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "real frame",
        }
    }

    assert not validate_cover_route_decision(incomplete_v2)
    assert validate_cover_route_decision(legacy_v1)
    assert not validate_cover_route_decision(
        legacy_v1, allow_legacy_v1=False
    )


def test_fresh_producer_rejects_legacy_route_and_accepts_replayable_v3(
    tmp_path: Path,
):
    contract = build_story_contract(
        candidate_id="candidate",
        selection_hook="真实画面",
        transcript_text="真实画面",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=None,
    )
    generation = {
        "story_contract": contract,
        "cover_text": "真实画面",
        "rendered_lines": ["真实画面"],
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "real frame",
        },
    }
    staging = {"cover_generation": generation, "cover_text": "真实画面"}

    reasons, _audits = _audit_story_bound_cover(staging, contract)
    assert "COVER_ROUTE_DECISION_MISSING_OR_INVALID" in reasons

    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="real frame",
        story_contract=contract,
        reference_authority=None,
        decision_inputs={"cover_mode": "auto"},
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )
    pre_overlay, final_cover, pixel_evidence = (
        _materialize_pixel_evidence(tmp_path, "真实画面")
    )
    generation.update(
        {
            "font_size": 180,
            "font_selection": {
                "font": COVER_FONT.name,
                "glyph_risk": [],
            },
            "angle_degrees": 0,
            "overlay_position": pixel_evidence["overlay_position"],
            "rendered_text_pixels": pixel_evidence,
            "pre_overlay_path": str(pre_overlay),
            "pre_overlay_sha256": pixel_evidence[
                "pre_overlay_sha256"
            ],
            "ai_background": str(pre_overlay),
            "ai_background_sha256": sha256_file(pre_overlay),
            "text_backing": "outline",
            "scrim": False,
            "final_cover": str(final_cover),
            "final_cover_sha256": pixel_evidence[
                "final_cover_sha256"
            ],
        }
    )
    reasons, _audits = _audit_story_bound_cover(staging, contract)
    assert reasons == []


def test_duplicate_reference_rows_fail_closed(tmp_path):
    row = {
        "candidate_id": "candidate",
        "content_time_ms": 1,
        "source_time_ms": 2,
        "source_sha256": "sha256:" + "1" * 64,
        "reference_png_sha256": "sha256:" + "2" * 64,
        "visible_participant_ids": ["lidousha", "nancho"],
        "required_treatment": "screenshot_direct",
        "authority": "reviewed",
    }
    path = tmp_path / "cover-references.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-cover-reference-overrides.v1",
                "overrides": [row, dict(row)],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CoverReferenceAuthorityError, match="AMBIGUOUS"):
        load_candidate_cover_reference("candidate", ledger_path=path)


def test_generic_reference_without_relation_action_remains_compatible(
    tmp_path: Path,
):
    path = _write_reference_ledger(tmp_path, _hash_bound_dual_reference())

    reference = load_candidate_cover_reference("candidate", ledger_path=path)

    assert reference is not None
    assert "source_visual_verification" not in reference


@pytest.mark.parametrize(
    "missing_field",
    [
        "source_visible_claims",
        "narrative_presentation",
        "source_visual_verification",
    ],
)
def test_relation_action_requires_every_source_visual_contract_field(
    tmp_path: Path,
    missing_field: str,
):
    row = _source_visual_contract_row()
    del row[missing_field]
    path = _write_reference_ledger(tmp_path, row)

    with pytest.raises(CoverReferenceAuthorityError, match="INVALID"):
        load_candidate_cover_reference("candidate", ledger_path=path)


def test_optional_source_visual_field_also_triggers_complete_contract(
    tmp_path: Path,
):
    row = _hash_bound_dual_reference()
    row["source_visible_claims"] = ["李豆沙与南町同时可见"]
    path = _write_reference_ledger(tmp_path, row)

    with pytest.raises(CoverReferenceAuthorityError, match="INVALID"):
        load_candidate_cover_reference("candidate", ledger_path=path)


def test_source_visual_contract_rejects_verified_claim_drift(
    tmp_path: Path,
):
    row = _source_visual_contract_row()
    verification = dict(row["source_visual_verification"])
    verification["verified_claims"] = ["两人同框"]
    row["source_visual_verification"] = verification
    path = _write_reference_ledger(tmp_path, row)

    with pytest.raises(CoverReferenceAuthorityError, match="INVALID"):
        load_candidate_cover_reference("candidate", ledger_path=path)


def test_source_visual_contract_rejects_reference_hash_drift(
    tmp_path: Path,
):
    row = _source_visual_contract_row()
    verification = dict(row["source_visual_verification"])
    verification["reference_png_sha256"] = "sha256:" + "9" * 64
    row["source_visual_verification"] = verification
    path = _write_reference_ledger(tmp_path, row)

    with pytest.raises(CoverReferenceAuthorityError, match="INVALID"):
        load_candidate_cover_reference("candidate", ledger_path=path)


def test_source_visual_contract_rejects_visible_participant_drift(
    tmp_path: Path,
):
    row = _source_visual_contract_row()
    verification = dict(row["source_visual_verification"])
    verification["visible_participant_ids"] = ["lidousha", "other"]
    row["source_visual_verification"] = verification
    path = _write_reference_ledger(tmp_path, row)

    with pytest.raises(CoverReferenceAuthorityError, match="INVALID"):
        load_candidate_cover_reference("candidate", ledger_path=path)


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [""],
        ["同框", "同框"],
        [" 同框"],
    ],
)
def test_source_visual_contract_rejects_non_unique_or_empty_claims(
    tmp_path: Path,
    claims: list[str],
):
    row = _source_visual_contract_row()
    row["source_visible_claims"] = claims
    verification = dict(row["source_visual_verification"])
    verification["verified_claims"] = claims
    row["source_visual_verification"] = verification
    path = _write_reference_ledger(tmp_path, row)

    with pytest.raises(CoverReferenceAuthorityError, match="INVALID"):
        load_candidate_cover_reference("candidate", ledger_path=path)


def test_complete_source_visual_contract_passes_and_keeps_narrative_separate(
    tmp_path: Path,
):
    row = _source_visual_contract_row()
    path = _write_reference_ledger(tmp_path, row)

    reference = load_candidate_cover_reference("candidater4", ledger_path=path)

    assert reference is not None
    verification = reference["source_visual_verification"]
    assert verification["verified_claims"] == reference["source_visible_claims"]
    assert reference["narrative_presentation"] not in verification["verified_claims"]
