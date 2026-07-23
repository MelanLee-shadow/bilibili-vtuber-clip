import json
from pathlib import Path

import pytest

from src.autoslice.cover_reference_authority import (
    CoverReferenceAuthorityError,
    load_candidate_cover_reference,
)
from src.autoslice.story_contract import (
    build_story_contract,
    cover_relation_prompt,
)
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
    validate_cover_route_decision,
)
from src.autoslice.producer_package_finalization import _audit_story_bound_cover


REPO_ROOT = Path(__file__).resolve().parents[1]


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
    assert validate_cover_route_decision(generation)


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


def test_fresh_producer_rejects_legacy_route_evidence_but_accepts_complete_v2():
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
