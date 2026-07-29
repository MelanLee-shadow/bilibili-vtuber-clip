from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.authorized_upload import UploadLockBusy, exclusive_upload_lock
import src.autoslice.final_human_review as final_human_review
import src.autoslice.same_bv_repair as same_bv
import src.autoslice.same_bv_cover_reconciliation as cover_reconciliation
from src.autoslice.same_bv_repair import (
    DuplicateBvid,
    JournalCorrupt,
    PlanInvalid,
    RemoteMutationError,
    append_journal,
    create_plan,
    initialise_journal,
    load_plan,
    normalise_snapshot,
    read_journal,
    repair_status,
    repair_step,
    run_repair,
    sha256_file,
    validate_plan,
    validate_repair_publication_target,
    write_plan,
)

BVID = "BV1Mug46EEQz"
OLD_CID = 101
NEW_CID = 202
COVER_URL = "https://img.example/new-cover.png"
FINAL_TITLE = "【李豆沙】新标题"
PUBLICATION_AUTHORITY = {
    "schema_version": "test-recovery-publication-authority.v1",
    "candidate_id": "candidate-test",
    "bvid": BVID,
    "aid": 42,
    "cid": OLD_CID,
    "title_mode": "preserve_verified_public",
    "observed_public_title": FINAL_TITLE,
    "authority_sha256": "sha256:" + "a" * 64,
}


@pytest.fixture(autouse=True)
def _stub_recovery_publication_authority(monkeypatch):
    def validate(value, *, candidate_id, expected_final_title=None):
        if (
            value != PUBLICATION_AUTHORITY
            or candidate_id != PUBLICATION_AUTHORITY["candidate_id"]
            or (expected_final_title is not None and expected_final_title != FINAL_TITLE)
        ):
            raise same_bv.RecoveryTitleAuthorityError(
                "test recovery publication authority mismatch"
            )
        return copy.deepcopy(PUBLICATION_AUTHORITY)

    monkeypatch.setattr(same_bv, "validate_recovery_publication_authority", validate)


@pytest.fixture(autouse=True)
def _stub_final_media_review_contract(tmp_path, monkeypatch):
    contract_path = tmp_path / "final-media-review-contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": ("lidousha-final-media-review-contracts.v1"),
                "authority": "same-bv repair test exact review point",
                "contracts": [
                    {
                        "candidate_id": PUBLICATION_AUTHORITY["candidate_id"],
                        "subtitle_review_points": [
                            {
                                "point_id": "corrected-cue",
                                "final_video_start_ms": 13_000,
                                "final_video_end_ms": 15_000,
                                "expectation": ("字幕与该段最终烧录人声一致"),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        final_human_review,
        "FINAL_MEDIA_REVIEW_CONTRACT_PATH",
        contract_path,
    )


def _before_snapshot() -> dict:
    creator_metadata = {
        "title": "旧标题",
        "desc": "https://live.bilibili.com/\n简介",
        "tags": ["李豆沙", "直播切片"],
        "tid": 21,
        "copyright": 2,
        "source": "https://live.bilibili.com/",
        "cover": "https://img.example/old-cover.png",
    }
    public_metadata = {key: value for key, value in creator_metadata.items() if key != "source"}
    return {
        "creator": {
            "available": True,
            "bvid": BVID,
            "aid": 42,
            "state": 0,
            "state_desc": "开放浏览",
            "metadata": creator_metadata,
            "videos": [{"cid": OLD_CID, "filename": "old-file", "title": "旧标题"}],
        },
        "public": {
            "available": True,
            "bvid": BVID,
            "aid": 42,
            "cid": OLD_CID,
            "state": 0,
            "metadata": public_metadata,
        },
        "section": {
            "available": True,
            "section_id": 9320779,
            "matches": [
                {
                    "bvid": BVID,
                    "aid": 42,
                    "cid": OLD_CID,
                    "title": "旧标题",
                }
            ],
        },
    }


def _manifest(tmp_path: Path) -> tuple[Path, dict]:
    video = tmp_path / "new.mp4"
    cover = tmp_path / "new.png"
    subtitle = tmp_path / "new.srt"
    video.write_bytes(b"new exact video")
    cover.write_bytes(b"new exact cover")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n测试\n",
        encoding="utf-8",
    )
    source_claim = "李豆沙与联动对象均可见"
    narrative = "封面文字准确表达两人互动"
    record = {
        "duration_ms": 180_000,
        "burned_preview": {"branding_intro": {"verification": {"duration_ms": 186_000}}},
        "story_contract": {
            "candidate_id": PUBLICATION_AUTHORITY["candidate_id"],
            "cover_reference_authority": {
                "source_visible_claims": [source_claim],
                "narrative_presentation": narrative,
            },
        },
        "publish_staging": {"title": FINAL_TITLE},
        "recovery_publication_authority": copy.deepcopy(PUBLICATION_AUTHORITY),
    }
    record_path = tmp_path / "new.record.json"
    record_path.write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    review = {
        "status": ("finished_review_package_no_upload_pending_human_review"),
        "upload_allowed": False,
        "exact_candidate_ids": [PUBLICATION_AUTHORITY["candidate_id"]],
        "selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [PUBLICATION_AUTHORITY["candidate_id"]],
        },
        "items": [
            {
                "candidate_id": PUBLICATION_AUTHORITY["candidate_id"],
                "title": FINAL_TITLE,
                "video": video.name,
                "mp4": video.name,
                "subtitle_srt": subtitle.name,
                "cover": cover.name,
                "record": record_path.name,
                "recovery_publication_authority": copy.deepcopy(PUBLICATION_AUTHORITY),
            }
        ],
    }
    review_path = tmp_path / "review_manifest.json"
    review_path.write_text(
        json.dumps(review, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    package_audit_path = tmp_path / "package-audit.json"
    package_audit_path.write_text(
        json.dumps(
            {"passed": True, "blocking_issue_count": 0},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    candidate_id = PUBLICATION_AUTHORITY["candidate_id"]
    artifact_bindings = {
        kind: {
            "path": path.name,
            "sha256": "sha256:" + sha256_file(path),
        }
        for kind, path in {
            "video": video,
            "subtitle": subtitle,
            "cover": cover,
        }.items()
    }
    record_binding = {
        "path": record_path.name,
        "sha256": "sha256:" + sha256_file(record_path),
    }
    reviewer = {
        "reviewer_kind": "delegated_root_agent",
        "reviewed_by": "Codex root",
        "reviewed_at": "2026-07-23T23:00:00-04:00",
        "approval_quote": "完整播放并核对最终成片",
    }
    check_details = {
        "final_burned_full_playback": (
            "从00:00开头连续播放到EOS结尾，最后一帧停在互动自然收束处。"
        ),
        "subtitle_audio": ("在00:13实际听到纠错台词，烧录字幕随音频发声同步出现。"),
        "silence_hallucination": ("复看静音段确认无声，画面没有多出幻听字幕cue。"),
        "boundary_closure": ("开头保留完整问句，结尾落在回答后的自然停顿。"),
        "title_story": ("最终标题写明双人互动，完整故事先提问再回答。"),
        "cover_identity": ("最终封面左侧人物身份与右侧联动立绘均清楚可辨。"),
        "cover_story": ("最终封面文字概括双人关系，叙事与画面故事一致。"),
        "intro_timing": ("片头结束后平滑切入正片第一句，衔接没有吞字。"),
    }
    point_detail = "在00:13到00:15实际听到纠错台词，烧录字幕cue与发声同步。"
    claim_rows = (
        (
            source_claim,
            "SOURCE_FRAME",
            "最终封面源帧左侧和右侧人物均清楚可见，身份没有被大字遮挡。",
        ),
        (
            narrative,
            "COVER_TEXT",
            "最终封面大字呈现双人互动故事，文字叙事与版式内容一致。",
        ),
    )
    review_contract_sha256 = "sha256:" + sha256_file(
        final_human_review.FINAL_MEDIA_REVIEW_CONTRACT_PATH
    )
    evidence = {
        "schema_version": (final_human_review.REVIEW_EVIDENCE_SCHEMA_VERSION),
        "bindings": {
            "review_contract_sha256": review_contract_sha256,
            "review_manifest": {
                "path": review_path.name,
                "sha256": "sha256:" + sha256_file(review_path),
            },
            "package_audit": {
                "path": package_audit_path.name,
                "sha256": "sha256:" + sha256_file(package_audit_path),
            },
            "items": [
                {
                    "candidate_id": candidate_id,
                    "record": record_binding,
                    "artifacts": artifact_bindings,
                }
            ],
        },
        **reviewer,
        "items": [
            {
                "candidate_id": candidate_id,
                "checks": {
                    check: {
                        "anchor": final_human_review._CHECK_ANCHORS[check],
                        "detail": detail,
                    }
                    for check, detail in check_details.items()
                },
                "subtitle_review_points": [
                    {
                        "point_id": "corrected-cue",
                        "observation": {
                            "anchor": ("00:00:13.000-00:00:15.000"),
                            "detail": point_detail,
                        },
                    }
                ],
                "cover_story_claims": [
                    {
                        "claim": claim,
                        "presentation": presentation,
                        "observation": {
                            "anchor": (f"FINAL_COVER/{presentation}"),
                            "detail": detail,
                        },
                    }
                    for claim, presentation, detail in claim_rows
                ],
            }
        ],
    }
    evidence_path = tmp_path / "final-human-review-evidence.v2.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    receipt = {
        "schema_version": final_human_review.SCHEMA_VERSION,
        "scope": "same_bv_repair",
        "status": "ACCEPTED_FOR_SAME_BV",
        **reviewer,
        "review_contract_sha256": review_contract_sha256,
        "package_evidence": {
            "review_manifest": {
                "path": review_path.name,
                "sha256": "sha256:" + sha256_file(review_path),
            },
            "package_audit": {
                "path": package_audit_path.name,
                "sha256": "sha256:" + sha256_file(package_audit_path),
            },
            "review_evidence": {
                "path": evidence_path.name,
                "sha256": "sha256:" + sha256_file(evidence_path),
                "bytes": evidence_path.stat().st_size,
            },
        },
        "items": [
            {
                "candidate_id": candidate_id,
                "reviewed_title": FINAL_TITLE,
                "artifacts": artifact_bindings,
                "record": record_binding,
                "publication_target": {
                    "candidate_id": candidate_id,
                    "bvid": BVID,
                    "aid": 42,
                    "cid": OLD_CID,
                    "final_title": FINAL_TITLE,
                    "authority_sha256": PUBLICATION_AUTHORITY["authority_sha256"],
                },
                "checks": {
                    check: {
                        "status": "PASS",
                        "evidence": (f"{final_human_review._CHECK_ANCHORS[check]} — {detail}"),
                    }
                    for check, detail in check_details.items()
                },
                "subtitle_review_points": [
                    {
                        "point_id": "corrected-cue",
                        "final_video_start_ms": 13_000,
                        "final_video_end_ms": 15_000,
                        "expectation": "字幕与该段最终烧录人声一致",
                        "status": "PASS",
                        "evidence": ("00:00:13.000-00:00:15.000 — " + point_detail),
                    }
                ],
                "cover_story_claims": [
                    {
                        "claim": claim,
                        "presentation": presentation,
                        "status": "PASS",
                        "evidence": (f"FINAL_COVER/{presentation} — {detail}"),
                    }
                    for claim, presentation, detail in claim_rows
                ],
            }
        ],
    }
    receipt_path = tmp_path / "final_human_review.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "manifest_version": 3,
        "schema_version": "authorized-upload-manifest.v3",
        "video": {
            "path": str(video.resolve()),
            "sha256": sha256_file(video),
            "bytes": video.stat().st_size,
        },
        "cover": {
            "path": str(cover.resolve()),
            "sha256": sha256_file(cover),
            "bytes": cover.stat().st_size,
        },
        "title": FINAL_TITLE,
        "description": "https://live.bilibili.com/\n简介",
        "tags": ["李豆沙", "直播切片"],
        "publish_policy": {
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "season": {
            "season_id": 8383206,
            "section_id": 9320779,
            "season_title": "小李切片",
        },
        "authorization": {"by": "Ivan", "quote": "尽量上传"},
        "recovery_publication_authority": copy.deepcopy(PUBLICATION_AUTHORITY),
        "package_attestation": {
            "schema_version": "authorized-upload-package-attestation.v1",
            "package_root": str(tmp_path.resolve()),
            "review_manifest": {
                "path": str(review_path.resolve()),
                "sha256": sha256_file(review_path),
                "bytes": review_path.stat().st_size,
            },
            "package_audit": {
                "path": str(package_audit_path.resolve()),
                "sha256": sha256_file(package_audit_path),
                "bytes": package_audit_path.stat().st_size,
            },
            "final_human_review": {
                "path": str(receipt_path.resolve()),
                "sha256": sha256_file(receipt_path),
                "bytes": receipt_path.stat().st_size,
            },
        },
    }
    path = tmp_path / "new.upload_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, manifest


def _plan_authority(tmp_path: Path, *, initialise: bool = True):
    manifest_path, manifest = _manifest(tmp_path)
    plan = create_plan(
        manifest_path=manifest_path,
        manifest=manifest,
        bvid=BVID,
        snapshot=_before_snapshot(),
    )
    plan_path = tmp_path / "repair.plan.json"
    write_plan(plan_path, plan)
    journal = tmp_path / "repair.journal.jsonl"
    if initialise:
        initialise_journal(journal, plan_path, plan)
    return manifest, plan, plan_path, journal


def _completed_predecessor(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
    )
    assert result.state == "VERIFIED"
    rows = read_journal(journal)
    rows[-1]["details"]["live_snapshot"]["creator"]["state"] = 0
    rows[-1]["details"]["live_snapshot"]["creator"]["state_desc"] = "\u5f00\u653e\u6d4f\u89c8"
    rows[-1]["row_sha256"] = same_bv._row_hash(
        {
            key: value
            for key, value in rows[-1].items()
            if key != "row_sha256"
        }
    )
    journal.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    verified = read_journal(journal)[-1]
    snapshot = copy.deepcopy(verified["details"]["live_snapshot"])
    completed = {
        "schema_version": "same-bv-repair-completed.v1",
        "status": "VERIFIED_FRESH_LIVE",
        "rc": 0,
        "verified_at": "2026-07-29T00:00:00+00:00",
        "remote_mutation": False,
        "candidate_id": PUBLICATION_AUTHORITY["candidate_id"],
        "bvid": BVID,
        "aid": PUBLICATION_AUTHORITY["aid"],
        "new_cid": NEW_CID,
        "plan": {
            "path": str(plan_path.resolve()),
            "sha256": sha256_file(plan_path),
            "plan_id": plan["plan_id"],
        },
        "manifest": copy.deepcopy(plan["manifest"]),
        "replacement": copy.deepcopy(plan["replacement"]),
        "verified_journal_row": {
            "journal_path": str(journal.resolve()),
            "seq": verified["seq"],
            "at": verified["at"],
            "row_sha256": verified["row_sha256"],
        },
        "live_snapshot": snapshot,
    }
    completed_path = tmp_path / "same-bv-repair-completed.json"
    completed_path.write_text(
        json.dumps(completed, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return completed_path, snapshot


class FakeAdapter:
    def __init__(
        self,
        plan: dict,
        *,
        append_mode: str = "success",
        swap_mode: str = "success",
        section_sync_mode: str = "success",
        publish_immediately: bool = True,
    ) -> None:
        self.plan = plan
        self.snapshot = copy.deepcopy(plan["before"])
        self.append_mode = append_mode
        self.swap_mode = swap_mode
        self.section_sync_mode = section_sync_mode
        self.publish_immediately = publish_immediately
        self.append_calls = 0
        self.cover_calls = 0
        self.swap_calls = 0
        self.section_sync_calls = 0
        self.observe_calls = 0

    def observe(self, bvid: str, section_id: int) -> dict:
        self.observe_calls += 1
        assert bvid == BVID
        assert section_id == 9320779
        return copy.deepcopy(self.snapshot)

    def _append_new(self) -> None:
        self.snapshot["creator"]["videos"].append(
            {"cid": NEW_CID, "filename": "new-file", "title": "new-file"}
        )

    def append_existing(self, bvid: str, media_path: Path) -> None:
        self.append_calls += 1
        assert bvid == BVID
        assert media_path.read_bytes() == b"new exact video"
        if self.append_mode == "raise_before":
            raise RuntimeError("transport failed before known response")
        self._append_new()
        if self.append_mode == "crash_after":
            raise SystemExit("simulated process death after remote append")

    def prepare_cover(self, cover_path: Path) -> str:
        self.cover_calls += 1
        assert cover_path.read_bytes() == b"new exact cover"
        return COVER_URL

    def _make_creator_target(self, target_metadata: dict) -> None:
        target = copy.deepcopy(target_metadata)
        target["cover"] = "//img.example/new-cover.png"
        self.snapshot["creator"]["metadata"] = target
        self.snapshot["creator"]["videos"] = [
            {
                "cid": NEW_CID,
                "filename": "new-file",
                "title": target["title"],
            }
        ]
        self.snapshot["creator"]["state"] = -30
        self.snapshot["creator"]["state_desc"] = "审核中"

    def make_public_target(self, *, section_title: str | None = None) -> None:
        target = copy.deepcopy(self.plan["target_metadata"])
        target["cover"] = "//img.example/new-cover.png"
        self.snapshot["public"] = {
            "available": True,
            "bvid": BVID,
            "aid": 42,
            "cid": NEW_CID,
            "state": 0,
            "metadata": {key: value for key, value in target.items() if key != "source"},
        }
        self.snapshot["section"]["matches"] = [
            {
                "bvid": BVID,
                "aid": 42,
                "cid": NEW_CID,
                "title": section_title or target["title"],
            }
        ]

    def swap_keep_only(
        self,
        bvid: str,
        *,
        keep_cid: int,
        target_metadata: dict,
        cover_url: str,
    ) -> dict:
        self.swap_calls += 1
        assert bvid == BVID
        assert keep_cid == NEW_CID
        assert cover_url in {COVER_URL, "//img.example/new-cover.png"}
        if self.swap_mode == "21540_once" and self.swap_calls == 1:
            raise RemoteMutationError("稿件编辑中", code=21540)
        if self.swap_mode == "fatal":
            raise RemoteMutationError("permission denied", code=-403)
        self._make_creator_target(target_metadata)
        if self.publish_immediately:
            self.make_public_target()
        if self.swap_mode == "timeout_after_success":
            raise TimeoutError("response lost after successful edit")
        return {"code": 0}

    def sync_section_title(
        self,
        bvid: str,
        section_id: int,
        *,
        expected_current_title: str,
        target_title: str,
    ) -> dict:
        self.section_sync_calls += 1
        assert bvid == BVID
        assert section_id == 9320779
        assert expected_current_title == "旧标题"
        assert target_title == FINAL_TITLE
        if self.section_sync_mode == "raise_before":
            raise RuntimeError("section title edit response failed")
        if self.section_sync_mode != "no_change":
            self.snapshot["section"]["matches"][0]["title"] = target_title
        if self.section_sync_mode == "crash_after":
            raise SystemExit("simulated death after section title edit")
        return {"code": 0}


def _make_cover_alias_false_block(
    tmp_path: Path,
    *,
    blocked_reason: str = ("post-swap observation is neither exact two-P nor exact single-new"),
    snapshot_cover_hash: str = "f40a21b0c123456789abcdef0123456789abcdef",
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    new_video = {
        "cid": NEW_CID,
        "filename": "new-file",
        "title": "new-file",
    }
    frozen_hash = "f40a21b0c123456789abcdef0123456789abcdef"
    cover_url = f"https://archive.biliimg.com/bfs/archive/{frozen_hash}.png"
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
        details={"remote_mutation": "simulated"},
    )
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="TWO_P_READY",
        details={"new_video": new_video, "remote_mutation": False},
    )
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="SWAP_RETRYABLE",
        details={
            "new_video": new_video,
            "cover_url": cover_url,
            "remote_mutation": "edit_keep_only_new_cid",
        },
    )
    adapter._make_creator_target(plan["target_metadata"])
    adapter.make_public_target()
    projected_cover = f"//i0.hdslb.com/bfs/archive/{snapshot_cover_hash}.png"
    adapter.snapshot["creator"]["metadata"]["cover"] = projected_cover
    adapter.snapshot["public"]["metadata"]["cover"] = projected_cover
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="BLOCKED_DRIFT",
        details={
            "reason": blocked_reason,
            "snapshot": copy.deepcopy(adapter.snapshot),
            "remote_mutation": False,
        },
    )
    return manifest, plan, plan_path, journal, adapter


def test_bilibili_cover_asset_identity_folds_only_known_cdn_aliases():
    asset = "f40a21b0c123456789abcdef0123456789abcdef"

    assert same_bv._normalise_cover_url(
        f"https://archive.biliimg.com/bfs/archive/{asset}.png"
    ) == same_bv._normalise_cover_url(f"//i0.hdslb.com/bfs/archive/{asset}.png")
    assert same_bv._normalise_cover_url(
        f"https://unrelated.example/bfs/archive/{asset}.png"
    ) != same_bv._normalise_cover_url(f"https://another.example/bfs/archive/{asset}.png")
    assert same_bv._normalise_cover_url(
        "https://archive.biliimg.com/not-an-archive-cover.png"
    ) != same_bv._normalise_cover_url("https://i0.hdslb.com/not-an-archive-cover.png")


def test_cover_alias_false_block_reconciles_without_remote_mutation(tmp_path):
    manifest, _plan, plan_path, journal, adapter = _make_cover_alias_false_block(tmp_path)
    before = journal.read_bytes()

    dry_run = cover_reconciliation.reconcile_cover_alias_false_block(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        commit=False,
    )

    assert dry_run.state == "VERIFIED"
    assert dry_run.changed is False
    assert dry_run.details["would_append_journal"] is True
    assert journal.read_bytes() == before
    assert (adapter.append_calls, adapter.cover_calls, adapter.swap_calls) == (
        0,
        0,
        0,
    )

    applied = cover_reconciliation.reconcile_cover_alias_false_block(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        commit=True,
    )

    assert applied.state == "VERIFIED"
    assert applied.changed is True
    assert (adapter.append_calls, adapter.cover_calls, adapter.swap_calls) == (
        0,
        0,
        0,
    )
    last = read_journal(journal)[-1]
    assert last["state"] == "VERIFIED"
    assert (
        last["details"]["reconciliation"]["blocked_row_sha256"]
        == read_journal(journal)[-2]["row_sha256"]
    )
    assert last["details"]["remote_mutation"] is False


def test_cover_alias_reconciliation_can_resume_from_public_pending(tmp_path):
    manifest, plan, plan_path, journal, adapter = _make_cover_alias_false_block(tmp_path)
    adapter.snapshot["public"] = copy.deepcopy(plan["before"]["public"])
    adapter.snapshot["section"] = copy.deepcopy(plan["before"]["section"])

    reconciled = cover_reconciliation.reconcile_cover_alias_false_block(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        commit=True,
    )

    assert reconciled.state == "PUBLIC_PENDING"
    assert read_journal(journal)[-1]["state"] == "PUBLIC_PENDING"
    adapter.make_public_target()
    adapter.snapshot["public"]["metadata"]["cover"] = adapter.snapshot["creator"]["metadata"][
        "cover"
    ]
    finished = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert finished.state == "VERIFIED"
    assert (adapter.append_calls, adapter.cover_calls, adapter.swap_calls) == (
        0,
        0,
        0,
    )


@pytest.mark.parametrize(
    ("blocked_reason", "snapshot_cover_hash"),
    [
        ("unrelated real drift", "f40a21b0c123456789abcdef0123456789abcdef"),
        (
            "post-swap observation is neither exact two-P nor exact single-new",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
    ],
)
def test_cover_alias_reconciliation_refuses_other_blocks(
    tmp_path,
    blocked_reason,
    snapshot_cover_hash,
):
    manifest, _plan, plan_path, journal, adapter = _make_cover_alias_false_block(
        tmp_path,
        blocked_reason=blocked_reason,
        snapshot_cover_hash=snapshot_cover_hash,
    )
    before = journal.read_bytes()

    result = cover_reconciliation.reconcile_cover_alias_false_block(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        commit=True,
    )

    assert result.state == "BLOCKED_DRIFT"
    assert result.changed is False
    assert journal.read_bytes() == before
    assert (adapter.append_calls, adapter.cover_calls, adapter.swap_calls) == (
        0,
        0,
        0,
    )


def test_happy_path_verifies_exact_new_cid_metadata_and_section(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
    )

    assert result.state == "VERIFIED"
    assert adapter.append_calls == 1
    assert adapter.swap_calls == 1
    states = [row["state"] for row in read_journal(journal)]
    assert states == [
        "PLANNED",
        "APPEND_INTENT",
        "APPEND_AMBIGUOUS",
        "TWO_P_READY",
        "SWAP_RETRYABLE",
        "CREATOR_SINGLE_NEW",
        "VERIFIED",
    ]
    verified = read_journal(journal)[-1]["details"]["live_snapshot"]
    assert verified["creator"]["videos"][0]["cid"] == NEW_CID
    assert verified["public"]["cid"] == NEW_CID
    assert verified["section"]["matches"][0]["cid"] == NEW_CID


def test_crash_after_append_response_resumes_by_polling_without_reappend(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, append_mode="crash_after")

    with pytest.raises(SystemExit, match="process death"):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )
    assert read_journal(journal)[-1]["state"] == "APPEND_INTENT"
    assert adapter.append_calls == 1

    adapter.append_mode = "success"
    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=1,
        poll_seconds=0,
    )
    assert result.state == "VERIFIED"
    assert adapter.append_calls == 1


def test_crash_before_append_effect_is_permanently_ambiguous_and_never_retries(
    tmp_path,
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, append_mode="raise_before")

    first = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert first.state == "APPEND_AMBIGUOUS"
    assert adapter.append_calls == 1
    adapter.append_mode = "success"

    second = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    third = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert second.state == third.state == "APPEND_AMBIGUOUS"
    assert adapter.append_calls == 1
    assert adapter.snapshot["creator"]["videos"][0]["cid"] == OLD_CID


def test_edit_21540_retries_the_same_cid_and_payload(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, swap_mode="21540_once")

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=5,
        poll_seconds=0,
    )

    assert result.state == "VERIFIED"
    assert adapter.append_calls == 1
    assert adapter.swap_calls == 2
    retry_rows = [row for row in read_journal(journal) if row["state"] == "SWAP_RETRYABLE"]
    assert any("21540" in str(row["details"].get("reason")) for row in retry_rows)
    assert {row["details"]["new_video"]["cid"] for row in retry_rows} == {NEW_CID}
    assert {row["details"]["cover_url"] for row in retry_rows} == {COVER_URL}


def test_swap_timeout_but_live_success_advances_without_second_edit(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, swap_mode="timeout_after_success")

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "VERIFIED"
    assert adapter.swap_calls == 1
    creator_row = next(row for row in read_journal(journal) if row["state"] == "CREATOR_SINGLE_NEW")
    assert creator_row["details"]["swap_observation"] == ("live success after ambiguous error")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot["creator"]["videos"].append(
            {"cid": 303, "filename": "unknown", "title": "unknown"}
        ),
        lambda snapshot: snapshot["creator"]["videos"].__setitem__(
            1, {"cid": OLD_CID, "filename": "dup", "title": "dup"}
        ),
        lambda snapshot: snapshot["section"]["matches"].append(
            {
                "bvid": BVID,
                "aid": 42,
                "cid": OLD_CID,
                "title": "重复 BV",
            }
        ),
    ],
)
def test_unknown_three_p_duplicate_cid_or_duplicate_section_bvid_blocks(tmp_path, mutate):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    adapter._append_new()
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
        details={"remote_mutation": "simulated"},
    )
    mutate(adapter.snapshot)

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "BLOCKED_DRIFT"
    assert adapter.append_calls == 0
    assert adapter.swap_calls == 0


def test_wrong_public_cid_after_creator_swap_blocks(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)
    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
    )
    assert result.state == "PUBLIC_PENDING"
    adapter.snapshot["public"]["cid"] = 999999

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state == "BLOCKED_DRIFT"


@pytest.mark.parametrize("field", ["title", "desc", "tid", "copyright", "source"])
def test_creator_metadata_drift_after_swap_blocks(tmp_path, field):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)
    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state == "PUBLIC_PENDING"
    adapter.snapshot["creator"]["metadata"][field] = "DRIFT"

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state == "BLOCKED_DRIFT"


def test_public_pending_then_verified_without_more_mutation(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)

    first = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert first.state == "PUBLIC_PENDING"
    assert adapter.append_calls == adapter.swap_calls == 1

    adapter.make_public_target()
    second = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert second.state == "VERIFIED"
    assert adapter.append_calls == adapter.swap_calls == 1


def test_section_old_title_only_is_synced_once_then_verified(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)
    assert (
        run_repair(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "PUBLIC_PENDING"
    )
    adapter.make_public_target(section_title="旧标题")

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "VERIFIED"
    assert adapter.section_sync_calls == 1
    states = [row["state"] for row in read_journal(journal)]
    assert states[-3:] == [
        "SECTION_TITLE_SYNC_INTENT",
        "SECTION_TITLE_SYNC_AMBIGUOUS",
        "VERIFIED",
    ]


def test_crash_after_section_title_sync_resumes_without_reedit(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(
        plan,
        publish_immediately=False,
        section_sync_mode="crash_after",
    )
    assert (
        run_repair(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "PUBLIC_PENDING"
    )
    adapter.make_public_target(section_title="旧标题")

    with pytest.raises(SystemExit, match="death after section title"):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )
    assert read_journal(journal)[-1]["state"] == "SECTION_TITLE_SYNC_INTENT"
    assert adapter.section_sync_calls == 1

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state == "VERIFIED"
    assert adapter.section_sync_calls == 1


def test_ambiguous_section_title_sync_is_never_retried(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(
        plan,
        publish_immediately=False,
        section_sync_mode="no_change",
    )
    assert (
        run_repair(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "PUBLIC_PENDING"
    )
    adapter.make_public_target(section_title="旧标题")

    first = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    second = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert first.state == second.state == "SECTION_TITLE_SYNC_AMBIGUOUS"
    assert first.changed is True
    assert second.changed is False
    assert adapter.section_sync_calls == 1
    assert (
        repair_status(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
        ).details["next_action"]
        == "POLL_ONLY_NEVER_REEDIT_SECTION_TITLE"
    )


def test_unexpected_section_title_blocks_without_sync(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)
    assert (
        run_repair(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "PUBLIC_PENDING"
    )
    adapter.make_public_target(section_title="第三种标题")

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "BLOCKED_DRIFT"
    assert adapter.section_sync_calls == 0


def test_section_title_sync_waits_for_public_target(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "PUBLIC_PENDING"
    assert adapter.section_sync_calls == 0


def test_production_adapter_rebinds_exact_live_episode_identity():
    class Session:
        def __init__(self):
            self.edit_kwargs = None

        def archive_view(self, bvid):
            assert bvid == BVID
            return {
                "archive": {
                    "bvid": BVID,
                    "aid": 42,
                    "title": FINAL_TITLE,
                },
                "videos": [{"cid": NEW_CID}],
            }

        def season_episode_edit(self, **kwargs):
            self.edit_kwargs = kwargs
            return {"code": 0}

    session = Session()
    section_payload = {
        "code": 0,
        "data": {
            "id": 9320779,
            "episodes": [
                {
                    "id": 210909973,
                    "title": "旧标题",
                    "aid": 42,
                    "bvid": BVID,
                    "cid": NEW_CID,
                    "seasonId": 8383206,
                    "sectionId": 9320779,
                    "order": 68,
                }
            ],
        },
    }
    adapter = same_bv.BilibiliRepairAdapter(
        session=session,
        http=lambda url: section_payload,
        view_url="https://example.test/view?bvid={bvid}",
        tags_url="https://example.test/tags?bvid={bvid}",
        section_url="https://example.test/section/{section_id}",
    )

    response = adapter.sync_section_title(
        BVID,
        9320779,
        expected_current_title="旧标题",
        target_title=FINAL_TITLE,
    )

    assert response == {"code": 0}
    assert session.edit_kwargs == {
        "episode_id": 210909973,
        "title": FINAL_TITLE,
        "aid": 42,
        "cid": NEW_CID,
        "season_id": 8383206,
        "section_id": 9320779,
        "order": 68,
        "page_cids": [NEW_CID],
    }


def test_verified_terminal_is_idempotent_even_if_adapter_would_fail(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    assert (
        run_repair(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "VERIFIED"
    )
    counts = (adapter.append_calls, adapter.cover_calls, adapter.swap_calls)
    adapter.snapshot = {"corrupt": True}

    again = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert again.state == "VERIFIED"
    assert (adapter.append_calls, adapter.cover_calls, adapter.swap_calls) == counts
    assert read_journal(journal)[-1]["state"] == "VERIFIED"


def test_non_retryable_swap_error_blocks_without_a_second_edit(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, swap_mode="fatal")

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "BLOCKED_DRIFT"
    assert adapter.swap_calls == 1


def test_journal_partial_row_hash_edit_and_invalid_transition_are_corruption(
    tmp_path,
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    original = journal.read_bytes()
    journal.write_bytes(original.rstrip(b"\n"))
    with pytest.raises(JournalCorrupt, match="partial"):
        read_journal(journal)

    journal.write_bytes(original.replace(b"PLANNED", b"VERIFIED", 1))
    with pytest.raises(JournalCorrupt, match="hash mismatch"):
        read_journal(journal)

    journal.write_bytes(original)
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
    )
    # A syntactically valid but illegal VERIFIED transition must also fail.
    rows = read_journal(journal)
    body = {key: value for key, value in rows[-1].items() if key != "row_sha256"}
    body["seq"] = 3
    body["previous_row_sha256"] = rows[-1]["row_sha256"]
    body["state"] = "VERIFIED"
    import hashlib

    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    body["row_sha256"] = hashlib.sha256(payload).hexdigest()
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")
    with pytest.raises(JournalCorrupt, match="transition"):
        read_journal(journal)


def test_manifest_or_artifact_drift_invalidates_plan_before_remote_action(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    Path(plan["replacement"]["video"]["path"]).write_bytes(b"tampered")
    adapter = FakeAdapter(plan)

    with pytest.raises(Exception, match="hash drift"):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )
    assert adapter.append_calls == adapter.swap_calls == 0


def test_plan_binds_recovery_publication_authority_to_manifest(tmp_path):
    manifest, plan, plan_path, _journal = _plan_authority(tmp_path)
    assert plan["recovery_publication_authority"] == PUBLICATION_AUTHORITY

    tampered_plan = copy.deepcopy(plan)
    tampered_plan["recovery_publication_authority"]["bvid"] = "BV1tTg46UE3y"
    with pytest.raises(PlanInvalid, match="recovery_publication_authority"):
        validate_plan(
            tampered_plan,
            manifest=manifest,
            plan_path=plan_path,
        )


def test_same_bv_target_requires_valid_final_human_review_receipt(tmp_path):
    _manifest_path, manifest = _manifest(tmp_path)
    del manifest["package_attestation"]["final_human_review"]

    with pytest.raises(PlanInvalid, match="FINAL_HUMAN_REVIEW_ATTESTED_FILE_INVALID"):
        validate_repair_publication_target(manifest, BVID)


def test_same_bv_target_rejects_legacy_v1_human_review_receipt(
    tmp_path,
):
    _manifest_path, manifest = _manifest(tmp_path)
    attestation = manifest["package_attestation"]["final_human_review"]
    receipt_path = Path(attestation["path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["schema_version"] = "lidousha-final-human-review.v1"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    attestation.update(
        {
            "sha256": sha256_file(receipt_path),
            "bytes": receipt_path.stat().st_size,
        }
    )

    with pytest.raises(PlanInvalid, match="FINAL_HUMAN_REVIEW_SCHEMA_INVALID"):
        validate_repair_publication_target(manifest, BVID)


def test_plan_freezes_complete_final_human_review_input_closure(tmp_path):
    manifest, plan, _plan_path, _journal = _plan_authority(tmp_path)

    assert plan["package_attestation"] == {
        "package_root": manifest["package_attestation"]["package_root"],
        "review_manifest": manifest["package_attestation"]["review_manifest"],
        "package_audit": manifest["package_attestation"]["package_audit"],
        "final_human_review": manifest["package_attestation"]["final_human_review"],
    }


@pytest.mark.parametrize("drift", ["edit", "delete"])
def test_final_human_review_drift_after_plan_blocks_before_observe_or_mutation(tmp_path, drift):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    receipt = Path(plan["package_attestation"]["final_human_review"]["path"])
    if drift == "edit":
        receipt.write_text(
            receipt.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
    else:
        receipt.unlink()
    journal_before = journal.read_bytes()

    with pytest.raises(
        PlanInvalid,
        match="FINAL_HUMAN_REVIEW_ATTESTED_FILE_(HASH_DRIFT|INVALID)",
    ):
        repair_status(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
        )
    with pytest.raises(
        PlanInvalid,
        match="FINAL_HUMAN_REVIEW_ATTESTED_FILE_(HASH_DRIFT|INVALID)",
    ):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )

    assert adapter.observe_calls == 0
    assert adapter.append_calls == 0
    assert adapter.cover_calls == 0
    assert adapter.swap_calls == 0
    assert journal.read_bytes() == journal_before


def test_committed_review_contract_drift_blocks_before_observe_or_mutation(
    tmp_path,
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    contract_path = final_human_review.FINAL_MEDIA_REVIEW_CONTRACT_PATH
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["authority"] = "changed after plan"
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    journal_before = journal.read_bytes()

    with pytest.raises(PlanInvalid, match="FINAL_HUMAN_REVIEW_CONTRACT_HASH_MISMATCH"):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )

    assert adapter.observe_calls == 0
    assert adapter.append_calls == 0
    assert adapter.cover_calls == 0
    assert adapter.swap_calls == 0
    assert journal.read_bytes() == journal_before


@pytest.mark.parametrize(
    ("surface", "field", "value", "message"),
    [
        ("creator", "aid", 999, "Creator AID differs"),
        ("public", "cid", 999, "public CID differs"),
        ("section", "cid", 999, "section CID differs"),
    ],
)
def test_planning_requires_live_aid_cid_to_match_publication_authority(
    tmp_path, surface, field, value, message
):
    manifest_path, manifest = _manifest(tmp_path)
    snapshot = _before_snapshot()
    if surface == "section":
        snapshot["section"]["matches"][0][field] = value
    else:
        snapshot[surface][field] = value

    with pytest.raises(PlanInvalid, match=message):
        create_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=BVID,
            snapshot=snapshot,
        )


def test_later_repair_requires_and_replays_explicit_completed_predecessor(
    tmp_path,
):
    completed_path, current_snapshot = _completed_predecessor(
        tmp_path / "predecessor"
    )
    next_root = tmp_path / "next"
    next_root.mkdir()
    manifest_path, manifest = _manifest(next_root)

    with pytest.raises(
        PlanInvalid,
        match="sole Creator CID differs from proven planning predecessor",
    ):
        create_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=BVID,
            snapshot=current_snapshot,
        )

    plan = create_plan(
        manifest_path=manifest_path,
        manifest=manifest,
        bvid=BVID,
        snapshot=current_snapshot,
        predecessor_completed_path=completed_path,
    )

    assert plan["before"]["creator"]["videos"][0]["cid"] == NEW_CID
    assert plan["predecessor_completion"]["new_cid"] == NEW_CID
    assert plan["predecessor_completion"]["completed"] == {
        "path": str(completed_path.resolve()),
        "sha256": sha256_file(completed_path),
    }
    validate_plan(plan, manifest=manifest)

    completed = json.loads(completed_path.read_text(encoding="utf-8"))
    shared_journal = Path(
        completed["verified_journal_row"]["journal_path"]
    )
    next_plan_path = next_root / "repair.plan.json"
    write_plan(next_plan_path, plan)
    initialised = initialise_journal(shared_journal, next_plan_path, plan)
    assert initialised["state"] == "PLANNED"
    assert initialised["details"]["lineage_predecessor"] == {
        "plan_id": completed["plan"]["plan_id"],
        "verified_row_sha256": completed["verified_journal_row"][
            "row_sha256"
        ],
    }
    assert read_journal(shared_journal)[-1]["plan_id"] == plan["plan_id"]


def test_completed_predecessor_survives_later_review_package_refresh(
    tmp_path,
):
    """A terminal CID transition is historical authority, not a request to
    republish the old package under today's mutable review-manifest paths."""

    completed_path, current_snapshot = _completed_predecessor(
        tmp_path / "predecessor"
    )
    completed = json.loads(completed_path.read_text(encoding="utf-8"))
    predecessor_plan = json.loads(
        Path(completed["plan"]["path"]).read_text(encoding="utf-8")
    )
    old_review_manifest = Path(
        predecessor_plan["package_attestation"]["review_manifest"]["path"]
    )
    old_review_manifest.write_text(
        old_review_manifest.read_text(encoding="utf-8")
        + "\n",
        encoding="utf-8",
    )

    next_root = tmp_path / "next"
    next_root.mkdir()
    manifest_path, manifest = _manifest(next_root)
    plan = create_plan(
        manifest_path=manifest_path,
        manifest=manifest,
        bvid=BVID,
        snapshot=current_snapshot,
        predecessor_completed_path=completed_path,
    )

    assert plan["predecessor_completion"]["new_cid"] == NEW_CID
    validate_plan(plan, manifest=manifest)


def test_later_repair_refuses_stale_or_tampered_predecessor_completion(
    tmp_path,
):
    completed_path, current_snapshot = _completed_predecessor(
        tmp_path / "predecessor"
    )
    next_root = tmp_path / "next"
    next_root.mkdir()
    manifest_path, manifest = _manifest(next_root)
    drifted = copy.deepcopy(current_snapshot)
    drifted["public"]["cid"] = 999

    with pytest.raises(
        PlanInvalid,
        match="fresh planning snapshot differs from predecessor completion",
    ):
        create_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=BVID,
            snapshot=drifted,
            predecessor_completed_path=completed_path,
        )

    completed = json.loads(completed_path.read_text(encoding="utf-8"))
    completed["verified_journal_row"]["row_sha256"] = "0" * 64
    completed_path.write_text(
        json.dumps(completed, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PlanInvalid, match="journal row_sha256 mismatch"):
        create_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=BVID,
            snapshot=current_snapshot,
            predecessor_completed_path=completed_path,
        )


def test_manifest_authority_drift_refuses_resume_before_observe(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    manifest["recovery_publication_authority"] = {
        **PUBLICATION_AUTHORITY,
        "cid": 999,
    }

    with pytest.raises(PlanInvalid, match="recovery_publication_authority"):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )
    assert adapter.observe_calls == 0
    assert adapter.append_calls == adapter.swap_calls == 0


def test_duplicate_bvid_plan_is_rejected_in_separate_repair_ledger(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    other = copy.deepcopy(plan)
    other["plan_id"] = "different-plan"
    other_path = tmp_path / "other.plan.json"
    write_plan(other_path, other)

    with pytest.raises(DuplicateBvid, match=BVID):
        initialise_journal(journal, other_path, other)


def test_shared_upload_lock_refuses_a_second_repair_owner(tmp_path):
    lock = tmp_path / "upload.lock"
    with exclusive_upload_lock(lock):
        with pytest.raises(UploadLockBusy, match="busy"):
            with exclusive_upload_lock(lock):
                pass


def test_status_and_dry_read_do_not_append_journal(tmp_path):
    manifest, _plan, plan_path, journal = _plan_authority(tmp_path)
    before = journal.read_bytes()

    result = repair_status(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
    )

    assert result.state == "PLANNED"
    assert result.details["next_action"] == "APPEND_EXISTING_ONCE"
    assert journal.read_bytes() == before


def test_plan_and_journal_files_are_create_only(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    assert load_plan(plan_path)["plan_id"] == plan["plan_id"]
    with pytest.raises(Exception, match="already exists"):
        write_plan(plan_path, plan)
    assert initialise_journal(journal, plan_path, plan)["state"] == "PLANNED"
    assert len(read_journal(journal)) == 1


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda snapshot: snapshot["public"]["metadata"].__setitem__("title", "另一标题"),
            "Creator and public metadata disagree",
        ),
        (
            lambda snapshot: snapshot["section"]["matches"][0].__setitem__("title", "另一标题"),
            "section title disagrees",
        ),
    ],
)
def test_planning_refuses_preexisting_cross_surface_metadata_drift(tmp_path, mutate, match):
    manifest_path, manifest = _manifest(tmp_path)
    snapshot = _before_snapshot()
    mutate(snapshot)

    with pytest.raises(PlanInvalid, match=match):
        create_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=BVID,
            snapshot=snapshot,
        )


def test_public_tags_failure_is_unavailable_not_an_empty_tag_observation():
    creator = {
        "archive": {
            "bvid": BVID,
            "aid": 42,
            "state": 0,
            "title": "旧标题",
            "desc": "简介",
            "tag": "李豆沙,直播切片",
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
            "cover": "https://img.example/old-cover.png",
        },
        "videos": [{"cid": OLD_CID, "filename": "old", "title": "旧标题"}],
    }
    public = {
        "code": 0,
        "data": {
            "bvid": BVID,
            "aid": 42,
            "cid": OLD_CID,
            "state": 0,
            "title": "旧标题",
            "desc": "简介",
            "tid": 21,
            "copyright": 2,
            "pic": "https://img.example/old-cover.png",
        },
    }
    section = {
        "code": 0,
        "data": {
            "id": 9320779,
            "episodes": [
                {
                    "bvid": BVID,
                    "aid": 42,
                    "cid": OLD_CID,
                    "title": "旧标题",
                }
            ],
        },
    }

    snapshot = normalise_snapshot(
        bvid=BVID,
        section_id=9320779,
        creator_data=creator,
        public_payload=public,
        public_tags_payload={"code": -500},
        section_payload=section,
    )

    assert snapshot["public"]["available"] is False
    assert snapshot["public"]["metadata"]["tags"] == []


def test_transient_public_read_failure_after_append_polls_without_block_or_reappend(
    tmp_path,
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    adapter._append_new()
    adapter.snapshot["public"]["available"] = False
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
        details={"remote_mutation": "simulated"},
    )

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "APPEND_INTENT"
    assert adapter.append_calls == 0
    assert adapter.swap_calls == 0
    assert read_journal(journal)[-1]["state"] == "APPEND_INTENT"


def test_protocol_relative_cover_url_survives_journal_resume(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    adapter._append_new()
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
        details={"remote_mutation": "simulated"},
    )
    assert (
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "TWO_P_READY"
    )
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="SWAP_RETRYABLE",
        details={
            "new_video": {
                "cid": NEW_CID,
                "filename": "new-file",
                "title": "new-file",
            },
            "cover_url": "//img.example/new-cover.png",
            "remote_mutation": False,
        },
    )

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state in {"CREATOR_SINGLE_NEW", "SWAP_RETRYABLE"}
    assert adapter.append_calls == 0
