import copy
from pathlib import Path

import pytest

from src.autoslice import delivery_recovery


OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
ALT_NEW = "sha256:" + "4" * 64
STATE_SHA = "sha256:" + "3" * 64


class _Runner:
    DELIVERED_TALK_STATUSES = {"review_ready", "ok"}

    def __init__(self, base: Path) -> None:
        self.BASE = base
        self.REC_ROOT = base / "recordings"

    @staticmethod
    def talk_pipeline_fingerprint(_candidate_id: str) -> str:
        return NEW

    @staticmethod
    def ffprobe_ms(_segment: Path) -> int:
        return 900_000

    @staticmethod
    def find_danmaku_xml(_segment: Path):
        return None

    @staticmethod
    def find_chat_jsonl(_segment: Path):
        return None

    @staticmethod
    def resolve_structured_chat_binding(
        _segment: Path,
        *,
        source_sha256: str | None = None,
    ):
        del source_sha256
        return {
            "chat_jsonl": None,
            "structured_chat_required": False,
            "chat_binding_status": "OPTIONAL_ABSENT",
        }


def _fixture(tmp_path: Path, monkeypatch):
    runner = _Runner(tmp_path)
    monkeypatch.setattr(delivery_recovery, "_runner", runner)
    date = "2026-07-22"
    segment = runner.REC_ROOT / date / "official.mp4"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"media")
    bcut = runner.BASE / "cache" / date / "official.bcut.srt"
    bcut.parent.mkdir(parents=True)
    bcut.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n")
    scorecard = {"status": "VALID", "tier": 1, "effective_score": 90}
    relation = {"state": "CONFIRMED", "participants": ["李豆沙", "南町"]}
    record = {
        "candidate_id": "auto_current",
        "status": "review_ready",
        "bundle_lifecycle": "CURRENT",
        "bundle_compliance": "COMPLIANT",
        "pipeline_fingerprint": OLD,
        "segment": segment.name,
        "start_ms": 10_000,
        "end_ms": 120_000,
        "hook": "当面对质",
        "confidence": 0.95,
        "selection_scorecard": scorecard,
        "session_relation_authority": relation,
        "session_id": "live-20260722",
        "filler_proposals": [{"start_ms": 30_000, "end_ms": 31_000}],
        "merge_gap_removals": [{"start_ms": 40_000, "end_ms": 41_000}],
        "cover_diversity_slot": 3,
        "title": "【李豆沙】旧标题",
    }
    state = {
        "status": "review_ready",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "pending_talk": [],
        "picks": [record],
        "talk_superseded_attempts": [],
    }
    return date, state


def _plan(date: str, state: dict):
    return delivery_recovery.plan_current_talk_recovery_rerun(
        date,
        state,
        candidate_ids=["auto_current"],
        expected_source_state_sha256=STATE_SHA,
        expected_old_fingerprint=OLD,
        expected_new_fingerprint=NEW,
    )


def test_current_delivery_moves_to_hash_bound_recovery_queue(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)

    plan = _plan(date, state)

    assert plan["queued_count"] == 1
    assert state["picks"] == []
    item = state["pending_talk"][0]
    assert item["cid"] == "auto_current"
    assert item["selected_repair"] is True
    assert item["retry_reason"] == "explicit_recovery_review_pipeline_rerun"
    assert item["selection_scorecard"]["tier"] == 1
    assert item["session_relation_authority"]["state"] == "CONFIRMED"
    assert item["filler_proposals"]
    assert item["merge_gap_removals"]
    assert item["cover_diversity_slot"] == 3
    assert item["bcut_srt_path"].endswith("official.bcut.srt")
    assert item["structured_chat_required"] is False
    assert item["chat_binding_status"] == "OPTIONAL_ABSENT"
    assert item.get("reuse_cover") is None
    archived = state["talk_superseded_attempts"][0]
    assert archived["title"] == "【李豆沙】旧标题"
    assert archived["bundle_lifecycle"] == "SUPERSEDED"
    assert archived["bundle_compliance"] == "STALE_PIPELINE"
    assert archived["source_state_sha256"] == STATE_SHA
    assert state["status"] == "recovery_rerun_queued"
    assert state["delivery_rerun_plan"] == plan


def test_recovery_queue_preserves_hash_bound_structured_chat_binding(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    chat = tmp_path / "canonical.jsonl"
    chat.write_text('{"cmd":"DANMU_MSG"}\n', encoding="utf-8")
    binding = {
        "chat_jsonl": str(chat),
        "chat_jsonl_sha256": "sha256:" + "a" * 64,
        "chat_origin_epoch_ms": 1_750_000_000_000,
        "chat_timeline_offset_ms": 37,
        "structured_chat_required": True,
        "chat_source_alias_id": "official-replay-alias",
        "chat_canonical_recording_basename": "canonical.mp4",
        "chat_binding_status": "BOUND_SOURCE_ALIAS",
        "chat_binding_authority": "hash-bound test authority",
    }
    monkeypatch.setattr(
        delivery_recovery._runner,
        "resolve_structured_chat_binding",
        lambda *_args, **_kwargs: binding,
    )

    _plan(date, state)

    item = state["pending_talk"][0]
    assert {
        key: item[key] for key in binding
    } == binding


def test_recovery_queue_fails_closed_when_chat_alias_cannot_bind(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)

    def fail_binding(*_args, **_kwargs):
        raise RuntimeError("STRUCTURED_CHAT_ALIAS_CANONICAL_JSONL_MISSING")

    monkeypatch.setattr(
        delivery_recovery._runner,
        "resolve_structured_chat_binding",
        fail_binding,
    )

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match=(
            "RECOVERY_RERUN_CHAT_AUTHORITY_MISSING:auto_current:"
            "STRUCTURED_CHAT_ALIAS_CANONICAL_JSONL_MISSING"
        ),
    ):
        _plan(date, state)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda state: state.update(run_mode="PRODUCTION"), "NO_UPLOAD"),
        (lambda state: state.update(upload_allowed=True), "NO_UPLOAD"),
        (
            lambda state: state["picks"][0].update(
                bundle_lifecycle="SUPERSEDED"
            ),
            "ALLOWLIST_MUST_EQUAL_ALL_CURRENT",
        ),
        (
            lambda state: state["picks"][0].update(
                pipeline_fingerprint="sha256:" + "9" * 64
            ),
            "OLD_FINGERPRINT_MISMATCH",
        ),
    ],
)
def test_recovery_plan_fails_closed_on_authority_drift(
    tmp_path, monkeypatch, mutation, error
):
    date, state = _fixture(tmp_path, monkeypatch)
    mutation(state)

    with pytest.raises(delivery_recovery.RecoveryReviewRerunError, match=error):
        _plan(date, state)


def test_recovery_plan_requires_exact_complete_allowlist(tmp_path, monkeypatch):
    date, state = _fixture(tmp_path, monkeypatch)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="ALLOWLIST_MUST_EQUAL_ALL_CURRENT",
    ):
        delivery_recovery.plan_current_talk_recovery_rerun(
            date,
            state,
            candidate_ids=["another_candidate"],
            expected_source_state_sha256=STATE_SHA,
            expected_old_fingerprint=OLD,
            expected_new_fingerprint=NEW,
        )


def test_recovery_plan_requires_new_fingerprint(tmp_path, monkeypatch):
    date, state = _fixture(tmp_path, monkeypatch)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="FINGERPRINT_UNCHANGED",
    ):
        delivery_recovery.plan_current_talk_recovery_rerun(
            date,
            state,
            candidate_ids=["auto_current"],
            expected_source_state_sha256=STATE_SHA,
            expected_old_fingerprint=OLD,
            expected_new_fingerprint=OLD,
        )


def test_recovery_plan_can_suppress_current_and_promote_backlog_with_manual_end(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    puzzle = copy.deepcopy(state["picks"][0])
    puzzle["candidate_id"] = "auto_puzzle"
    puzzle["hook"] = "已有同题材连线谜题"
    state["picks"].append(puzzle)
    brainflick = {
        "cid": "auto_brainflick",
        "segment_path": state["picks"][0]["segment"],
        "start_ms": 130_000,
        "end_ms": 200_000,
        "hook": "弹幕要求脑瓜崩",
        "selection_scorecard": {
            "status": "VALID",
            "tier": 1,
            "effective_score": 68.75,
        },
        "session_relation_authority": state["picks"][0][
            "session_relation_authority"
        ],
        "lane": "semantic_recall_sharded",
    }
    higher_baseline = {
        **brainflick,
        "cid": "auto_higher_baseline",
        "hook": "分数更高但用户没有点名",
        "selection_scorecard": {
            "status": "VALID",
            "tier": 1,
            "effective_score": 70.25,
        },
    }
    state["talk_backlog"] = [higher_baseline, brainflick]
    monkeypatch.setattr(
        delivery_recovery._runner,
        "talk_pipeline_fingerprint",
        lambda cid: ALT_NEW if cid == "auto_brainflick" else NEW,
    )

    plan = delivery_recovery.plan_current_talk_recovery_rerun(
        date,
        state,
        candidate_ids=["auto_current"],
        suppressed_candidate_ids=["auto_puzzle"],
        replacement_candidate_ids=["auto_brainflick"],
        user_suppression_authority=(
            "Ivan-stated-20260722: B站已有同题材，明确不再制作上传"
        ),
        replacement_selection_authority=(
            "Ivan-stated-20260722: 明确要求制作脑瓜崩切片"
        ),
        given_end_ms_by_candidate={"auto_current": 115_000},
        given_end_authority="Ivan-reviewed semantic closure 2026-07-22",
        expected_source_state_sha256=STATE_SHA,
        expected_old_fingerprint=OLD,
        expected_new_fingerprints_by_candidate={
            "auto_current": NEW,
            "auto_brainflick": ALT_NEW,
        },
    )

    assert plan["schema_version"] == "recovery-review-talk-rerun-plan.v5"
    assert plan["new_pipeline_fingerprint"] is None
    assert plan["new_pipeline_fingerprints_by_candidate"] == {
        "auto_brainflick": ALT_NEW,
        "auto_current": NEW,
    }
    assert plan["queued_count"] == 2
    assert [row["cid"] for row in state["pending_talk"]] == [
        "auto_current",
        "auto_brainflick",
    ]
    assert state["pending_talk"][0]["given_end_ms"] == 115_000
    assert state["pending_talk"][0]["given_end_authority"].startswith(
        "Ivan-reviewed"
    )
    assert state["pending_talk"][1]["selected_repair"] is False
    override = state["pending_talk"][1]["selection_override"]
    assert override["event_type"] == "USER_SELECTION_OVERRIDE"
    assert override["baseline_rank"] == 2
    assert override["selected_slot"] == 2
    assert override["displaced_baseline_candidate"] == "auto_higher_baseline"
    assert state["talk_selection_overrides"] == [override]
    assert state["talk_selection_contract"]["mode"] == (
        "EXACT_CANDIDATE_SET_NO_BACKFILL"
    )
    assert state["talk_selection_contract"]["candidate_ids"] == [
        "auto_current",
        "auto_brainflick",
    ]
    assert [row["cid"] for row in state["talk_backlog"]] == [
        "auto_higher_baseline"
    ]
    assert state["picks"] == []
    suppression = state["talk_user_suppressions"][0]
    assert suppression["candidate_id"] == "auto_puzzle"
    assert suppression["disposition"] == "EXCLUDE_FROM_DELIVERY"
    assert suppression["bundle_compliance"] == "USER_SUPPRESSED"
    assert suppression["reason_codes"] == [
        "IVAN_SUPPRESSED_ALREADY_UPLOADED_ELSEWHERE_UNVERIFIED"
    ]


def test_recovery_suppression_requires_explicit_authority(tmp_path, monkeypatch):
    date, state = _fixture(tmp_path, monkeypatch)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="SUPPRESSION_AUTHORITY_REQUIRED",
    ):
        delivery_recovery.plan_current_talk_recovery_rerun(
            date,
            state,
            candidate_ids=[],
            suppressed_candidate_ids=["auto_current"],
            replacement_candidate_ids=["auto_replacement"],
            expected_source_state_sha256=STATE_SHA,
            expected_old_fingerprint=OLD,
            expected_new_fingerprint=NEW,
        )
