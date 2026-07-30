from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.autoslice import final_human_review as fhr
from src.autoslice.final_human_review import (
    ACCEPTED_STATUS,
    FinalHumanReviewError,
    REVIEW_SCOPE,
    SCHEMA_VERSION,
    validate_final_human_review,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

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
CHECK_DETAILS = {
    "final_burned_full_playback": (
        "从00:00开头连续播放到EOS结尾，最后看到双人对话自然停止。"
    ),
    "subtitle_audio": (
        "在对话段实际听到人物发声，烧录字幕随音频逐句出现。"
    ),
    "silence_hallucination": (
        "复看静音段确认无声，画面没有出现对应的幻听字幕cue。"
    ),
    "boundary_closure": (
        "开头保留完整问句，结尾在回答后的自然停顿收束。"
    ),
    "title_story": (
        "最终标题写明争论焦点，完整故事确实先提问再回答。"
    ),
    "cover_identity": (
        "最终封面左侧人物身份与右侧立绘均清楚可辨。"
    ),
    "cover_story": (
        "最终封面文字概括双人关系，叙事与画面故事一致。"
    ),
    "intro_timing": (
        "片头结束后平滑切入正片第一句，衔接没有吞掉发音。"
    ),
}


def test_committed_review_contract_extends_exact_recovery_five_for_daily_repairs():
    recovery_expected = [
        "auto_193450_3573_3665",
        "auto_193450_672_945",
        "auto_193450_1863_2056",
        "auto_193450_1573_1672",
        "auto_193450_1475_1543",
    ]
    review_expected = [
        *recovery_expected,
        "auto_193129_850_940",
        "auto_183122_1209_1410",
        "auto_162016_20_319",
        "auto_183122_607_723",
        "auto_192000_909_1014",
        "auto_195000_1493_1579",
    ]
    review = json.loads(
        (
            REPO_ROOT
            / "assets/lidousha/final_media_review_contracts.v1.json"
        ).read_text(encoding="utf-8")
    )
    cover = json.loads(
        (
            REPO_ROOT
            / "assets/lidousha/cover_reference_overrides.v1.json"
        ).read_text(encoding="utf-8")
    )
    publication = json.loads(
        (
            REPO_ROOT
            / "assets/lidousha/recovery_publication_authority.v1.json"
        ).read_text(encoding="utf-8")
    )

    assert [row["candidate_id"] for row in review["contracts"]] == review_expected
    pink = next(
        row
        for row in review["contracts"]
        if row["candidate_id"] == "auto_162016_20_319"
    )
    pink_close = next(
        point
        for point in pink["subtitle_review_points"]
        if point["point_id"] == "pink-girl-confession-close"
    )
    # The final cue ends at 304.886s after the branded intro, while the
    # verified media duration is 305.320s. Keep the review point in bounds.
    assert pink_close["final_video_end_ms"] == 305_000
    assert [row["candidate_id"] for row in cover["overrides"]] == recovery_expected
    assert [row["candidate_id"] for row in publication["entries"]] == (
        recovery_expected
    )

    confrontation = next(
        row
        for row in review["contracts"]
        if row["candidate_id"] == "auto_193450_672_945"
    )
    points = {
        row["point_id"]: row
        for row in confrontation["subtitle_review_points"]
    }
    assert points["no-wocao-partial-silence"]["expectation"] == (
        "前半段无声、后半段有真实语音，但整段从未说“我草”；"
        "只删除幻听，不得吞掉后半真实话。"
    )
    assert points["silent-hallucinated-l-question"]["expectation"] == (
        "南町nightin 后的整条 L 问句没有说话，必须完全删除，"
        "不得保留“这个L是李豆沙/李乐莎的L吗”。"
    )

    chair = next(
        row
        for row in review["contracts"]
        if row["candidate_id"] == "auto_193450_1573_1672"
    )
    chair_points = {
        row["point_id"]: row
        for row in chair["subtitle_review_points"]
    }
    assert chair_points["fake-cry"]["final_video_end_ms"] == 105_700
    assert chair_points["complete-ending"] == {
        "point_id": "complete-ending",
        "final_video_start_ms": 99_000,
        "final_video_end_ms": 105_700,
        "expectation": (
            "结尾必须完整收束在“假哭”，不能断在话中间，也不得带入随后"
            "感谢钢镚和修鼠标的新 SC 话题。"
        ),
    }

    skill = next(
        row
        for row in review["contracts"]
        if row["candidate_id"] == "auto_193129_850_940"
    )
    skill_points = {
        row["point_id"]: row
        for row in skill["subtitle_review_points"]
    }
    assert skill_points["opening-nonsemantic-vocalization-blank"][
        "final_video_start_ms"
    ] == 10_000
    assert skill_points["ta-yifu-yifu-context"]["expectation"] == (
        "必须是“让我打他，他一副，一副”，保留“他一副……的样子”的"
        "语境搭配，不得写成“欺负欺负”。"
    )
    assert skill_points["kmx-qifu-ren-complete"]["final_video_end_ms"] == (
        88_000
    )

    beans = next(
        row
        for row in review["contracts"]
        if row["candidate_id"] == "auto_183122_1209_1410"
    )
    beans_points = {
        row["point_id"]: row
        for row in beans["subtitle_review_points"]
    }
    assert beans_points["limo-first-repeat"]["expectation"] == (
        "0:36 附近连续两次点名都必须写成专名“礼墨”，不得残留“林墨”"
        "“李默”或其他近音字。"
    )
    assert beans_points["limo-microphone-later"]["final_video_end_ms"] == (
        107_000
    )
    assert beans_points["bean-story-tail-separation"]["final_video_end_ms"] == (
        115_000
    )

    white_dragon = next(
        row
        for row in review["contracts"]
        if row["candidate_id"] == "auto_192000_909_1014"
    )
    white_dragon_points = {
        row["point_id"]: row
        for row in white_dragon["subtitle_review_points"]
    }
    assert white_dragon_points["gift-canon-no-unique-acoustic-truth"]["expectation"] == (
        "开场送礼感谢必须使用操作员裁定的高概率礼物真值“粉丝团灯牌”；"
        "不得把 AGY 声学猜测伪装成人耳唯一真值，也不得回退成钢镚等其他礼物。"
    )
    assert white_dragon_points["white-dragon-horror-ending"]["final_video_end_ms"] == (
        112_500
    )

    team = next(
        row
        for row in review["contracts"]
        if row["candidate_id"] == "auto_195000_1493_1579"
    )
    assert team["subtitle_review_points"] == [
        {
            "point_id": "shadouli-team-name",
            "final_video_start_ms": 0,
            "final_video_end_ms": 59_000,
            "expectation": (
                "整片前59秒所有指向该粉丝队的称呼都必须写成玩梗专名“沙豆李”，"
                "不得残留声学近音“下斗里”或把队名误写成普通短语“杀队友”；随后"
                "发0/2支持李豆沙的对照关系也必须完整。"
            ),
        }
    ]


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _raw_sha256(path: Path) -> str:
    return _sha256(path)[7:]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _review_contract(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "review-contract.json"
    _write_json(
        path,
        {
            "schema_version": (
                "lidousha-final-media-review-contracts.v1"
            ),
            "authority": "test exact review points",
            "contracts": [
                {
                    "candidate_id": candidate_id,
                    "subtitle_review_points": [
                        {
                            "point_id": "corrected-cue",
                            "final_video_start_ms": 13000,
                            "final_video_end_ms": 15000,
                            "expectation": (
                                f"{candidate_id}: corrected cue matches audio"
                            ),
                        }
                    ],
                }
                for candidate_id in ("3573", "672")
            ],
        },
    )
    monkeypatch.setattr(
        fhr, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", path
    )


def _fixture(
    root: Path,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    review_items: list[dict[str, object]] = []
    receipt_items: list[dict[str, object]] = []
    evidence_items: list[dict[str, object]] = []
    for ordinal, candidate_id in enumerate(("3573", "672"), start=1):
        stem = f"clip-{candidate_id}"
        title = f"reviewed title {candidate_id}"
        scene_marker = (
            "蓝色对话框与桌面构图"
            if candidate_id == "3573"
            else "金色角色卡与聊天栏构图"
        )
        paths = {
            "video": f"{stem}.mp4",
            "subtitle": f"{stem}.srt",
            "cover": f"{stem}.cover.png",
            "record": f"{stem}.record.json",
        }
        for artifact in ("video", "subtitle", "cover"):
            (root / paths[artifact]).write_bytes(
                f"{candidate_id}:{artifact}".encode()
            )
        source_claims = [
            f"{candidate_id} source subject is visible",
            f"{candidate_id} source interaction is visible",
        ]
        narrative = (
            f"{candidate_id} story is carried by cover text, not invented pixels"
        )
        authority = {
            "candidate_id": candidate_id,
            "observed_public_title": title,
            "bvid": f"BV{ordinal:010d}",
            "aid": 1000 + ordinal,
            "cid": 2000 + ordinal,
            "authority_sha256": "sha256:" + f"{ordinal:064x}",
        }
        record = {
            "duration_ms": 180_000,
            "burned_preview": {
                "branding_intro": {
                    "verification": {"duration_ms": 186_000}
                }
            },
            "story_contract": {
                "candidate_id": candidate_id,
                "cover_reference_authority": {
                    "source_visible_claims": source_claims,
                    "narrative_presentation": narrative,
                },
            },
            "publish_staging": {"title": title},
            "recovery_publication_authority": authority,
        }
        _write_json(root / paths["record"], record)
        review_items.append(
            {
                "candidate_id": candidate_id,
                "title": title,
                "video": paths["video"],
                "mp4": paths["video"],
                "subtitle_srt": paths["subtitle"],
                "cover": paths["cover"],
                "record": paths["record"],
                "recovery_publication_authority": authority,
            }
        )
        checks = {
            name: {
                "status": "PASS",
                "evidence": (
                    f"{fhr._CHECK_ANCHORS[name]} — "
                    f"{candidate_id}号片的{scene_marker}："
                    f"{CHECK_DETAILS[name]}"
                ),
            }
            for name in CHECK_NAMES
        }
        point_detail = (
            f"{candidate_id}号片的{scene_marker}段实际听到修正台词，"
            "对应烧录字幕cue与发声同步出现。"
        )
        claim_details = (
            (
                f"{candidate_id}号片{scene_marker}的最终封面源帧"
                "左侧人物清楚可见，"
                "面部没有被文字遮挡。"
            ),
            (
                f"{candidate_id}号片{scene_marker}的最终封面画面"
                "右侧人物立绘可见，"
                "与左侧对象保持同框。"
            ),
            (
                f"{candidate_id}号片{scene_marker}的最终封面大字"
                "呈现双方争论故事，"
                "文字叙事与人物关系一致。"
            ),
        )
        receipt_item = {
                "candidate_id": candidate_id,
                "reviewed_title": title,
                "artifacts": {
                    artifact: {
                        "path": paths[artifact],
                        "sha256": _sha256(root / paths[artifact]),
                    }
                    for artifact in ("video", "subtitle", "cover")
                },
                "record": {
                    "path": paths["record"],
                    "sha256": _sha256(root / paths["record"]),
                },
                "publication_target": {
                    "candidate_id": candidate_id,
                    "bvid": authority["bvid"],
                    "aid": authority["aid"],
                    "cid": authority["cid"],
                    "final_title": title,
                    "authority_sha256": authority["authority_sha256"],
                },
                "checks": checks,
                "subtitle_review_points": [
                    {
                        "point_id": "corrected-cue",
                        "final_video_start_ms": 13_000,
                        "final_video_end_ms": 15_000,
                        "expectation": (
                            f"{candidate_id}: corrected cue matches audio"
                        ),
                        "status": "PASS",
                        "evidence": (
                            "00:00:13.000-00:00:15.000 — "
                            + point_detail
                        ),
                    }
                ],
                "cover_story_claims": [
                    *[
                        {
                            "claim": claim,
                            "presentation": "SOURCE_FRAME",
                            "status": "PASS",
                            "evidence": (
                                "FINAL_COVER/SOURCE_FRAME — "
                                + claim_details[claim_index]
                            ),
                        }
                        for claim_index, claim in enumerate(source_claims)
                    ],
                    {
                        "claim": narrative,
                        "presentation": "COVER_TEXT",
                        "status": "PASS",
                        "evidence": (
                            "FINAL_COVER/COVER_TEXT — "
                            + claim_details[2]
                        ),
                    },
                ],
            }
        receipt_items.append(receipt_item)
        evidence_items.append(
            {
                "candidate_id": candidate_id,
                "checks": {
                    name: {
                        "anchor": fhr._CHECK_ANCHORS[name],
                        "detail": (
                            f"{candidate_id}号片的{scene_marker}："
                            f"{CHECK_DETAILS[name]}"
                        ),
                    }
                    for name in CHECK_NAMES
                },
                "subtitle_review_points": [
                    {
                        "point_id": "corrected-cue",
                        "observation": {
                            "anchor": (
                                "00:00:13.000-00:00:15.000"
                            ),
                            "detail": point_detail,
                        },
                    }
                ],
                "cover_story_claims": [
                    *[
                        {
                            "claim": claim,
                            "presentation": "SOURCE_FRAME",
                            "observation": {
                                "anchor": (
                                    "FINAL_COVER/SOURCE_FRAME"
                                ),
                                "detail": claim_details[claim_index],
                            },
                        }
                        for claim_index, claim in enumerate(source_claims)
                    ],
                    {
                        "claim": narrative,
                        "presentation": "COVER_TEXT",
                        "observation": {
                            "anchor": "FINAL_COVER/COVER_TEXT",
                            "detail": claim_details[2],
                        },
                    },
                ],
            }
        )

    candidate_ids = ["3573", "672"]
    manifest: dict[str, object] = {
        "status": (
            "finished_review_package_no_upload_pending_human_review"
        ),
        "upload_allowed": False,
        "exact_candidate_ids": candidate_ids,
        "selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": candidate_ids,
        },
        "items": review_items,
    }
    review_path = root / "review_manifest.json"
    audit_path = root / "verification" / "package-audit.json"
    _write_json(review_path, manifest)
    _write_json(audit_path, {"passed": True, "blocking_issue_count": 0})
    evidence_path = (
        root / "verification" / "final-human-review-evidence.v2.json"
    )
    evidence = {
        "schema_version": fhr.REVIEW_EVIDENCE_SCHEMA_VERSION,
        "bindings": {
            "review_contract_sha256": _sha256(
                fhr.FINAL_MEDIA_REVIEW_CONTRACT_PATH
            ),
            "review_manifest": {
                "path": "review_manifest.json",
                "sha256": _sha256(review_path),
            },
            "package_audit": {
                "path": "verification/package-audit.json",
                "sha256": _sha256(audit_path),
            },
            "items": [
                {
                    "candidate_id": item["candidate_id"],
                    "record": item["record"],
                    "artifacts": item["artifacts"],
                }
                for item in receipt_items
            ],
        },
        "reviewer_kind": "delegated_root_agent",
        "reviewed_by": "Codex root",
        "reviewed_at": "2026-07-23T23:59:00-04:00",
        "approval_quote": (
            "你在自己用修复的流水线过了一遍，自己review并修改后"
            "觉得有信心了之后可以权宜上传"
        ),
        "items": evidence_items,
    }
    _write_json(evidence_path, evidence)
    package_evidence = {
        "review_manifest": {
            "path": "review_manifest.json",
            "sha256": _sha256(review_path),
        },
        "package_audit": {
            "path": "verification/package-audit.json",
            "sha256": _sha256(audit_path),
        },
        "review_evidence": {
            "path": "verification/final-human-review-evidence.v2.json",
            "sha256": _sha256(evidence_path),
            "bytes": evidence_path.stat().st_size,
        },
    }
    attestation: dict[str, object] = {
        "package_root": str(root.resolve()),
        "review_manifest": {
            "path": str(review_path.resolve()),
            "sha256": _raw_sha256(review_path),
            "bytes": review_path.stat().st_size,
        },
        "package_audit": {
            "path": str(audit_path.resolve()),
            "sha256": _raw_sha256(audit_path),
            "bytes": audit_path.stat().st_size,
        },
    }
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "scope": REVIEW_SCOPE,
        "status": ACCEPTED_STATUS,
        "reviewer_kind": "delegated_root_agent",
        "reviewed_by": "Codex root",
        "reviewed_at": "2026-07-23T23:59:00-04:00",
        "approval_quote": (
            "你在自己用修复的流水线过了一遍，自己review并修改后"
            "觉得有信心了之后可以权宜上传"
        ),
        "review_contract_sha256": _sha256(
            fhr.FINAL_MEDIA_REVIEW_CONTRACT_PATH
        ),
        "package_evidence": package_evidence,
        "items": receipt_items,
    }
    return manifest, receipt, attestation


def _assert_reason(
    root: Path,
    receipt: object,
    manifest: object,
    attestation: object,
    reason_code: str,
) -> None:
    with pytest.raises(FinalHumanReviewError) as caught:
        validate_final_human_review(
            receipt, root, manifest, attestation
        )
    assert caught.value.reason_code == reason_code


def _rebind_review(
    root: Path,
    manifest: dict[str, object],
    receipt: dict[str, object],
    attestation: dict[str, object],
) -> None:
    review_path = root / "review_manifest.json"
    _write_json(review_path, manifest)
    receipt["package_evidence"]["review_manifest"]["sha256"] = _sha256(
        review_path
    )
    attestation["review_manifest"]["sha256"] = _raw_sha256(
        review_path
    )
    attestation["review_manifest"]["bytes"] = review_path.stat().st_size


def _rebind_evidence_metadata(
    root: Path,
    receipt: dict[str, object],
) -> None:
    evidence_path = (
        root / "verification" / "final-human-review-evidence.v2.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    for field in (
        "reviewer_kind",
        "reviewed_by",
        "reviewed_at",
        "approval_quote",
    ):
        evidence[field] = receipt[field]
    _write_json(evidence_path, evidence)
    binding = receipt["package_evidence"]["review_evidence"]
    binding["sha256"] = _sha256(evidence_path)
    binding["bytes"] = evidence_path.stat().st_size


def test_failed_machine_audit_cannot_be_signed_by_perceptual_receipt(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    audit_path = tmp_path / "verification" / "package-audit.json"
    _write_json(
        audit_path,
        {"passed": False, "blocking_issue_count": 1},
    )
    receipt["package_evidence"]["package_audit"]["sha256"] = _sha256(
        audit_path
    )
    attestation["package_audit"]["sha256"] = _raw_sha256(audit_path)
    attestation["package_audit"]["bytes"] = audit_path.stat().st_size

    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_PACKAGE_AUDIT_NOT_PASS",
    )


def test_validates_complete_receipt_in_manifest_order(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"] = list(reversed(receipt["items"]))

    normalized = validate_final_human_review(
        receipt, tmp_path, manifest, attestation
    )

    assert [
        item["candidate_id"] for item in normalized["items"]
    ] == ["3573", "672"]
    assert normalized["reviewer_kind"] == "delegated_root_agent"
    assert normalized["items"][0]["reviewed_title"] == (
        "reviewed title 3573"
    )
    assert normalized["items"][0]["checks"]["subtitle_audio"][
        "status"
    ] == "PASS"


def test_old_v1_receipt_and_unbound_evidence_cannot_authorize(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["schema_version"] = "lidousha-final-human-review.v1"
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_SCHEMA_INVALID",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["package_evidence"].pop("review_evidence")
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_PACKAGE_EVIDENCE_FIELDS_INVALID",
    )


def test_bound_evidence_bytes_and_non_generic_observations_are_required(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    evidence_path = (
        tmp_path / "verification" / "final-human-review-evidence.v2.json"
    )
    with evidence_path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_REVIEW_EVIDENCE_BINDING_MISMATCH",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    generic = "编号１：均无异常。"
    evidence["items"][0]["checks"]["subtitle_audio"]["detail"] = generic
    _write_json(evidence_path, evidence)
    receipt["package_evidence"]["review_evidence"].update(
        {
            "sha256": _sha256(evidence_path),
            "bytes": evidence_path.stat().st_size,
        }
    )
    receipt["items"][0]["checks"]["subtitle_audio"]["evidence"] = (
        f"{fhr._CHECK_ANCHORS['subtitle_audio']} — {generic}"
    )
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_OBSERVATION_INVALID",
    )


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        (
            "schema_version",
            "lidousha-final-human-review.v0",
            "FINAL_HUMAN_REVIEW_SCHEMA_INVALID",
        ),
        ("scope", "new_upload", "FINAL_HUMAN_REVIEW_SCOPE_INVALID"),
        ("status", "PASS", "FINAL_HUMAN_REVIEW_STATUS_NOT_ACCEPTED"),
        (
            "reviewer_kind",
            "Ivan",
            "FINAL_HUMAN_REVIEW_REVIEWER_KIND_INVALID",
        ),
        (
            "reviewer_kind",
            [],
            "FINAL_HUMAN_REVIEW_REVIEWER_KIND_INVALID",
        ),
        (
            "reviewed_by",
            "Ivan",
            "FINAL_HUMAN_REVIEW_REVIEWER_IDENTITY_MISMATCH",
        ),
        (
            "reviewed_at",
            "2026-07-23T23:59:00",
            "FINAL_HUMAN_REVIEW_REVIEWED_AT_INVALID",
        ),
    ],
)
def test_rejects_top_level_authority_drift(
    tmp_path: Path,
    field: str,
    value: object,
    reason_code: str,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt[field] = value
    _assert_reason(
        tmp_path, receipt, manifest, attestation, reason_code
    )


@pytest.mark.parametrize(
    ("reviewer_kind", "reviewed_by"),
    [
        ("human_owner", "Codex root"),
        ("human_delegate", "Ivan"),
        ("human_delegate", "ivan"),
        ("human_delegate", "IVAN"),
        ("human_delegate", "Ｉｖａｎ"),
        ("human_delegate", "Ivan本人"),
        ("human_delegate", "Codex root"),
        ("human_delegate", "Codex Root"),
        ("human_delegate", "Codex   root"),
        ("human_delegate", "Codex root agent"),
    ],
)
def test_reviewer_kind_cannot_mislabel_reserved_identity(
    tmp_path: Path,
    reviewer_kind: str,
    reviewed_by: str,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["reviewer_kind"] = reviewer_kind
    receipt["reviewed_by"] = reviewed_by

    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_REVIEWER_IDENTITY_MISMATCH",
    )


@pytest.mark.parametrize(
    ("reviewer_kind", "reviewed_by"),
    [
        ("human_owner", "Ivan"),
        ("human_delegate", "Alice"),
    ],
)
def test_truthful_human_reviewer_identities_remain_valid(
    tmp_path: Path,
    reviewer_kind: str,
    reviewed_by: str,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["reviewer_kind"] = reviewer_kind
    receipt["reviewed_by"] = reviewed_by
    _rebind_evidence_metadata(tmp_path, receipt)

    normalized = validate_final_human_review(
        receipt, tmp_path, manifest, attestation
    )
    assert normalized["reviewer_kind"] == reviewer_kind
    assert normalized["reviewed_by"] == reviewed_by


def test_requires_exact_candidate_declarations_and_pending_state(
    tmp_path: Path,
) -> None:
    for mutation in (
        lambda manifest: manifest.pop("exact_candidate_ids"),
        lambda manifest: manifest.pop("selection_contract"),
        lambda manifest: manifest["selection_contract"].update(
            {"mode": "TOP_N"}
        ),
        lambda manifest: manifest.update({"upload_allowed": True}),
    ):
        manifest, receipt, attestation = _fixture(tmp_path)
        mutation(manifest)
        _rebind_review(tmp_path, manifest, receipt, attestation)
        _assert_reason(
            tmp_path,
            receipt,
            manifest,
            attestation,
            (
                "FINAL_HUMAN_REVIEW_MANIFEST_STATE_INVALID"
                if manifest.get("upload_allowed") is True
                else "FINAL_HUMAN_REVIEW_MANIFEST_CANDIDATE_SET_INVALID"
            ),
        )


def test_rejects_missing_extra_and_duplicate_candidates(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"].pop()
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_CANDIDATE_SET_MISMATCH",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"].append(copy.deepcopy(receipt["items"][0]))
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_DUPLICATE_CANDIDATE",
    )


def test_rejects_title_reuse_after_package_is_rebound(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    item = manifest["items"][0]
    item["title"] = "attacker changed title"
    item["recovery_publication_authority"]["observed_public_title"] = (
        "attacker changed title"
    )
    record_path = tmp_path / item["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["publish_staging"]["title"] = "attacker changed title"
    record["recovery_publication_authority"] = item[
        "recovery_publication_authority"
    ]
    _write_json(record_path, record)
    receipt["items"][0]["record"]["sha256"] = _sha256(record_path)
    _rebind_review(tmp_path, manifest, receipt, attestation)

    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_TITLE_MISMATCH",
    )


def test_rejects_cross_bv_receipt_reuse_after_rebinding(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    item = manifest["items"][0]
    item["recovery_publication_authority"]["bvid"] = "BV9999999999"
    record_path = tmp_path / item["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["recovery_publication_authority"] = item[
        "recovery_publication_authority"
    ]
    _write_json(record_path, record)
    receipt["items"][0]["record"]["sha256"] = _sha256(record_path)
    _rebind_review(tmp_path, manifest, receipt, attestation)

    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_PUBLICATION_TARGET_MISMATCH",
    )


@pytest.mark.parametrize("artifact", ["video", "subtitle", "cover"])
def test_rejects_artifact_path_hash_or_byte_drift(
    tmp_path: Path, artifact: str
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"][0]["artifacts"][artifact]["path"] = "other.bin"
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"][0]["artifacts"][artifact]["sha256"] = (
        "sha256:" + "0" * 64
    )
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_FILE_HASH_MISMATCH",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    path = tmp_path / receipt["items"][0]["artifacts"][artifact]["path"]
    path.write_bytes(b"drift")
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_FILE_HASH_MISMATCH",
    )


def test_rejects_symlinked_artifact_and_parent(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    video = tmp_path / "clip-3573.mp4"
    target = tmp_path / "actual.mp4"
    target.write_bytes(video.read_bytes())
    video.unlink()
    video.symlink_to(target.name)
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_video = nested / "video.mp4"
    nested_video.write_bytes(b"nested")
    os.symlink("nested", tmp_path / "alias")
    manifest["items"][0]["video"] = "alias/video.mp4"
    manifest["items"][0]["mp4"] = "alias/video.mp4"
    receipt["items"][0]["artifacts"]["video"] = {
        "path": "alias/video.mp4",
        "sha256": _sha256(nested_video),
    }
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_MANIFEST_OBJECT_MISMATCH",
    )


def test_rejects_package_evidence_or_attestation_drift(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["package_evidence"]["package_audit"]["sha256"] = (
        "sha256:" + "0" * 64
    )
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_FILE_HASH_MISMATCH",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    attestation["package_audit"]["sha256"] = "0" * 64
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_PACKAGE_ATTESTATION_MISMATCH",
    )


@pytest.mark.parametrize("check", CHECK_NAMES)
def test_every_check_requires_pass_and_specific_evidence(
    tmp_path: Path, check: str
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"][0]["checks"][check]["status"] = "BLOCK"
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_CHECK_NOT_PASS",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"][0]["checks"][check]["evidence"] = ""
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_CHECK_EVIDENCE_INVALID",
    )


def test_subtitle_review_points_are_mandatory_and_bounded(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"][0]["subtitle_review_points"] = []
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_SUBTITLE_POINTS_INVALID",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    point = receipt["items"][0]["subtitle_review_points"][0]
    point["final_video_end_ms"] = 999_999
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_INVALID",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    point = receipt["items"][0]["subtitle_review_points"][0]
    point["expectation"] = "generic point substituted by reviewer"
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_INVALID",
    )


def test_rejects_review_contract_hash_drift(tmp_path: Path) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["review_contract_sha256"] = "sha256:" + "0" * 64
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_CONTRACT_HASH_MISMATCH",
    )


def test_cover_claims_must_exactly_match_hash_bound_story_contract(
    tmp_path: Path,
) -> None:
    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"][0]["cover_story_claims"] = [
        {
            "claim": "generic story hook",
            "presentation": "COVER_TEXT",
            "status": "PASS",
            "evidence": "generic",
        }
    ]
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIM_SET_MISMATCH",
    )

    manifest, receipt, attestation = _fixture(tmp_path)
    receipt["items"][0]["cover_story_claims"][0][
        "presentation"
    ] = "COVER_TEXT"
    _assert_reason(
        tmp_path,
        receipt,
        manifest,
        attestation,
        "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIM_SET_MISMATCH",
    )
