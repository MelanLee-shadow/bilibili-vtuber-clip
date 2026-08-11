from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from src.autoslice import producer_request


ROOT = Path(__file__).resolve().parents[1]


def _stale_scorecard() -> dict[str, object]:
    return {
        "dimensions": {
            "audience_salience": 2,
            "comedic_payoff": 3,
            "lidousha_centrality": 3,
            "persona_reversal": 4,
            "relationship_interaction": 4,
            "self_contained": 3,
            "stance_intensity": 2,
        },
        "effective_score": 69.5,
        "fatigue_penalty": 0.0,
        "raw_score": 72.5,
        "reason_codes": [],
        "requested_tier": 1,
        "schema_version": "lidousha-selection-scorecard.v1",
        "status": "VALID",
        "tier": 1,
        "tier_basis": "relationship_chain",
        "tier_evidence_cues": [250, 255, 256, 258, 259, 262, 263, 264, 265],
        "tier_reason": (
            "片内有小李向莉亚求饶、对方答应、两人结伴以及随后连续道歉的"
            "关系互动和强烈反转，画面可补足具体游戏动作。"
        ),
        "uncertainty_penalty": 3.0,
        "weights": {
            "audience_salience": 15,
            "comedic_payoff": 10,
            "lidousha_centrality": 25,
            "persona_reversal": 10,
            "relationship_interaction": 15,
            "self_contained": 5,
            "stance_intensity": 20,
        },
    }


def _spec(tmp_path: Path) -> dict[str, object]:
    return {
        "candidate_id": "auto_223750_578_734",
        "selection_hook": (
            "莉娅求小李‘就算你是狼也放过我’，小李让她放心并提议一起走，"
            "莉娅答应后小李突然连声道歉。"
        ),
        "selection_scorecard": _stale_scorecard(),
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "path": str(
                ROOT
                / "assets/lidousha/reviewed_subtitle_baselines/"
                "auto_223750_578_734.reviewed.srt"
            ),
            "sha256": "6d79fdab105d4c7b5edffec66fe526aa01839de342a7c96b8784f4d0100a8062",
            "source_recording_basename": "22966160_20260807-22-37-50.mp4",
            "source_sha256": "66e9ec0707fe8d7f38cf8a552e863dafbba34fb7eadaece496adb30d04b1ad1e",
            "absolute_source_start_ms": 577_780,
            "absolute_source_end_ms": 735_090,
        },
        "human_truth_mode": "delivery",
        "output_root": str(tmp_path / "out"),
        "pieces": [],
    }


def _args(path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        spec=path,
        subtitle_text_overrides=None,
        subtitle_regression=None,
        speaker_overrides=None,
        ssh_host="localhost",
    )


def test_producer_accepts_exact_legacy_stale_card_but_not_an_unreceipted_new_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(producer_request, "require_branding_intro", lambda *_a, **_k: None)
    path = tmp_path / "spec.json"
    spec = _spec(tmp_path)
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    request = producer_request.load_producer_request(
        _args(path),
        repo_root=ROOT,
        profile_asset_file=lambda _name: tmp_path / "unused.json",
    )
    assert request.spec["selection_scorecard"] == _stale_scorecard()

    spec["selection_scorecard"] = {
        **_stale_scorecard(),
        "tier_reason": "new card without receipt",
    }
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="changed without a rescore receipt",
    ):
        producer_request.load_producer_request(
            _args(path),
            repo_root=ROOT,
            profile_asset_file=lambda _name: tmp_path / "unused.json",
        )
