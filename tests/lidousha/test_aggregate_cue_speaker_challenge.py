from __future__ import annotations

import pytest

from scripts import aggregate_cue_speaker_challenge as aggregate
from scripts import run_cue_aligned_speaker_shadow as shadow
from src.autoslice import host_occupancy as ho


def _strategy(name: str, *, pass_gate: bool) -> dict[str, object]:
    return {
        "strategy": name,
        "confusion_cues": {
            ho.LABEL_HOST: {ho.LABEL_HOST: 90, ho.LABEL_OTHER: 0, ho.LABEL_UNKNOWN: 0},
            ho.LABEL_OTHER: {ho.LABEL_HOST: 0, ho.LABEL_OTHER: 100, ho.LABEL_UNKNOWN: 0},
        },
        "mixed_hard_label_count": 0,
        "passes_provisional_accuracy_gate": pass_gate,
    }


def _document(candidate_id: str, *, pass_gate: bool = True) -> dict[str, object]:
    return {
        "purpose": aggregate.PURPOSE,
        "evaluation_mode": "CUE_ALIGNED",
        "candidate_id": candidate_id,
        "strategies": [_strategy(strategy, pass_gate=pass_gate) for strategy in shadow.STRATEGIES],
    }


def test_aggregate_requires_every_candidate_and_pooled_metrics_to_pass() -> None:
    result = aggregate.aggregate_documents([_document("a"), _document("b")])
    assert result["development_gate_passes"] is True
    assert result["production_promotion_authorized"] is False
    assert "TWO_LOCKED_CROSS_SESSION_HOLDOUTS_MISSING" in result["promotion_blockers"]


def test_one_failed_candidate_blocks_designated_strategy() -> None:
    result = aggregate.aggregate_documents([_document("a"), _document("b", pass_gate=False)])
    assert result["development_gate_passes"] is False
    assert "DESIGNATED_STRATEGY_FAILED_DEVELOPMENT_GATE" in result["promotion_blockers"]


def test_malformed_mixed_count_fails_closed() -> None:
    first = _document("a")
    second = _document("b")
    first["strategies"][0]["mixed_hard_label_count"] = -1
    with pytest.raises(
        aggregate.AggregateChallengeError,
        match="mixed hard-label count is invalid",
    ):
        aggregate.aggregate_documents([first, second])
