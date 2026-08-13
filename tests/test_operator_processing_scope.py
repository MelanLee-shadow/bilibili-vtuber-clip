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

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import operator_processing_scope as operator_scope_module
from src.autoslice import talk_quota_authority
from src.autoslice.candidate_selection import prioritize
from src.autoslice.operator_processing_scope import (
    DISCLOSURE_KEY,
    GRANT_SCHEMA,
    STATE_KEY,
    operator_scope_admission,
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


def _state_with_backlog(grant: dict | None, *, backlog_ids=TIER1_IDS) -> dict:
    state: dict = {
        "status": "ready_unpublished_with_failures",
        "picks": [],
        "songs": [],
        "pending_talk": [],
        "pending_song": [],
        "talk_backlog": [
            _candidate(cid, _NAMED_SCORES.get(cid, 86.0)) for cid in backlog_ids
        ],
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
        ("schema_version_wrong", {"schema_version": "operator-processing-scope-grant.v2"}),
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
    admission = operator_scope_admission(
        _state_with_backlog(grant), date=RECORDING_DATE, now=NOW
    )
    assert admission.reason_code == "UNKNOWN_CANDIDATE"


def test_grant_written_into_the_wrong_date_is_inert(tmp_path, monkeypatch):
    """块被复制到别的日子的 state 里 → 那边不生效（recording_date 必须自洽）。"""

    admission = operator_scope_admission(
        _state_with_backlog(_grant()), date="2026-08-09", now=NOW
    )

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
    assert operator_scope_admission(done, date=RECORDING_DATE, now=NOW).reason_code == (
        "CONVERGED"
    )


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
    requeued["picks"] = [
        row for row in settled["picks"] if row["candidate_id"] != TIER1_IDS[0]
    ]
    requeued["pending_talk"] = [_candidate(TIER1_IDS[0], _NAMED_SCORES[TIER1_IDS[0]])]
    admission = operator_scope_admission(requeued, now=NOW)
    assert admission.admitted is True
    assert admission.outstanding_candidate_ids == (TIER1_IDS[0],)


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
