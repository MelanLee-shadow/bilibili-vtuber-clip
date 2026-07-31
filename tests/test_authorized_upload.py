"""Manifest-bound upload channel (2026-07-09 audit P0-3): authorization must be
cryptographically tied to the exact reviewed artifacts, uploads must be
idempotent, and the uploader can only receive manifest args — never hand-typed."""
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import scripts.authorized_upload as au
from src.autoslice import final_human_review as fhr

TEST_TAGS = ["李豆沙", "虚拟主播", "直播切片"]
VALID_TITLE = "【李豆沙】这是一个足够长度的测试标题"
VALID_SONG_TITLE = "【李豆沙】豆沙歌，《海海海》"
RECOVERY_BVID = "BV1Mug46EEQz"
TEST_PUBLICATION_AUTHORITY = {
    "schema_version": "test-recovery-publication-authority.v1",
    "candidate_id": "candidate-test",
    "bvid": RECOVERY_BVID,
    "aid": 42,
    "cid": 101,
    "title_mode": "preserve_verified_public",
    "observed_public_title": VALID_TITLE,
    "authority_sha256": "sha256:" + "1" * 64,
}


@pytest.fixture(autouse=True)
def _isolated_default_upload_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "DEFAULT_UPLOAD_LOCK", tmp_path / "default-upload.lock")


@pytest.fixture(autouse=True)
def _final_media_review_contract(tmp_path, monkeypatch):
    contract = tmp_path / "final-media-review-contracts.json"
    contract.write_text(
        json.dumps(
            {
                "schema_version": (
                    "lidousha-final-media-review-contracts.v1"
                ),
                "authority": "authorized_upload candidate-test fixture",
                "contracts": [
                    {
                        "candidate_id": "candidate-test",
                        "subtitle_review_points": [
                            {
                                "point_id": "corrected-cue",
                                "final_video_start_ms": 13_000,
                                "final_video_end_ms": 15_000,
                                "expectation": (
                                    "纠错点与最终音频一致"
                                ),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        fhr, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract
    )


@pytest.fixture(autouse=True)
def _stub_season_http(monkeypatch):
    """Canned always-successful season API so pre-existing upload tests keep
    exercising the manifest/ledger/uploader contract; season-specific behavior
    is covered in test_authorized_upload_season.py."""

    def fake_build(cookie_json):
        def http(url, data=None, is_json=False):
            if "web-interface/view" in url:
                return {
                    "code": 0,
                    "data": {
                        "state": 0,
                        "aid": 111,
                        "cid": 222,
                        "title": "t",
                        "is_season_display": True,
                        "ugc_season": {"id": 8383206, "title": "小李切片"},
                    },
                }
            if "x/web/archives" in url:
                return {"code": 0, "data": {"arc_audits": [], "page": {"count": 0}}}
            if "web/seasons" in url:
                return {
                    "code": 0,
                    "data": {
                        "seasons": [
                            {
                                "season": {"id": 8383206, "title": "小李切片"},
                                "sections": {"sections": [{"id": 9320779, "title": "正片"}]},
                            },
                            {
                                "season": {"id": 8410735, "title": "小李歌唱"},
                                "sections": {"sections": [{"id": 9364628, "title": "正片"}]},
                            },
                        ]
                    },
                }
            if "episodes/add" in url:
                return {"code": 0, "message": "0"}
            if "tag/archive/tags" in url:
                return {"code": 0, "data": [{"tag_name": "李豆沙"}]}
            raise AssertionError(f"unexpected url {url}")

        return http, "csrf-test"

    monkeypatch.setattr(au, "_build_season_http", fake_build)
    monkeypatch.setattr(
        au,
        "public_verify_flow",
        lambda manifest, bvid, **kwargs: {
            "schema_version": "authorized-upload-public-verify.v2",
            "status": "VERIFIED_PUBLIC",
            "bvid": bvid,
            "public_view": {"aid": 111, "cid": 222},
            "verified_at": au.now(),
            "problems": [],
        },
    )
    monkeypatch.setattr(
        au,
        "_reconcile_new_bv_publication",
        lambda **kwargs: {
            "candidate_id": "candidate-test",
            "bvid": kwargs["bvid"],
        },
    )
    monkeypatch.setattr(
        au,
        "_reconcile_same_bv_publication",
        lambda **_kwargs: {
            "candidate_id": "candidate-test",
            "bvid": RECOVERY_BVID,
        },
    )


def _write_v3_package(
    video,
    cover,
    title,
    tags=None,
    *,
    recovery_publication_authority=None,
):
    tags = list(TEST_TAGS if tags is None else tags)
    stem = video.stem
    subtitle = video.parent / f"{stem}.srt"
    record = video.parent / f"{stem}.record.json"
    review = video.parent / "review_manifest.json"
    audit = video.parent / f"{stem}.package_audit.json"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
    record_payload = {
                "artifact_hashes": {
                    "burned_video_sha256": "sha256:" + au.sha256_file(video),
                    "cover_sha256": "sha256:" + au.sha256_file(cover),
                    "delivery_subtitle_sha256": "sha256:" + au.sha256_file(subtitle),
                },
                "publish_staging": {"title": title},
                "story_contract": {
                    "schema_version": "lidousha-story-contract.v1",
                    "candidate_id": "candidate-test",
                    "transcript_sha256": "sha256:"
                    + au.hashlib.sha256("测试".encode("utf-8")).hexdigest(),
                    "cover_reference_authority": {
                        "source_visible_claims": [
                            "李豆沙与联动对象都在源帧中可见"
                        ],
                        "narrative_presentation": (
                            "关系叙事由封面文字与版式表达"
                        ),
                    },
                },
                "duration_ms": 120_000,
                "burned_preview": {
                    "branding_intro": {
                        "verification": {"duration_ms": 126_000}
                    }
                },
                "cover_generation": {
                    "workflow": "test-image-cover",
                    "method": "images.edit",
                    "model": "test-image-model",
                    "attempted_models": ["test-image-model"],
                    "ai_background": str(cover),
                    "ai_background_sha256": "sha256:" + au.sha256_file(cover),
                    "final_cover": str(cover),
                    "final_cover_sha256": "sha256:" + au.sha256_file(cover),
                    "fallback_used": False,
                },
                "upload_tags": {
                    "engine": "test",
                    "status": "OK",
                    "final_tags": tags,
                },
            }
    if recovery_publication_authority is not None:
        record_payload["recovery_publication_authority"] = (
            recovery_publication_authority
        )
    record.write_text(
        json.dumps(record_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    review_item = {
        "stem": stem,
        "candidate_id": "candidate-test",
        "media": video.name,
        "video": video.name,
        "cover": cover.name,
        "record": record.name,
        "subtitle_srt": subtitle.name,
        "title": title,
        **(
            {
                "classification": "song",
                "lyrics_alignment_report": "test-fixture",
            }
            if title.startswith(au.SONG_TITLE_PREFIX)
            else {}
        ),
    }
    if recovery_publication_authority is not None:
        review_item["recovery_publication_authority"] = (
            recovery_publication_authority
        )
    review.write_text(
        json.dumps(
            {
                **(
                    {
                        "status": (
                            "finished_review_package_no_upload_pending_human_review"
                        ),
                        "upload_allowed": False,
                        "exact_candidate_ids": ["candidate-test"],
                        "selection_contract": {
                            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
                            "candidate_ids": ["candidate-test"],
                        },
                    }
                    if recovery_publication_authority is not None
                    else {}
                ),
                "items": [review_item]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audit.write_text(
        json.dumps(au.audit_package(video.parent), ensure_ascii=False),
        encoding="utf-8",
    )
    return audit


def _write_title_cover_qc(
    cover,
    title,
    *,
    candidate_id="candidate-test",
):
    verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "physical_text_line_count": 2,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "unrelated_or_misleading_elements": [],
        "pass": True,
        "reason": "李豆沙是清晰主体，两行单一钩子与标题一致。",
    }
    receipt = cover.parent / f"{cover.stem}.title-cover-joint-qc.json"
    cover_sha = au.sha256_file(cover)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": au.TITLE_COVER_QC_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "title": title,
                "title_sha256": "sha256:" + au._sha256_text(title),
                "cover_path": str(cover.resolve()),
                "cover_sha256": "sha256:" + cover_sha,
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "witness": {
                    "schema_version": (
                        au.TITLE_COVER_QC_WITNESS_SCHEMA_VERSION
                    ),
                    "image_path": str(cover.resolve()),
                    "model": "gpt-test-cpa",
                    "provider": "cpa",
                    "image_sha256": cover_sha,
                    "status": "OBSERVED",
                    "answer": json.dumps(
                        verdict, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                "verdict": verdict,
                "status": "PASS",
                "pass": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return receipt


def _write_verified_song_package_without_story_contract(
    root,
):
    root.mkdir(parents=True, exist_ok=True)
    stem = "verified-song"
    paths = {
        "video": root / f"{stem}.mp4",
        "subtitle": root / f"{stem}.srt",
        "record": root / f"{stem}.record.json",
        "publish": root / f"{stem}.publish.json",
        "cover": root / f"{stem}.cover.png",
        "cover_title_mask": root / f"{stem}.cover.title-mask.png",
        "cover_pre_overlay": root / f"{stem}.cover.pre-overlay.png",
        "cover_route_background": (
            root / f"{stem}.cover.route-background.png"
        ),
        "lyrics_alignment_report": (
            root / f"{stem}.lyrics-alignment-report.json"
        ),
        "host_vocal_proof": root / f"{stem}.host-vocal-proof.json",
        "recut_manifest": root / f"{stem}.recut.manifest.json",
        "delivery_manifest": root / f"{stem}.delivery.manifest.json",
    }
    paths["video"].write_bytes(b"verified-song-video")
    paths["subtitle"].write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n不能停止我对你的爱\n",
        encoding="utf-8",
    )
    for role in (
        "cover",
        "cover_title_mask",
        "cover_pre_overlay",
        "cover_route_background",
    ):
        paths[role].write_bytes(role.encode("utf-8"))
    paths["lyrics_alignment_report"].write_text(
        json.dumps(
            {"schema_version": "lyrics-alignment-report.v1"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    paths["host_vocal_proof"].write_text(
        json.dumps(
            {
                "schema_version": "host-vocal-proof.v3",
                "status": "READY",
                "decision": (
                    "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    paths["recut_manifest"].write_text(
        json.dumps(
            {
                "schema_version": "materialized-recut.v2",
                "status": "MATERIALIZED",
                "subtitle_source": "external_lrc_global_shift",
                "reason_codes": [],
                "verified_output_binding": {
                    "schema_version": (
                        "verified-song-output-binding.v1"
                    ),
                    "artifacts": {
                        "burned_media_sha256": (
                            "sha256:" + au.sha256_file(paths["video"])
                        ),
                        "subtitle_sha256": (
                            "sha256:" + au.sha256_file(paths["subtitle"])
                        ),
                    },
                    "proofs": {
                        "lyrics_alignment_report_sha256": (
                            "sha256:"
                            + au.sha256_file(
                                paths["lyrics_alignment_report"]
                            )
                        ),
                        "host_vocal_proof_sha256": (
                            "sha256:"
                            + au.sha256_file(paths["host_vocal_proof"])
                        ),
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_hashes = {
        "burned_video_sha256": "sha256:"
        + au.sha256_file(paths["video"]),
        "cover_sha256": "sha256:" + au.sha256_file(paths["cover"]),
        "subtitle_sha256": "sha256:"
        + au.sha256_file(paths["subtitle"]),
    }
    tags = ["李豆沙", "虚拟主播", "直播切片", "唱歌"]
    record = {
        "delivery_candidate_id": "song_verified",
        "artifact_hashes": artifact_hashes,
        "publish_staging": {"title": VALID_SONG_TITLE},
        "upload_tags": {
            "engine": "suggest-upload-tags.v1",
            "status": "OK_NO_LLM",
            "final_tags": tags,
        },
    }
    paths["record"].write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    paths["publish"].write_text(
        json.dumps(
            {
                "schema_version": "shadow-publish-draft.v1",
                "title": VALID_SONG_TITLE,
                "upload_enabled": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    delivery_roles = {
        role: {
            "path": str(path),
            "sha256": "sha256:" + au.sha256_file(path),
            "source_path": str(path),
            "source_sha256": "sha256:" + au.sha256_file(path),
        }
        for role, path in paths.items()
        if role != "delivery_manifest"
    }
    delivery_roles["active_record"] = delivery_roles.pop("record")
    paths["delivery_manifest"].write_text(
        json.dumps(
            {
                "schema_version": "verified-song-delivery.v1",
                "status": "DELIVERED_NO_UPLOAD",
                "candidate_id": "song_verified",
                "upload_enabled": False,
                "artifacts": delivery_roles,
                "absent_artifacts": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = {
        "candidate_id": "song_verified",
        "stem": stem,
        "kind": "song",
        "classification": "Song",
        "title": VALID_SONG_TITLE,
        "video": paths["video"].name,
        "subtitle_srt": paths["subtitle"].name,
        "record": paths["record"].name,
        "publish_json": paths["publish"].name,
        "cover": paths["cover"].name,
        "cover_title_mask": paths["cover_title_mask"].name,
        "cover_pre_overlay": paths["cover_pre_overlay"].name,
        "cover_route_background": paths[
            "cover_route_background"
        ].name,
        "lyrics_alignment_report": paths[
            "lyrics_alignment_report"
        ].name,
        "host_vocal_proof": paths["host_vocal_proof"].name,
        "recut_manifest": paths["recut_manifest"].name,
        "delivery_manifest": paths["delivery_manifest"].name,
        "sha256": {
            (
                "active_record" if role == "record" else role
            ): "sha256:" + au.sha256_file(path)
            for role, path in paths.items()
            if role != "delivery_manifest"
        },
    }
    review = {
        "schema_version": "lidousha-song-review-manifest.v1",
        "generated_by": "build_lidousha_song_review_manifest.v1",
        "status": (
            "finished_review_package_no_upload_pending_human_review"
        ),
        "candidate_id": "song_verified",
        "classification": "Song",
        "story_contract_required": False,
        "run_mode": "PRODUCTION_REVIEW",
        "upload_allowed": False,
        "delivery_authority": {
            "schema_version": "verified-song-delivery.v1",
            "manifest": paths["delivery_manifest"].name,
            "manifest_sha256": "sha256:"
            + au.sha256_file(paths["delivery_manifest"]),
            "song_completion_evidence": {
                "ready": True,
                "reason_codes": [],
                "song_boundary_status": "FULL_SONG_READY",
                "lyrics_alignment_status": "READY",
                "host_vocal_status": "READY",
                "live_performance_status": "READY",
                "live_performance_mode": "LIVE_STREAMER_SINGING",
                "joint_singing_decision": (
                    "VERIFIED_LIDOUSHA_SINGING"
                ),
                "subtitle_source": "external_lrc_global_shift",
                "burned_preview_sha256": (
                    "sha256:" + au.sha256_file(paths["video"])
                ),
                "alignment_report_sha256": (
                    "sha256:"
                    + au.sha256_file(paths["lyrics_alignment_report"])
                ),
                "host_vocal_proof_sha256": (
                    "sha256:"
                    + au.sha256_file(paths["host_vocal_proof"])
                ),
                "recut_manifest_sha256": (
                    "sha256:"
                    + au.sha256_file(paths["recut_manifest"])
                ),
            },
            "upload_enabled": False,
        },
        "items": [item],
    }
    review_path = root / "review_manifest.json"
    review_path.write_text(
        json.dumps(review, ensure_ascii=False), encoding="utf-8"
    )
    audit_payload = _passing_package_audit(root)
    audit_payload["audited_inputs"] = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": au.sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in [review_path, *paths.values()]
    ]
    audit_path = root / f"{stem}.package_audit.json"
    audit_path.write_text(
        json.dumps(audit_payload, ensure_ascii=False), encoding="utf-8"
    )
    return paths, audit_path, audit_payload, tags


def _write_final_human_review(
    video,
    package_audit,
    *,
    title=VALID_TITLE,
    publication_authority=TEST_PUBLICATION_AUTHORITY,
):
    stem = video.stem
    subtitle = video.parent / f"{stem}.srt"
    cover = video.parent / f"{stem}.cover.png"
    record = video.parent / f"{stem}.record.json"
    review_manifest = video.parent / "review_manifest.json"
    receipt = video.parent / "final_human_review.json"
    evidence_path = (
        video.parent / "final-human-review-evidence.v2.json"
    )
    reviewer = {
        "reviewer_kind": "delegated_root_agent",
        "reviewed_by": "Codex root",
        "reviewed_at": "2026-07-23T23:30:00-04:00",
        "approval_quote": "终片完整看过，可以同 BV 修复",
    }
    artifact_bindings = {
        "video": {
            "path": video.name,
            "sha256": "sha256:" + au.sha256_file(video),
        },
        "subtitle": {
            "path": subtitle.name,
            "sha256": "sha256:" + au.sha256_file(subtitle),
        },
        "cover": {
            "path": cover.name,
            "sha256": "sha256:" + au.sha256_file(cover),
        },
    }
    record_binding = {
        "path": record.name,
        "sha256": "sha256:" + au.sha256_file(record),
    }
    check_details = {
        "final_burned_full_playback": (
            "从00:00开头连续播放到EOS结尾，最后一帧停在双人对话收束。"
        ),
        "subtitle_audio": (
            "在00:13实际听到纠错台词，烧录字幕随音频发声同步出现。"
        ),
        "silence_hallucination": (
            "复看静音段确认无声，画面没有多出幻听字幕cue。"
        ),
        "boundary_closure": (
            "开头保留完整问句，结尾落在回答后的自然停顿。"
        ),
        "title_story": (
            "最终标题写明测试争论，完整故事先提问再回答。"
        ),
        "cover_identity": (
            "最终封面左侧人物身份与右侧联动立绘均清楚可辨。"
        ),
        "cover_story": (
            "最终封面文字概括双人关系，叙事与画面故事一致。"
        ),
        "intro_timing": (
            "片头结束后平滑切入正片第一句，衔接没有吞字。"
        ),
    }
    point_detail = (
        "在00:13到00:15实际听到纠错台词，烧录字幕cue与发声同步。"
    )
    claims = (
        ("李豆沙与联动对象都在源帧中可见", "SOURCE_FRAME"),
        ("关系叙事由封面文字与版式表达", "COVER_TEXT"),
    )
    claim_details = (
        "最终封面源帧左侧和右侧人物均清楚可见，身份没有被大字遮挡。",
        "最终封面大字呈现双人关系故事，文字叙事与版式内容一致。",
    )
    contract_sha256 = (
        "sha256:"
        + au.sha256_file(fhr.FINAL_MEDIA_REVIEW_CONTRACT_PATH)
    )
    evidence = {
        "schema_version": fhr.REVIEW_EVIDENCE_SCHEMA_VERSION,
        "bindings": {
            "review_contract_sha256": contract_sha256,
            "review_manifest": {
                "path": review_manifest.name,
                "sha256": "sha256:"
                + au.sha256_file(review_manifest),
            },
            "package_audit": {
                "path": package_audit.name,
                "sha256": "sha256:"
                + au.sha256_file(package_audit),
            },
            "items": [
                {
                    "candidate_id": "candidate-test",
                    "record": record_binding,
                    "artifacts": artifact_bindings,
                }
            ],
        },
        **reviewer,
        "items": [
            {
                "candidate_id": "candidate-test",
                "checks": {
                    check: {
                        "anchor": fhr._CHECK_ANCHORS[check],
                        "detail": detail,
                    }
                    for check, detail in check_details.items()
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
                    {
                        "claim": claim,
                        "presentation": presentation,
                        "observation": {
                            "anchor": (
                                f"FINAL_COVER/{presentation}"
                            ),
                            "detail": detail,
                        },
                    }
                    for (claim, presentation), detail in zip(
                        claims, claim_details, strict=True
                    )
                ],
            }
        ],
    }
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False),
        encoding="utf-8",
    )
    receipt.write_text(
        json.dumps(
            {
                "schema_version": fhr.SCHEMA_VERSION,
                "scope": "same_bv_repair",
                "status": "ACCEPTED_FOR_SAME_BV",
                **reviewer,
                "review_contract_sha256": contract_sha256,
                "package_evidence": {
                    "review_manifest": {
                        "path": review_manifest.name,
                        "sha256": "sha256:"
                        + au.sha256_file(review_manifest),
                    },
                    "package_audit": {
                        "path": package_audit.name,
                        "sha256": "sha256:"
                        + au.sha256_file(package_audit),
                    },
                    "review_evidence": {
                        "path": evidence_path.name,
                        "sha256": "sha256:"
                        + au.sha256_file(evidence_path),
                        "bytes": evidence_path.stat().st_size,
                    },
                },
                "items": [
                    {
                        "candidate_id": "candidate-test",
                        "reviewed_title": title,
                        "artifacts": artifact_bindings,
                        "record": record_binding,
                        "publication_target": {
                            "candidate_id": "candidate-test",
                            "bvid": publication_authority["bvid"],
                            "aid": publication_authority["aid"],
                            "cid": publication_authority["cid"],
                            "final_title": title,
                            "authority_sha256": publication_authority[
                                "authority_sha256"
                            ],
                        },
                        "checks": {
                            check: {
                                "status": "PASS",
                                "evidence": (
                                    f"{fhr._CHECK_ANCHORS[check]} — "
                                    f"{detail}"
                                ),
                            }
                            for check, detail in check_details.items()
                        },
                        "subtitle_review_points": [
                            {
                                "point_id": "corrected-cue",
                                "final_video_start_ms": 13_000,
                                "final_video_end_ms": 15_000,
                                "expectation": "纠错点与最终音频一致",
                                "status": "PASS",
                                "evidence": (
                                    "00:00:13.000-00:00:15.000 — "
                                    + point_detail
                                ),
                            }
                        ],
                        "cover_story_claims": [
                            {
                                "claim": claim,
                                "presentation": presentation,
                                "status": "PASS",
                                "evidence": (
                                    f"FINAL_COVER/{presentation} — "
                                    f"{detail}"
                                ),
                            }
                            for (claim, presentation), detail in zip(
                                claims,
                                claim_details,
                                strict=True,
                            )
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return receipt


def _passing_package_audit(root):
    return {
        "schema_version": au.AUDIT_SCHEMA_VERSION,
        "policy_epoch": au.AUDIT_POLICY_EPOCH,
        "policy_fingerprint": "test-policy",
        "auditor_source_sha256": "test-auditor",
        "passed": True,
        "root": str(root.resolve()),
        "audited_inputs": [],
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }


def _mk(tmp_path, title=VALID_TITLE, quote="可以上传了"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    video = tmp_path / "clip.mp4"
    cover = tmp_path / "clip.cover.png"
    video.write_bytes(b"fake-video-bytes")
    cover.write_bytes(b"fake-cover-bytes")
    audit = _write_v3_package(video, cover, title)
    title_cover_qc = _write_title_cover_qc(cover, title)
    manifest = tmp_path / "clip.upload_manifest.json"
    rc = au.main([
        "make-manifest", "--video", str(video), "--cover", str(cover),
        "--package-audit", str(audit), "--title", title, "--quote", quote,
        "--title-cover-qc", str(title_cover_qc),
        "--out", str(manifest),
    ])
    assert rc == 0
    return video, cover, manifest


def test_make_and_verify_accept_verified_song_without_story_contract(
    tmp_path, monkeypatch
):
    paths, audit_path, audit_payload, tags = (
        _write_verified_song_package_without_story_contract(tmp_path)
    )
    monkeypatch.setattr(
        au, "audit_package", lambda _root: dict(audit_payload)
    )
    title_cover_qc = _write_title_cover_qc(
        paths["cover"], VALID_SONG_TITLE, candidate_id="song_verified"
    )
    manifest_path = tmp_path / "verified-song.upload_manifest.json"

    assert au.main(
        [
            "make-manifest",
            "--video",
            str(paths["video"]),
            "--cover",
            str(paths["cover"]),
            "--package-audit",
            str(audit_path),
            "--title",
            VALID_SONG_TITLE,
            "--quote",
            "可以上传",
            "--title-cover-qc",
            str(title_cover_qc),
            "--out",
            str(manifest_path),
        ]
    ) == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tags"] == tags
    assert manifest["season"]["lane"] == "song"
    assert "story_contract" not in json.loads(
        paths["record"].read_text(encoding="utf-8")
    )
    assert au.main(
        ["verify", "--manifest", str(manifest_path)]
    ) == 0


def test_talk_without_story_contract_remains_fail_closed(
    tmp_path, monkeypatch, capsys
):
    video = tmp_path / "talk.mp4"
    cover = tmp_path / "talk.cover.png"
    video.write_bytes(b"talk-video")
    cover.write_bytes(b"talk-cover")
    audit_path = _write_v3_package(
        video, cover, VALID_TITLE, TEST_TAGS
    )
    record_path = tmp_path / "talk.record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("story_contract")
    record_path.write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    audit_payload = _passing_package_audit(tmp_path)
    audit_path.write_text(
        json.dumps(audit_payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        au, "audit_package", lambda _root: dict(audit_payload)
    )

    assert au.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit_path),
            "--title",
            VALID_TITLE,
            "--quote",
            "可以上传",
            "--out",
            str(tmp_path / "talk.upload_manifest.json"),
        ]
    ) == 2
    assert "record.json has no story_contract object" in (
        capsys.readouterr().err
    )


def test_song_label_alone_cannot_bypass_story_contract(
    tmp_path, monkeypatch, capsys
):
    paths, audit_path, audit_payload, _tags = (
        _write_verified_song_package_without_story_contract(tmp_path)
    )
    review_path = tmp_path / "review_manifest.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["generated_by"] = "self-declared-song"
    review_path.write_text(
        json.dumps(review, ensure_ascii=False), encoding="utf-8"
    )
    for row in audit_payload["audited_inputs"]:
        if row["path"] == "review_manifest.json":
            row["sha256"] = au.sha256_file(review_path)
            row["bytes"] = review_path.stat().st_size
    audit_path.write_text(
        json.dumps(audit_payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        au, "audit_package", lambda _root: dict(audit_payload)
    )

    assert au.main(
        [
            "make-manifest",
            "--video",
            str(paths["video"]),
            "--cover",
            str(paths["cover"]),
            "--package-audit",
            str(audit_path),
            "--title",
            VALID_SONG_TITLE,
            "--quote",
            "可以上传",
            "--out",
            str(tmp_path / "spoofed-song.upload_manifest.json"),
        ]
    ) == 2
    assert "record.json has no story_contract object" in (
        capsys.readouterr().err
    )


def _ledger_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    ("command", "handler_name", "required_args"),
    [
        (
            "repair-plan",
            "repair_plan",
            ["--manifest", "manifest.json", "--bvid", "BV1TEST", "--out", "plan.json"],
        ),
        (
            "repair-run",
            "repair_run",
            ["--plan", "plan.json"],
        ),
        (
            "repair-reconcile-blocked",
            "repair_reconcile_blocked",
            ["--plan", "plan.json"],
        ),
        (
            "repair-verify-live",
            "repair_verify_live",
            [
                "--plan",
                "plan.json",
                "--out",
                "completed.json",
            ],
        ),
    ],
)
def test_repair_cli_routes_separate_api_and_biliup_cookie_files(
    monkeypatch,
    command,
    handler_name,
    required_args,
):
    seen = {}

    def fake_handler(args):
        seen["api"] = args.cookie_json
        seen["biliup"] = args.biliup_cookie_json
        return 0

    monkeypatch.setattr(au, handler_name, fake_handler)

    assert au.main([
        command,
        *required_args,
        "--cookie-json",
        "/cookies/member-api.json",
        "--biliup-cookie-json",
        "/cookies/biliup.json",
    ]) == 0
    assert seen == {
        "api": "/cookies/member-api.json",
        "biliup": "/cookies/biliup.json",
    }


def test_biliup_readonly_canary_precedes_append_intent_without_leaking_output(
    tmp_path, monkeypatch
):
    cookie = tmp_path / "biliup.json"
    cookie.write_text("{}", encoding="utf-8")
    observed = {}

    monkeypatch.setattr(
        au.member_api,
        "validate_biliup_cookie_file",
        lambda path: observed.setdefault("validated", path),
    )
    monkeypatch.setattr(
        au.member_api,
        "BILIUP_BIN",
        tmp_path / "biliup",
    )

    def run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            returncode=7,
            stdout="secret-cookie-output",
            stderr="secret-cookie-error",
        )

    monkeypatch.setattr(au.subprocess, "run", run)
    with pytest.raises(
        au.RepairError,
        match=r"read-only login canary failed.*rc=7",
    ) as caught:
        au._biliup_readonly_canary(cookie, RECOVERY_BVID)
    assert "secret-cookie" not in str(caught.value)
    assert observed["validated"] == cookie
    assert observed["command"] == [
        str(tmp_path / "biliup"),
        "-u",
        "biliup.json",
        "show",
        RECOVERY_BVID,
    ]
    assert observed["kwargs"] == {
        "cwd": str(tmp_path),
        "capture_output": True,
        "text": True,
        "timeout": 120,
    }


def test_repair_verify_live_creates_fresh_hash_bound_completed_sidecar(
    tmp_path, monkeypatch
):
    plan_path = tmp_path / "repair-plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    journal = tmp_path / "repair-journal.jsonl"
    journal.write_text("journal-placeholder\n", encoding="utf-8")
    completed_path = tmp_path / "completed.json"
    manifest = {"manifest_version": 3}
    snapshot = {
        "creator": {
            "videos": [{"cid": 987654321, "filename": "new", "title": VALID_TITLE}]
        },
        "public": {"cid": 987654321},
        "section": {"matches": [{"cid": 987654321}]},
    }
    plan = {
        "plan_id": "plan-test",
        "bvid": RECOVERY_BVID,
        "manifest": {
            "path": str(tmp_path / "manifest.json"),
            "sha256": "manifest-sha",
        },
        "replacement": {
            "video": {"sha256": "video-sha"},
            "cover": {"sha256": "cover-sha"},
        },
        "season": {"section_id": 9320779},
        "recovery_publication_authority": {
            "candidate_id": "candidate-test",
            "aid": TEST_PUBLICATION_AUTHORITY["aid"],
        },
    }
    verified_row = {
        "state": "VERIFIED",
        "seq": 8,
        "at": "2026-07-24T08:00:00+00:00",
        "row_sha256": "verified-row-sha",
        "details": {"live_snapshot": snapshot},
    }

    class ReadOnlyAdapter:
        def observe(self, bvid, section_id):
            assert bvid == RECOVERY_BVID
            assert section_id == 9320779
            return snapshot

    monkeypatch.setattr(
        au,
        "_load_repair_manifest",
        lambda _path: (plan, manifest, []),
    )
    monkeypatch.setattr(
        au,
        "same_bv_repair_status",
        lambda **_kwargs: SimpleNamespace(state="VERIFIED"),
    )
    monkeypatch.setattr(
        au.repair_binding,
        "plan_entries",
        lambda *_args: [verified_row],
    )
    monkeypatch.setattr(
        au,
        "_same_bv_adapter",
        lambda *_args: ReadOnlyAdapter(),
    )
    reconciled = {}

    def reconcile_same_bv(**kwargs):
        reconciled.update(kwargs)
        return {
            "candidate_id": "candidate-test",
            "bvid": RECOVERY_BVID,
        }

    monkeypatch.setattr(
        au,
        "_reconcile_same_bv_publication",
        reconcile_same_bv,
    )

    assert au.main(
        [
            "repair-verify-live",
            "--plan",
            str(plan_path),
            "--journal",
            str(journal),
            "--out",
            str(completed_path),
            "--lock",
            str(tmp_path / "upload.lock"),
        ]
    ) == 0
    completed = json.loads(completed_path.read_text(encoding="utf-8"))
    assert completed["schema_version"] == "same-bv-repair-completed.v1"
    assert completed["status"] == "VERIFIED_FRESH_LIVE"
    assert completed["rc"] == 0
    assert completed["remote_mutation"] is False
    assert completed["candidate_id"] == "candidate-test"
    assert completed["bvid"] == RECOVERY_BVID
    assert completed["new_cid"] == 987654321
    assert completed["live_snapshot"] == snapshot
    assert completed["verified_journal_row"] == {
        "journal_path": str(journal.resolve()),
        "seq": 8,
        "at": "2026-07-24T08:00:00+00:00",
        "row_sha256": "verified-row-sha",
    }
    assert completed["plan"]["path"] == str(plan_path.resolve())
    assert completed["plan"]["sha256"] == au.sha256_file(plan_path)
    assert reconciled["completed_path"] == completed_path.resolve()
    assert reconciled["manifest"] == manifest
    assert reconciled["manifest_path"] == au.Path(plan["manifest"]["path"])


def test_repair_verify_live_refuses_fresh_surface_drift_without_sidecar(
    tmp_path, monkeypatch
):
    plan_path = tmp_path / "repair-plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    journal = tmp_path / "repair-journal.jsonl"
    journal.write_text("journal-placeholder\n", encoding="utf-8")
    completed_path = tmp_path / "completed.json"
    expected = {
        "creator": {"videos": [{"cid": 10}]},
        "public": {"cid": 10},
        "section": {"matches": [{"cid": 10}]},
    }
    plan = {
        "plan_id": "plan-test",
        "bvid": RECOVERY_BVID,
        "manifest": {"path": "manifest", "sha256": "manifest-sha"},
        "replacement": {},
        "season": {"section_id": 9320779},
        "recovery_publication_authority": {},
    }

    class DriftedAdapter:
        def observe(self, _bvid, _section_id):
            return {
                **expected,
                "public": {"cid": 11},
            }

    monkeypatch.setattr(
        au,
        "_load_repair_manifest",
        lambda _path: (plan, {"manifest_version": 3}, []),
    )
    monkeypatch.setattr(
        au,
        "same_bv_repair_status",
        lambda **_kwargs: SimpleNamespace(state="VERIFIED"),
    )
    monkeypatch.setattr(
        au.repair_binding,
        "plan_entries",
        lambda *_args: [
            {
                "state": "VERIFIED",
                "details": {"live_snapshot": expected},
            }
        ],
    )
    monkeypatch.setattr(
        au,
        "_same_bv_adapter",
        lambda *_args: DriftedAdapter(),
    )

    assert au.main(
        [
            "repair-verify-live",
            "--plan",
            str(plan_path),
            "--journal",
            str(journal),
            "--out",
            str(completed_path),
            "--lock",
            str(tmp_path / "upload.lock"),
        ]
    ) == 5
    assert not completed_path.exists()


def test_repair_plan_refuses_wrong_bvid_before_adapter_or_observe(
    tmp_path, monkeypatch, capsys
):
    manifest = {
        "manifest_version": 3,
        "recovery_publication_authority": dict(
            TEST_PUBLICATION_AUTHORITY
        ),
    }
    adapter_created = 0
    lock_held = False

    @contextmanager
    def fake_lock(_path):
        nonlocal lock_held
        lock_held = True
        yield
        lock_held = False

    def load_inside_lock(_path, **_kwargs):
        assert lock_held
        return manifest, []

    def make_adapter(*_args):
        nonlocal adapter_created
        adapter_created += 1
        raise AssertionError("adapter must not be created")

    monkeypatch.setattr(au, "exclusive_upload_lock", fake_lock)
    monkeypatch.setattr(au, "load_and_verify", load_inside_lock)
    monkeypatch.setattr(
        au.repair_binding,
        "validate_recovery_publication_authority",
        lambda value, **_kwargs: dict(value),
    )
    monkeypatch.setattr(
        au.repair_binding,
        "_final_human_review_attestation",
        lambda _manifest: {},
    )
    monkeypatch.setattr(au, "_same_bv_adapter", make_adapter)

    assert au.main(
        [
            "repair-plan",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--bvid",
            "BV1tTg46UE3y",
            "--out",
            str(tmp_path / "plan.json"),
        ]
    ) == 2
    assert adapter_created == 0
    assert (
        "repair BVID differs from recovery_publication_authority"
        in capsys.readouterr().err
    )


def test_repair_run_validates_plan_before_adapter_or_cookie_access(
    tmp_path, monkeypatch, capsys
):
    plan_path = tmp_path / "repair-plan.json"
    manifest_path = tmp_path / "manifest.json"
    plan = {"manifest": {"path": str(manifest_path)}}
    adapter_created = 0
    lock_held = False

    @contextmanager
    def fake_lock(_path):
        nonlocal lock_held
        lock_held = True
        yield
        lock_held = False

    def load_inside_lock(_path, **_kwargs):
        assert lock_held
        return {"manifest_version": 3}, []

    monkeypatch.setattr(
        au, "load_same_bv_repair_plan", lambda _path: plan
    )
    monkeypatch.setattr(au, "exclusive_upload_lock", fake_lock)
    monkeypatch.setattr(au, "load_and_verify", load_inside_lock)

    monkeypatch.setattr(
        au.repair_binding,
        "validate_plan_problems",
        lambda *_args, **_kwargs: ["bound manifest hash drift"],
    )

    def make_adapter(*_args):
        nonlocal adapter_created
        adapter_created += 1
        raise AssertionError("adapter/cookies must not be accessed")

    monkeypatch.setattr(au, "_same_bv_adapter", make_adapter)
    assert au.main(
        [
            "repair-run",
            "--plan",
            str(plan_path),
            "--journal",
            str(tmp_path / "journal.json"),
        ]
    ) == 2
    assert adapter_created == 0
    assert "bound manifest hash drift" in capsys.readouterr().err


def test_make_manifest_then_verify_ok(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    assert au.main(["verify", "--manifest", str(manifest)]) == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["artifact_id"] == data["video"]["sha256"][:12]
    assert data["authorization"]["quote"] == "可以上传了"
    assert "final_human_review" not in data["package_attestation"]
    assert data["package_attestation"]["title_cover_qc"] == {
        "path": str(
            (
                tmp_path
                / "clip.cover.title-cover-joint-qc.json"
            ).resolve()
        ),
        "sha256": au.sha256_file(
            tmp_path / "clip.cover.title-cover-joint-qc.json"
        ),
        "bytes": (
            tmp_path / "clip.cover.title-cover-joint-qc.json"
        ).stat().st_size,
    }
    assert data["description"] == (
        "https://live.bilibili.com/\n"
        "李豆沙个人主页：https://space.bilibili.com/1703797642\n"
        "李豆沙直播间：https://live.bilibili.com/22966160"
    )


def test_make_manifest_requires_title_cover_qc_for_new_bv(
    tmp_path, capsys
):
    video = tmp_path / "new.mp4"
    cover = tmp_path / "new.cover.png"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    audit = _write_v3_package(video, cover, VALID_TITLE)
    manifest = tmp_path / "new.upload_manifest.json"

    assert au.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
            "--quote",
            "可以上传",
            "--out",
            str(manifest),
        ]
    ) == 2
    assert not manifest.exists()
    assert "new-BV manifest requires package_attestation.title_cover_qc" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("field_path", "bad_value", "expected_problem"),
    [
        (
            "schema_version",
            "lidousha-title-cover-joint-qc.v0",
            "schema_version is invalid",
        ),
        ("candidate_id", "other", "candidate_id does not match record"),
        ("title", "别的标题", "title does not match manifest title"),
        ("title_sha256", "sha256:" + "0" * 64, "title_sha256 does not bind"),
        ("cover_path", "/tmp/other.png", "cover_path does not bind"),
        ("cover_sha256", "sha256:" + "0" * 64, "cover_sha256 does not bind"),
        ("selected_provider", "agy", "selected_provider must be cpa"),
        ("witness.provider", "agy", "witness provider must be cpa"),
        ("witness.status", "FAILED", "CPA witness was not OBSERVED"),
        ("verdict.lidousha_primary", False, "verdict.lidousha_primary must be true"),
        ("verdict.thumbnail_readable", False, "verdict.thumbnail_readable must be true"),
        ("verdict.physical_text_line_count", 3, "physical_text_line_count must be 1 or 2"),
        ("verdict.single_clear_hook", False, "verdict.single_clear_hook must be true"),
        ("verdict.text_overcrowded", True, "verdict.text_overcrowded must be false"),
        ("verdict.title_cover_aligned", False, "verdict.title_cover_aligned must be true"),
        (
            "verdict.unrelated_or_misleading_elements",
            ["无关物件"],
            "unrelated_or_misleading_elements must be empty",
        ),
        ("verdict.pass", False, "verdict.pass must be true"),
        ("status", "BLOCK", "status must be PASS"),
        ("pass", False, "top-level pass must be true"),
    ],
)
def test_verify_replays_strict_title_cover_qc_semantics_after_hash_rebind(
    tmp_path,
    capsys,
    field_path,
    bad_value,
    expected_problem,
):
    _, _, manifest_path = _mk(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding = manifest["package_attestation"]["title_cover_qc"]
    receipt_path = au.Path(binding["path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    target = receipt
    parts = field_path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = bad_value
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False), encoding="utf-8"
    )
    binding["sha256"] = au.sha256_file(receipt_path)
    binding["bytes"] = receipt_path.stat().st_size
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    assert au.main(["verify", "--manifest", str(manifest_path)]) == 2
    assert expected_problem in capsys.readouterr().err


def test_upload_refuses_title_cover_qc_byte_drift_before_uploader(
    tmp_path, capsys
):
    _, _, manifest_path = _mk(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt_path = au.Path(
        manifest["package_attestation"]["title_cover_qc"]["path"]
    )
    receipt_path.write_text(
        receipt_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    marker = tmp_path / "uploader-called"
    uploader = tmp_path / "uploader.sh"
    uploader.write_text(
        f"#!/bin/sh\ntouch {marker!s}\nexit 0\n", encoding="utf-8"
    )
    uploader.chmod(0o755)

    assert au.main(
        [
            "upload",
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--uploader",
            str(uploader),
        ]
    ) == 2
    assert not marker.exists()
    assert "title_cover_qc HASH DRIFT" in capsys.readouterr().err


def test_make_manifest_freezes_matching_package_publication_authority(
    tmp_path, monkeypatch
):
    def audit_package(root):
        return {
            "schema_version": au.AUDIT_SCHEMA_VERSION,
            "policy_epoch": au.AUDIT_POLICY_EPOCH,
            "policy_fingerprint": "test-policy",
            "auditor_source_sha256": "test-auditor",
            "passed": True,
            "root": str(root.resolve()),
            "audited_inputs": [],
            "issues": [],
            "issue_count": 0,
            "blocking_issue_count": 0,
        }

    def validate(value, *, candidate_id, expected_final_title=None):
        assert candidate_id == "candidate-test"
        assert expected_final_title == VALID_TITLE
        if value != TEST_PUBLICATION_AUTHORITY:
            raise au.repair_binding.RecoveryTitleAuthorityError(
                "test mismatch"
            )
        return dict(TEST_PUBLICATION_AUTHORITY)

    monkeypatch.setattr(
        au.repair_binding,
        "validate_recovery_publication_authority",
        validate,
    )
    monkeypatch.setattr(
        au,
        "_publication_block",
        lambda _manifest: (_ for _ in ()).throw(
            AssertionError(
                "make-manifest must not apply the ordinary-upload registry gate"
            )
        ),
    )
    monkeypatch.setattr(au, "audit_package", audit_package)
    video = tmp_path / "recovery.mp4"
    cover = tmp_path / "recovery.cover.png"
    video.write_bytes(b"recovery-video")
    cover.write_bytes(b"recovery-cover")
    audit = _write_v3_package(
        video,
        cover,
        VALID_TITLE,
        recovery_publication_authority=TEST_PUBLICATION_AUTHORITY,
    )
    final_human_review = _write_final_human_review(video, audit)
    manifest = tmp_path / "recovery.upload_manifest.json"

    assert au.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
            "--quote",
            "尽量上传",
            "--final-human-review",
            str(final_human_review),
            "--out",
            str(manifest),
        ]
    ) == 0
    assert (
        json.loads(manifest.read_text(encoding="utf-8"))[
            "recovery_publication_authority"
        ]
        == TEST_PUBLICATION_AUTHORITY
    )
    receipt_binding = json.loads(
        manifest.read_text(encoding="utf-8")
    )["package_attestation"]["final_human_review"]
    assert receipt_binding == {
        "path": str(final_human_review.resolve()),
        "sha256": au.sha256_file(final_human_review),
        "bytes": final_human_review.stat().st_size,
    }
    recovery_manifest = json.loads(
        manifest.read_text(encoding="utf-8")
    )
    assert recovery_manifest["season"]["season_id"] == 8383206
    assert recovery_manifest["season"]["section_id"] == 9320779
    assert au.main(["verify", "--manifest", str(manifest)]) == 0

    class ReadOnlyAdapter:
        def observe(self, bvid, section_id):
            assert bvid == RECOVERY_BVID
            assert section_id == 9320779
            creator_metadata = {
                "title": VALID_TITLE,
                "desc": au.DEFAULT_DESCRIPTION,
                "tags": list(TEST_TAGS),
                "tid": au.EXPECTED_TID,
                "copyright": au.EXPECTED_COPYRIGHT,
                "source": au.EXPECTED_SOURCE,
                "cover": "https://img.example/old-cover.png",
            }
            return {
                "creator": {
                    "available": True,
                    "bvid": bvid,
                    "aid": TEST_PUBLICATION_AUTHORITY["aid"],
                    "state": 0,
                    "state_desc": "开放浏览",
                    "metadata": creator_metadata,
                    "videos": [
                        {
                            "cid": TEST_PUBLICATION_AUTHORITY["cid"],
                            "filename": "old-file",
                            "title": VALID_TITLE,
                        }
                    ],
                },
                "public": {
                    "available": True,
                    "bvid": bvid,
                    "aid": TEST_PUBLICATION_AUTHORITY["aid"],
                    "cid": TEST_PUBLICATION_AUTHORITY["cid"],
                    "state": 0,
                    "metadata": {
                        key: value
                        for key, value in creator_metadata.items()
                        if key != "source"
                    },
                },
                "section": {
                    "available": True,
                    "section_id": section_id,
                    "matches": [
                        {
                            "bvid": bvid,
                            "aid": TEST_PUBLICATION_AUTHORITY["aid"],
                            "cid": TEST_PUBLICATION_AUTHORITY["cid"],
                            "title": VALID_TITLE,
                        }
                    ],
                },
            }

    monkeypatch.setattr(
        au, "_same_bv_adapter", lambda *_args: ReadOnlyAdapter()
    )
    assert au.main(
        [
            "repair-plan",
            "--manifest",
            str(manifest),
            "--bvid",
            RECOVERY_BVID,
            "--out",
            str(tmp_path / "valid-repair-plan.json"),
            "--dry-run",
        ]
    ) == 0

    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_data["recovery_publication_authority"]["cid"] = 999
    manifest.write_text(
        json.dumps(manifest_data, ensure_ascii=False), encoding="utf-8"
    )
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    manifest_data["recovery_publication_authority"] = dict(
        TEST_PUBLICATION_AUTHORITY
    )
    manifest.write_text(
        json.dumps(manifest_data, ensure_ascii=False), encoding="utf-8"
    )

    final_human_review.write_text(
        final_human_review.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    adapter_created = 0

    def make_adapter(*_args):
        nonlocal adapter_created
        adapter_created += 1
        raise AssertionError("adapter must not be created")

    monkeypatch.setattr(au, "_same_bv_adapter", make_adapter)
    assert au.main(
        [
            "repair-plan",
            "--manifest",
            str(manifest),
            "--bvid",
            RECOVERY_BVID,
            "--out",
            str(tmp_path / "repair-plan.json"),
        ]
    ) == 2
    assert adapter_created == 0


def test_make_manifest_refuses_recovery_without_final_human_review(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(au, "audit_package", _passing_package_audit)
    monkeypatch.setattr(
        au.repair_binding,
        "validate_recovery_publication_authority",
        lambda value, **_kwargs: dict(value),
    )
    video = tmp_path / "recovery.mp4"
    cover = tmp_path / "recovery.cover.png"
    video.write_bytes(b"recovery-video")
    cover.write_bytes(b"recovery-cover")
    audit = _write_v3_package(
        video,
        cover,
        VALID_TITLE,
        recovery_publication_authority=TEST_PUBLICATION_AUTHORITY,
    )

    assert au.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
            "--quote",
            "尽量上传",
        ]
    ) == 2
    assert (
        "same-BV recovery manifest requires --final-human-review"
        in capsys.readouterr().err
    )


def test_verify_replays_final_human_review_after_hash_is_rebound(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(au, "audit_package", _passing_package_audit)
    monkeypatch.setattr(
        au.repair_binding,
        "validate_recovery_publication_authority",
        lambda value, **_kwargs: dict(value),
    )
    video = tmp_path / "recovery.mp4"
    cover = tmp_path / "recovery.cover.png"
    video.write_bytes(b"recovery-video")
    cover.write_bytes(b"recovery-cover")
    audit = _write_v3_package(
        video,
        cover,
        VALID_TITLE,
        recovery_publication_authority=TEST_PUBLICATION_AUTHORITY,
    )
    final_human_review = _write_final_human_review(video, audit)
    manifest = tmp_path / "recovery.upload_manifest.json"
    assert au.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
            "--quote",
            "尽量上传",
            "--final-human-review",
            str(final_human_review),
            "--out",
            str(manifest),
        ]
    ) == 0

    receipt_data = json.loads(
        final_human_review.read_text(encoding="utf-8")
    )
    receipt_data["items"][0]["checks"]["subtitle_audio"][
        "status"
    ] = "FAIL"
    final_human_review.write_text(
        json.dumps(receipt_data, ensure_ascii=False), encoding="utf-8"
    )
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_data["package_attestation"]["final_human_review"][
        "sha256"
    ] = au.sha256_file(final_human_review)
    manifest_data["package_attestation"]["final_human_review"][
        "bytes"
    ] = final_human_review.stat().st_size
    manifest.write_text(
        json.dumps(manifest_data, ensure_ascii=False), encoding="utf-8"
    )

    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert (
        "FINAL_HUMAN_REVIEW_CHECK_NOT_PASS"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("title", "FINAL_HUMAN_REVIEW_TITLE_MISMATCH"),
        (
            "bvid",
            "FINAL_HUMAN_REVIEW_PUBLICATION_TARGET_MISMATCH",
        ),
    ],
)
def test_verify_refuses_old_receipt_after_rebinding_reviewed_target(
    tmp_path, monkeypatch, capsys, mutation, reason_code
):
    monkeypatch.setattr(au, "audit_package", _passing_package_audit)
    monkeypatch.setattr(
        au.repair_binding,
        "validate_recovery_publication_authority",
        lambda value, **_kwargs: dict(value),
    )
    video = tmp_path / "recovery.mp4"
    cover = tmp_path / "recovery.cover.png"
    video.write_bytes(b"recovery-video")
    cover.write_bytes(b"recovery-cover")
    audit = _write_v3_package(
        video,
        cover,
        VALID_TITLE,
        recovery_publication_authority=TEST_PUBLICATION_AUTHORITY,
    )
    receipt = _write_final_human_review(video, audit)
    manifest_path = tmp_path / "recovery.upload_manifest.json"
    assert au.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
            "--quote",
            "尽量上传",
            "--final-human-review",
            str(receipt),
            "--out",
            str(manifest_path),
        ]
    ) == 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    review_path = tmp_path / "review_manifest.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    record_path = tmp_path / "recovery.record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
    changed_authority = dict(TEST_PUBLICATION_AUTHORITY)
    if mutation == "title":
        changed_title = "【李豆沙】攻击者事后更换的另一个标题"
        changed_authority["observed_public_title"] = changed_title
        manifest["title"] = changed_title
        review["items"][0]["title"] = changed_title
        record["publish_staging"]["title"] = changed_title
    else:
        changed_authority["bvid"] = "BV9999999999"
    manifest["recovery_publication_authority"] = changed_authority
    review["items"][0]["recovery_publication_authority"] = (
        changed_authority
    )
    record["recovery_publication_authority"] = changed_authority

    record_path.write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    review_path.write_text(
        json.dumps(review, ensure_ascii=False), encoding="utf-8"
    )
    receipt_data["items"][0]["record"]["sha256"] = (
        "sha256:" + au.sha256_file(record_path)
    )
    receipt_data["package_evidence"]["review_manifest"][
        "sha256"
    ] = "sha256:" + au.sha256_file(review_path)
    receipt.write_text(
        json.dumps(receipt_data, ensure_ascii=False), encoding="utf-8"
    )

    for key, path in (
        ("record", record_path),
        ("review_manifest", review_path),
    ):
        manifest["package_attestation"][key].update(
            {
                "sha256": au.sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    manifest["package_attestation"]["final_human_review"][
        "sha256"
    ] = au.sha256_file(receipt)
    manifest["package_attestation"]["final_human_review"][
        "bytes"
    ] = receipt.stat().st_size
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    assert au.main(["verify", "--manifest", str(manifest_path)]) == 2
    assert reason_code in capsys.readouterr().err


def test_make_manifest_refuses_one_sided_publication_authority(
    tmp_path, monkeypatch, capsys
):
    video = tmp_path / "recovery.mp4"
    cover = tmp_path / "recovery.cover.png"
    video.write_bytes(b"recovery-video")
    cover.write_bytes(b"recovery-cover")
    audit = _write_v3_package(video, cover, VALID_TITLE)
    record = tmp_path / "recovery.record.json"
    record_data = json.loads(record.read_text(encoding="utf-8"))
    record_data["recovery_publication_authority"] = (
        TEST_PUBLICATION_AUTHORITY
    )
    record.write_text(
        json.dumps(record_data, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(au, "_validate_v3_package_attestation", lambda *_a, **_k: [])

    assert au.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
            "--quote",
            "尽量上传",
        ]
    ) == 2
    assert "must exist on both record and review item" in capsys.readouterr().err


def test_package_binding_replays_real_committed_publication_authority():
    from src.autoslice.recovery_title_authority import (
        build_recovery_publication_authorities,
        expected_recovery_publish_title,
    )

    candidate_id = "auto_193450_1475_1543"
    registry = (
        au.ROOT
        / "assets/lidousha/recovery_publication_authority.v1.json"
    )
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=registry,
        expected_registry_sha256="sha256:" + au.sha256_file(registry),
    )[candidate_id]
    record = {
        "story_contract": {"candidate_id": candidate_id},
        "recovery_publication_authority": authority,
    }
    review_item = {
        "candidate_id": candidate_id,
        "recovery_publication_authority": authority,
    }

    validated, problems = (
        au.repair_binding.package_recovery_publication_authority(
            record,
            review_item,
            expected_final_title=expected_recovery_publish_title(authority),
        )
    )
    assert problems == []
    assert validated == authority


def test_verify_refuses_hash_drift(tmp_path, capsys):
    video, _, manifest = _mk(tmp_path)
    video.write_bytes(b"tampered-after-review")  # 审的是A、传的必须还是A
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert "HASH DRIFT" in capsys.readouterr().err


def test_verify_refuses_attested_srt_or_audit_drift(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    data = json.loads(manifest.read_text())
    subtitle = data["package_attestation"]["subtitle"]["path"]
    with open(subtitle, "a", encoding="utf-8") as handle:
        handle.write("tamper\n")
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert "package_attestation.subtitle HASH DRIFT" in capsys.readouterr().err

    _, _, manifest2 = _mk(tmp_path / "other")
    data2 = json.loads(manifest2.read_text())
    audit = data2["package_attestation"]["package_audit"]["path"]
    with open(audit, "w", encoding="utf-8") as handle:
        json.dump({"passed": False, "root": str((tmp_path / "other").resolve())}, handle)
    assert au.main(["verify", "--manifest", str(manifest2)]) == 2
    err = capsys.readouterr().err
    assert "package_attestation.package_audit HASH DRIFT" in err
    assert "package audit did not pass" in err


def test_verify_refuses_record_coherence_even_if_attestation_hash_is_rebound(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    data = json.loads(manifest.read_text())
    record = data["package_attestation"]["record"]["path"]
    record_data = json.loads(open(record, encoding="utf-8").read())
    record_data["publish_staging"]["title"] = "被事后改掉的标题"
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(record_data, handle, ensure_ascii=False)
    data["package_attestation"]["record"]["sha256"] = au.sha256_file(au.Path(record))
    with open(manifest, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert "record publish title mismatch" in capsys.readouterr().err


def test_make_manifest_requires_authorization_quote(tmp_path):
    video = tmp_path / "v.mp4"
    cover = tmp_path / "v.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(video, cover, VALID_TITLE)
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "   "])
    assert rc == 2


def _mk_with_tags(tmp_path, tags: str):
    video = tmp_path / "v.mp4"
    cover = tmp_path / "v.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    tag_list = [part.strip() for part in tags.split(",") if part.strip()]
    audit = _write_v3_package(video, cover, VALID_TITLE, tag_list)
    title_cover_qc = _write_title_cover_qc(cover, VALID_TITLE)
    manifest = tmp_path / "v.upload_manifest.json"
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--package-audit", str(audit),
                  "--title", VALID_TITLE, "--quote", "可以上传了",
                  "--title-cover-qc", str(title_cover_qc),
                  "--tags", tags, "--out", str(manifest)])
    return rc, manifest


def test_make_manifest_freezes_valid_tags(tmp_path):
    rc, manifest = _mk_with_tags(tmp_path, "李豆沙,虚拟主播,虚拟UP主,直播切片,侄女,百合")
    assert rc == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["tags"] == ["李豆沙", "虚拟主播", "虚拟UP主", "直播切片", "侄女", "百合"]
    assert au.main(["verify", "--manifest", str(manifest)]) == 0


def test_make_manifest_refuses_bad_tag_lines(tmp_path, capsys):
    # 超过实测上限 12 个
    rc, _ = _mk_with_tags(tmp_path, ",".join(f"t{i}" for i in range(13)))
    assert rc == 2
    # 大小写视作重复
    rc, _ = _mk_with_tags(tmp_path, "VUP,vup")
    assert rc == 2
    # 单 tag 超 20 字符
    rc, _ = _mk_with_tags(tmp_path, "一" * 21)
    assert rc == 2


def test_verify_refuses_manifest_with_tampered_tags(tmp_path, capsys):
    rc, manifest = _mk_with_tags(tmp_path, "李豆沙,虚拟主播")
    assert rc == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["tags"] = data["tags"] + [f"t{i}" for i in range(12)]  # 事后塞爆 tag 位
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert au.main(["verify", "--manifest", str(manifest)]) == 2


def _write_record_sidecar(video, tags, status="OK"):
    record = video.parent / (video.name[: -len(".mp4")] + ".record.json")
    record.write_text(json.dumps({
        "upload_tags": {"engine": "suggest-upload-tags.v1", "status": status, "final_tags": tags},
    }, ensure_ascii=False), encoding="utf-8")
    return record


def test_make_manifest_auto_picks_tags_from_record_sidecar(tmp_path):
    video, cover = tmp_path / "clip.mp4", tmp_path / "clip.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(
        video, cover, VALID_TITLE, ["李豆沙", "虚拟主播", "侄女", "百合"]
    )
    title_cover_qc = _write_title_cover_qc(cover, VALID_TITLE)
    manifest = tmp_path / "m.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit),
                    "--title", VALID_TITLE, "--quote", "q",
                    "--title-cover-qc", str(title_cover_qc),
                    "--out", str(manifest)]) == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["tags"] == ["李豆沙", "虚拟主播", "侄女", "百合"]
    assert data["tags_source"].startswith("record.json:")


def test_make_manifest_cli_tags_must_match_record_and_no_tags_refuses(tmp_path):
    video, cover = tmp_path / "clip.mp4", tmp_path / "clip.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(video, cover, VALID_TITLE, ["李豆沙", "记录里的"])
    m1 = tmp_path / "m1.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                    "--tags", "李豆沙,手给的", "--out", str(m1)]) == 2
    m2 = tmp_path / "m2.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                    "--no-tags", "--out", str(m2)]) == 2


def test_make_manifest_refuses_missing_or_unreadable_record_tags(tmp_path, capsys):
    video, cover = tmp_path / "clip.mp4", tmp_path / "clip.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(video, cover, VALID_TITLE, [])
    record = sidecar = video.parent / "clip.record.json"
    data = json.loads(sidecar.read_text())
    data["upload_tags"] = {"engine": "test", "status": "FAILED", "final_tags": []}
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    m1 = tmp_path / "m1.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                    "--out", str(m1)]) == 2
    record.write_text("{not-json", encoding="utf-8")
    m2 = tmp_path / "m2.json"
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                  "--out", str(m2)])
    assert rc == 2  # 坏 sidecar 必须响, 不许静默无tag
    assert "unreadable" in capsys.readouterr().err


def test_upload_passes_manifest_tag_line_to_uploader(tmp_path, capsys):
    rc, manifest = _mk_with_tags(tmp_path, "李豆沙,虚拟主播,侄女")
    assert rc == 0
    stub = tmp_path / "stub_upload.sh"
    stub.write_text(
        "#!/bin/bash\n"
        '[ "${AUTHORIZED_UPLOAD:-}" = "1" ] || exit 4\n'
        'echo "args=$#"\n'
        'echo "tagline=${4:-<none>}"\n'
        "echo rc=0\necho BVID=BV1TAG\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                    "--uploader", str(stub)]) == 0
    out = capsys.readouterr().out
    assert "args=4" in out
    assert "tagline=李豆沙,虚拟主播,侄女" in out
    finished = _ledger_rows(ledger)[-1]
    assert finished["tags"] == "李豆沙,虚拟主播,侄女"


def test_upload_refuses_same_bv_recovery_before_uploader(
    tmp_path, monkeypatch, capsys
):
    manifest = {
        "manifest_version": 3,
        "recovery_publication_authority": dict(
            TEST_PUBLICATION_AUTHORITY
        ),
    }
    def load_recovery(_path, *, ordinary_upload=False):
        assert ordinary_upload
        return manifest, au.human_review.ordinary_upload_problems(manifest)

    monkeypatch.setattr(au, "load_and_verify", load_recovery)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("recovery upload must stop before any side effect")

    monkeypatch.setattr(au.subprocess, "run", must_not_run)
    monkeypatch.setattr(au, "_build_season_http", must_not_run)
    monkeypatch.setattr(au, "ledger_guard", must_not_run)
    monkeypatch.setattr(au, "append_ledger", must_not_run)
    assert au.main(
        [
            "upload",
            "--manifest",
            str(tmp_path / "recovery.upload_manifest.json"),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--uploader",
            str(tmp_path / "uploader"),
        ]
    ) == 2
    assert (
        "same-BV recovery manifests cannot use upload"
        in capsys.readouterr().err
    )


def test_upload_v3_always_passes_record_bound_tag_line(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    stub = tmp_path / "stub_upload.sh"
    stub.write_text(
        "#!/bin/bash\n"
        '[ "${AUTHORIZED_UPLOAD:-}" = "1" ] || exit 4\n'
        'echo "args=$#"\n'
        "echo rc=0\necho BVID=BV1NOTAG\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                    "--uploader", str(stub)]) == 0
    out = capsys.readouterr().out
    assert "args=4" in out
    assert _ledger_rows(ledger)[-1]["tags"] == ",".join(TEST_TAGS)


def _stub_uploader(tmp_path):
    """Uploader stub that proves it got manifest args + the authorization env."""
    stub = tmp_path / "stub_upload.sh"
    stub.write_text(
        "#!/bin/bash\n"
        '[ "${AUTHORIZED_UPLOAD:-}" = "1" ] || { echo "no-auth-env" >&2; exit 4; }\n'
        'echo "got: $1 | $2 | $3"\n'
        "echo rc=0\n"
        "echo BVID=BV1TEST\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def test_upload_runs_uploader_with_manifest_args_and_ledgers(tmp_path, capsys):
    video, cover, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    rc = au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                  "--uploader", str(_stub_uploader(tmp_path))])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"got: {video.resolve()} | {cover.resolve()} | {VALID_TITLE}" in out
    rows = _ledger_rows(ledger)
    assert [row["event"] for row in rows] == ["UPLOAD_ATTEMPT_STARTED", "UPLOAD_ATTEMPT_FINISHED"]
    assert rows[0]["attempt_id"] == rows[1]["attempt_id"]
    entry = rows[-1]
    assert entry["rc"] == 0 and entry["bvid"] == "BV1TEST"
    manifest_data = json.loads(manifest.read_text())
    assert entry["video_sha256"] == manifest_data["video"]["sha256"]
    assert entry["title_cover_qc_sha256"] == (
        manifest_data["package_attestation"]["title_cover_qc"]["sha256"]
    )
    assert entry["authorization_quote"] == "可以上传了"
    uploaded = json.loads(
        au.uploaded_sidecar_path(manifest).read_text(encoding="utf-8")
    )
    assert uploaded["title_cover_qc_sha256"] == (
        manifest_data["package_attestation"]["title_cover_qc"]["sha256"]
    )


def test_public_success_without_local_reconciliation_stays_posted_unverified(
    tmp_path, monkeypatch, capsys
):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"

    def fail_reconciliation(**_kwargs):
        raise au.publication_reconciliation.PublicationReconciliationError(
            "state projection unavailable"
        )

    monkeypatch.setattr(
        au, "_reconcile_new_bv_publication", fail_reconciliation
    )
    rc = au.main(
        [
            "upload",
            "--manifest",
            str(manifest),
            "--ledger",
            str(ledger),
            "--uploader",
            str(_stub_uploader(tmp_path)),
        ]
    )
    assert rc == 6
    rows = _ledger_rows(ledger)
    assert rows[-1]["uploader_rc"] == 0
    assert rows[-1]["rc"] == 6
    assert rows[-1]["public_verify_status"] == "VERIFIED_PUBLIC"
    assert au.uploaded_sidecar_path(manifest).is_file()
    assert "never re-upload" in capsys.readouterr().err


def test_upload_is_idempotent_by_video_hash(tmp_path, capsys):
    """充电器重复投稿事故的防线：同一视频 hash 第二次上传必须硬拒。"""
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    stub = _stub_uploader(tmp_path)
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger), "--uploader", str(stub)]) == 0
    rc = au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger), "--uploader", str(stub)])
    assert rc == 3
    assert "already uploaded" in capsys.readouterr().err
    assert len(ledger.read_text().splitlines()) == 2  # 拒绝的不追加第二个 attempt


def test_upload_refuses_drifted_artifact(tmp_path, capsys):
    video, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    video.write_bytes(b"drifted")
    rc = au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                  "--uploader", str(_stub_uploader(tmp_path))])
    assert rc == 2
    assert not ledger.exists()  # 没上传就没有账本条目


def test_failed_upload_ledgered_but_retryable(tmp_path):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    bad = tmp_path / "bad_upload.sh"
    bad.write_text("#!/bin/bash\necho boom >&2\nexit 7\n", encoding="utf-8")
    bad.chmod(0o755)
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger), "--uploader", str(bad)]) == 7
    entry = _ledger_rows(ledger)[-1]
    assert entry["rc"] == 7 and entry["bvid"] is None
    # rc!=0 的账本条目不算已上传 → 重试不会被幂等门误拦
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                    "--uploader", str(_stub_uploader(tmp_path))]) == 0
    assert len(_ledger_rows(ledger)) == 4


def test_upload_holds_shared_lock_through_uploader_and_ledger_append(tmp_path, capsys):
    """The child uploader must observe the repair/upload lock as already held."""
    ledger = tmp_path / "ledger.jsonl"
    lock = tmp_path / "shared-upload.lock"
    probe = tmp_path / "probe_lock.mp4"
    probe.write_text(
        "import fcntl, os, sys\n"
        f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    print('LOCK_HELD')\n"
        "    print('BVID=BV1LOCK')\n"
        "    raise SystemExit(0)\n"
        "print('LOCK_NOT_HELD')\n"
        "raise SystemExit(9)\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    cover = tmp_path / "probe_lock.cover.png"
    cover.write_bytes(b"cover")
    audit = _write_v3_package(probe, cover, VALID_TITLE)
    title_cover_qc = _write_title_cover_qc(cover, VALID_TITLE)
    manifest = tmp_path / "lock-probe.upload_manifest.json"
    assert au.main(
        [
            "make-manifest",
            "--video",
            str(probe),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
            "--quote",
            "test only",
            "--title-cover-qc",
            str(title_cover_qc),
            "--out",
            str(manifest),
        ]
    ) == 0

    rc = au.main(
        [
            "upload",
            "--manifest",
            str(manifest),
            "--ledger",
            str(ledger),
            "--lock",
            str(lock),
            "--uploader",
            sys.executable,
        ]
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "LOCK_HELD" in output
    assert "LOCK_NOT_HELD" not in output
    assert _ledger_rows(ledger)[-1]["rc"] == 0


def test_upload_refuses_instead_of_queueing_behind_repair_lock(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    lock = tmp_path / "shared-upload.lock"
    marker = tmp_path / "uploader-ran"
    stub = tmp_path / "must-not-run.sh"
    stub.write_text(f"#!/bin/bash\ntouch {str(marker)!r}\n", encoding="utf-8")
    stub.chmod(0o755)

    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc = au.main(
            [
                "upload",
                "--manifest",
                str(manifest),
                "--ledger",
                str(ledger),
                "--lock",
                str(lock),
                "--uploader",
                str(stub),
            ]
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert rc == 4
    assert "shared upload/repair lock is busy" in capsys.readouterr().err
    assert not marker.exists()
    assert not ledger.exists()


def test_started_intent_is_fsynced_before_subprocess_and_terminal_is_durable(tmp_path, monkeypatch):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    fsync_calls = []
    real_fsync = au.os.fsync

    def tracked_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    def inspect_then_finish(cmd, **kwargs):
        rows = _ledger_rows(ledger)
        assert len(rows) == 1
        assert rows[0]["event"] == "UPLOAD_ATTEMPT_STARTED"
        assert len(rows[0]["manifest_sha256"]) == 64
        # file + parent directory were both fsynced before the side effect.
        assert len(fsync_calls) >= 2
        return au.subprocess.CompletedProcess(cmd, 0, stdout="BVID=BV1INTENT\n", stderr="")

    monkeypatch.setattr(au.os, "fsync", tracked_fsync)
    monkeypatch.setattr(au.subprocess, "run", inspect_then_finish)

    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger)]) == 0
    rows = _ledger_rows(ledger)
    assert [row["event"] for row in rows] == ["UPLOAD_ATTEMPT_STARTED", "UPLOAD_ATTEMPT_FINISHED"]
    assert rows[0]["attempt_id"] == rows[1]["attempt_id"]
    assert rows[1]["bvid"] == "BV1INTENT"
    assert len(fsync_calls) >= 4


def test_crash_after_started_intent_blocks_all_later_uploads(tmp_path, monkeypatch, capsys):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"

    def crash_after_intent(cmd, **kwargs):
        assert _ledger_rows(ledger)[-1]["event"] == "UPLOAD_ATTEMPT_STARTED"
        raise RuntimeError("simulated process crash before terminal row")

    monkeypatch.setattr(au.subprocess, "run", crash_after_intent)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger)])
    assert [row["event"] for row in _ledger_rows(ledger)] == ["UPLOAD_ATTEMPT_STARTED"]

    called = False

    def must_not_run(cmd, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("unresolved intent must block before uploader")

    monkeypatch.setattr(au.subprocess, "run", must_not_run)
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger)]) == 5
    assert "unresolved UPLOAD_ATTEMPT_STARTED" in capsys.readouterr().err
    assert called is False
    assert len(_ledger_rows(ledger)) == 1


def test_rolling_quota_guard_uses_max_of_ledger_and_creator_estimates(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    current = 2_000_000_000.0
    timestamp = au.dt.datetime.fromtimestamp(current, tz=au.dt.timezone.utc).isoformat()
    rows = [
        {
            "at": timestamp,
            "video_sha256": f"{index:064x}",
            "rc": 0,
        }
        for index in range(9)
    ]
    ledger.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    def http(url):
        return {
            "code": 0,
            "data": {
                "arc_audits": [
                    {"Archive": {"bvid": f"BV{index}", "ptime": current - 10}}
                    for index in range(10)
                ],
                "page": {"count": 10},
            },
        }

    evidence, problems = au.rolling_quota_guard(ledger, http=http, now_epoch=current)
    assert evidence["local_ledger_successes"] == 9
    assert evidence["creator_recent_archives"] == 10
    assert evidence["estimated_used"] == 10
    assert problems and "10/10" in problems[0]


def test_code_21566_is_recorded_as_authoritative_quota_signal(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    uploader = tmp_path / "quota.sh"
    uploader.write_text(
        "#!/bin/bash\n"
        'echo "ResponseData { code: 21566, message: 投稿过于频繁 }" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(uploader),
    ]) == 1
    finished = _ledger_rows(ledger)[-1]
    assert finished["quota_frequency_code"] == 21566
    assert "QUOTA AUTHORITATIVE" in capsys.readouterr().err


def test_public_verification_failure_never_writes_success_or_allows_reupload(
    tmp_path, monkeypatch, capsys
):
    _, _, manifest = _mk(tmp_path)
    monkeypatch.setattr(
        au,
        "public_verify_flow",
        lambda *args, **kwargs: {
            "schema_version": "authorized-upload-public-verify.v2",
            "status": "PUBLIC_VERIFY_FAILED",
            "problems": ["Creator archive title mismatch"],
            "public_view": {"aid": 111, "cid": 222},
        },
    )
    ledger = tmp_path / "ledger.jsonl"
    uploader = _stub_uploader(tmp_path)
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(uploader), "--public-wait", "0",
    ]) == 6
    rows = _ledger_rows(ledger)
    assert rows[-1]["rc"] == 6
    assert rows[-1]["uploader_rc"] == 0
    assert not (tmp_path / "clip.uploaded.json").exists()
    assert json.loads((tmp_path / "clip.public_verify.json").read_text())["status"] == "PUBLIC_VERIFY_FAILED"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(uploader),
    ]) == 6
    assert "already created a Bilibili archive" in capsys.readouterr().err


def test_success_sidecars_and_ledger_bind_package_attestation(tmp_path):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(_stub_uploader(tmp_path)),
    ]) == 0
    manifest_data = json.loads(manifest.read_text())
    uploaded = json.loads((tmp_path / "clip.uploaded.json").read_text())
    public = json.loads((tmp_path / "clip.public_verify.json").read_text())
    finished = _ledger_rows(ledger)[-1]
    assert public["status"] == "VERIFIED_PUBLIC"
    assert uploaded["status"] == "VERIFIED_PUBLIC"
    for name in ("package_audit", "record", "subtitle", "review_manifest"):
        key = f"{name}_sha256"
        assert uploaded[key] == manifest_data["package_attestation"][name]["sha256"]
        assert finished[key] == manifest_data["package_attestation"][name]["sha256"]


def test_audit_content_binding_tolerates_auditor_identity_churn():
    """1573 在飞事务案（2026-07-27）：B站审核窗横跨多次部署，
    policy_fingerprint/auditor_source_sha256 变了但内容判决逐字相同——
    canonical 等值只比内容判决；字节/issue 漂移仍拒。"""

    from src.autoslice import package_audit_binding as mod

    stored = {
        "schema_version": "v",
        "policy_epoch": 3,
        "policy_fingerprint": "OLD",
        "auditor_source_sha256": "OLDSHA",
        "passed": True,
        "root": "/x",
        "audited_inputs": {"a.mp4": "sha256:1"},
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }
    current = dict(stored, policy_fingerprint="NEW", auditor_source_sha256="NEWSHA")
    assert mod.audit_content_binding(stored) == mod.audit_content_binding(current)

    drifted = dict(current, audited_inputs={"a.mp4": "sha256:2"})
    assert mod.audit_content_binding(stored) != mod.audit_content_binding(drifted)
    new_issue = dict(current, issues=[{"code": "X"}], issue_count=1)
    assert mod.audit_content_binding(stored) != mod.audit_content_binding(new_issue)
