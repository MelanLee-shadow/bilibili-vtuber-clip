"""说话人证据不足 → 人工审阅停泊态（Ivan 2026-08-10 裁定）。

覆盖三条硬要求：
  (a) 说话人证据不足落停泊态而**非** ``candidate_rejected``；
  (b) 停泊态**不可上传**、**不进** ``review_ready``；
  (c) 停泊态**不会**进入无限基础设施重试（指纹不变就一动不动）。

外加化石迁移：Ivan 裁定之前已被铸成 ``candidate_rejected`` 的两个真实受害者
（``auto_220747_1271_1323`` / ``auto_213135_62_138``，字段形状取自 free 只读
取证）必须迁回停泊态，否则裁定对它们等于没发生。
"""

from __future__ import annotations

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import speaker_manual_review
from src.autoslice.batch_terminal_state import project_terminal_batch_state
from src.autoslice.delivery_recovery import (
    TALK_RECOVERY_FAILURE_STATUSES,
    apply_talk_backfill_rejection_policy,
)
from src.autoslice.reporting import write_reports


SPEAKER_STATUSES = ("speaker_review_required", "speaker_evidence_insufficient")


def _speaker_failure(status: str, **extra) -> dict:
    result = {
        "candidate_id": "auto_213135_62_138",
        "status": status,
        "failure_kind": "speaker_evidence",
        "failure_stage": "speaker_finalization",
        "failure_recoverable": False,
        "failure_message": (
            "RuntimeError: SPEAKER_FINALIZATION_BLOCKED: "
            "SpeakerFinalizationError: not enough Li Dousha clip anchors: [2]"
        ),
        "hook": "李豆沙让观众猜两个字的收尾曲",
    }
    result.update(extra)
    return result


# --- (a) 转停泊态，不是判死 -------------------------------------------------


@pytest.mark.parametrize("status", SPEAKER_STATUSES)
def test_speaker_failure_parks_for_manual_review_instead_of_rejecting(status):
    """两条分支同命：都停泊等人看，都不铸 candidate_rejected 化石。

    ``SPEAKER_REVIEW_REQUIRED``（流水线主动要求人看，带 cue 清单）与
    ``_speaker_evidence_insufficient_failure``（主播声纹锚点不足）语义不同，
    但 failure_kind 同为 ``speaker_evidence``、修复权威同为人工覆盖件、
    backfill 原因码同为 ``speaker_identity_unresolved_backfilled``——处置同命。
    """

    result = _speaker_failure(status)

    # 返回 True：席位照常让给候补（35fc448 的既有裁定不变）。
    assert apply_talk_backfill_rejection_policy(result, exact_selected=False) is True

    assert result["status"] == status
    assert "rejected_status" not in result
    assert "rejection_reason" not in result
    # 不能靠 recoverable=True 兜底——那是基础设施重试车道，见 (c)。
    assert result["failure_recoverable"] is False
    receipt = result["speaker_manual_review"]
    assert receipt["schema_version"] == "speaker-manual-review-hold.v1"
    assert receipt["status"] == "PENDING_HUMAN_REVIEW"
    assert receipt["held_status"] == status
    assert receipt["reason"] == "speaker_identity_unresolved_backfilled"
    assert speaker_manual_review.is_speaker_manual_review_hold(result) is True


def test_speaker_review_manifest_is_carried_into_the_hold_receipt():
    """带 cue 清单的那条分支要把清单摆到 Ivan 面前，否则他无从下手。"""

    result = _speaker_failure(
        "speaker_review_required",
        speaker_review_manifest="/opt/bilive/autoslice/out/x/speaker-review.json",
        speaker_review_context_unresolved_cues=7,
    )
    apply_talk_backfill_rejection_policy(result, exact_selected=False)

    receipt = result["speaker_manual_review"]
    assert receipt["speaker_review_manifest"].endswith("speaker-review.json")
    assert receipt["context_unresolved_cues"] == 7


@pytest.mark.parametrize("status", SPEAKER_STATUSES)
def test_exact_recovery_hold_carries_the_same_receipt(status):
    """exact 契约下 backfill 本来就被抑制（既有范式），回执要盖同一份。"""

    result = _speaker_failure(status)
    assert apply_talk_backfill_rejection_policy(result, exact_selected=True) is False
    assert result["status"] == status
    assert result["backfill_suppressed_by_exact_contract"]["status"] == status
    assert result["speaker_manual_review"]["upload_authorized"] is False
    assert speaker_manual_review.is_speaker_manual_review_hold(result) is True


def test_non_speaker_rejections_keep_terminal_candidate_rejected():
    """负向金丝雀：本次只改说话人处置，别的门一律照旧判死。"""

    result = {
        "candidate_id": "auto_boundary",
        "status": "boundary_unrepairable",
        "failure_kind": "content_boundary",
    }
    assert apply_talk_backfill_rejection_policy(result, exact_selected=False) is True
    assert result["status"] == "candidate_rejected"
    assert result["rejected_status"] == "boundary_unrepairable"
    assert result["rejection_reason"] == "unsafe_boundary_backfilled"
    assert "speaker_manual_review" not in result


# --- (b) fail-closed：停泊态永不可上传、永不 review_ready -------------------


@pytest.mark.parametrize("status", SPEAKER_STATUSES)
def test_parked_status_is_never_a_delivered_or_uploadable_status(status):
    assert status not in runner.DELIVERED_TALK_STATUSES
    assert status != runner.TALK_COVER_PENDING_STATUS
    # 停泊态仍属恢复面（可被人工覆盖件唤醒），不是交付面。
    assert status in TALK_RECOVERY_FAILURE_STATUSES


@pytest.mark.parametrize("status", SPEAKER_STATUSES)
def test_batch_with_only_parked_talks_is_not_review_ready(status):
    state = {"picks": [_speaker_failure(status)], "songs": []}
    runner._project_terminal_batch_state(state)
    assert state["status"] == "no_delivery"


@pytest.mark.parametrize("status", SPEAKER_STATUSES)
def test_parked_talk_keeps_the_batch_out_of_clean_review_ready(status):
    state = {
        "picks": [
            _speaker_failure(status),
            {
                "candidate_id": "auto_delivered",
                "status": "review_ready",
                "bundle_lifecycle": "CURRENT",
                "bundle_compliance": "COMPLIANT",
            },
        ],
        "songs": [],
    }
    terminal = project_terminal_batch_state(
        state,
        delivered_talk_statuses=runner.DELIVERED_TALK_STATUSES,
        talk_failure_statuses=TALK_RECOVERY_FAILURE_STATUSES,
        cover_pending_status=runner.TALK_COVER_PENDING_STATUS,
        exact_closure={"status": "NOT_APPLICABLE"},
        retry_epoch=None,
    )
    assert state["status"] == "review_ready_with_failures"
    assert [row["candidate_id"] for row in terminal["delivered_talk"]] == [
        "auto_delivered"
    ]
    assert [row.get("candidate_id") for row in terminal["failures"]] == [
        "auto_213135_62_138"
    ]


def test_publication_registry_is_not_written_by_the_hold():
    """停泊件没有产物可传；运行时不得代 Ivan 往仓内出版登记写行。"""

    result = _speaker_failure("speaker_evidence_insufficient")
    apply_talk_backfill_rejection_policy(result, exact_selected=False)
    assert result["speaker_manual_review"]["upload_authorized"] is False
    assert "publication_registry" not in result
    assert "bvid" not in result


# --- (c) 不进无限基础设施重试 ----------------------------------------------


def _requeue_fixture(tmp_path, monkeypatch, record: dict, *, recovery: str):
    date = "2026-08-08"
    rec_root = tmp_path / "recordings"
    (rec_root / date).mkdir(parents=True)
    segment = rec_root / date / "22966160_20260808-21-30-00.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:pipe")
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: recovery,
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    record = dict(record)
    record.setdefault("segment", segment.name)
    record.setdefault("start_ms", 62_000)
    record.setdefault("end_ms", 138_000)
    return date, {"pending_talk": [], "picks": [record]}


@pytest.mark.parametrize("status", SPEAKER_STATUSES)
def test_parked_talk_never_churns_while_the_speaker_fingerprint_holds(
    tmp_path, monkeypatch, status
):
    """证据不足重试一万次还是不足：指纹不动就一次都不重排。"""

    record = _speaker_failure(
        status, failure_recovery_fingerprint="sha256:speaker-recovery"
    )
    speaker_manual_review.park_for_manual_review(
        record, reason="speaker_identity_unresolved_backfilled"
    )
    date, state = _requeue_fixture(
        tmp_path, monkeypatch, record, recovery="sha256:speaker-recovery"
    )

    for _ in range(5):  # 五个 tick,模拟"每 tick 空转"的反例
        assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["pending_talk"] == []
    assert state["picks"][0]["status"] == status
    assert state["picks"][0].get("talk_transient_retry_count") is None


def test_human_speaker_override_wakes_the_parked_candidate(tmp_path, monkeypatch):
    """唯一唤醒面：说话人恢复指纹变化（人工覆盖件已在该指纹里）。"""

    record = _speaker_failure(
        "speaker_evidence_insufficient",
        failure_recovery_fingerprint="sha256:before-override",
    )
    speaker_manual_review.park_for_manual_review(
        record, reason="speaker_identity_unresolved_backfilled"
    )
    date, state = _requeue_fixture(
        tmp_path, monkeypatch, record, recovery="sha256:after-override"
    )

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == []
    assert state["pending_talk"][0]["retry_reason"] == "pipeline_fingerprint_changed"


# --- 化石迁移：两个真实受害者 ----------------------------------------------


def _fossilized(candidate_id: str) -> dict:
    """free 只读取证的真实行形状（2026-08-10）。"""

    return {
        "candidate_id": candidate_id,
        "status": "candidate_rejected",
        "rejected_status": "speaker_evidence_insufficient",
        "rejection_reason": "speaker_identity_unresolved_backfilled",
        "failure_kind": "speaker_evidence",
        "failure_stage": "speaker_finalization",
        "failure_recoverable": False,
        "failure_message": (
            "RuntimeError: SPEAKER_FINALIZATION_BLOCKED: "
            "SpeakerFinalizationError: not enough Li Dousha clip anchors: []"
        ),
        "rc": 1,
    }


def test_fossilized_speaker_rejection_migrates_to_manual_review_hold():
    state = {
        "picks": [
            _fossilized("auto_220747_1271_1323"),
            _fossilized("auto_213135_62_138"),
        ]
    }
    assert speaker_manual_review.restore_fossilized_speaker_holds(state) == 2
    for row in state["picks"]:
        assert row["status"] == "speaker_evidence_insufficient"
        assert "rejected_status" not in row
        assert "rejection_reason" not in row
        migrated = row["speaker_manual_review"]["migrated_from"]
        assert migrated["status"] == "candidate_rejected"
        assert migrated["authority"].startswith("IVAN_2026-08-10")
    # 幂等：第二次没有可迁的行。
    assert speaker_manual_review.restore_fossilized_speaker_holds(state) == 0


def test_migration_refuses_every_other_rejection_shape():
    """窄识别负向金丝雀：这不是通用复活器。"""

    state = {
        "picks": [
            {
                "candidate_id": "auto_boundary",
                "status": "candidate_rejected",
                "rejected_status": "boundary_unrepairable",
                "rejection_reason": "unsafe_boundary_backfilled",
            },
            {
                "candidate_id": "auto_subtitle",
                "status": "candidate_rejected",
                "rejected_status": "failed",
                "failure_kind": "subtitle_authority",
                "rejection_reason": "subtitle_authority_unresolved_backfilled",
            },
            {
                "candidate_id": "auto_wrong_reason",
                "status": "candidate_rejected",
                "rejected_status": "speaker_evidence_insufficient",
                "rejection_reason": "selection_rescore_failed",
            },
        ]
    }
    assert speaker_manual_review.restore_fossilized_speaker_holds(state) == 0
    assert all(row["status"] == "candidate_rejected" for row in state["picks"])


def test_requeue_migrates_fossilized_rows_before_the_recovery_decision(
    tmp_path, monkeypatch
):
    """接线面：迁移挂在 requeue 顶部，两个 requeue 入口都吃得到。"""

    record = _fossilized("auto_213135_62_138")
    record["failure_recovery_fingerprint"] = "sha256:speaker-recovery"
    date, state = _requeue_fixture(
        tmp_path, monkeypatch, record, recovery="sha256:speaker-recovery"
    )

    # 指纹没动：迁回停泊态但不重排（不是"复活即重产"）。
    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["picks"][0]["status"] == "speaker_evidence_insufficient"
    assert state["picks"][0]["speaker_manual_review"]["upload_authorized"] is False


# --- 可见面：Ivan 一眼看到"这条在等我看" -----------------------------------


def test_report_shows_the_hold_outside_the_rejection_table(tmp_path, monkeypatch):
    date = "2026-08-08"
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    held = _speaker_failure("speaker_evidence_insufficient")
    speaker_manual_review.park_for_manual_review(
        held, reason="speaker_identity_unresolved_backfilled"
    )
    state = {
        "status": "no_delivery",
        "picks": [held],
        "songs": [],
        "segments_done": [],
        "segments_dead": {},
        "pending_talk": [],
        "pending_song": [],
    }

    write_reports(date, state)
    report = (
        runner.profile_delivery_root() / date / "AUTOSLICE_SUMMARY.md"
    ).read_text(encoding="utf-8")

    assert "## 等待人工说话人审阅" in report
    assert "auto_213135_62_138" in report
    assert "1 条等待人工说话人审阅（停泊，禁传）" in report
    # 停泊件不得出现在"候选门禁拒绝（终态）"表里——那正是 Ivan 反对的判死叙事。
    rejection_header = "## 候选门禁拒绝（终态，不是成品）"
    assert rejection_header not in report
