import copy
from pathlib import Path

import pytest

from src.autoslice import delivery_recovery
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)


OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
ALT_NEW = "sha256:" + "4" * 64
STATE_SHA = "sha256:" + "3" * 64
PUBLIC_TITLE_CANDIDATE = "auto_193450_1475_1543"
PUBLICATION_AUTHORITY_ASSET = (
    ROOT / "assets/lidousha/recovery_publication_authority.v1.json"
)
PUBLICATION_AUTHORITY_SHA256 = (
    "sha256:0bbb26c63c30b1e30af13e33d5513c49aa10b98afa8730ee9761f59865317e30"
)


def _publication_authorities(*candidate_ids: str):
    return build_recovery_publication_authorities(
        candidate_ids=set(candidate_ids),
        registry_path=PUBLICATION_AUTHORITY_ASSET,
        expected_registry_sha256=PUBLICATION_AUTHORITY_SHA256,
    )


def _fake_publication_authority(
    candidate_id: str,
    *,
    required_given_end_ms: int,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "required_given_end_ms": required_given_end_ms,
        "registry_authority": "Ivan-reviewed recovery fixture",
    }


@pytest.fixture(autouse=True)
def _allow_synthetic_publication_authorities(monkeypatch):
    original = delivery_recovery._validated_recovery_publication

    def validate(*, candidate_id, recovery_publication_authority):
        if (
            isinstance(recovery_publication_authority, dict)
            and recovery_publication_authority.get("schema_version")
            == "recovery-same-bv-publication-authority.v1"
        ):
            return original(
                candidate_id=candidate_id,
                recovery_publication_authority=(
                    recovery_publication_authority
                ),
            )
        assert recovery_publication_authority["candidate_id"] == candidate_id
        return "【李豆沙】恢复测试", recovery_publication_authority

    monkeypatch.setattr(
        delivery_recovery,
        "_validated_recovery_publication",
        validate,
    )


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
    authority = _fake_publication_authority(
        "auto_current",
        required_given_end_ms=125_000,
    )
    return delivery_recovery.plan_current_talk_recovery_rerun(
        date,
        state,
        candidate_ids=["auto_current"],
        expected_source_state_sha256=STATE_SHA,
        expected_old_fingerprint=OLD,
        expected_new_fingerprint=NEW,
        given_end_ms_by_candidate={"auto_current": 125_000},
        given_end_authority=str(authority["registry_authority"]),
        recovery_publication_authorities_by_candidate={
            "auto_current": authority
        },
    )


def _bind_exact_contract(state: dict, candidate_ids: list[str]) -> None:
    contract = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": candidate_ids,
        "authority": "Ivan-stated recovery review exact set",
        "source_state_sha256": STATE_SHA,
    }
    state["talk_selection_contract"] = contract
    given_end_ms_by_candidate: dict[str, int] = {}
    authorities: dict[str, dict[str, object]] = {}
    rows_by_id = {
        str(row.get("candidate_id") or row.get("cid") or ""): row
        for row in [*(state.get("picks") or []), *(state.get("pending_talk") or [])]
        if isinstance(row, dict)
    }
    for candidate_id in candidate_ids:
        row = rows_by_id.get(candidate_id)
        end_ms = (
            int(row.get("given_end_ms"))
            if isinstance(row, dict)
            and isinstance(row.get("given_end_ms"), int)
            else int((row or {}).get("end_ms") or 120_000) + 5_000
        )
        authority = _fake_publication_authority(
            candidate_id,
            required_given_end_ms=end_ms,
        )
        given_end_ms_by_candidate[candidate_id] = end_ms
        authorities[candidate_id] = authority
        if row is not None:
            row["given_end_ms"] = end_ms
            row["given_end_authority"] = str(
                authority["registry_authority"]
            )
            row["recovery_publication_authority"] = authority
    state["delivery_rerun_plan"] = {
        "schema_version": "recovery-review-talk-rerun-plan.v7",
        "talk_selection_contract": copy.deepcopy(contract),
        "given_end_ms_by_candidate": given_end_ms_by_candidate,
        "given_end_authority": "Ivan-reviewed recovery fixture",
        "recovery_publication_authorities_by_candidate": authorities,
    }


def test_natural_recovery_tick_requeues_stale_current_success(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    record = state["picks"][0]
    record["given_end_ms"] = 125_000
    record["given_end_authority"] = "Ivan-reviewed semantic closure"
    _bind_exact_contract(state, ["auto_current"])

    count = delivery_recovery.requeue_stale_current_recovery_talks(
        date, state
    )

    assert count == 1
    assert state["picks"] == []
    item = state["pending_talk"][0]
    assert item["cid"] == "auto_current"
    assert item["selected_repair"] is True
    assert item["retry_reason"] == (
        "current_delivery_pipeline_fingerprint_changed"
    )
    assert item["given_end_ms"] == 125_000
    assert item["given_end_authority"].startswith("Ivan-reviewed")
    assert item["selection_scorecard"]["tier"] == 1
    assert item["session_relation_authority"]["state"] == "CONFIRMED"
    assert item["filler_proposals"] and item["merge_gap_removals"]
    assert item["cover_diversity_slot"] == 3
    archived = state["talk_superseded_attempts"][0]
    assert archived["title"] == "【李豆沙】旧标题"
    assert archived["bundle_lifecycle"] == "SUPERSEDED"
    assert archived["bundle_compliance"] == "STALE_PIPELINE"
    assert archived["pipeline_fingerprint"] == OLD
    assert archived["superseded_by"] == NEW


def test_natural_recovery_tick_rejects_legacy_exact_state_atomically(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    state["talk_selection_contract"] = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": ["auto_current"],
        "authority": "legacy exact state without v7 publication contract",
        "source_state_sha256": STATE_SHA,
    }
    before = copy.deepcopy(state)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="RECOVERY_REVIEW_V7_PLAN_REQUIRED",
    ):
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)

    assert state == before


def test_natural_recovery_tick_rejects_missing_record_authority_atomically(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    _bind_exact_contract(state, ["auto_current"])
    state["picks"][0].pop("recovery_publication_authority")
    before = copy.deepcopy(state)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="RECOVERY_REVIEW_CURRENT_AUTHORITY_DRIFT:auto_current",
    ):
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)

    assert state == before


def test_natural_recovery_tick_same_fingerprint_is_byte_stable(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    _bind_exact_contract(state, ["auto_current"])
    monkeypatch.setattr(
        delivery_recovery._runner,
        "talk_pipeline_fingerprint",
        lambda _candidate_id: OLD,
    )
    before = copy.deepcopy(state)

    assert (
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)
        == 0
    )
    assert state == before


def test_explicit_recovery_carries_hash_bound_public_title(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    state["picks"][0]["candidate_id"] = PUBLIC_TITLE_CANDIDATE
    state["picks"][0]["start_ms"] = 1_475_000
    state["picks"][0]["end_ms"] = 1_543_000
    monkeypatch.setattr(
        delivery_recovery._runner,
        "ffprobe_ms",
        lambda _segment: 2_000_000,
    )
    authority = _publication_authorities(
        PUBLIC_TITLE_CANDIDATE
    )[PUBLIC_TITLE_CANDIDATE]
    required_given_end_ms = int(authority["required_given_end_ms"])

    plan = delivery_recovery.plan_current_talk_recovery_rerun(
        date,
        state,
        candidate_ids=[PUBLIC_TITLE_CANDIDATE],
        expected_source_state_sha256=STATE_SHA,
        expected_old_fingerprint=OLD,
        expected_new_fingerprint=NEW,
        given_end_ms_by_candidate={
            PUBLIC_TITLE_CANDIDATE: required_given_end_ms
        },
        given_end_authority=str(authority["registry_authority"]),
        recovery_publication_authorities_by_candidate={
            PUBLIC_TITLE_CANDIDATE: authority
        },
    )

    item = state["pending_talk"][0]
    assert item["given_title"] == expected_recovery_publish_title(
        authority
    )
    assert item["recovery_publication_authority"] == authority
    assert plan["recovery_publication_authorities_by_candidate"] == {
        PUBLIC_TITLE_CANDIDATE: authority
    }


@pytest.mark.parametrize(
    (
        "recorded_recovery_fingerprint",
        "failure_recoverable",
        "next_retry_at_epoch",
        "expected_retry_reason",
        "retry_mode",
    ),
    [
        (
            NEW,
            True,
            0,
            # producer_error is not in INFRASTRUCTURE_WAIT_FAILURE_KINDS, so a
            # same-fingerprint retry is the bounded one-shot transient path,
            # not an unlimited infrastructure timer.
            "transient_produce_failure",
            "ordinary",
        ),
        (
            OLD,
            False,
            None,
            "pipeline_fingerprint_changed",
            "ordinary",
        ),
        (
            NEW,
            True,
            None,
            "sanctioned_candidate_revival",
            "sanctioned",
        ),
        (
            NEW,
            True,
            None,
            "final_review_carryover",
            "carryover",
        ),
    ],
    ids=(
        "transient-same-fingerprint",
        "systemic-fingerprint-change",
        "sanctioned-revival",
        "final-review-carryover",
    ),
)
def test_failure_requeue_preserves_hash_bound_publication_fields(
    tmp_path,
    monkeypatch,
    recorded_recovery_fingerprint,
    failure_recoverable,
    next_retry_at_epoch,
    expected_retry_reason,
    retry_mode,
):
    date, state = _fixture(tmp_path, monkeypatch)
    state["picks"][0]["candidate_id"] = PUBLIC_TITLE_CANDIDATE
    state["picks"][0]["start_ms"] = 1_475_000
    state["picks"][0]["end_ms"] = 1_543_000
    monkeypatch.setattr(
        delivery_recovery._runner,
        "ffprobe_ms",
        lambda _segment: 2_000_000,
    )
    monkeypatch.setattr(
        delivery_recovery._runner,
        "talk_failure_recovery_fingerprint",
        lambda _failure_kind, _candidate_id: NEW,
        raising=False,
    )
    monkeypatch.setattr(
        delivery_recovery._runner,
        "TALK_REPAIR_LIFETIME_RETRY_CAP",
        3,
        raising=False,
    )
    authority = _publication_authorities(
        PUBLIC_TITLE_CANDIDATE
    )[PUBLIC_TITLE_CANDIDATE]
    required_given_end_ms = int(authority["required_given_end_ms"])

    delivery_recovery.plan_current_talk_recovery_rerun(
        date,
        state,
        candidate_ids=[PUBLIC_TITLE_CANDIDATE],
        expected_source_state_sha256=STATE_SHA,
        expected_old_fingerprint=OLD,
        expected_new_fingerprint=NEW,
        given_end_ms_by_candidate={
            PUBLIC_TITLE_CANDIDATE: required_given_end_ms
        },
        given_end_authority=str(authority["registry_authority"]),
        recovery_publication_authorities_by_candidate={
            PUBLIC_TITLE_CANDIDATE: authority
        },
    )
    failed_record = state["pending_talk"].pop()
    expected_title = failed_record["given_title"]
    expected_authority = copy.deepcopy(
        failed_record["recovery_publication_authority"]
    )
    revival_block = {
        "schema_version": "candidate-revival.v1",
        "revived_at": "2026-07-25T23:00:00Z",
        "reason": "test revival audit block",
        "fix_commit": "deadbeef",
    }
    failed_record.update(
        {
            "status": "failed",
            "pipeline_fingerprint": NEW,
            "failure_kind": "producer_error",
            "failure_stage": "producer",
            "failure_recoverable": failure_recoverable,
            "failure_recovery_fingerprint": (
                recorded_recovery_fingerprint
            ),
            "next_retry_at_epoch": next_retry_at_epoch,
            "revivals": [revival_block],
        }
    )
    if retry_mode in {"sanctioned", "carryover"}:
        failed_record["talk_repair_retry_count"] = 99
        failed_record["talk_transient_retry_count"] = 1
    if retry_mode == "sanctioned":
        failed_record["sanctioned_revival_retry"] = {
            "schema_version": "sanctioned-revival-retry.v1",
            "status": "PENDING",
            "revival_index": 0,
            "revived_at": revival_block["revived_at"],
            "expected_fix_commit": "deadbeef",
        }
        revival_block["expected_fix_commit"] = "deadbeef"
    if retry_mode == "carryover":
        failed_record["failure_stage"] = "final_review_carryover"
        failed_record["failure_fingerprint"] = (
            "sha256:" + "a" * 64
        )
    state["picks"] = [failed_record]

    assert delivery_recovery.requeue_recoverable_talks(date, state) == 1

    assert state["picks"] == []
    retry = state["pending_talk"][0]
    assert retry["retry_reason"] == expected_retry_reason
    assert retry["given_title"] == expected_title
    assert retry["given_title"] == expected_recovery_publish_title(
        expected_authority
    )
    assert retry["recovery_publication_authority"] == expected_authority
    # 复活审计块是治理证据，必须跨 requeue 存活
    assert retry["revivals"] == [revival_block]
    if retry_mode == "sanctioned":
        assert retry["sanctioned_revival_retry"]["status"] == "QUEUED"
    if retry_mode == "carryover":
        assert retry[
            "final_review_carryover_consumed_fingerprints"
        ] == ["sha256:" + "a" * 64]
        state["pending_talk"] = []
        retry.update(
            {
                "status": "failed",
                "failure_kind": "subtitle_authority",
                "failure_stage": "final_review_carryover",
                "failure_recoverable": True,
                "failure_recovery_fingerprint": NEW,
                "pipeline_fingerprint": NEW,
                "failure_fingerprint": "sha256:" + "a" * 64,
            }
        )
        state["picks"] = [retry]
        assert delivery_recovery.requeue_recoverable_talks(
            date, state
        ) == 0


def test_explicit_recovery_rejects_public_title_for_unqueued_candidate(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    authority = _publication_authorities(
        PUBLIC_TITLE_CANDIDATE
    )[PUBLIC_TITLE_CANDIDATE]

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="RECOVERY_RERUN_PUBLICATION_AUTHORITY_MUST_EQUAL_QUEUE",
    ):
        fake_authority = _fake_publication_authority(
            "auto_current",
            required_given_end_ms=125_000,
        )
        delivery_recovery.plan_current_talk_recovery_rerun(
            date,
            state,
            candidate_ids=["auto_current"],
            expected_source_state_sha256=STATE_SHA,
            expected_old_fingerprint=OLD,
            expected_new_fingerprint=NEW,
            given_end_ms_by_candidate={"auto_current": 125_000},
            given_end_authority=str(
                fake_authority["registry_authority"]
            ),
            recovery_publication_authorities_by_candidate={
                PUBLIC_TITLE_CANDIDATE: authority
            },
        )


def test_explicit_same_bv_recovery_rejects_missing_publication_authority(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    before = copy.deepcopy(state)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="RECOVERY_RERUN_PUBLICATION_AUTHORITY_MUST_EQUAL_QUEUE",
    ):
        delivery_recovery.plan_current_talk_recovery_rerun(
            date,
            state,
            candidate_ids=["auto_current"],
            expected_source_state_sha256=STATE_SHA,
            expected_old_fingerprint=OLD,
            expected_new_fingerprint=NEW,
            given_end_ms_by_candidate={"auto_current": 125_000},
            given_end_authority="Ivan-reviewed recovery fixture",
            recovery_publication_authorities_by_candidate={},
        )
    assert state == before


def test_explicit_same_bv_recovery_rejects_wrong_reviewed_end(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    authority = _fake_publication_authority(
        "auto_current",
        required_given_end_ms=125_000,
    )
    before = copy.deepcopy(state)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="RECOVERY_RERUN_GIVEN_END_AUTHORITY_MISMATCH:auto_current",
    ):
        delivery_recovery.plan_current_talk_recovery_rerun(
            date,
            state,
            candidate_ids=["auto_current"],
            expected_source_state_sha256=STATE_SHA,
            expected_old_fingerprint=OLD,
            expected_new_fingerprint=NEW,
            given_end_ms_by_candidate={"auto_current": 124_999},
            given_end_authority=str(authority["registry_authority"]),
            recovery_publication_authorities_by_candidate={
                "auto_current": authority
            },
        )

    assert state == before


def test_natural_recovery_tick_does_not_refresh_ordinary_production(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    state["run_mode"] = "PRODUCTION"
    before = copy.deepcopy(state)

    assert (
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)
        == 0
    )
    assert state == before


def test_natural_recovery_tick_invalid_exact_contract_is_atomic(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    _bind_exact_contract(state, ["auto_current"])
    state["upload_allowed"] = True
    before = copy.deepcopy(state)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="INVALID_EXACT_TALK_SELECTION_CONTRACT",
    ):
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)
    assert state == before


def test_natural_recovery_tick_rejects_truncating_given_end_atomically(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    _bind_exact_contract(state, ["auto_current"])
    record = state["picks"][0]
    record["given_end_ms"] = 115_000
    record["given_end_authority"] = "Ivan-reviewed source closure"
    before = copy.deepcopy(state)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="RECOVERY_REVIEW_CURRENT_AUTHORITY_DRIFT:auto_current",
    ):
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)

    assert state == before


def test_natural_recovery_tick_adds_only_three_stale_successes(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    first = state["picks"][0]
    state["picks"] = []
    for cid in ("auto_one", "auto_two", "auto_three"):
        row = copy.deepcopy(first)
        row["candidate_id"] = cid
        state["picks"].append(row)
    state["pending_talk"] = [
        {"cid": "auto_four"},
        {"cid": "auto_five"},
    ]
    expected = [
        "auto_one",
        "auto_two",
        "auto_three",
        "auto_four",
        "auto_five",
    ]
    _bind_exact_contract(state, expected)

    assert (
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)
        == 3
    )
    active = [row["cid"] for row in state["pending_talk"]]
    assert sorted(active) == sorted(expected)
    assert len(active) == len(set(active)) == 5
    assert state["picks"] == []


def test_natural_recovery_tick_build_failure_keeps_all_current(
    tmp_path, monkeypatch
):
    date, state = _fixture(tmp_path, monkeypatch)
    second = copy.deepcopy(state["picks"][0])
    second["candidate_id"] = "auto_second"
    state["picks"].append(second)
    _bind_exact_contract(state, ["auto_current", "auto_second"])
    before = copy.deepcopy(state)
    original = delivery_recovery._recovery_queue_item

    def fail_second(*args, **kwargs):
        # A contract/authority failure (unlike a chat-infrastructure read
        # failure, which defers only that delivered row) must still abort
        # the whole transaction without mutating state.
        if kwargs.get("candidate_id") == "auto_second":
            raise delivery_recovery.RecoveryReviewRerunError(
                "RECOVERY_RERUN_BCUT_AUTHORITY_MISSING:auto_second"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        delivery_recovery, "_recovery_queue_item", fail_second
    )

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="BCUT_AUTHORITY_MISSING:auto_second",
    ):
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)
    assert state == before


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
    authority = _fake_publication_authority(
        "another_candidate",
        required_given_end_ms=125_000,
    )

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
            given_end_ms_by_candidate={"another_candidate": 125_000},
            given_end_authority=str(authority["registry_authority"]),
            recovery_publication_authorities_by_candidate={
                "another_candidate": authority
            },
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
        given_end_ms_by_candidate={
            "auto_current": 125_000,
            "auto_brainflick": 205_000,
        },
        given_end_authority="Ivan-reviewed semantic closure 2026-07-22",
        expected_source_state_sha256=STATE_SHA,
        expected_old_fingerprint=OLD,
        expected_new_fingerprints_by_candidate={
            "auto_current": NEW,
            "auto_brainflick": ALT_NEW,
        },
        recovery_publication_authorities_by_candidate={
            "auto_current": {
                **_fake_publication_authority(
                    "auto_current",
                    required_given_end_ms=125_000,
                ),
                "registry_authority": (
                    "Ivan-reviewed semantic closure 2026-07-22"
                ),
            },
            "auto_brainflick": {
                **_fake_publication_authority(
                    "auto_brainflick",
                    required_given_end_ms=205_000,
                ),
                "registry_authority": (
                    "Ivan-reviewed semantic closure 2026-07-22"
                ),
            },
        },
    )

    assert plan["schema_version"] == "recovery-review-talk-rerun-plan.v7"
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
    assert state["pending_talk"][0]["given_end_ms"] == 125_000
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


def test_chat_unreadable_defers_delivered_refresh_without_freezing_queue(
    tmp_path, monkeypatch
):
    """Live V15 case (2026-07-24): the bound chat jsonl of an already
    delivered CURRENT package became unreadable on CloudFS.  Its optional
    fingerprint refresh must be deferred with a typed disclosure instead of
    aborting the whole exact-queue transaction."""

    date, state = _fixture(tmp_path, monkeypatch)
    runner = delivery_recovery._runner

    keeper = state["picks"][0]
    keeper["candidate_id"] = "auto_keeper"
    segment2 = runner.REC_ROOT / date / "official2.mp4"
    segment2.write_bytes(b"media2")
    bcut2 = runner.BASE / "cache" / date / "official2.bcut.srt"
    bcut2.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n")
    rerunner = copy.deepcopy(keeper)
    rerunner["candidate_id"] = "auto_rerunner"
    rerunner["segment"] = segment2.name
    state["picks"].append(rerunner)

    def broken_binding(segment: Path, *, source_sha256=None):
        del source_sha256
        if Path(segment).name == "official.mp4":
            raise OSError("STRUCTURED_CHAT_JSONL_UNREADABLE")
        return {
            "chat_jsonl": None,
            "structured_chat_required": False,
            "chat_binding_status": "OPTIONAL_ABSENT",
        }

    monkeypatch.setattr(
        runner, "resolve_structured_chat_binding", broken_binding
    )
    _bind_exact_contract(state, ["auto_keeper", "auto_rerunner"])

    count = delivery_recovery.requeue_stale_current_recovery_talks(
        date, state
    )

    assert count == 1
    queued = [row["cid"] for row in state["pending_talk"]]
    assert queued == ["auto_rerunner"]
    kept = [
        row
        for row in state["picks"]
        if row.get("candidate_id") == "auto_keeper"
    ]
    assert len(kept) == 1
    assert kept[0]["bundle_lifecycle"] == "CURRENT"
    assert kept[0]["bundle_compliance"] == "COMPLIANT"
    (deferral,) = state["recovery_requeue_deferrals"]
    assert deferral["candidate_id"] == "auto_keeper"
    assert deferral["reason_code"] == (
        "STALE_REFRESH_DEFERRED_CHAT_UNREADABLE"
    )
    assert "STRUCTURED_CHAT_JSONL_UNREADABLE" in deferral["detail"]


def test_non_infrastructure_queue_item_failure_still_fails_closed(
    tmp_path, monkeypatch
):
    """Only the chat-infrastructure wrapper defers; a missing BCUT authority
    is a contract failure and must abort the whole transaction."""

    date, state = _fixture(tmp_path, monkeypatch)
    runner = delivery_recovery._runner
    record = state["picks"][0]
    record["given_end_ms"] = 125_000
    record["given_end_authority"] = "Ivan-reviewed semantic closure"
    _bind_exact_contract(state, ["auto_current"])
    (runner.BASE / "cache" / date / "official.bcut.srt").unlink()
    before = copy.deepcopy(state)

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match="RECOVERY_RERUN_BCUT_AUTHORITY_MISSING",
    ):
        delivery_recovery.requeue_stale_current_recovery_talks(date, state)
    assert state == before

def test_legacy_foreign_provider_rejection_is_reviveable():
    record = {
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_stage": "foreign_source_transcription",
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "gate_violation": {
            "unresolved_findings": [{"cue_index": 16}],
            "witness_rows": [
                {
                    "cue_index": 16,
                    "witnessed": False,
                    "failure": (
                        "RuntimeError: WITNESS_PROVIDERS_FAILED: "
                        "HTTPError,HTTPError"
                    ),
                }
            ],
        },
    }

    assert delivery_recovery._is_provider_backfilled_foreign_rejection(record)


@pytest.mark.parametrize(
    "failure_stage",
    [
        "foreign_source_transcription",
        "final_review_findings",
        "chat_authority_final_artifact",
    ],
)
def test_selected_authority_rejection_revives_after_pipeline_change(
    tmp_path, monkeypatch, failure_stage
):
    date, state = _fixture(tmp_path, monkeypatch)
    record = state["picks"][0]
    record.update(
        {
            "status": "candidate_rejected",
            "rejected_status": "failed",
            "selected_repair": True,
            "failure_kind": "subtitle_authority",
            "failure_stage": failure_stage,
            "failure_recoverable": False,
            "failure_recovery_fingerprint": OLD,
            "rejection_reason": "subtitle_authority_unresolved_backfilled",
            "given_title": "【李豆沙】恢复测试",
            "recovery_publication_authority": _fake_publication_authority(
                "auto_current", required_given_end_ms=120_000
            ),
        }
    )
    monkeypatch.setattr(
        delivery_recovery._runner,
        "talk_failure_recovery_fingerprint",
        lambda _failure_kind, _candidate_id: NEW,
        raising=False,
    )
    monkeypatch.setattr(
        delivery_recovery._runner,
        "TALK_REPAIR_LIFETIME_RETRY_CAP",
        3,
        raising=False,
    )

    assert delivery_recovery.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == []
    assert state["pending_talk"][0]["cid"] == "auto_current"
    assert state["pending_talk"][0]["retry_reason"] == (
        "pipeline_fingerprint_changed"
    )
