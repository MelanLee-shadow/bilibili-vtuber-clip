"""运维范围通道：显式点名某天进处理窗口，出处缺一不生效，干完自动出圈。

背景：`list_dates()` 只取最新三个录制日期，加两个例外（`source_incomplete`、
`historical_source_recovery_in_progress`）。2026-08-10 Ivan 逐字「**把 tier1 的 4
条做了**」「**87 现在需要纳入处理范围**」，但 2026-08-07 早已滑出窗口，且它的
`source_recoveries` 是空的——两个例外一个都不成立，仓里此前没有第三条路。伪造
`source_recoveries` 骗它进窗口是编造证据，绝不允许。

金丝雀：
① 带完整出处的授权块 → 那一天（且只有那一天）进窗口；
② 出处残缺/被篡改的每一种形态 → 整块不生效（fail-closed，不是部分生效）；
③ 被点名的活干完 → 下一个 tick 自动出圈，不需要人回来清理；
④ `expires_at` 是硬兜底：候选根本坐不上席时也不会让老日期永远赖着；
⑤ 点名到候选粒度时，`prioritize()` 只坐被点名的那几条，其余原样留在 backlog；
⑥ 席位数/分数门一字不动——授权点名再多，也不会多坐出一个席位。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import operator_processing_scope as operator_scope_module
from src.autoslice import runner_state_writeback
from src.autoslice import talk_quota_authority
from src.autoslice.candidate_selection import prioritize
from src.autoslice.operator_processing_scope import (
    DISCLOSURE_KEY,
    FAILED_PICK_RECOVERY_GRANT_SCHEMA,
    FAILED_PICK_RECOVERY_INTENT,
    GRANT_SCHEMA,
    HELD_CURRENT_RERENDER_GRANT_SCHEMA,
    HELD_CURRENT_RERENDER_INTENT,
    FINAL_REVIEW_RECOVERY_GRANT_SCHEMA,
    FINAL_REVIEW_RECOVERY_INTENT,
    SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA,
    SPEAKER_HOLD_RECOVERY_INTENT,
    SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
    SOURCE_FACT_RECOVERY_INTENT,
    STATE_KEY,
    TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
    TOPIC_HOLD_RECOVERY_INTENT,
    operator_scope_admission,
    operator_talk_scope,
)
from src.autoslice.selection_scorecard import normalize_selection_scorecard

RECORDING_DATE = "2026-08-07"
SESSION_ID = "live-20260807T190000+0800"
NOW = datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc)
# Ivan 2026-08-10 逐字（本通道的授权出处，也是本文件所有 fixture 的引文来源）。
IVAN_QUOTE = "把 tier1 的 4 条做了；87 现在需要纳入处理范围"
# 8/7 talk_backlog 里真实排在最前的四条 tier-1。
TIER1_IDS = (
    "auto_210739_1695_1804",
    "auto_223750_734_822",
    "auto_210739_727_840",
    "auto_223750_578_654",
)
# 罚分上限（uncertainty ≤ 15）决定了可构造的 effective_score 只能落在 85 以上；
# 本文件只关心**相对**排序，被点名的四条一律低于没被点名的四条。
_NAMED_SCORES = {cid: 86.0 + index * 0.5 for index, cid in enumerate(TIER1_IDS)}
_RESERVE_SCORES = {f"auto_tier2_{index}": 99.0 - index for index in range(4)}
_DIMENSIONS = {
    "lidousha_centrality": 4,
    "stance_intensity": 4,
    "audience_salience": 4,
    "relationship_interaction": 4,
    "persona_reversal": 4,
    "comedic_payoff": 4,
    "self_contained": 4,
}
_FIELD_ABSENT = object()
OLD_SPEAKER_RECOVERY = "sha256:" + "1" * 64
NEW_SPEAKER_RECOVERY = "sha256:" + "2" * 64


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def freeze_operator_scope_clock(monkeypatch):
    """Keep integration paths on the same clock as explicit admission checks."""

    monkeypatch.setattr(operator_scope_module, "datetime", _FrozenDateTime)


def _scorecard(effective_score: float, *, tier: int = 2) -> dict:
    normalized = normalize_selection_scorecard(
        {
            "tier": tier,
            "tier_basis": "personal_stance",
            "tier_reason": "operator scope canary",
            "tier_evidence_cues": [1, 2],
            "dimensions": dict(_DIMENSIONS),
            "uncertainty_penalty": round(100.0 - effective_score, 2),
            "fatigue_penalty": 0.0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert normalized is not None
    assert normalized["effective_score"] == effective_score
    return normalized


def _candidate(cid: str, effective_score: float, *, tier: int = 2) -> dict:
    return {
        "cid": cid,
        "segment_path": f"/rec/{cid}.mp4",
        "start_ms": 100_000,
        "end_ms": 190_000,
        "confidence": 0.9,
        "hook": f"候选 {cid}",
        "session_id": SESSION_ID,
        "selection_scorecard": _scorecard(effective_score, tier=tier),
    }


def _grant(**overrides) -> dict:
    grant = {
        "schema_version": GRANT_SCHEMA,
        "grant_id": "2026-08-07-tier1-four",
        "recording_date": RECORDING_DATE,
        "reason": "8/7 滑出最新三天窗口，tier-1 四条尚未产出；本次只放这一天进范围。",
        "candidate_ids": list(TIER1_IDS),
        "user_authorization": {
            "quote": IVAN_QUOTE,
            "timestamp": "2026-08-10T15:40:00Z",
        },
        "expires_at": "2026-08-13T00:00:00Z",
    }
    grant.update(overrides)
    return grant


def _failed_pick_recovery_grant(**overrides) -> dict:
    grant = _grant(
        schema_version=FAILED_PICK_RECOVERY_GRANT_SCHEMA,
        intent=FAILED_PICK_RECOVERY_INTENT,
        candidate_ids=[TIER1_IDS[0]],
        grant_id="2026-08-07-recover-failed-tier1",
        reason="8/7 的点名失败件需要进入 maintenance recovery，但不绕过任何恢复门。",
    )
    grant.update(overrides)
    return grant


def _held_current_rerender_grant(**overrides) -> dict:
    grant = _grant(
        schema_version=HELD_CURRENT_RERENDER_GRANT_SCHEMA,
        intent=HELD_CURRENT_RERENDER_INTENT,
        candidate_ids=[TIER1_IDS[0]],
        grant_id="2026-08-07-rerender-held-current",
        reason="8/7 点名 held CURRENT 审片包因候选级流水线指纹变化需要无上传重出。",
        upload_allowed=False,
    )
    grant.update(overrides)
    return grant


def _speaker_hold_recovery_grant(**overrides) -> dict:
    grant = _grant(
        schema_version=SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA,
        intent=SPEAKER_HOLD_RECOVERY_INTENT,
        candidate_ids=[TIER1_IDS[0]],
        grant_id="2026-08-07-recover-speaker-hold",
        reason="人工已确认点名候选的说话人真值，只恢复该停泊件且禁止上传。",
        upload_allowed=False,
    )
    grant.update(overrides)
    return grant


def _topic_hold_recovery_grant(**overrides) -> dict:
    grant = _grant(
        schema_version=TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
        intent=TOPIC_HOLD_RECOVERY_INTENT,
        candidate_ids=[TIER1_IDS[0]],
        grant_id="2026-08-07-recover-resolved-topic-hold",
        reason="人工去重结论与刷新后的 scorecard 已绑定，只释放这一条停泊候选且禁止上传。",
        upload_allowed=False,
    )
    grant.update(overrides)
    return grant


def _source_fact_recovery_grant(**overrides) -> dict:
    grant = _grant(
        schema_version=SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
        intent=SOURCE_FACT_RECOVERY_INTENT,
        candidate_ids=[TIER1_IDS[0]],
        grant_id="2026-08-09-recover-selected-source-fact",
        reason="人工点名恢复 source-fact 修复阶段拒绝件，只重跑该候选且禁止上传。",
        upload_allowed=False,
    )
    grant.update(overrides)
    return grant


def _source_fact_rejection_state(grant: dict | None = None) -> dict:
    return {
        "status": "no_delivery",
        "upload_allowed": False,
        "picks": [
            {
                "candidate_id": TIER1_IDS[0],
                "status": "candidate_rejected",
                "rejected_status": "failed",
                "rc": 1,
                "selected_repair": True,
                "failure_kind": "story_contract",
                "failure_stage": "source_fact_repair",
                "failure_recoverable": False,
                "rejection_reason": "story_contract_unresolved_backfilled",
                "failure_recovery_fingerprint": OLD_SPEAKER_RECOVERY,
            }
        ],
        "pending_talk": [],
        "talk_backlog": [],
        "pending_song": [],
        "song_backlog": [],
        "song_selection_backlog": [],
        "songs": [],
        "song_superseded_attempts": [],
        STATE_KEY: grant or _source_fact_recovery_grant(),
    }


def _final_review_recovery_grant(**overrides) -> dict:
    grant = _grant(
        schema_version=FINAL_REVIEW_RECOVERY_GRANT_SCHEMA,
        intent=FINAL_REVIEW_RECOVERY_INTENT,
        candidate_ids=[TIER1_IDS[0]],
        grant_id="2026-08-09-recover-selected-final-review",
        reason="人工点名恢复 final-review 拒绝件及后续 provider-budget 重试，只跑该候选且禁止上传。",
        upload_allowed=False,
    )
    grant.update(overrides)
    return grant


def _final_review_rejection_state(grant: dict | None = None) -> dict:
    state = _source_fact_rejection_state(
        grant or _final_review_recovery_grant()
    )
    state["picks"][0].update(
        {
            "failure_kind": "subtitle_authority",
            "failure_stage": "final_review_findings",
            "rejection_reason": "subtitle_authority_unresolved_backfilled",
        }
    )
    return state


def _topic_hold_state(grant: dict | None = None) -> dict:
    return {
        "status": "no_delivery",
        "upload_allowed": False,
        "picks": [],
        "pending_talk": [],
        "talk_backlog": [],
        "pending_song": [],
        "song_backlog": [],
        "songs": [],
        "published_topic_dedup_review": {
            "schema_version": "published-topic-dedup-review-state.v1",
            "holds": [
                {
                    "candidate_id": TIER1_IDS[0],
                    "candidate": {
                        "cid": TIER1_IDS[0],
                        "segment_path": f"/rec/{TIER1_IDS[0]}.mp4",
                    },
                    "queue_origin": "pending_talk",
                    "suppression_authorized": False,
                    "upload_authorized": False,
                }
            ],
        },
        STATE_KEY: grant or _topic_hold_recovery_grant(),
    }


def _speaker_hold_state(grant: dict | None = None) -> dict:
    status = "speaker_evidence_insufficient"
    return {
        "status": "no_delivery",
        "upload_allowed": False,
        "picks": [
            {
                "candidate_id": TIER1_IDS[0],
                "status": status,
                "failure_kind": "speaker_evidence",
                "failure_recoverable": False,
                "failure_recovery_fingerprint": OLD_SPEAKER_RECOVERY,
                "speaker_manual_review": {
                    "schema_version": "speaker-manual-review-hold.v1",
                    "status": "PENDING_HUMAN_REVIEW",
                    "held_status": status,
                    "upload_authorized": False,
                },
            }
        ],
        "pending_talk": [],
        "talk_backlog": [],
        "pending_song": [],
        "song_backlog": [],
        "songs": [],
        STATE_KEY: grant or _speaker_hold_recovery_grant(),
    }


def _state_with_held_current(grant: dict) -> dict:
    return {
        "status": "ready_unpublished_with_failures",
        "run_mode": "PRODUCTION",
        "upload_allowed": False,
        "picks": [
            {
                "candidate_id": TIER1_IDS[0],
                "status": "review_ready",
                "bundle_lifecycle": "CURRENT",
                "bundle_compliance": "COMPLIANT",
                "pipeline_fingerprint": "sha256:" + "1" * 64,
            }
        ],
        "songs": [],
        "pending_talk": [],
        "pending_song": [],
        "talk_backlog": [],
        STATE_KEY: grant,
    }


def _state_with_failed_pick(
    grant: dict,
    *,
    status: str = "failed",
    failure_recoverable: object = True,
) -> dict:
    row = {
        "candidate_id": TIER1_IDS[0],
        "status": status,
        "segment": f"{TIER1_IDS[0]}.mp4",
        "start_ms": 100_000,
        "end_ms": 190_000,
    }
    if failure_recoverable is not _FIELD_ABSENT:
        row["failure_recoverable"] = failure_recoverable
    return {
        "status": "ready_unpublished_with_failures",
        "picks": [row],
        "songs": [],
        "pending_talk": [],
        "pending_song": [],
        "talk_backlog": [],
        STATE_KEY: grant,
    }


def _state_with_backlog(grant: dict | None, *, backlog_ids=TIER1_IDS) -> dict:
    state: dict = {
        "status": "ready_unpublished_with_failures",
        "picks": [],
        "songs": [],
        "pending_talk": [],
        "pending_song": [],
        "talk_backlog": [_candidate(cid, _NAMED_SCORES.get(cid, 86.0)) for cid in backlog_ids],
    }
    if grant is not None:
        state[STATE_KEY] = grant
    return state


# --------------------------------------------------------------------------
# ① / ② / ③ / ④ —— list_dates 的准入判定
# --------------------------------------------------------------------------


def _window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aged_out_state: dict) -> None:
    """最新三天 = 08-08/09/10；8/7 已经滑出窗口，state 由调用方给。"""

    rec_root = tmp_path / "recordings"
    state_root = tmp_path / "autoslice" / "state"
    state_root.mkdir(parents=True)
    for date in (RECORDING_DATE, "2026-08-08", "2026-08-09", "2026-08-10"):
        (rec_root / date).mkdir(parents=True)
    (state_root / f"{RECORDING_DATE}.json").write_text(
        json.dumps(aged_out_state, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")


def test_aged_out_date_stays_out_without_a_grant(tmp_path, monkeypatch):
    """基线：没有出处块，8/7 就是滑出去的（既有两个例外都不成立）。"""

    state = _state_with_backlog(None)
    assert state.get("source_recoveries") is None
    _window(tmp_path, monkeypatch, state)

    assert runner.list_dates() == ["2026-08-08", "2026-08-09", "2026-08-10"]


def test_grant_with_full_provenance_admits_only_that_date(tmp_path, monkeypatch):
    _window(tmp_path, monkeypatch, _state_with_backlog(_grant()))

    assert runner.list_dates() == [
        RECORDING_DATE,
        "2026-08-08",
        "2026-08-09",
        "2026-08-10",
    ]


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("no_authorization_block", {"user_authorization": {}}),
        (
            "quote_missing",
            {"user_authorization": {"timestamp": "2026-08-10T15:40:00Z"}},
        ),
        (
            "quote_is_a_rubber_stamp",
            {"user_authorization": {"quote": "ok", "timestamp": "2026-08-10T15:40:00Z"}},
        ),
        ("timestamp_missing", {"user_authorization": {"quote": IVAN_QUOTE}}),
        (
            "timestamp_has_no_timezone",
            {"user_authorization": {"quote": IVAN_QUOTE, "timestamp": "2026-08-10 15:40"}},
        ),
        (
            "extra_authorization_field",
            {
                "user_authorization": {
                    "quote": IVAN_QUOTE,
                    "timestamp": "2026-08-10T15:40:00Z",
                    "approved_by_agent": True,
                }
            },
        ),
        ("reason_missing", {"reason": ""}),
        ("grant_id_missing", {"grant_id": ""}),
        ("schema_version_wrong", {"schema_version": "operator-processing-scope-grant.v3"}),
        ("candidate_ids_empty", {"candidate_ids": []}),
        ("candidate_ids_not_a_list", {"candidate_ids": "auto_210739_1695_1804"}),
        ("candidate_ids_duplicated", {"candidate_ids": [TIER1_IDS[0], TIER1_IDS[0]]}),
        ("recording_date_malformed", {"recording_date": "2026/08/07"}),
        ("expires_at_missing", {"expires_at": ""}),
        ("expires_at_has_no_timezone", {"expires_at": "2026-08-13 00:00"}),
    ],
)
def test_incomplete_provenance_never_admits(tmp_path, monkeypatch, label, overrides):
    """出处残缺的每一种形态都整块失效——没有"部分生效"这回事。"""

    grant = _grant(**overrides)
    if label in {"grant_id_missing", "reason_missing", "expires_at_missing"}:
        # 空串必须与"缺字段"同样致命，不是 falsy-but-present 的漏网。
        assert overrides[list(overrides)[0]] == ""
    _window(tmp_path, monkeypatch, _state_with_backlog(grant))

    assert RECORDING_DATE not in runner.list_dates()


def test_grant_missing_a_required_field_is_inert(tmp_path, monkeypatch):
    grant = _grant()
    grant.pop("expires_at")
    _window(tmp_path, monkeypatch, _state_with_backlog(grant))

    assert RECORDING_DATE not in runner.list_dates()


def test_unknown_candidate_id_makes_the_grant_inert(tmp_path, monkeypatch):
    """点名了这一天根本不存在的候选 → 整块不生效。

    否则那个 id 永远无法收敛，这一天就会被永久钉在窗口里——正是「老日期赖着」。
    """

    grant = _grant(candidate_ids=[*TIER1_IDS, "auto_does_not_exist_1_2"])
    _window(tmp_path, monkeypatch, _state_with_backlog(grant))

    assert RECORDING_DATE not in runner.list_dates()
    admission = operator_scope_admission(_state_with_backlog(grant), date=RECORDING_DATE, now=NOW)
    assert admission.reason_code == "UNKNOWN_CANDIDATE"


def test_grant_written_into_the_wrong_date_is_inert(tmp_path, monkeypatch):
    """块被复制到别的日子的 state 里 → 那边不生效（recording_date 必须自洽）。"""

    admission = operator_scope_admission(_state_with_backlog(_grant()), date="2026-08-09", now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "DATE_MISMATCH"


def test_grant_converges_and_leaves_the_window_on_its_own(tmp_path, monkeypatch):
    """被点名的四条都产出后，这一天下一个 tick 自动出圈，无需人工清理。"""

    state = _state_with_backlog(_grant())
    _window(tmp_path, monkeypatch, state)
    assert RECORDING_DATE in runner.list_dates()

    # 四条全部离开队列、落进 picks —— 活干完了。
    done = {
        "status": "ready_unpublished_with_failures",
        "picks": [{"candidate_id": cid, "status": "review_ready"} for cid in TIER1_IDS],
        "pending_talk": [],
        "talk_backlog": [],
        STATE_KEY: _grant(),
    }
    (tmp_path / "autoslice" / "state" / f"{RECORDING_DATE}.json").write_text(
        json.dumps(done, ensure_ascii=False), encoding="utf-8"
    )

    assert RECORDING_DATE not in runner.list_dates()
    assert operator_scope_admission(done, date=RECORDING_DATE, now=NOW).reason_code == ("CONVERGED")


def test_requeued_candidate_reopens_the_grant_then_settles_again():
    """恢复车道把失败件重新排队 = 活没干完；重排本身有终身重试上限，故有界。"""

    settled = {
        "picks": [{"candidate_id": cid, "status": "review_ready"} for cid in TIER1_IDS],
        "pending_talk": [],
        "talk_backlog": [],
        STATE_KEY: _grant(),
    }
    assert operator_scope_admission(settled, now=NOW).admitted is False

    requeued = dict(settled)
    requeued["picks"] = [row for row in settled["picks"] if row["candidate_id"] != TIER1_IDS[0]]
    requeued["pending_talk"] = [_candidate(TIER1_IDS[0], _NAMED_SCORES[TIER1_IDS[0]])]
    admission = operator_scope_admission(requeued, now=NOW)
    assert admission.admitted is True
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)


def test_v1_failed_pick_keeps_its_original_converged_semantics():
    """v2 must not silently reinterpret any already-written v1 grant."""

    state = _state_with_failed_pick(_grant(candidate_ids=[TIER1_IDS[0]]))

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "CONVERGED"


@pytest.mark.parametrize(
    "failure_recoverable",
    [True, None, _FIELD_ABSENT],
    ids=["typed_recoverable", "legacy_null", "legacy_field_absent"],
)
def test_failed_pick_recovery_intent_keeps_named_failure_outstanding(
    failure_recoverable,
):
    state = _state_with_failed_pick(
        _failed_pick_recovery_grant(),
        failure_recoverable=failure_recoverable,
    )

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is True
    assert admission.reason_code == "ADMITTED"
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert admission.disclosure["intent"] == FAILED_PICK_RECOVERY_INTENT


@pytest.mark.parametrize(
    ("status", "failure_recoverable"),
    [
        ("failed", False),
        ("failed", "true"),
        ("failed", 0),
        ("review_ready", True),
        ("published", True),
        ("candidate_rejected", True),
    ],
)
def test_failed_pick_recovery_intent_does_not_reopen_terminal_or_untyped_rows(
    status,
    failure_recoverable,
):
    state = _state_with_failed_pick(
        _failed_pick_recovery_grant(),
        status=status,
        failure_recoverable=failure_recoverable,
    )

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "CONVERGED"


@pytest.mark.parametrize(
    "grant",
    [
        _grant(
            schema_version=FAILED_PICK_RECOVERY_GRANT_SCHEMA,
            candidate_ids=[TIER1_IDS[0]],
        ),
        _failed_pick_recovery_grant(intent="RECOVER_ANYTHING"),
        _grant(intent=FAILED_PICK_RECOVERY_INTENT, candidate_ids=[TIER1_IDS[0]]),
    ],
    ids=["v2_missing_intent", "v2_unknown_intent", "v1_extra_intent"],
)
def test_failed_pick_recovery_intent_is_strictly_typed_and_fail_closed(grant):
    admission = operator_scope_admission(
        _state_with_failed_pick(grant), date=RECORDING_DATE, now=NOW
    )

    assert admission.admitted is False
    assert admission.reason_code == "SCHEMA_INVALID"


def test_failed_pick_recovery_intent_still_expires_hard():
    state = _state_with_failed_pick(_failed_pick_recovery_grant(expires_at="2026-08-10T00:00:00Z"))

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "EXPIRED"
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)


def test_failed_pick_recovery_intent_admits_aged_out_date_to_list_dates(tmp_path, monkeypatch):
    state = _state_with_failed_pick(_failed_pick_recovery_grant())
    _window(tmp_path, monkeypatch, state)

    assert runner.list_dates() == [
        RECORDING_DATE,
        "2026-08-08",
        "2026-08-09",
        "2026-08-10",
    ]


def test_held_current_rerender_intent_admits_exactly_one_no_upload_candidate(
    monkeypatch,
):
    from src.autoslice import held_current_talk_rerender as held

    monkeypatch.setattr(
        held,
        "inspect_named_held_current_talk_rerender",
        lambda *_a, candidate_id, **_k: held.HeldCurrentTalkRerenderInspection(
            held.OUTSTANDING_CURRENT,
            "HELD_CURRENT_RERENDER_REQUIRED",
            candidate_id,
        ),
    )
    state = _state_with_held_current(_held_current_rerender_grant())

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is True
    assert admission.candidate_ids == (TIER1_IDS[0],)
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert admission.disclosure == {
        "schema_version": "operator-processing-scope-disclosure.v3",
        "grant_id": "2026-08-07-rerender-held-current",
        "recording_date": RECORDING_DATE,
        "candidate_ids": [TIER1_IDS[0]],
        "outstanding_candidate_ids": [TIER1_IDS[0]],
        "quote": IVAN_QUOTE,
        "intent": HELD_CURRENT_RERENDER_INTENT,
        "upload_allowed": False,
    }
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"upload_allowed": True},
        {"upload_allowed": None},
        {"candidate_ids": [TIER1_IDS[0], TIER1_IDS[1]]},
        {"intent": "RERENDER_ANY_CURRENT"},
    ],
)
def test_held_current_rerender_schema_is_strictly_single_candidate_no_upload(
    overrides,
):
    state = _state_with_held_current(_held_current_rerender_grant(**overrides))

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "SCHEMA_INVALID"
    # A recognizable v3 block freezes work instead of falling through to the
    # ordinary Talk/Song lanes on a latest-three date.
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_held_current_rerender_block_is_fail_closed_but_convergence_stands_down(
    monkeypatch,
):
    from src.autoslice import held_current_talk_rerender as held

    state = _state_with_held_current(_held_current_rerender_grant())
    monkeypatch.setattr(
        held,
        "inspect_named_held_current_talk_rerender",
        lambda *_a, candidate_id, **_k: held.HeldCurrentTalkRerenderInspection(
            held.BLOCKED,
            "HELD_CURRENT_REGISTRY_UNSEALED",
            candidate_id,
        ),
    )
    blocked = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert blocked.reason_code == "HELD_CURRENT_REGISTRY_UNSEALED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()

    monkeypatch.setattr(
        held,
        "inspect_named_held_current_talk_rerender",
        lambda *_a, candidate_id, **_k: held.HeldCurrentTalkRerenderInspection(
            held.CONVERGED,
            "HELD_CURRENT_PIPELINE_ALREADY_CURRENT",
            candidate_id,
        ),
    )
    converged = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert converged.reason_code == "CONVERGED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_expired_held_current_scope_wins_before_any_registry_or_source_inspection(
    monkeypatch,
):
    from src.autoslice import held_current_talk_rerender as held

    state = _state_with_held_current(
        _held_current_rerender_grant(expires_at="2026-08-10T15:00:00Z")
    )
    monkeypatch.setattr(
        held,
        "inspect_named_held_current_talk_rerender",
        lambda *_a, **_k: pytest.fail("expired v3 must not inspect external authority"),
    )

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "EXPIRED"
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_v4_admits_only_one_strict_speaker_hold_after_recovery_fingerprint_change(
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: (
            NEW_SPEAKER_RECOVERY
            if (kind, cid) == ("speaker_evidence", TIER1_IDS[0])
            else pytest.fail("v4 computed an unrelated recovery fingerprint")
        ),
    )
    state = _speaker_hold_state()

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is True
    assert admission.candidate_ids == (TIER1_IDS[0],)
    assert admission.disclosure == {
        "schema_version": "operator-processing-scope-disclosure.v4",
        "grant_id": "2026-08-07-recover-speaker-hold",
        "recording_date": RECORDING_DATE,
        "candidate_ids": [TIER1_IDS[0]],
        "outstanding_candidate_ids": [TIER1_IDS[0]],
        "quote": IVAN_QUOTE,
        "intent": SPEAKER_HOLD_RECOVERY_INTENT,
        "upload_allowed": False,
    }
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )


def test_v4_unchanged_speaker_recovery_fingerprint_blocks_without_fallthrough(
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: OLD_SPEAKER_RECOVERY,
    )
    state = _speaker_hold_state()

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "SPEAKER_RECOVERY_FINGERPRINT_UNCHANGED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v4_missing_or_malformed_recorded_fingerprint_fails_closed(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: NEW_SPEAKER_RECOVERY,
    )
    for invalid in (None, "sha256:not-a-digest"):
        state = _speaker_hold_state()
        state["picks"][0]["failure_recovery_fingerprint"] = invalid
        admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
        assert admission.reason_code == "SPEAKER_RECOVERY_FINGERPRINT_INVALID"
        assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "failed"),
        ("failure_kind", "subtitle_authority"),
        ("failure_recoverable", True),
        ("speaker_manual_review", {"schema_version": "speaker-manual-review-hold.v1"}),
    ],
)
def test_v4_rejects_every_non_strict_speaker_hold_shape(monkeypatch, field, value):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("invalid hold must fail before fingerprint calculation"),
    )
    state = _speaker_hold_state()
    state["picks"][0][field] = value

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.reason_code == "SPEAKER_MANUAL_REVIEW_HOLD_INVALID"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"upload_allowed": True},
        {"intent": "RECOVER_ANY_MANUAL_HOLD"},
        {"candidate_ids": [TIER1_IDS[0], TIER1_IDS[1]]},
    ],
)
def test_v4_malformed_grants_freeze_recent_dates_instead_of_broad_fallthrough(
    overrides,
):
    state = _speaker_hold_state(_speaker_hold_recovery_grant(**overrides))

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.reason_code == "SCHEMA_INVALID"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v4_expiry_wins_before_fingerprint_and_conflicting_scope_is_empty(
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("expired v4 must not calculate a fingerprint"),
    )
    expired = _speaker_hold_state(
        _speaker_hold_recovery_grant(expires_at="2026-08-10T15:00:00Z")
    )
    assert operator_scope_admission(
        expired, date=RECORDING_DATE, now=NOW
    ).reason_code == "EXPIRED"
    assert operator_talk_scope(expired, date=RECORDING_DATE, now=NOW) is None

    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: NEW_SPEAKER_RECOVERY,
    )
    conflicting = _speaker_hold_state()
    conflicting["talk_selection_contract"] = {"schema_version": "conflict.v1"}
    assert operator_scope_admission(
        conflicting, date=RECORDING_DATE, now=NOW
    ).admitted is True
    assert operator_talk_scope(conflicting, date=RECORDING_DATE, now=NOW) == ()


def test_v4_stays_outstanding_while_queued_then_converges_only_on_terminal(
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: NEW_SPEAKER_RECOVERY,
    )
    state = _speaker_hold_state()
    state["picks"] = []
    state["pending_talk"] = [{"cid": TIER1_IDS[0]}]
    queued = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert queued.admitted is True
    assert queued.outstanding_candidate_ids == (TIER1_IDS[0],)

    state["pending_talk"] = []
    state["picks"] = [{"candidate_id": TIER1_IDS[0], "status": "review_ready"}]
    terminal = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert terminal.reason_code == "CONVERGED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_v4_duplicate_target_and_fingerprint_error_both_fail_closed(monkeypatch):
    state = _speaker_hold_state()
    state["picks"].append(dict(state["picks"][0]))
    assert operator_scope_admission(
        state, date=RECORDING_DATE, now=NOW
    ).reason_code == "SPEAKER_MANUAL_REVIEW_TARGET_NOT_UNIQUE"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()

    state = _speaker_hold_state()
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: (_ for _ in ()).throw(ValueError("broken authority")),
    )
    assert operator_scope_admission(
        state, date=RECORDING_DATE, now=NOW
    ).reason_code == "SPEAKER_RECOVERY_FINGERPRINT_UNAVAILABLE"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v4_target_in_any_song_collection_is_never_reinterpreted_as_talk(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: NEW_SPEAKER_RECOVERY,
    )
    for collection in (
        "pending_song",
        "song_backlog",
        "song_selection_backlog",
        "songs",
        "song_superseded_attempts",
    ):
        state = _speaker_hold_state()
        state[collection] = [{"candidate_id": TIER1_IDS[0]}]
        assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v5_admits_one_resolved_nested_topic_hold_without_mutating_state(monkeypatch):
    from src.autoslice import published_topic_collision as topic_collision

    state = _topic_hold_state()
    preimage = copy.deepcopy(state)
    calls: list[str] = []

    def probe(value, candidate_id, **_kwargs):
        assert value == preimage
        calls.append(candidate_id)
        return "READY_TO_RELEASE"

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        probe,
        raising=False,
    )

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is True
    assert admission.candidate_ids == (TIER1_IDS[0],)
    assert admission.disclosure == {
        "schema_version": "operator-processing-scope-disclosure.v5",
        "grant_id": "2026-08-07-recover-resolved-topic-hold",
        "recording_date": RECORDING_DATE,
        "candidate_ids": [TIER1_IDS[0]],
        "outstanding_candidate_ids": [TIER1_IDS[0]],
        "quote": IVAN_QUOTE,
        "intent": TOPIC_HOLD_RECOVERY_INTENT,
        "upload_allowed": False,
    }
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )
    assert calls == [TIER1_IDS[0], TIER1_IDS[0]]
    assert state == preimage


def test_v5_arbitrary_queued_target_without_release_marker_is_blocked(monkeypatch):
    from src.autoslice import published_topic_collision as topic_collision

    state = _topic_hold_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["pending_talk"] = [{"cid": TIER1_IDS[0]}]
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "BLOCKED",
        raising=False,
    )

    blocked = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert blocked.admitted is False
    assert blocked.reason_code == "TOPIC_DEDUP_RECOVERY_BLOCKED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v5_valid_released_queue_and_retryable_failure_stay_outstanding_then_terminal_converges(
    monkeypatch,
):
    from src.autoslice import published_topic_collision as topic_collision

    disposition = {"value": "RELEASED_QUEUED"}
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: disposition["value"],
        raising=False,
    )
    state = _topic_hold_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["pending_talk"] = [{"cid": TIER1_IDS[0]}]
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-marker.v1"
    }
    queued = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert queued.admitted is True
    assert queued.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )

    state["pending_talk"] = []
    state["picks"] = [
        {
            "candidate_id": TIER1_IDS[0],
            "status": "failed",
            "failure_recoverable": True,
        }
    ]
    disposition["value"] = "RELEASED_RETRY_PENDING"
    retry_pending = operator_scope_admission(
        state, date=RECORDING_DATE, now=NOW
    )
    assert retry_pending.admitted is True
    assert retry_pending.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )

    state["picks"] = [{"candidate_id": TIER1_IDS[0], "status": "review_ready"}]
    disposition["value"] = "CONVERGED"
    terminal = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert terminal.reason_code == "CONVERGED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_v5_changed_boundary_fingerprint_keeps_false_failure_outstanding(
    monkeypatch,
):
    from src.autoslice import published_topic_collision as topic_collision

    state = _topic_hold_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["picks"] = [
        {
            "candidate_id": TIER1_IDS[0],
            "status": "failed",
            "failure_kind": "content_boundary",
            "failure_recoverable": False,
            "failure_recovery_fingerprint": "sha256:" + "1" * 64,
        }
    ]
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-ledger.v1"
    }
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "RELEASED_RETRY_PENDING",
    )

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is True
    assert admission.reason_code == "ADMITTED"
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )


@pytest.mark.parametrize("drift", ["marker", "queued_row", "resolution"])
def test_v5_drifted_durable_release_authority_blocks_queued_resume(
    monkeypatch, drift
):
    from src.autoslice import published_topic_collision as topic_collision

    state = _topic_hold_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["pending_talk"] = [{"cid": TIER1_IDS[0], "drift": drift}]
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-marker.v1",
        "drift": drift,
    }
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "BLOCKED",
        raising=False,
    )

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "TOPIC_DEDUP_RECOVERY_BLOCKED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"upload_allowed": True},
        {"intent": "RECOVER_ANY_TOPIC_HOLD"},
        {"candidate_ids": [TIER1_IDS[0], TIER1_IDS[1]]},
    ],
)
def test_v5_recognizable_malformed_grants_freeze_instead_of_falling_through(
    overrides,
):
    state = _topic_hold_state(_topic_hold_recovery_grant(**overrides))

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.reason_code == "SCHEMA_INVALID"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v5_expiry_wins_before_resolution_probe_io(monkeypatch):
    from src.autoslice import published_topic_collision as topic_collision

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: pytest.fail("expired v5 must not probe repository authority"),
        raising=False,
    )
    state = _topic_hold_state(
        _topic_hold_recovery_grant(expires_at="2026-08-10T15:00:00Z")
    )

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.reason_code == "EXPIRED"
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_v5_unresolved_or_malformed_nested_hold_blocks_all_recent_date_work(
    monkeypatch,
):
    from src.autoslice import published_topic_collision as topic_collision

    state = _topic_hold_state()
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "BLOCKED",
        raising=False,
    )
    blocked = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert blocked.reason_code == "TOPIC_DEDUP_RECOVERY_BLOCKED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()

    state["published_topic_dedup_review"]["holds"].append(
        copy.deepcopy(state["published_topic_dedup_review"]["holds"][0])
    )
    malformed = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert malformed.reason_code == "TOPIC_DEDUP_RECOVERY_BLOCKED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()

    state = _topic_hold_state()
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("authority unavailable")),
        raising=False,
    )
    unavailable = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert unavailable.reason_code == "TOPIC_DEDUP_RESOLUTION_PROBE_UNAVAILABLE"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("queue_origin", None),
        ("suppression_authorized", True),
        ("upload_authorized", True),
        ("candidate", {"cid": TIER1_IDS[1]}),
    ],
)
def test_v5_invalid_nested_hold_is_blocked_by_canonical_inspection(
    monkeypatch, field, value
):
    from src.autoslice import published_topic_collision as topic_collision

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "BLOCKED",
        raising=False,
    )
    state = _topic_hold_state()
    state["published_topic_dedup_review"]["holds"][0][field] = value

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.reason_code == "TOPIC_DEDUP_RECOVERY_BLOCKED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v5_resolved_topic_hold_cannot_reinterpret_song_identity(monkeypatch):
    from src.autoslice import published_topic_collision as topic_collision

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "READY_TO_RELEASE",
        raising=False,
    )
    for collection in (
        "pending_song",
        "song_backlog",
        "song_selection_backlog",
        "songs",
        "song_superseded_attempts",
    ):
        state = _topic_hold_state()
        state[collection] = [{"candidate_id": TIER1_IDS[0]}]
        assert operator_scope_admission(
            state, date=RECORDING_DATE, now=NOW
        ).admitted is True
        assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v6_admits_only_exact_changed_selected_source_fact_rejection(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: (
            NEW_SPEAKER_RECOVERY
            if (kind, cid) == ("story_contract", TIER1_IDS[0])
            else pytest.fail("v6 computed an unrelated recovery fingerprint")
        ),
    )
    state = _source_fact_rejection_state()

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is True
    assert admission.candidate_ids == (TIER1_IDS[0],)
    assert admission.disclosure == {
        "schema_version": "operator-processing-scope-disclosure.v6",
        "grant_id": "2026-08-09-recover-selected-source-fact",
        "recording_date": RECORDING_DATE,
        "candidate_ids": [TIER1_IDS[0]],
        "outstanding_candidate_ids": [TIER1_IDS[0]],
        "quote": IVAN_QUOTE,
        "intent": SOURCE_FACT_RECOVERY_INTENT,
        "upload_allowed": False,
    }
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )


def test_v6_unchanged_source_fact_fingerprint_converges(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: OLD_SPEAKER_RECOVERY,
    )
    state = _source_fact_rejection_state()

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "CONVERGED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_v6_queue_is_outstanding_only_with_exact_typed_receipt(monkeypatch):
    from src.autoslice.selected_source_fact_recovery import (
        RECOVERY_RECEIPT_FIELD,
        build_selected_source_fact_recovery_receipt,
    )

    state = _source_fact_rejection_state()
    old_row = state["picks"].pop()
    queued_row = {
        "cid": TIER1_IDS[0],
        "segment_path": "/recordings/221.mp4",
        "selected_repair": True,
    }
    queued_row[RECOVERY_RECEIPT_FIELD] = build_selected_source_fact_recovery_receipt(
        old_row=old_row,
        queued_row=queued_row,
        candidate_id=TIER1_IDS[0],
        grant_id="2026-08-09-recover-selected-source-fact",
        current_fingerprint=NEW_SPEAKER_RECOVERY,
    )
    state["pending_talk"] = [queued_row]
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: NEW_SPEAKER_RECOVERY,
    )

    assert operator_scope_admission(
        state, date=RECORDING_DATE, now=NOW
    ).admitted is True
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )

    state["pending_talk"][0][RECOVERY_RECEIPT_FIELD]["receipt_sha256"] = (
        OLD_SPEAKER_RECOVERY
    )
    blocked = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert blocked.reason_code == "SELECTED_SOURCE_FACT_RECOVERY_BLOCKED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v6_unknown_leaf_disposition_fails_closed(monkeypatch):
    from src.autoslice import selected_source_fact_recovery

    monkeypatch.setattr(
        selected_source_fact_recovery,
        "inspect_selected_source_fact_recovery",
        lambda *_a, **_k: selected_source_fact_recovery.SelectedSourceFactRecoveryInspection(
            "FUTURE_UNKNOWN",
            "FUTURE_UNKNOWN",
        ),
    )
    state = _source_fact_rejection_state()

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == (
        "SELECTED_SOURCE_FACT_RECOVERY_UNKNOWN_DISPOSITION"
    )
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"upload_allowed": True},
        {"intent": "RECOVER_ANY_SOURCE_FACT_REJECTION"},
        {"candidate_ids": [TIER1_IDS[0], TIER1_IDS[1]]},
        {"speaker_truth_authority": "sha256:" + "3" * 64},
    ],
)
def test_v6_recognizable_malformed_grants_freeze_without_fallthrough(overrides):
    state = _source_fact_rejection_state(_source_fact_recovery_grant(**overrides))

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.reason_code == "SCHEMA_INVALID"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v6_blocked_shape_and_song_conflict_freeze_without_fallthrough(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("blocked v6 shape must fail before fingerprint I/O"),
    )
    malformed = _source_fact_rejection_state()
    malformed["picks"][0]["failure_stage"] = "speaker_evidence"
    blocked = operator_scope_admission(malformed, date=RECORDING_DATE, now=NOW)
    assert blocked.admitted is False
    assert blocked.reason_code == "SELECTED_SOURCE_FACT_RECOVERY_BLOCKED"
    assert operator_talk_scope(malformed, date=RECORDING_DATE, now=NOW) == ()

    song_conflict = _source_fact_rejection_state()
    song_conflict["pending_song"] = [{"candidate_id": TIER1_IDS[0]}]
    blocked = operator_scope_admission(song_conflict, date=RECORDING_DATE, now=NOW)
    assert blocked.reason_code == "SELECTED_SOURCE_FACT_RECOVERY_BLOCKED"
    assert operator_talk_scope(song_conflict, date=RECORDING_DATE, now=NOW) == ()


def test_v6_expiry_wins_before_source_fact_fingerprint_io(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("expired v6 must not calculate a fingerprint"),
    )
    expired = _source_fact_rejection_state(
        _source_fact_recovery_grant(expires_at="2026-08-10T15:00:00Z")
    )

    admission = operator_scope_admission(expired, date=RECORDING_DATE, now=NOW)

    assert admission.reason_code == "EXPIRED"
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert operator_talk_scope(expired, date=RECORDING_DATE, now=NOW) is None


def test_v2_cannot_reinterpret_selected_final_review_rejection(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("v2 terminal rejection must not probe fingerprint"),
    )
    state = _final_review_rejection_state(
        _grant(
            schema_version=FAILED_PICK_RECOVERY_GRANT_SCHEMA,
            intent=FAILED_PICK_RECOVERY_INTENT,
            candidate_ids=[TIER1_IDS[0]],
        )
    )
    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert admission.reason_code == "CONVERGED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_v7_admits_only_exact_changed_selected_final_review_rejection(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: NEW_SPEAKER_RECOVERY
        if (kind, cid) == ("subtitle_authority", TIER1_IDS[0])
        else pytest.fail("v7 computed unrelated recovery fingerprint"),
    )
    state = _final_review_rejection_state()
    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert admission.admitted is True
    assert admission.candidate_ids == (TIER1_IDS[0],)
    assert admission.disclosure == {
        "schema_version": "operator-processing-scope-disclosure.v7",
        "grant_id": "2026-08-09-recover-selected-final-review",
        "recording_date": RECORDING_DATE,
        "candidate_ids": [TIER1_IDS[0]],
        "outstanding_candidate_ids": [TIER1_IDS[0]],
        "quote": IVAN_QUOTE,
        "intent": FINAL_REVIEW_RECOVERY_INTENT,
        "upload_allowed": False,
    }
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == (
        TIER1_IDS[0],
    )


def test_v7_same_fingerprint_converges_and_bad_shape_blocks(monkeypatch):
    monkeypatch.setattr(
        runner, "talk_failure_recovery_fingerprint", lambda *_a: OLD_SPEAKER_RECOVERY
    )
    state = _final_review_rejection_state()
    assert operator_scope_admission(
        state, date=RECORDING_DATE, now=NOW
    ).reason_code == "CONVERGED"
    bad = _final_review_rejection_state()
    bad["picks"][0]["failure_stage"] = "source_fact_repair"
    blocked = operator_scope_admission(bad, date=RECORDING_DATE, now=NOW)
    assert blocked.reason_code == "SELECTED_FINAL_REVIEW_RECOVERY_BLOCKED"
    assert operator_talk_scope(bad, date=RECORDING_DATE, now=NOW) == ()


def test_v7_queue_requires_exact_grant_bound_self_sealed_receipt():
    from src.autoslice.selected_final_review_recovery import (
        RECOVERY_RECEIPT_FIELD,
        build_selected_final_review_recovery_receipt,
    )

    state = _final_review_rejection_state()
    old_row = state["picks"].pop()
    queued = {
        "cid": TIER1_IDS[0],
        "segment_path": "/recordings/1576.mp4",
        "selected_repair": True,
    }
    queued[RECOVERY_RECEIPT_FIELD] = (
        build_selected_final_review_recovery_receipt(
            old_row=old_row,
            queued_row=queued,
            candidate_id=TIER1_IDS[0],
            grant_id="2026-08-09-recover-selected-final-review",
            current_fingerprint=NEW_SPEAKER_RECOVERY,
        )
    )
    state["pending_talk"] = [queued]
    assert operator_scope_admission(
        state, date=RECORDING_DATE, now=NOW
    ).admitted is True
    missing_grant = copy.deepcopy(state)
    missing_grant.pop("operator_processing_scope")
    assert operator_talk_scope(
        missing_grant, date=RECORDING_DATE, now=NOW
    ) == ()
    wrong_grant_kind = copy.deepcopy(state)
    wrong_grant_kind["operator_processing_scope"]["schema_version"] = (
        "operator-processing-scope-grant.future"
    )
    assert operator_talk_scope(
        wrong_grant_kind, date=RECORDING_DATE, now=NOW
    ) == ()
    queued[RECOVERY_RECEIPT_FIELD]["receipt_sha256"] = OLD_SPEAKER_RECOVERY
    blocked = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert blocked.reason_code == "SELECTED_FINAL_REVIEW_RECOVERY_BLOCKED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"upload_allowed": True},
        {"intent": "RECOVER_ANY_FINAL_REVIEW_REJECTION"},
        {"candidate_ids": [TIER1_IDS[0], TIER1_IDS[1]]},
        {"provider_budget_override": True},
    ],
)
def test_v7_recognizable_malformed_grants_freeze_without_fallthrough(overrides):
    state = _final_review_rejection_state(
        _final_review_recovery_grant(**overrides)
    )
    assert operator_scope_admission(
        state, date=RECORDING_DATE, now=NOW
    ).reason_code == "SCHEMA_INVALID"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v7_expiry_wins_before_fingerprint_and_provider_ledger_io(monkeypatch):
    from src.autoslice import selected_final_review_recovery

    monkeypatch.setattr(
        selected_final_review_recovery,
        "inspect_selected_final_review_recovery",
        lambda *_a, **_k: pytest.fail("expired v7 must not inspect recovery state"),
    )
    state = _final_review_rejection_state(
        _final_review_recovery_grant(expires_at="2026-08-10T15:00:00Z")
    )
    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)
    assert admission.reason_code == "EXPIRED"
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) == ()


def test_v7_song_conflict_and_duplicate_talk_rows_fail_closed(monkeypatch):
    monkeypatch.setattr(
        runner, "talk_failure_recovery_fingerprint", lambda *_a: NEW_SPEAKER_RECOVERY
    )
    song = _final_review_rejection_state()
    song["pending_song"] = [{"candidate_id": TIER1_IDS[0]}]
    assert operator_scope_admission(
        song, date=RECORDING_DATE, now=NOW
    ).reason_code == "SELECTED_FINAL_REVIEW_RECOVERY_BLOCKED"
    assert operator_talk_scope(song, date=RECORDING_DATE, now=NOW) == ()
    duplicate = _final_review_rejection_state()
    duplicate["pending_talk"] = [{"cid": TIER1_IDS[0]}]
    assert operator_scope_admission(
        duplicate, date=RECORDING_DATE, now=NOW
    ).reason_code == "SELECTED_FINAL_REVIEW_RECOVERY_BLOCKED"
    assert operator_talk_scope(duplicate, date=RECORDING_DATE, now=NOW) == ()


def test_old_v4_speaker_grant_cannot_reinterpret_current_source_fact_rejection(
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("terminal v4 row must converge before speaker authority I/O"),
    )
    state = _source_fact_rejection_state(_speaker_hold_recovery_grant())

    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "CONVERGED"
    assert operator_talk_scope(state, date=RECORDING_DATE, now=NOW) is None


def test_failed_pick_scope_reaches_process_date_maintenance(tmp_path, monkeypatch):
    """The old date gets far enough for existing maintenance to requeue it."""

    state = _state_with_failed_pick(_failed_pick_recovery_grant())
    _window(tmp_path, monkeypatch, state)
    assert RECORDING_DATE in runner.list_dates()

    calls: list[tuple[str, str]] = []

    def requeue(_date, value, *, candidate_ids=None):
        assert set(candidate_ids or ()) == {TIER1_IDS[0]}
        failed = value["picks"].pop()
        calls.append((_date, failed["candidate_id"]))
        value["pending_talk"].append(
            {
                "cid": failed["candidate_id"],
                "segment_path": f"/recordings/{failed['segment']}",
                "start_ms": failed["start_ms"],
                "end_ms": failed["end_ms"],
                "selected_repair": True,
            }
        )
        return 1

    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_finalized_legacy_hls", lambda *_a, **_k: [])
    monkeypatch.setattr(
        runner,
        "audit_finalized_recording_inventory",
        lambda *_a, **_k: {"can_select": True, "issues": []},
    )
    monkeypatch.setattr(runner, "annotate_state_sessions", lambda *_a, **_k: False)
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", RECORDING_DATE)
    monkeypatch.setattr(
        runner,
        "recover_bound_song_deliveries",
        lambda *_a, **_k: pytest.fail("v2 Talk recovery must not recover Song work"),
    )
    monkeypatch.setattr(runner, "requeue_recoverable_talks", requeue)
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "sha256:test")
    monkeypatch.setattr(runner, "write_state", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: False)

    runner.process_date(RECORDING_DATE)

    assert calls == [(RECORDING_DATE, TIER1_IDS[0])]
    assert [item["cid"] for item in state["pending_talk"]] == [TIER1_IDS[0]]
    assert state["status"] == "paused_cpa_down"


def test_expiry_is_the_hard_backstop_even_with_work_outstanding(tmp_path, monkeypatch):
    """候选一条都没坐上席（8/7 真实处境）时，到期也必须无条件出圈。"""

    grant = _grant(expires_at="2026-08-10T00:00:00Z")
    state = _state_with_backlog(grant)
    admission = operator_scope_admission(state, date=RECORDING_DATE, now=NOW)

    assert admission.admitted is False
    assert admission.reason_code == "EXPIRED"
    assert admission.outstanding_candidate_ids == TIER1_IDS

    _window(tmp_path, monkeypatch, state)
    assert RECORDING_DATE not in runner.list_dates()


def test_grant_does_not_pull_any_other_aged_out_date_in(tmp_path, monkeypatch):
    """blast radius：窗口仍是最新三天，只多了被点名的那一天。"""

    rec_root = tmp_path / "recordings"
    state_root = tmp_path / "autoslice" / "state"
    state_root.mkdir(parents=True)
    for date in (
        "2026-08-05",
        "2026-08-06",
        RECORDING_DATE,
        "2026-08-08",
        "2026-08-09",
        "2026-08-10",
    ):
        (rec_root / date).mkdir(parents=True)
    (state_root / f"{RECORDING_DATE}.json").write_text(
        json.dumps(_state_with_backlog(_grant()), ensure_ascii=False), encoding="utf-8"
    )
    for stale in ("2026-08-05", "2026-08-06"):
        (state_root / f"{stale}.json").write_text(
            json.dumps(_state_with_backlog(None), ensure_ascii=False), encoding="utf-8"
        )
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")

    assert runner.list_dates() == [
        RECORDING_DATE,
        "2026-08-08",
        "2026-08-09",
        "2026-08-10",
    ]


# --------------------------------------------------------------------------
# ⑤ / ⑥ —— prioritize() 的候选限定与"不放宽任何门"
# --------------------------------------------------------------------------


@pytest.fixture()
def hermetic_quota(tmp_path, monkeypatch):
    """无配额授权资产 + 无游戏语境 → talk scope，5 席、无额外席位分数门。"""

    monkeypatch.setattr(talk_quota_authority, "DEFAULT_AUTHORITY_PATH", None)
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    (tmp_path / "autoslice" / "state").mkdir(parents=True, exist_ok=True)


def _selection_state(grant: dict | None) -> dict:
    """四条被点名的低分候选 + 四条没被点名的高分候选。"""

    state: dict = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": [],
        "talk_backlog": [
            *[_candidate(cid, score) for cid, score in _NAMED_SCORES.items()],
            *[_candidate(cid, score) for cid, score in _RESERVE_SCORES.items()],
        ],
    }
    if grant is not None:
        state[STATE_KEY] = grant
    return state


def test_without_a_grant_the_higher_scored_reserves_take_the_seats(hermetic_quota):
    """对照组：不加限定时，坐席的是高分的 tier-2，不是 Ivan 点名的四条。"""

    state = _selection_state(None)
    prioritize(state)

    seated = [item["cid"] for item in state["pending_talk"]]
    assert seated[:4] == [f"auto_tier2_{index}" for index in range(4)]
    assert set(TIER1_IDS) - set(seated)


def test_grant_seats_only_the_named_candidates(hermetic_quota):
    """Ivan「tier1 的 4 条做了，其他不用管了」——只有那四条进准入池。"""

    state = _selection_state(_grant())
    prioritize(state)

    assert sorted(item["cid"] for item in state["pending_talk"]) == sorted(TIER1_IDS)
    # 没被点名的原样留在 backlog，一条都没丢。
    assert sorted(item["cid"] for item in state["talk_backlog"]) == sorted(
        f"auto_tier2_{index}" for index in range(4)
    )
    disclosure = state[DISCLOSURE_KEY]
    assert disclosure["grant_id"] == "2026-08-07-tier1-four"
    assert sorted(disclosure["held_candidate_ids"]) == sorted(
        f"auto_tier2_{index}" for index in range(4)
    )
    assert disclosure["quote"] == IVAN_QUOTE


def test_grant_never_widens_the_seat_cap(hermetic_quota):
    """点名 8 条也只坐得下 5 席——本通道只会收窄，绝不放宽配额门。"""

    named = [*TIER1_IDS, *(f"auto_tier2_{index}" for index in range(4))]
    state = _selection_state(_grant(candidate_ids=named))
    prioritize(state)

    assert len(state["pending_talk"]) == runner.MAX_TALK_PICKS
    assert len(state["pending_talk"]) + len(state["talk_backlog"]) == 8


def test_inert_grant_leaves_selection_exactly_as_it_was(hermetic_quota):
    """出处残缺的块不许"部分生效"：选片结果必须与完全没有块时逐条相同。"""

    baseline = _selection_state(None)
    prioritize(baseline)

    broken = _selection_state(_grant(user_authorization={"quote": "ok"}))
    prioritize(broken)

    assert [item["cid"] for item in broken["pending_talk"]] == [
        item["cid"] for item in baseline["pending_talk"]
    ]
    assert [item["cid"] for item in broken["talk_backlog"]] == [
        item["cid"] for item in baseline["talk_backlog"]
    ]
    assert DISCLOSURE_KEY not in broken


def test_expired_grant_stops_filtering_selection(hermetic_quota):
    state = _selection_state(_grant(expires_at="2020-01-01T00:00:00Z"))
    prioritize(state)

    seated = [item["cid"] for item in state["pending_talk"]]
    assert seated[:4] == [f"auto_tier2_{index}" for index in range(4)]


def test_operator_filter_stands_down_for_an_exact_talk_contract(hermetic_quota):
    """两个"只做这几条"的机制不许互相踩：精确恢复契约在场时本通道不介入。"""

    state = _selection_state(_grant())
    state["run_mode"] = "RECOVERY_REVIEW"
    state["upload_allowed"] = False
    state["talk_selection_contract"] = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "authority": "canary exact recovery",
        "source_state_sha256": "sha256:" + "a" * 64,
        "candidate_ids": ["auto_tier2_0"],
    }
    prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == ["auto_tier2_0"]
    assert DISCLOSURE_KEY not in state


def test_frozen_held_current_scope_rejects_mid_tick_backfill_after_target_rejection(
    hermetic_quota,
):
    """A deterministic target rejection cannot reopen ordinary Talk or Song work."""

    from src.autoslice import historical_failed_talk_scope

    reserve_ids = [f"auto_tier2_{index}" for index in range(4)]
    state = {
        "upload_allowed": False,
        "picks": [
            {
                "candidate_id": TIER1_IDS[0],
                "status": "candidate_rejected",
            }
        ],
        "pending_talk": [],
        "talk_backlog": [
            _candidate(cid, _RESERVE_SCORES[cid]) for cid in reserve_ids
        ],
        "pending_song": [{"cid": "song_must_stay_parked"}],
        "song_backlog": [{"cid": "song_backlog_must_stay_parked"}],
        "songs": [],
        STATE_KEY: _held_current_rerender_grant(),
    }
    song_preimage = (
        list(state["pending_song"]),
        list(state["song_backlog"]),
    )

    historical_failed_talk_scope.reprioritize(state, (TIER1_IDS[0],))

    assert state["pending_talk"] == []
    assert sorted(item["cid"] for item in state["talk_backlog"]) == reserve_ids
    assert (state["pending_song"], state["song_backlog"]) == song_preimage
    assert state[DISCLOSURE_KEY]["scope_mode"] == "FROZEN_FOR_TICK_TALK_ONLY"
    assert state[DISCLOSURE_KEY]["intent"] == HELD_CURRENT_RERENDER_INTENT


def test_process_date_held_current_rejection_never_dispatches_backfill_or_song(
    hermetic_quota,
    tmp_path: Path,
    monkeypatch,
):
    """Full runner canary: frozen scope survives deterministic rejection mid-tick."""

    from src.autoslice import held_current_talk_rerender as held
    from src.autoslice import historical_failed_talk_scope

    target = _candidate(TIER1_IDS[0], 86.0)
    target.update(
        {
            "selected_repair": True,
            "retry_reason": held.RETRY_REASON,
            "operator_scope_grant_id": "2026-08-07-rerender-held-current",
        }
    )
    reserve_ids = [f"auto_tier2_{index}" for index in range(4)]
    state = {
        "status": "ready_unpublished_with_failures",
        "run_mode": "PRODUCTION",
        "upload_allowed": False,
        "picks": [],
        "pending_talk": [target],
        "talk_backlog": [
            _candidate(cid, _RESERVE_SCORES[cid]) for cid in reserve_ids
        ],
        "talk_superseded_attempts": [
            {
                "candidate_id": TIER1_IDS[0],
                "bundle_lifecycle": "SUPERSEDED",
                "bundle_compliance": "STALE_PIPELINE",
                "pipeline_fingerprint": "sha256:" + "1" * 64,
                "superseded_by": "sha256:" + "2" * 64,
                "retry_reason": held.RETRY_REASON,
                "operator_scope_grant_id": "2026-08-07-rerender-held-current",
            }
        ],
        "pending_song": [{"cid": "song_active_must_stay"}],
        "song_backlog": [{"cid": "song_backlog_must_stay"}],
        "song_selection_backlog": [{"cid": "song_selection_must_stay"}],
        "songs": [],
        STATE_KEY: _held_current_rerender_grant(),
    }
    base = tmp_path / "autoslice"
    base.mkdir(exist_ok=True)
    monkeypatch.setattr(runner, "BASE", base)

    def persist_test_state(_date: str, value: dict) -> None:
        path = runner.state_path(_date)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(runner_state_writeback.state_bytes(value))

    persist_test_state(RECORDING_DATE, state)
    song_preimage = copy.deepcopy(
        (
            state["pending_song"],
            state["song_backlog"],
            state["song_selection_backlog"],
            state["songs"],
        )
    )
    monkeypatch.setattr(
        held,
        "inspect_named_held_current_talk_rerender",
        lambda *_a, candidate_id, **_k: held.HeldCurrentTalkRerenderInspection(
            held.OUTSTANDING_QUEUED,
            "HELD_CURRENT_RERENDER_QUEUED",
            candidate_id,
        ),
    )
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "write_state", persist_test_state)
    monkeypatch.setattr(runner, "write_reports", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_finalized_legacy_hls", lambda *_a, **_k: [])
    monkeypatch.setattr(
        runner,
        "audit_finalized_recording_inventory",
        lambda *_a, **_k: {"can_select": True, "issues": []},
    )
    monkeypatch.setattr(runner, "annotate_state_sessions", lambda *_a, **_k: False)
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", RECORDING_DATE)
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain",
        lambda *_a, **_k: (0, 0, 0, 0, False),
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "work_flags",
        lambda *_a, **_k: (True, True, False),
    )
    monkeypatch.setattr(
        runner,
        "discover_segments",
        lambda *_a, **_k: pytest.fail("held scope must not discover unnamed Talk"),
    )
    monkeypatch.setattr(runner, "session_sealed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner.semantic_chat_refresh,
        "refresh_operator_scoped_chat_scorecards",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "prepare_speaker_routing", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "collect_song_name_candidates", lambda *_a, **_k: [])
    dispatched: list[str] = []

    def reject_only_target(_date, items, _produce, *, prepare_only: bool):
        assert prepare_only is True
        ids = [str(item.get("cid") or item.get("candidate_id") or "") for item in items]
        assert ids == [TIER1_IDS[0]]
        dispatched.extend(ids)
        return [
            {
                "candidate_id": TIER1_IDS[0],
                "status": "candidate_rejected",
                "rejection_reason": "deterministic_canary_rejection",
            }
        ]

    monkeypatch.setattr(runner, "produce_batch", reject_only_target)
    monkeypatch.setattr(
        runner,
        "refill_songs",
        lambda *_a, **_k: pytest.fail("held scope must not refill Song"),
    )
    monkeypatch.setattr(runner, "cover_repair_needed", lambda *_a, **_k: False)
    monkeypatch.setattr(
        runner,
        "repair_covers",
        lambda *_a, **_k: pytest.fail("held scope must not generate or repair covers"),
    )
    monkeypatch.setattr(
        runner,
        "_project_terminal_batch_state",
        lambda value, **_kwargs: {
            "picks": value["picks"],
            "songs": value["songs"],
            "delivered_talk": [],
            "repaired": [],
            "delivered_songs": [],
            "blocked_songs": [],
            "rejected_songs": [],
            "failures": [],
        },
    )
    monkeypatch.setattr(runner, "queue_collab_evidence_capture", lambda *_a, **_k: None)

    runner.process_date(RECORDING_DATE)

    assert dispatched == [TIER1_IDS[0]]
    assert state["pending_talk"] == []
    assert sorted(item["cid"] for item in state["talk_backlog"]) == reserve_ids
    assert (
        state["pending_song"],
        state["song_backlog"],
        state["song_selection_backlog"],
        state["songs"],
    ) == song_preimage
