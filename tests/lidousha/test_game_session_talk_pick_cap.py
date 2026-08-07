"""Ivan 2026-08-07 游戏场配额放宽指令测试：

「本场游戏直播的切片可突破5个上限，放宽到10个。当然，前提是分数在90分以上。」

session_game_context 已 RESOLVED 的场次话题上限由 5 提到 10；第 6-10 席只收
selection_scorecard.effective_score>=90 的候选，1-5 席不变；非游戏场/
缺失/AMBIGUOUS 语境一律留在 5；exact-contract 招回模式不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice.selection_scorecard import normalize_selection_scorecard

SESSION_ID = "live-20260807T190000+0800"
RECORDING_DATE = "2026-08-07"


def _scorecard(*, uncertainty_penalty: float = 0.0) -> dict:
    normalized = normalize_selection_scorecard(
        {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "game session cap test",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 4,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 4,
                "comedic_payoff": 4,
                "self_contained": 4,
            },
            "uncertainty_penalty": uncertainty_penalty,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert normalized is not None
    return normalized


def _candidate(index: int, *, effective_uncertainty: float = 0.0) -> dict:
    return {
        "cid": f"eguoshai-{index}",
        "segment_path": f"/rec/eguoshai-{index}.mp4",
        "start_ms": index * 100_000,
        "end_ms": index * 100_000 + 30_000,
        "confidence": 0.9,
        "hook": f"鹅鸭杀候选{index}",
        "session_id": SESSION_ID,
        "selection_scorecard": _scorecard(uncertainty_penalty=effective_uncertainty),
    }


def _write_game_context(base: Path, *, status: str = "RESOLVED") -> None:
    state_path = base / "state" / "session_game_context" / f"{RECORDING_DATE}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "session-game-context.v1",
        "occurrence_policy": "GAME_TERM_EXISTS_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY",
        "recording_date": RECORDING_DATE,
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


def test_resolved_game_session_fills_up_to_ten_when_scores_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": [_candidate(i, effective_uncertainty=0.0) for i in range(10)],
    }

    runner.prioritize(state)

    assert len(state["pending_talk"]) == 10
    assert state["talk_backlog"] == []


def test_resolved_game_session_slot_six_refused_below_ninety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    # Five unconditional slots (effective score 96..100, all comfortably >=90
    # to isolate the boundary case at position six) plus a sixth candidate
    # that must clear the 90 gate.
    candidates = [_candidate(i, effective_uncertainty=i) for i in range(5)]
    sixth_89_99 = _candidate(5, effective_uncertainty=10.01)
    assert sixth_89_99["selection_scorecard"]["effective_score"] == 89.99
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": candidates + [sixth_89_99],
    }

    runner.prioritize(state)

    assert len(state["pending_talk"]) == 5
    assert [item["cid"] for item in state["pending_talk"]] == [
        c["cid"] for c in candidates
    ]
    assert sixth_89_99["cid"] in [item["cid"] for item in state["talk_backlog"]]


def test_resolved_game_session_slot_six_refused_at_exactly_89(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    candidates = [_candidate(i, effective_uncertainty=i) for i in range(5)]
    sixth_89 = _candidate(5, effective_uncertainty=11)
    assert sixth_89["selection_scorecard"]["effective_score"] == 89.0
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": candidates + [sixth_89],
    }

    runner.prioritize(state)

    assert len(state["pending_talk"]) == 5
    assert sixth_89["cid"] not in [item["cid"] for item in state["pending_talk"]]


def test_resolved_game_session_slot_six_admits_at_exactly_ninety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    candidates = [_candidate(i, effective_uncertainty=i) for i in range(5)]
    sixth_90 = _candidate(5, effective_uncertainty=10)
    assert sixth_90["selection_scorecard"]["effective_score"] == 90.0
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": candidates + [sixth_90],
    }

    runner.prioritize(state)

    assert len(state["pending_talk"]) == 6
    assert sixth_90["cid"] in [item["cid"] for item in state["pending_talk"]]


def test_non_game_session_stays_at_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    # No session_game_context state at all for this date.
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": [_candidate(i, effective_uncertainty=0.0) for i in range(10)],
    }

    runner.prioritize(state)

    assert len(state["pending_talk"]) == runner.MAX_TALK_PICKS


def test_ambiguous_game_context_stays_at_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base, status="AMBIGUOUS")
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": [_candidate(i, effective_uncertainty=0.0) for i in range(10)],
    }

    runner.prioritize(state)

    assert len(state["pending_talk"]) == runner.MAX_TALK_PICKS


def test_resolved_game_session_gate_is_ordinal_across_already_produced_picks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 already-delivered picks occupy positions 1-5; new candidates land at
    position 6+ and must clear the 90 gate regardless of an empty batch."""

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    produced_picks = [
        {
            "cid": f"delivered-{i}",
            "status": "review_ready",
            "session_id": SESSION_ID,
        }
        for i in range(5)
    ]
    admitted = _candidate(10, effective_uncertainty=8)  # effective 92
    refused = _candidate(11, effective_uncertainty=11)  # effective 89
    state = {
        "picks": produced_picks,
        "songs": [],
        "pending_song": [],
        "pending_talk": [admitted, refused],
    }

    runner.prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == [admitted["cid"]]
    assert refused["cid"] in [item["cid"] for item in state["talk_backlog"]]


def test_resolved_game_session_gate_counts_reserved_revival_seats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 delivered + 2 revival-reserved failed picks already occupy positions
    1-5; a new below-90 candidate must NOT slip into an ungated position 4-5
    just because ``produced`` alone undercounts the session's taken seats."""

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    picks = [
        {"cid": f"delivered-{i}", "status": "review_ready", "session_id": SESSION_ID}
        for i in range(3)
    ] + [
        {
            "cid": f"reserved-{i}",
            "status": "failed",
            "failure_recoverable": True,
            "talk_transient_retry_count": 0,
            "talk_repair_retry_count": 0,
            "session_id": SESSION_ID,
        }
        for i in range(2)
    ]
    below_gate = _candidate(20, effective_uncertainty=15)  # effective 85
    state = {
        "picks": picks,
        "songs": [],
        "pending_song": [],
        "pending_talk": [below_gate],
    }

    runner.prioritize(state)

    assert state["pending_talk"] == []
    assert below_gate["cid"] in [item["cid"] for item in state["talk_backlog"]]


def test_exact_contract_mode_unaffected_by_game_session_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    _write_game_context(base)
    candidates = [_candidate(i, effective_uncertainty=0.0) for i in range(10)]
    exact_ids = [candidates[0]["cid"], candidates[3]["cid"]]
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": exact_ids,
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": candidates,
    }

    runner.prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == exact_ids
