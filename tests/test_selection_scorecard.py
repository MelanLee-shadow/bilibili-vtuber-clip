from src.autoslice.selection_scorecard import (
    SCHEMA_VERSION,
    normalize_selection_scorecard,
    selection_rank_key,
    selection_scorecard_is_valid,
)


def _raw(*, tier: int, basis: str, dimensions: dict[str, int]) -> dict:
    return {
        "tier": tier,
        "tier_basis": basis,
        "tier_reason": "two-step evidence",
        "tier_evidence_cues": [10, 12],
        "dimensions": dimensions,
        "uncertainty_penalty": 2,
        "fatigue_penalty": 1,
    }


def _dimensions(**overrides: int) -> dict[str, int]:
    base = {
        "lidousha_centrality": 4,
        "stance_intensity": 3,
        "audience_salience": 3,
        "relationship_interaction": 3,
        "persona_reversal": 3,
        "comedic_payoff": 3,
        "self_contained": 4,
    }
    base.update(overrides)
    return base


def test_scorecard_arithmetic_is_deterministic_and_evidence_bound() -> None:
    scorecard = normalize_selection_scorecard(
        _raw(
            tier=1,
            basis="relationship_chain",
            dimensions=_dimensions(),
        ),
        start_cue=8,
        end_cue=20,
    )

    assert scorecard is not None
    assert scorecard["schema_version"] == SCHEMA_VERSION
    assert scorecard["tier"] == 1
    assert scorecard["raw_score"] == 82.5
    assert scorecard["effective_score"] == 79.5


def test_incidental_name_cannot_self_declare_tier_one() -> None:
    scorecard = normalize_selection_scorecard(
        _raw(
            tier=1,
            basis="relationship_chain",
            dimensions=_dimensions(relationship_interaction=1),
        ),
        start_cue=8,
        end_cue=20,
    )

    assert scorecard is not None
    assert scorecard["tier"] == 2
    assert "TIER1_ADMISSION_DOWNGRADED" in scorecard["reason_codes"]


def test_fabricated_score_span_invalidates_scorecard() -> None:
    raw = _raw(
        tier=1,
        basis="relationship_chain",
        dimensions=_dimensions(),
    )
    raw["tier_evidence_cues"] = [10, 999]
    assert normalize_selection_scorecard(raw, start_cue=8, end_cue=20) is None


def test_hard_tier_beats_higher_numeric_lower_tier() -> None:
    tier_one = normalize_selection_scorecard(
        _raw(
            tier=1,
            basis="explicit_gl_stance",
            dimensions=_dimensions(comedic_payoff=2),
        ),
        start_cue=8,
        end_cue=20,
    )
    tier_two = normalize_selection_scorecard(
        _raw(
            tier=2,
            basis="personal_stance",
            dimensions=_dimensions(
                lidousha_centrality=4,
                stance_intensity=4,
                audience_salience=4,
                relationship_interaction=4,
                persona_reversal=4,
                comedic_payoff=4,
            ),
        ),
        start_cue=8,
        end_cue=20,
    )
    assert tier_one is not None and tier_two is not None
    assert tier_two["effective_score"] > tier_one["effective_score"]

    rows = [
        {"cid": "signoff", "confidence": 0.99, "selection_scorecard": tier_two},
        {"cid": "sumi-reversal", "confidence": 0.80, "selection_scorecard": tier_one},
    ]
    assert [row["cid"] for row in sorted(rows, key=selection_rank_key)] == [
        "sumi-reversal",
        "signoff",
    ]


def test_unknown_basis_and_tampered_arithmetic_fail_closed() -> None:
    raw = _raw(
        tier=2,
        basis="model_invented_category",
        dimensions=_dimensions(),
    )
    assert normalize_selection_scorecard(raw, start_cue=8, end_cue=20) is None

    valid = normalize_selection_scorecard(
        _raw(
            tier=1,
            basis="relationship_chain",
            dimensions=_dimensions(),
        ),
        start_cue=8,
        end_cue=20,
    )
    assert valid is not None
    valid["effective_score"] = 100.0
    assert selection_scorecard_is_valid(valid) is False
