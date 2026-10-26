from __future__ import annotations

import scripts.session_autoslice as runner
import pytest
from src.autoslice.selection_scorecard import (
    ADMISSION_POLICY_FIELD,
    DIMENSION_WEIGHTS,
    normalize_selection_scorecard,
    selection_scorecard_production_admission,
)


def _scorecard(*, quality: dict[str, object] | None) -> dict[str, object]:
    raw: dict[str, object] = {
        "tier": 2,
        "tier_basis": "personal_stance",
        "tier_reason": "candidate-scoped quality fixture",
        "tier_evidence_cues": [1, 2],
        "dimensions": {name: 3 for name in DIMENSION_WEIGHTS},
        "uncertainty_penalty": 0,
        "fatigue_penalty": 0,
    }
    if quality is not None:
        raw["candidate_quality"] = quality
    normalized = normalize_selection_scorecard(raw, start_cue=1, end_cue=2)
    assert normalized is not None
    return normalized


def _item(cid: str, scorecard: dict[str, object]) -> dict[str, object]:
    return {
        "cid": cid,
        "candidate_id": cid,
        "segment_path": "/recordings/20261002-190000.mp4",
        "start_ms": 0,
        "end_ms": 90_000,
        "session_id": "live-20261002T190000+0800",
        "confidence": 0.9,
        "hook": cid,
        "lane": "semantic_recall",
        "selection_scorecard": scorecard,
    }


def _state(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": items,
    }


def test_ordinary_personal_stance_is_reserved_without_changing_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scorecard = _scorecard(
        quality={
            "watchability": "ORDINARY",
            "differentiation": "ORDINARY",
            "production_disposition": "RESERVE",
            "reason": "只有普通抽盒反差，没有互动升级或清晰的喜剧收束。",
            "evidence_cues": [1, 2],
        }
    )

    assert scorecard["effective_score"] == 75.0
    assert selection_scorecard_production_admission(scorecard)["reason_code"] == (
        "CANDIDATE_QUALITY_RESERVE"
    )
    state = _state([_item("ordinary", scorecard)])
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)

    runner.prioritize(state)

    assert state["pending_talk"] == []
    assert [row["cid"] for row in state["talk_backlog"]] == ["ordinary"]
    assert any("CANDIDATE_QUALITY_RESERVE" in row for row in state["not_selected"])


def test_strong_distinctive_candidate_keeps_production_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scorecard = _scorecard(
        quality={
            "watchability": "STRONG",
            "differentiation": "DISTINCTIVE",
            "production_disposition": "PRODUCTION",
            "reason": "观众触发后出现连续回应和片内明确收束。",
            "evidence_cues": [1, 2],
        }
    )
    state = _state([_item("strong", scorecard)])
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)

    runner.prioritize(state)

    assert [row["cid"] for row in state["pending_talk"]] == ["strong"]
    assert state["talk_backlog"] == []


def test_current_card_without_quality_is_held_for_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state([_item("needs-review", _scorecard(quality=None))])
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)

    runner.prioritize(state)

    assert state["pending_talk"] == []
    assert [row["cid"] for row in state["talk_backlog"]] == ["needs-review"]
    assert any(
        "CANDIDATE_QUALITY_REVIEW_REQUIRED" in row
        for row in state["not_selected"]
    )


def test_historical_card_without_current_policy_keeps_legacy_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scorecard = _scorecard(quality=None)
    scorecard.pop(ADMISSION_POLICY_FIELD)
    assert selection_scorecard_production_admission(scorecard)["status"] == "ADMIT"
    state = _state([_item("historical", scorecard)])
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)

    runner.prioritize(state)

    assert [row["cid"] for row in state["pending_talk"]] == ["historical"]
