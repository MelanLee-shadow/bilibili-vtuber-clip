import json

import pytest

from src.autoslice.review_evidence import SourceCue
from src.autoslice.selection_scorecard import (
    PREVIOUS_ADMISSION_POLICY,
    _normalize_selection_scorecard,
    apply_reviewed_selection_calibration,
    normalize_selection_scorecard,
    selection_scorecard_is_valid,
)
from src.autoslice.semantic_candidate_selector import select_semantic_session_candidates


def _cute_raw(basis: str = "audience_driven_performance") -> dict:
    return {
        "tier": 1,
        "tier_basis": basis,
        "tier_reason": (
            "主播主动用软软的声音撒娇要抱抱，可爱时刻自身完整。"
            if basis == "cute_performance" else
            "主播用软软的声音回应观众的抱抱请求，完整呈现可爱的回应。"
        ),
        "tier_evidence_cues": [10],
        "dimensions": {
            "lidousha_centrality": 4,
            "stance_intensity": 0,
            "audience_salience": 3,
            "relationship_interaction": 1,
            "persona_reversal": 0,
            "comedic_payoff": 3,
            "self_contained": 4,
        },
        "uncertainty_penalty": 0,
        "fatigue_penalty": 0,
        "candidate_quality": {
            "watchability": "STRONG",
            "differentiation": "DISTINCTIVE",
            "production_disposition": "PRODUCTION",
            "reason": (
                "主播主动用软软的声音撒娇要抱抱，可爱表现本身就是看点。"
                if basis == "cute_performance" else
                "观众提出抱抱后，主播用软软的声音回应，可爱回应本身就是看点。"
            ),
            "evidence_cues": [10],
        },
    }


@pytest.mark.parametrize("basis", ["audience_driven_performance", "cute_performance"])
def test_clear_self_contained_cute_performance_can_reach_tier_one(basis: str) -> None:
    card = normalize_selection_scorecard(_cute_raw(basis), start_cue=8, end_cue=12)
    assert card is not None
    assert card["tier"] == 1
    assert selection_scorecard_is_valid(card)


def test_v3_cute_receipt_retains_old_admission_and_bytes() -> None:
    old = _normalize_selection_scorecard(
        _cute_raw(), start_cue=8, end_cue=12, admission_policy=PREVIOUS_ADMISSION_POLICY
    )
    assert old is not None and old["tier"] == 2
    assert selection_scorecard_is_valid(old)
    assert apply_reviewed_selection_calibration("not-an-anchor", old) == old


@pytest.mark.parametrize("missing", ["quality", "cue", "context", "payoff"])
def test_cute_candidate_still_obeys_existing_content_guards(missing: str) -> None:
    raw = _cute_raw("cute_performance")
    if missing == "quality":
        raw["candidate_quality"]["watchability"] = "THIN"
    elif missing == "cue":
        raw["tier_evidence_cues"] = []
    elif missing == "context":
        raw["dimensions"]["self_contained"] = 1
    else:
        raw["dimensions"]["comedic_payoff"] = 1
    card = normalize_selection_scorecard(raw, start_cue=8, end_cue=12)
    assert card is not None
    assert card["tier"] == (3 if missing in {"context", "payoff"} else 2)


@pytest.mark.parametrize("kind", ["cute", "solo_reaction"])
def test_ordinary_recall_keeps_clear_cute_and_solo_reaction(kind: str) -> None:
    cues = [
        SourceCue("1", 0, 20_000, "观众提问或作品画面引出话题"),
        SourceCue("2", 25_000, 40_000, "李豆沙给出自己的可爱回应或观看反应"),
        SourceCue("3", 45_000, 70_000, "回应在片内收束"),
    ]
    raw = _cute_raw("cute_performance" if kind == "cute" else "generic_event")
    raw["tier_evidence_cues"] = [2]
    raw["candidate_quality"]["evidence_cues"] = [2]
    payload = {"candidates": [{"start_cue": 1, "end_cue": 3, "kind": "talk",
        "event_key": kind, "hook": "主播的片内回应", "confidence": 0.95,
        "selection_scorecard": raw}]}
    prompts = []

    def reply(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(payload, ensure_ascii=False)

    candidates, diagnostics = select_semantic_session_candidates(cues, llm_call=reply)
    assert len(candidates) == 1
    cid = candidates[0].anchor.candidate_id
    assert diagnostics["scorecards"][cid]["tier"] == 1
    assert "cute_performance" in prompts[0]
