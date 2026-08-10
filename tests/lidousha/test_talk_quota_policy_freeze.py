"""按日期的配额授权 + 准入时冻结：堵死「改一次全局常量就回溯改写历史日子」。

事故（考据 `docs/reviews/2026-08-10-talk-pick-quota-forensics.md`）：Ivan 的
按日裁定「8.8切片配额到20条，分数在85分以上即可」被 `4af4a88` 写成游戏 lane 的
全局常量，8/8 是 NO_MATCH 根本没管到，却把全库唯一 RESOLVED 的游戏日 8/7 回溯
放宽了——四条 89.0 / 87.25 / 86.75 / 86.0 在 Ivan 8/7 原定的 90 门下一条都进
不来，在 85 门下四条全进。

Ivan 2026-08-10（逐字）:「追认。88改成15，85。日常还是5，并没有分数限制。」
—— 8/7 的四条就地合法化（本日门 = 85，cap 仍 10）；8/8 事件场 15/85；普通日
5 席且不开额外席。这些数字从此只住在 assets 的按日期授权条目里。

四条金丝雀：
① 8/7 真实形状 + 授权条目 10/85 → 那四条必须进（追认后的正确行为）；
② 同一批候选、把条目的门改成 90 → 四条必须落选（证明门真在起作用）；
   并钉死「全局常量改成 20/85 也不动摇结果」——常量不再是政策来源；
③ 普通日：第 6 名无论多高分都不得进席；某日无条目 → 回落 5/无额外席；
④ 已冻结在席的候选，政策收紧（甚至 slots 归零）后不被回溯打回。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import game_context, talk_quota_authority
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.talk_quota_authority import (
    QUOTA_AUTHORITY_SCHEMA,
    TalkQuotaAuthorityError,
    load_talk_quota_policy_authority,
    resolve_quota_grant,
    validate_talk_quota_policy_authority,
)
from src.autoslice.talk_quota_freeze import FREEZE_FIELD, FREEZE_SCHEMA

SESSION_ID = "live-20260807T190000+0800"
RECORDING_DATE = "2026-08-07"
# 8/7 那四条 pending 的真实 effective_score（考据报告逐条账）。
RATIFIED_SCORES = (89.0, 87.25, 86.75, 86.0)
_DIMENSIONS = {
    "lidousha_centrality": 4,
    "stance_intensity": 4,
    "audience_salience": 4,
    "relationship_interaction": 4,
    "persona_reversal": 4,
    "comedic_payoff": 4,
    "self_contained": 4,
}


def _scorecard(effective_score: float) -> dict:
    """A tier-2 scorecard whose effective_score lands exactly on the target."""

    normalized = normalize_selection_scorecard(
        {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "quota freeze canary",
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


def _candidate(index: int, effective_score: float, *, segment: str | None = None) -> dict:
    return {
        "cid": f"auto_2007{index:02d}_{index}00_{index}99",
        "segment_path": f"/rec/{segment or f'seg-{index}'}.mp4",
        "start_ms": index * 100_000,
        "end_ms": index * 100_000 + 90_000,
        "confidence": 0.9,
        "hook": f"候选{index}",
        "session_id": SESSION_ID,
        "selection_scorecard": _scorecard(effective_score),
    }


def _produced_pick(index: int, status: str) -> dict:
    """A delivered/in-cover pick occupying one of the five base seats.

    ``publication_row_is_verified`` re-checks authority bytes under ``out/``
    and is necessarily false off the production host, so the fixture uses the
    delivered statuses directly — the admission math counts them identically.
    """

    return {
        "candidate_id": f"auto_produced_{index}",
        "segment_path": f"/rec/produced-{index}.mp4",
        "session_id": SESSION_ID,
        "status": status,
        "start_ms": index * 1_000_000,
        "end_ms": index * 1_000_000 + 90_000,
    }


def _write_game_context(base: Path, *, date: str = RECORDING_DATE, status: str = "RESOLVED") -> None:
    state_path = base / "state" / "session_game_context" / f"{date}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "schema_version": "session-game-context.v1",
        "occurrence_policy": "GAME_TERM_EXISTS_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY",
        "recording_date": date,
        "status": status,
        "glossary_sha256": "sha256:" + "a" * 64,
        "input_inventory_sha256": "deadbeef",
        "qualifying_game_ids": ["eguoshai"] if status == "RESOLVED" else [],
    }
    if status == "RESOLVED":
        payload["game"] = {
            "game_id": "eguoshai",
            "canonical": "鹅鸭杀",
            "aliases": [],
            "terms": [{"surface": "警长", "kind": "role"}],
        }
    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _authority_document(entries: list[dict] | None = None, **default_policy) -> dict:
    body = {"cap": 5, "extra_slot_min_score": None, "authority": "canary default policy"}
    body.update(default_policy)
    return {
        "schema_version": QUOTA_AUTHORITY_SCHEMA,
        "authority": "canary quota authority",
        "default_policy": body,
        "entries": entries if entries is not None else [],
    }


def _install_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: dict | None
) -> Path | None:
    """Point the resolver at a hermetic authority (or at none at all)."""

    if document is None:
        monkeypatch.setattr(talk_quota_authority, "DEFAULT_AUTHORITY_PATH", None)
        return None
    path = tmp_path / "talk_quota_policy_authority.v1.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(talk_quota_authority, "DEFAULT_AUTHORITY_PATH", path)
    return path


def _game_entry(cap: int, gate: float | None, *, entry_id: str = "canary-2026-08-07") -> dict:
    return {
        "entry_id": entry_id,
        "recording_date": RECORDING_DATE,
        "scope": "game",
        "cap": cap,
        "extra_slot_min_score": gate,
        "authority": "canary: Ivan 2026-08-07 10 席 + 2026-08-10 追认门 85",
    }


def _eight_seven_state() -> dict:
    """The real 8/7 accounting shape: five produced picks, four pendings."""

    return {
        "picks": [
            _produced_pick(1, "review_ready"),
            _produced_pick(2, "review_ready"),
            _produced_pick(3, "ok"),
            _produced_pick(4, runner.TALK_COVER_PENDING_STATUS),
            _produced_pick(5, "review_ready"),
        ],
        "songs": [],
        "pending_song": [],
        "pending_talk": [
            _candidate(index, score) for index, score in enumerate(RATIFIED_SCORES, start=1)
        ],
    }


def _kept_ids(state: dict) -> list[str]:
    return [item["cid"] for item in state["pending_talk"]]


def _backlog_ids(state: dict) -> list[str]:
    return [item["cid"] for item in state["talk_backlog"]]


# --------------------------------------------------------------------------
# 金丝雀 ①：8/7 追认后的正确行为
# --------------------------------------------------------------------------


def test_canary_one_ratified_2026_08_07_admits_the_four_extra_picks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([_game_entry(10, 85.0)]))
    state = _eight_seven_state()

    runner.prioritize(state)

    assert len(_kept_ids(state)) == 4
    assert _backlog_ids(state) == []
    stamps = [item[FREEZE_FIELD] for item in state["pending_talk"]]
    assert [stamp["admitted_position"] for stamp in stamps] == [6, 7, 8, 9]
    assert {stamp["cap"] for stamp in stamps} == {10}
    assert {stamp["extra_slot_min_score"] for stamp in stamps} == {85.0}
    assert {stamp["policy_source"] for stamp in stamps} == {"asset:canary-2026-08-07"}


def test_committed_repo_authority_ratifies_the_same_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same canary against the COMMITTED asset, not a hermetic stand-in."""

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    state = _eight_seven_state()

    runner.prioritize(state)

    assert len(_kept_ids(state)) == 4
    disclosed = state["talk_quota_policy_disclosure"]["scopes"][0]
    assert disclosed["policy_source"] == "asset:2026-08-07-game-eguoshai"
    assert (disclosed["cap"], disclosed["extra_slot_min_score"]) == (10, 85.0)


# --------------------------------------------------------------------------
# 金丝雀 ②：门是真门；常量不是政策来源
# --------------------------------------------------------------------------


def test_canary_two_gate_ninety_refuses_all_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([_game_entry(10, 90.0)]))
    state = _eight_seven_state()

    runner.prioritize(state)

    assert _kept_ids(state) == []
    assert len(_backlog_ids(state)) == 4


def test_canary_two_global_constants_cannot_rewrite_a_dated_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`4af4a88` replayed: bump the lane constants to 20/85 and change nothing.

    The constants survive only as the historical anchor of Ivan's 2026-08-07
    wording.  If anyone re-wires them into the policy chain, this goes red.
    """

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([_game_entry(10, 90.0)]))
    monkeypatch.setattr(game_context, "GAME_SESSION_TALK_PICK_CAP", 20)
    monkeypatch.setattr(game_context, "GAME_SESSION_EXTRA_SLOT_MIN_SCORE", 85.0)
    state = _eight_seven_state()

    runner.prioritize(state)

    assert _kept_ids(state) == []
    assert len(_backlog_ids(state)) == 4


def test_repo_constants_are_ivans_2026_08_07_wording() -> None:
    assert game_context.GAME_SESSION_TALK_PICK_CAP == 10
    assert game_context.GAME_SESSION_EXTRA_SLOT_MIN_SCORE == 90.0


# --------------------------------------------------------------------------
# 金丝雀 ③：普通日没有额外席；无条目 fail-closed
# --------------------------------------------------------------------------


def test_canary_three_ordinary_day_never_seats_a_sixth_however_high(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _install_authority(tmp_path, monkeypatch, _authority_document([]))
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        # 六条满分：第 6 名不是因为分低落选，而是因为普通日根本没有第 6 席。
        "pending_talk": [_candidate(index, 100.0) for index in range(1, 7)],
    }

    runner.prioritize(state)

    assert len(_kept_ids(state)) == runner.MAX_TALK_PICKS
    assert len(_backlog_ids(state)) == 1
    scope = state["talk_quota_policy_disclosure"]["scopes"][0]
    assert scope["kind"] == "talk"
    assert (scope["cap"], scope["extra_slot_min_score"]) == (5, None)
    assert scope["policy_source"] == "asset:default_policy"


def test_canary_three_resolved_game_day_without_an_entry_falls_back_to_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RESOLVED game date with no dated entry inherits NO widening at all."""

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([]))
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": [_candidate(index, 100.0) for index in range(1, 11)],
    }

    runner.prioritize(state)

    assert len(_kept_ids(state)) == runner.MAX_TALK_PICKS
    scope = state["talk_quota_policy_disclosure"]["scopes"][0]
    assert scope["kind"] == "game"
    assert scope["scope_key"] == f"game:{SESSION_ID}"
    assert (scope["cap"], scope["extra_slot_min_score"]) == (5, None)


@pytest.mark.parametrize(
    "document, expected_source",
    [
        (None, "default:authority_absent"),
        ("not json at all", "default:authority_invalid"),
        ({"schema_version": "something-else"}, "default:authority_invalid"),
    ],
)
def test_missing_or_broken_authority_denies_every_widening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document, expected_source: str
) -> None:
    if isinstance(document, str):
        path = tmp_path / "broken.json"
        path.write_text(document, encoding="utf-8")
        monkeypatch.setattr(talk_quota_authority, "DEFAULT_AUTHORITY_PATH", path)
    else:
        _install_authority(tmp_path, monkeypatch, document)

    grant = resolve_quota_grant(RECORDING_DATE, "game", default_cap=5)

    assert (grant.cap, grant.extra_slot_min_score) == (5, None)
    assert grant.source == expected_source


def test_lookup_never_borrows_another_dates_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_authority(
        tmp_path, monkeypatch, _authority_document([_game_entry(10, 85.0)])
    )

    assert resolve_quota_grant("2026-08-08", "game").cap == 5
    assert resolve_quota_grant(RECORDING_DATE, "event").cap == 5
    assert resolve_quota_grant(RECORDING_DATE, "game").cap == 10


# --------------------------------------------------------------------------
# 金丝雀 ④：冻结席位不被回溯打回
# --------------------------------------------------------------------------


def _frozen_stamp(position: int) -> dict:
    return {
        "schema_version": FREEZE_SCHEMA,
        "kind": "game",
        "scope_key": f"game:{SESSION_ID}",
        "recording_date": RECORDING_DATE,
        "cap": 20,
        "extra_slot_min_score": 85.0,
        "policy_source": "asset:superseded-2026-08-07-entry",
        "admitted_position": position,
    }


def test_canary_four_frozen_seats_survive_a_tightened_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today's policy leaves zero slots; four frozen seats stay seated anyway."""

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([]))
    state = _eight_seven_state()
    for position, item in enumerate(state["pending_talk"], start=6):
        item[FREEZE_FIELD] = _frozen_stamp(position)
    before = json.dumps([item[FREEZE_FIELD] for item in state["pending_talk"]], sort_keys=True)

    runner.prioritize(state)

    assert len(_kept_ids(state)) == 4
    assert _backlog_ids(state) == []
    after = json.dumps([item[FREEZE_FIELD] for item in state["pending_talk"]], sort_keys=True)
    assert after == before, "冻结值被重新盖章 = 又一次回溯改写"
    scope = state["talk_quota_policy_disclosure"]["scopes"][0]
    assert (scope["slots"], scope["kept"], scope["kept_on_frozen_seat"]) == (0, 4, 4)


def test_canary_four_control_the_same_four_unstamped_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([]))
    state = _eight_seven_state()

    runner.prioritize(state)

    assert _kept_ids(state) == []
    assert len(_backlog_ids(state)) == 4


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": "talk-quota-policy-freeze.v0"},
        {"scope_key": "talk:live-20260807T190000+0800"},
        {"cap": 0},
        {"cap": "20"},
        {"extra_slot_min_score": "85"},
        {"policy_source": "  "},
    ],
)
def test_malformed_freeze_stamp_competes_as_fresh_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: dict
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([]))
    state = _eight_seven_state()
    for position, item in enumerate(state["pending_talk"], start=6):
        item[FREEZE_FIELD] = {**_frozen_stamp(position), **mutation}

    runner.prioritize(state)

    assert _kept_ids(state) == []


def test_frozen_seat_does_not_travel_into_another_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A game-scope stamp grants nothing once the date is no longer a game date."""

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base, status="NO_MATCH")
    _install_authority(tmp_path, monkeypatch, _authority_document([]))
    state = _eight_seven_state()
    for position, item in enumerate(state["pending_talk"], start=6):
        item[FREEZE_FIELD] = _frozen_stamp(position)

    runner.prioritize(state)

    assert _kept_ids(state) == []


# --------------------------------------------------------------------------
# 授权资产本身
# --------------------------------------------------------------------------


def test_committed_authority_asset_validates() -> None:
    document = load_talk_quota_policy_authority()

    assert document is not None
    grants = {
        (row["recording_date"], row["scope"]): (row["cap"], row["extra_slot_min_score"])
        for row in document["entries"]
    }
    assert grants[("2026-08-07", "game")] == (10, 85)
    assert grants[("2026-08-08", "event")] == (15, 85)
    assert document["default_policy"]["cap"] == 5
    assert document["default_policy"]["extra_slot_min_score"] is None
    for row in document["entries"]:
        assert "Ivan" in row["authority"]


def test_attempt_cap_covers_every_granted_cap() -> None:
    """An authorized cap the attempt budget cannot reach starves mid-run."""

    document = load_talk_quota_policy_authority()

    assert document is not None
    for row in document["entries"]:
        assert row["cap"] <= runner.TALK_ATTEMPT_CAP, row["entry_id"]


@pytest.mark.parametrize(
    "document",
    [
        # 文档默认只能是基础席，永远不能变成放宽全库的旋钮
        _authority_document(cap=6, extra_slot_min_score=85.0),
        _authority_document(cap=5, extra_slot_min_score=85.0),
        # cap 抬到基础席之上却没有分数门 = 无限放宽
        _authority_document([_game_entry(20, None)]),
        _authority_document(cap=20, extra_slot_min_score=None),
        # 同一 (date, scope) 两条 = 谁生效说不清
        _authority_document([_game_entry(10, 85.0), _game_entry(20, 85.0, entry_id="dup")]),
        # entry_id 重复
        _authority_document(
            [
                _game_entry(10, 85.0),
                {**_game_entry(15, 85.0), "recording_date": "2026-08-08"},
            ]
        ),
        # scope / date / cap / 出处
        _authority_document([{**_game_entry(10, 85.0), "scope": "song"}]),
        _authority_document([{**_game_entry(10, 85.0), "recording_date": "2026-8-7"}]),
        _authority_document([{**_game_entry(10, 85.0), "cap": 0}]),
        _authority_document([{**_game_entry(10, 85.0), "authority": ""}]),
        _authority_document([{**_game_entry(10, 85.0), "extra_slot_min_score": 120}]),
        {"schema_version": "wrong", "authority": "x" * 20, "entries": []},
    ],
)
def test_authority_validator_is_fail_closed(document: dict) -> None:
    with pytest.raises(TalkQuotaAuthorityError):
        validate_talk_quota_policy_authority(document)


def test_disclosure_is_written_for_every_scope_and_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    _install_authority(tmp_path, monkeypatch, _authority_document([_game_entry(10, 85.0)]))
    state = _eight_seven_state()

    runner.prioritize(state)

    disclosure = state["talk_quota_policy_disclosure"]
    assert disclosure["schema_version"] == "talk-quota-policy-disclosure.v1"
    assert [row["scope_key"] for row in disclosure["scopes"]] == [f"game:{SESSION_ID}"]
    assert "asset:canary-2026-08-07" in capsys.readouterr().out
