from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_lidousha_final_human_review as builder
from src.autoslice import final_human_review as human_review


CANDIDATE_ID = "auto_193450_1000_1020"
STEM = "reviewed-clip"
TITLE = "【李豆沙】测试片完整标题"
CHECK_NAMES = (
    "final_burned_full_playback",
    "subtitle_audio",
    "silence_hallucination",
    "boundary_closure",
    "title_story",
    "cover_identity",
    "cover_story",
    "intro_timing",
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def receipt_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    root = tmp_path / "package"
    root.mkdir()
    verification = root / "verification"
    verification.mkdir()
    paths = {
        "video": root / f"{STEM}.mp4",
        "subtitle": root / f"{STEM}.srt",
        "cover": root / f"{STEM}.cover.png",
        "record": root / f"{STEM}.record.json",
    }
    for kind in ("video", "subtitle", "cover"):
        paths[kind].write_bytes(f"final {kind} bytes".encode())

    source_claims = [
        "最终源帧左侧清楚可见李豆沙",
        "最终源帧右侧清楚可见南町",
    ]
    narrative = "封面文字表达两人围绕测试问题争论的故事"
    authority = {
        "candidate_id": CANDIDATE_ID,
        "observed_public_title": TITLE,
        "bvid": "BV1234567890",
        "aid": 123,
        "cid": 456,
        "authority_sha256": "sha256:" + "a" * 64,
    }
    record = {
        "burned_preview": {"branding_intro": {"verification": {"duration_ms": 20_000}}},
        "story_contract": {
            "candidate_id": CANDIDATE_ID,
            "cover_reference_authority": {
                "source_visible_claims": source_claims,
                "narrative_presentation": narrative,
            },
        },
        "publish_staging": {"title": TITLE},
        "recovery_publication_authority": authority,
    }
    _write_json(paths["record"], record)
    manifest = {
        "status": ("finished_review_package_no_upload_pending_human_review"),
        "upload_allowed": False,
        "exact_candidate_ids": [CANDIDATE_ID],
        "selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [CANDIDATE_ID],
        },
        "items": [
            {
                "candidate_id": CANDIDATE_ID,
                "title": TITLE,
                "video": paths["video"].name,
                "mp4": paths["video"].name,
                "subtitle_srt": paths["subtitle"].name,
                "cover": paths["cover"].name,
                "record": paths["record"].name,
                "recovery_publication_authority": authority,
            }
        ],
    }
    review_path = root / "review_manifest.json"
    _write_json(review_path, manifest)

    audit = {
        "schema_version": builder.AUDIT_SCHEMA_VERSION,
        "policy_epoch": builder.AUDIT_POLICY_EPOCH,
        "policy_fingerprint": "sha256:" + "b" * 64,
        "auditor_source_sha256": "sha256:" + "c" * 64,
        "passed": True,
        "root": str(root.resolve()),
        "audited_inputs": [],
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }
    audit_path = verification / "package-audit.json"
    _write_json(audit_path, audit)
    monkeypatch.setattr(builder, "audit_package", lambda _root: copy.deepcopy(audit))

    contract_path = tmp_path / "review-contract.json"
    contract = {
        "schema_version": ("lidousha-final-media-review-contracts.v1"),
        "authority": "test committed exact point",
        "contracts": [
            {
                "candidate_id": CANDIDATE_ID,
                "subtitle_review_points": [
                    {
                        "point_id": "heard-exact-line",
                        "final_video_start_ms": 1_000,
                        "final_video_end_ms": 3_000,
                        "expectation": "必须听到测试台词并与烧录字幕一致。",
                    }
                ],
            }
        ],
    }
    _write_json(contract_path, contract)
    monkeypatch.setattr(human_review, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract_path)

    check_details = {
        "final_burned_full_playback": (
            "从00:00.000开头连续看到播放器EOS自然结束，最后一帧停在双人对话收束。"
        ),
        "subtitle_audio": (
            "在00:01.000实际听到测试台词，烧录字幕随发声出现且字句相同。"
        ),
        "silence_hallucination": (
            "复看00:08.000静音处没有人声，画面也没有多出幻听字幕cue。"
        ),
        "boundary_closure": (
            "片头起点保留问句，结尾完整落在回答后的自然停顿，没有截断句子。"
        ),
        "title_story": (
            "最终标题写明测试争论，完整故事确实先提问再给出回答。"
        ),
        "cover_identity": (
            "最终封面左侧人物是李豆沙，右侧立绘是南町，两人身份均可辨认。"
        ),
        "cover_story": (
            "最终封面大字写测试争论，文字叙事与双人画面关系一致。"
        ),
        "intro_timing": (
            "片头结束后平滑切入正片第一句，intro到main衔接没有吃字。"
        ),
    }
    evidence = builder.build_evidence_template(
        package_root=root,
        package_audit_path=audit_path,
    )
    evidence.update(
        {
            "reviewer_kind": "delegated_root_agent",
            "reviewed_by": "Codex root",
            "reviewed_at": "2026-07-24T05:00:00-04:00",
            "approval_quote": "请完整看完最终五片后再生成真实复核凭据。",
        }
    )
    evidence_item = evidence["items"][0]
    for check, detail in check_details.items():
        evidence_item["checks"][check]["detail"] = detail
    evidence_item["subtitle_review_points"][0]["observation"]["detail"] = (
        "在该时间段实际听到“测试台词”，烧录字幕出现和收尾均与音频发声同步。"
    )
    claim_details = (
        "最终封面源帧左侧人物面部和李豆沙身份特征清楚可见，没有被标题层遮挡。",
        "最终封面画面右侧南町人物立绘完整可辨，与左侧人物保持同框关系。",
        "最终封面大字明确呈现测试问题争论，文字叙事与双人关系相符。",
    )
    for claim, detail in zip(
        evidence_item["cover_story_claims"], claim_details, strict=True
    ):
        claim["observation"]["detail"] = detail
    evidence_path = verification / "completed-review-evidence.v2.json"
    _write_json(evidence_path, evidence)
    return {
        "root": root,
        "paths": paths,
        "manifest": manifest,
        "review_path": review_path,
        "audit": audit,
        "audit_path": audit_path,
        "contract_path": contract_path,
        "contract": contract,
        "evidence": evidence,
        "evidence_path": evidence_path,
        "output": verification / "final-human-review.json",
        "source_claims": source_claims,
        "narrative": narrative,
        "authority": authority,
    }


def test_build_derives_all_authoritative_fields_and_validates_canonically(
    receipt_package: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[object] = []
    canonical_validate = human_review.validate_final_human_review

    def _recording_validate(*args: object, **kwargs: object) -> object:
        called.append(args[0])
        return canonical_validate(*args, **kwargs)

    monkeypatch.setattr(
        human_review,
        "validate_final_human_review",
        _recording_validate,
    )
    receipt = builder.build_receipt(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
        evidence_path=receipt_package["evidence_path"],
    )

    assert len(called) == 1
    assert receipt["schema_version"] == "lidousha-final-human-review.v2"
    assert receipt["review_contract_sha256"] == _sha256(receipt_package["contract_path"])
    assert receipt["package_evidence"] == {
        "review_manifest": {
            "path": "review_manifest.json",
            "sha256": _sha256(receipt_package["review_path"]),
        },
        "package_audit": {
            "path": "verification/package-audit.json",
            "sha256": _sha256(receipt_package["audit_path"]),
        },
        "review_evidence": {
            "path": "verification/completed-review-evidence.v2.json",
            "sha256": _sha256(receipt_package["evidence_path"]),
            "bytes": receipt_package["evidence_path"].stat().st_size,
        },
    }
    item = receipt["items"][0]
    assert item["candidate_id"] == CANDIDATE_ID
    assert item["reviewed_title"] == TITLE
    assert item["publication_target"] == {
        "candidate_id": CANDIDATE_ID,
        "bvid": "BV1234567890",
        "aid": 123,
        "cid": 456,
        "final_title": TITLE,
        "authority_sha256": "sha256:" + "a" * 64,
    }
    assert item["artifacts"]["video"] == {
        "path": f"{STEM}.mp4",
        "sha256": _sha256(receipt_package["paths"]["video"]),
    }
    assert item["subtitle_review_points"][0] == {
        **receipt_package["contract"]["contracts"][0]["subtitle_review_points"][0],
        "status": "PASS",
        "evidence": (
            "00:00:01.000-00:00:03.000 — "
            + receipt_package["evidence"]["items"][0]["subtitle_review_points"][0][
                "observation"
            ]["detail"]
        ),
    }
    assert [(row["claim"], row["presentation"]) for row in item["cover_story_claims"]] == [
        (receipt_package["source_claims"][0], "SOURCE_FRAME"),
        (receipt_package["source_claims"][1], "SOURCE_FRAME"),
        (receipt_package["narrative"], "COVER_TEXT"),
    ]


def test_multi_person_template_uses_hash_closed_rendered_text_not_art_direction(
    receipt_package: dict[str, object],
) -> None:
    record_path = receipt_package["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    cover_sha256 = _sha256(receipt_package["paths"]["cover"])
    rendered_lines = ["请南町吃火锅", "刚认识就互相霸凌"]
    record["artifact_hashes"] = {"cover_sha256": cover_sha256}
    record["publish_staging"]["cover_generation"] = {
        "final_cover_sha256": cover_sha256,
        "rendered_lines": rendered_lines,
        "rendered_text_pixels": {
            "status": "PASS",
            "rendered_text": "".join(rendered_lines),
            "final_cover_sha256": cover_sha256,
        },
    }
    _write_json(record_path, record)

    evidence = builder.build_evidence_template(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
    )

    claims = [
        (row["claim"], row["presentation"])
        for row in evidence["items"][0]["cover_story_claims"]
    ]
    assert claims == [
        (receipt_package["source_claims"][0], "SOURCE_FRAME"),
        (receipt_package["source_claims"][1], "SOURCE_FRAME"),
        ("封面文字呈现“请南町吃火锅 / 刚认识就互相霸凌”", "COVER_TEXT"),
    ]


def test_template_accepts_hash_closed_host_only_screenshot_cover(
    receipt_package: dict[str, object],
) -> None:
    record_path = receipt_package["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    cover_sha256 = _sha256(receipt_package["paths"]["cover"])
    rendered_lines = ["有骗子", "刚准备下注就没了"]
    record["story_contract"].update(
        {
            "cover_reference_authority": None,
            "cover_counterpart_reference_available": False,
            "relation_claim_allowed": False,
            "cover_fallback_mode": "HOST_ONLY_GENERIC",
        }
    )
    record["artifact_hashes"] = {"cover_sha256": cover_sha256}
    record["publish_staging"]["cover_generation"] = {
        "cover_origin": "SOURCE_SCREENSHOT",
        "image_generation_used": False,
        "final_cover_sha256": cover_sha256,
        "rendered_lines": rendered_lines,
        "rendered_text_pixels": {
            "status": "PASS",
            "rendered_text": "".join(rendered_lines),
            "final_cover_sha256": cover_sha256,
        },
        "route_decision": {
            "execution_status": "READY",
            "actual_treatment": "screenshot_direct",
            "selected_treatment": "screenshot_direct",
            "relationship_visual_required": False,
            "required_participant_ids": [],
            "source_visible_participant_ids": [],
            "image_generation_used": False,
            "source_visibility_authority": "NO_IDENTITY_AUTHORITY",
            "final_visibility_authority": "NOT_REQUIRED",
        },
    }
    _write_json(record_path, record)

    evidence = builder.build_evidence_template(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
    )

    assert evidence["items"][0]["cover_story_claims"] == [
        {
            "claim": "封面文字呈现“有骗子 / 刚准备下注就没了”",
            "presentation": "COVER_TEXT",
            "observation": {
                "anchor": "FINAL_COVER/COVER_TEXT",
                "detail": "<REQUIRED_POST_REVIEW_VISIBLE_COVER_DETAIL>",
            },
        }
    ]


def test_template_accepts_hash_closed_host_only_screenshot_polish_cover(
    receipt_package: dict[str, object],
) -> None:
    record_path = receipt_package["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    cover_sha256 = _sha256(receipt_package["paths"]["cover"])
    rendered_lines = ["发1支持沙豆李", "赶紧改成2"]
    record["story_contract"].update(
        {
            "cover_reference_authority": None,
            "cover_counterpart_reference_available": False,
            "relation_claim_allowed": False,
            "cover_fallback_mode": "HOST_ONLY_GENERIC",
        }
    )
    record["artifact_hashes"] = {"cover_sha256": cover_sha256}
    record["publish_staging"]["cover_generation"] = {
        "cover_origin": "SOURCE_SCREENSHOT_AI_POLISH",
        "image_generation_used": True,
        "final_cover_sha256": cover_sha256,
        "rendered_lines": rendered_lines,
        "rendered_text_pixels": {
            "status": "PASS",
            "rendered_text": "".join(rendered_lines),
            "final_cover_sha256": cover_sha256,
        },
        "route_decision": {
            "execution_status": "READY",
            "actual_treatment": "screenshot_polish",
            "selected_treatment": "screenshot_polish",
            "relationship_visual_required": False,
            "required_participant_ids": [],
            "source_visible_participant_ids": [],
            "image_generation_used": True,
            "source_visibility_authority": "NO_IDENTITY_AUTHORITY",
            "final_visibility_authority": "NOT_REQUIRED",
        },
    }
    _write_json(record_path, record)

    evidence = builder.build_evidence_template(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
    )

    assert evidence["items"][0]["cover_story_claims"] == [
        {
            "claim": "封面文字呈现“发1支持沙豆李 / 赶紧改成2”",
            "presentation": "COVER_TEXT",
            "observation": {
                "anchor": "FINAL_COVER/COVER_TEXT",
                "detail": "<REQUIRED_POST_REVIEW_VISIBLE_COVER_DETAIL>",
            },
        }
    ]


def test_template_accepts_hash_closed_host_only_cpa_redraw_cover(
    receipt_package: dict[str, object],
) -> None:
    record_path = receipt_package["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    cover_sha256 = _sha256(receipt_package["paths"]["cover"])
    rendered_lines = ["小李刚准备下注", "就没了"]
    record["story_contract"].update(
        {
            "cover_reference_authority": None,
            "cover_counterpart_reference_available": False,
            "relation_claim_allowed": False,
            "cover_fallback_mode": "HOST_ONLY_GENERIC",
        }
    )
    record["artifact_hashes"] = {"cover_sha256": cover_sha256}
    record["publish_staging"]["cover_generation"] = {
        "cover_origin": "AI_REDRAW",
        "image_generation_used": True,
        "image_gen_model": "cpa",
        "method": "images.edit",
        "model": "gpt-image-2",
        "final_cover_sha256": cover_sha256,
        "rendered_lines": rendered_lines,
        "rendered_text_pixels": {
            "status": "PASS",
            "rendered_text": "".join(rendered_lines),
            "final_cover_sha256": cover_sha256,
        },
        "route_decision": {
            "execution_status": "READY",
            "actual_treatment": "cpa_redraw",
            "selected_treatment": "cpa_redraw",
            "relationship_visual_required": False,
            "required_participant_ids": [],
            "source_visible_participant_ids": [],
            "image_generation_used": True,
            "source_visibility_authority": "NO_IDENTITY_AUTHORITY",
            "final_visibility_authority": "NOT_REQUIRED",
        },
    }
    _write_json(record_path, record)

    evidence = builder.build_evidence_template(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
    )

    assert evidence["items"][0]["cover_story_claims"] == [
        {
            "claim": "封面文字呈现“小李刚准备下注 / 就没了”",
            "presentation": "COVER_TEXT",
            "observation": {
                "anchor": "FINAL_COVER/COVER_TEXT",
                "detail": "<REQUIRED_POST_REVIEW_VISIBLE_COVER_DETAIL>",
            },
        }
    ]


@pytest.mark.parametrize(
    ("cover_origin", "method", "model", "image_gen_model"),
    [
        ("SOURCE_SCREENSHOT", "images.edit", "gpt-image-2", "cpa"),
        ("AI_REDRAW", "images.generate", "gpt-image-2", "cpa"),
        ("AI_REDRAW", "images.edit", "unverified-model", "cpa"),
        ("AI_REDRAW", "images.edit", "gpt-image-2", "unknown"),
    ],
)
def test_template_refuses_incoherent_host_only_cpa_redraw_provenance(
    receipt_package: dict[str, object],
    cover_origin: str,
    method: str,
    model: str,
    image_gen_model: str,
) -> None:
    record_path = receipt_package["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    cover_sha256 = _sha256(receipt_package["paths"]["cover"])
    record["story_contract"].update(
        {
            "cover_reference_authority": None,
            "cover_counterpart_reference_available": False,
            "relation_claim_allowed": False,
            "cover_fallback_mode": "HOST_ONLY_GENERIC",
        }
    )
    record["artifact_hashes"] = {"cover_sha256": cover_sha256}
    record["publish_staging"]["cover_generation"] = {
        "cover_origin": cover_origin,
        "image_generation_used": True,
        "image_gen_model": image_gen_model,
        "method": method,
        "model": model,
        "final_cover_sha256": cover_sha256,
        "rendered_lines": ["小李刚准备下注", "就没了"],
        "rendered_text_pixels": {
            "status": "PASS",
            "rendered_text": "小李刚准备下注就没了",
            "final_cover_sha256": cover_sha256,
        },
        "route_decision": {
            "execution_status": "READY",
            "actual_treatment": "cpa_redraw",
            "selected_treatment": "cpa_redraw",
            "relationship_visual_required": False,
            "required_participant_ids": [],
            "source_visible_participant_ids": [],
            "image_generation_used": True,
            "source_visibility_authority": "NO_IDENTITY_AUTHORITY",
            "final_visibility_authority": "NOT_REQUIRED",
        },
    }
    _write_json(record_path, record)

    with pytest.raises(builder.FinalHumanReviewBuildError):
        builder.build_evidence_template(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
        )


@pytest.mark.parametrize(
    ("cover_origin", "generation_used", "route_used"),
    [
        ("SOURCE_SCREENSHOT", True, True),
        ("SOURCE_SCREENSHOT_AI_POLISH", False, True),
        ("SOURCE_SCREENSHOT_AI_POLISH", True, False),
    ],
)
def test_template_refuses_incoherent_host_only_screenshot_provenance(
    receipt_package: dict[str, object],
    cover_origin: str,
    generation_used: bool,
    route_used: bool,
) -> None:
    record_path = receipt_package["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["story_contract"].update(
        {
            "cover_reference_authority": None,
            "cover_counterpart_reference_available": False,
            "relation_claim_allowed": False,
            "cover_fallback_mode": "HOST_ONLY_GENERIC",
        }
    )
    cover_sha256 = _sha256(receipt_package["paths"]["cover"])
    record["artifact_hashes"] = {"cover_sha256": cover_sha256}
    record["publish_staging"]["cover_generation"] = {
        "cover_origin": cover_origin,
        "image_generation_used": generation_used,
        "final_cover_sha256": cover_sha256,
        "rendered_lines": ["有骗子", "刚准备下注就没了"],
        "rendered_text_pixels": {
            "status": "PASS",
            "rendered_text": "有骗子刚准备下注就没了",
            "final_cover_sha256": cover_sha256,
        },
        "route_decision": {
            "execution_status": "READY",
            "actual_treatment": "screenshot_polish",
            "selected_treatment": "screenshot_polish",
            "relationship_visual_required": False,
            "required_participant_ids": [],
            "source_visible_participant_ids": [],
            "image_generation_used": route_used,
            "source_visibility_authority": "NO_IDENTITY_AUTHORITY",
            "final_visibility_authority": "NOT_REQUIRED",
        },
    }
    _write_json(record_path, record)

    with pytest.raises(builder.FinalHumanReviewBuildError):
        builder.build_evidence_template(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda evidence: evidence["items"][0]["checks"].update(
            {"subtitle_audio": "inspected"}
        ),
        lambda evidence: evidence["items"][0]["checks"].update(
            {
                "subtitle_audio": {
                    "anchor": "FULL_VIDEO_AUDIO_SUBTITLE",
                    "detail": "编号１：均无异常。",
                }
            }
        ),
        lambda evidence: evidence["items"][0]["checks"].update(
            {"subtitle_audio": evidence["items"][0]["checks"]["cover_story"]}
        ),
        lambda evidence: evidence.update({"status": "PASS"}),
        lambda evidence: evidence["items"][0]["subtitle_review_points"][0].update(
            {"status": "PASS"}
        ),
    ],
)
def test_refuses_generic_reused_or_prefilled_evidence(
    receipt_package: dict[str, object],
    mutation: object,
) -> None:
    evidence = copy.deepcopy(receipt_package["evidence"])
    mutation(evidence)
    _write_json(receipt_package["evidence_path"], evidence)

    with pytest.raises(builder.FinalHumanReviewBuildError):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )
    assert not receipt_package["output"].exists()


def test_refuses_stale_self_asserted_audit(
    receipt_package: dict[str, object],
) -> None:
    audit = copy.deepcopy(receipt_package["audit"])
    audit["policy_fingerprint"] = "sha256:" + "0" * 64
    _write_json(receipt_package["audit_path"], audit)

    with pytest.raises(builder.FinalHumanReviewBuildError, match="stale"):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )


def test_v2_template_binds_every_reviewed_byte_and_is_deliberately_incomplete(
    receipt_package: dict[str, object],
) -> None:
    template = builder.build_evidence_template(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
    )

    assert template["schema_version"] == (
        "lidousha-final-human-review-evidence.v2"
    )
    assert template["bindings"]["review_contract_sha256"] == _sha256(
        receipt_package["contract_path"]
    )
    assert template["bindings"]["review_manifest"] == {
        "path": "review_manifest.json",
        "sha256": _sha256(receipt_package["review_path"]),
    }
    assert template["bindings"]["package_audit"] == {
        "path": "verification/package-audit.json",
        "sha256": _sha256(receipt_package["audit_path"]),
    }
    item_binding = template["bindings"]["items"][0]
    assert item_binding["candidate_id"] == CANDIDATE_ID
    assert item_binding["record"]["sha256"] == _sha256(
        receipt_package["paths"]["record"]
    )
    for artifact in ("video", "subtitle", "cover"):
        assert item_binding["artifacts"][artifact]["sha256"] == _sha256(
            receipt_package["paths"][artifact]
        )
    assert template["items"][0]["checks"]["subtitle_audio"] == {
        "anchor": "FULL_VIDEO_AUDIO_SUBTITLE",
        "detail": "<REQUIRED_POST_REVIEW_OBSERVATION_FOR_subtitle_audio>",
    }

    template_path = (
        receipt_package["root"]
        / "verification"
        / "prepared-evidence.json"
    )
    written = builder.write_evidence_template_create_only(
        template_path,
        template,
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
    )
    assert written == template_path
    with pytest.raises(builder.FinalHumanReviewBuildError):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=template_path,
        )


def test_template_refuses_review_point_beyond_final_media(
    receipt_package: dict[str, object],
) -> None:
    contract = copy.deepcopy(receipt_package["contract"])
    point = contract["contracts"][0]["subtitle_review_points"][0]
    point["final_video_start_ms"] = 19_000
    point["final_video_end_ms"] = 21_000
    _write_json(receipt_package["contract_path"], contract)

    with pytest.raises(
        builder.FinalHumanReviewBuildError,
        match="review point exceeds final media duration",
    ):
        builder.build_evidence_template(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
        )


@pytest.mark.parametrize(
    "drift",
    [
        "review_manifest",
        "package_audit",
        "review_contract",
        "record",
        "video",
        "subtitle",
        "cover",
    ],
)
def test_byte_bound_evidence_rejects_every_authority_or_artifact_drift(
    receipt_package: dict[str, object],
    drift: str,
) -> None:
    if drift == "review_manifest":
        with receipt_package["review_path"].open("a", encoding="utf-8") as handle:
            handle.write("\n")
    elif drift == "package_audit":
        audit = copy.deepcopy(receipt_package["audit"])
        audit["policy_fingerprint"] = "sha256:" + "d" * 64
        _write_json(receipt_package["audit_path"], audit)
    elif drift == "review_contract":
        with receipt_package["contract_path"].open("a", encoding="utf-8") as handle:
            handle.write("\n")
    elif drift == "record":
        with receipt_package["paths"]["record"].open("a", encoding="utf-8") as handle:
            handle.write("\n")
    else:
        receipt_package["paths"][drift].write_bytes(
            receipt_package["paths"][drift].read_bytes() + b" drift"
        )

    with pytest.raises(builder.FinalHumanReviewBuildError):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )


def test_old_v1_evidence_is_rejected(
    receipt_package: dict[str, object],
) -> None:
    evidence = copy.deepcopy(receipt_package["evidence"])
    evidence["schema_version"] = "lidousha-final-human-review-evidence.v1"
    evidence.pop("bindings")
    _write_json(receipt_package["evidence_path"], evidence)

    with pytest.raises(builder.FinalHumanReviewBuildError):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )


def test_completed_evidence_must_be_a_bound_package_file(
    receipt_package: dict[str, object],
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-completed-evidence.json"
    _write_json(outside, receipt_package["evidence"])

    with pytest.raises(
        builder.FinalHumanReviewBuildError,
        match="inside package root",
    ):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=outside,
        )


def test_nfkc_number_and_punctuation_near_duplicate_is_rejected(
    receipt_package: dict[str, object],
) -> None:
    evidence = copy.deepcopy(receipt_package["evidence"])
    first = (
        "编号１：静音段没有发声，烧录字幕cue也没有出现；"
        "实际听到的音频与其他字幕一致。"
    )
    second = (
        "第2项! 静音段没有发声 烧录字幕cue也没有出现 "
        "实际听到的音频与其他字幕都一致"
    )
    evidence["items"][0]["checks"]["subtitle_audio"]["detail"] = first
    evidence["items"][0]["checks"]["silence_hallucination"]["detail"] = second
    _write_json(receipt_package["evidence_path"], evidence)

    with pytest.raises(
        builder.FinalHumanReviewBuildError,
        match="near-duplicate",
    ):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )


@pytest.mark.parametrize(
    ("category", "detail"),
    [
        ("check", "画面颜色清楚且构图稳定，没有记录其他内容。"),
        ("point", "编号３：均无异常。"),
        ("claim", "第４项，没有问题。"),
    ],
)
def test_each_category_requires_structured_specific_actual_observation(
    receipt_package: dict[str, object],
    category: str,
    detail: str,
) -> None:
    evidence = copy.deepcopy(receipt_package["evidence"])
    if category == "check":
        evidence["items"][0]["checks"]["subtitle_audio"]["detail"] = detail
    elif category == "point":
        evidence["items"][0]["subtitle_review_points"][0]["observation"][
            "detail"
        ] = detail
    else:
        evidence["items"][0]["cover_story_claims"][0]["observation"][
            "detail"
        ] = detail
    _write_json(receipt_package["evidence_path"], evidence)

    with pytest.raises(builder.FinalHumanReviewBuildError):
        builder.build_receipt(
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )


def test_create_only_writer_never_overwrites_existing_receipt(
    receipt_package: dict[str, object],
) -> None:
    receipt = builder.build_receipt(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
        evidence_path=receipt_package["evidence_path"],
    )
    output = receipt_package["output"]
    output.write_text("existing evidence\n", encoding="utf-8")

    with pytest.raises(builder.FinalHumanReviewBuildError, match="create-only"):
        builder.write_receipt_create_only(
            output,
            receipt,
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )
    assert output.read_text(encoding="utf-8") == "existing evidence\n"


def test_writer_revalidates_all_evidence_bindings_immediately_before_link(
    receipt_package: dict[str, object],
) -> None:
    receipt = builder.build_receipt(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
        evidence_path=receipt_package["evidence_path"],
    )
    receipt_package["paths"]["video"].write_bytes(b"new final video bytes")

    with pytest.raises(builder.FinalHumanReviewBuildError, match="bindings"):
        builder.write_receipt_create_only(
            receipt_package["output"],
            receipt,
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )
    assert not receipt_package["output"].exists()


def test_create_only_writer_rejects_symlinked_output_parent(
    receipt_package: dict[str, object],
) -> None:
    receipt = builder.build_receipt(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
        evidence_path=receipt_package["evidence_path"],
    )
    real_parent = receipt_package["root"] / "real-parent"
    real_parent.mkdir()
    symlink_parent = receipt_package["root"] / "symlink-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    output = symlink_parent / "receipt.json"

    with pytest.raises(
        builder.FinalHumanReviewBuildError,
        match="non-symlink directory",
    ):
        builder.write_receipt_create_only(
            output,
            receipt,
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )
    assert not (real_parent / "receipt.json").exists()


def test_post_link_fsync_failure_reports_committed_uncertain_not_uncreated(
    receipt_package: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = builder.build_receipt(
        package_root=receipt_package["root"],
        package_audit_path=receipt_package["audit_path"],
        evidence_path=receipt_package["evidence_path"],
    )
    real_fsync = builder.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(builder.os, "fsync", fail_directory_fsync)
    with pytest.raises(
        builder.FinalHumanReviewCommittedDurabilityUnconfirmed,
        match="COMMITTED_BUT_DURABILITY_UNCONFIRMED",
    ):
        builder.write_receipt_create_only(
            receipt_package["output"],
            receipt,
            package_root=receipt_package["root"],
            package_audit_path=receipt_package["audit_path"],
            evidence_path=receipt_package["evidence_path"],
        )
    assert json.loads(receipt_package["output"].read_text(encoding="utf-8"))[
        "items"
    ][0]["candidate_id"] == CANDIDATE_ID


def test_cli_returns_distinct_committed_uncertain_status(
    receipt_package: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_unlink = builder.os.unlink

    def fail_private_temp_cleanup(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if str(path).startswith(".final-human-review.json.tmp."):
            raise OSError(errno.EIO, "injected temp cleanup failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(builder.os, "unlink", fail_private_temp_cleanup)
    arguments = [
        str(receipt_package["root"]),
        "--package-audit",
        str(receipt_package["audit_path"]),
        "--evidence",
        str(receipt_package["evidence_path"]),
        "--out",
        str(receipt_package["output"]),
    ]
    assert builder.main(arguments) == 3
    result = json.loads(capsys.readouterr().err)
    assert result["status"] == "COMMITTED_BUT_DURABILITY_UNCONFIRMED"
    assert receipt_package["output"].is_file()


def test_cli_validates_then_atomically_creates_once(
    receipt_package: dict[str, object],
) -> None:
    arguments = [
        str(receipt_package["root"]),
        "--package-audit",
        str(receipt_package["audit_path"]),
        "--evidence",
        str(receipt_package["evidence_path"]),
        "--out",
        str(receipt_package["output"]),
    ]
    assert builder.main(arguments) == 0
    output = receipt_package["output"]
    first_bytes = output.read_bytes()
    assert json.loads(first_bytes)["items"][0]["candidate_id"] == (CANDIDATE_ID)

    assert builder.main(arguments) == 2
    assert output.read_bytes() == first_bytes


def test_cli_prepares_byte_bound_template_create_only(
    receipt_package: dict[str, object],
) -> None:
    output = (
        receipt_package["root"]
        / "verification"
        / "cli-evidence-template.json"
    )
    arguments = [
        str(receipt_package["root"]),
        "--package-audit",
        str(receipt_package["audit_path"]),
        "--prepare-evidence-template",
        "--out",
        str(output),
    ]
    assert builder.main(arguments) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["bindings"] == (
        receipt_package["evidence"]["bindings"]
    )
    first_bytes = output.read_bytes()
    assert builder.main(arguments) == 2
    assert output.read_bytes() == first_bytes


def test_real_cli_subprocess_smoke_and_canonical_auditor_negative(
    receipt_package: dict[str, object],
) -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "build_lidousha_final_human_review.py"
    )
    help_result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=script.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--prepare-evidence-template" in help_result.stdout

    # This fixture has the complete final-review authority surface, but its
    # tiny fake media and audit do not satisfy the real canonical package
    # auditor.  A subprocess cannot inherit the unit-test monkeypatch, so this
    # exercises the actual CLI import path and fail-closed audit integration.
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(receipt_package["root"]),
            "--package-audit",
            str(receipt_package["audit_path"]),
            "--prepare-evidence-template",
            "--out",
            str(
                receipt_package["root"]
                / "verification"
                / "must-not-be-created.json"
            ),
        ],
        cwd=script.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "current canonical package audit rejects the package" in result.stderr
    assert not (
        receipt_package["root"]
        / "verification"
        / "must-not-be-created.json"
    ).exists()
