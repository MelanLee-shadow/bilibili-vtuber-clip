import json
from pathlib import Path

import pytest

from src.autoslice.selection_scorecard import (
    SCHEMA_VERSION,
    SelectionCalibrationPolicyError,
    apply_reviewed_selection_calibration,
    load_selection_calibration_policy,
    normalize_selection_scorecard,
    selection_calibration_violations,
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


def test_722_reviewed_score_anchor_replaces_95_point_overrating() -> None:
    original = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "cp_positioning",
            "tier_reason": "金发有角妹妹与礼墨联想、男性角色反转",
            "tier_evidence_cues": [910, 916, 925],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 4,
                "audience_salience": 4,
                "relationship_interaction": 3,
                "persona_reversal": 4,
                "comedic_payoff": 4,
                "self_contained": 4,
            },
            "uncertainty_penalty": 1,
            "fatigue_penalty": 0,
        },
        start_cue=900,
        end_cue=950,
    )
    assert original is not None
    assert original["effective_score"] == 95.25
    assert selection_calibration_violations(
        "auto_193450_3573_3665", original
    ) == ["SELECTION_CALIBRATION_SCORE_OUT_OF_RANGE"]

    calibrated = apply_reviewed_selection_calibration(
        "auto_193450_3573_3665", original
    )
    assert calibrated is not None
    assert calibrated["tier"] == 1
    assert calibrated["effective_score"] == 80.25
    assert selection_calibration_violations(
        "auto_193450_3573_3665", calibrated
    ) == []


def _calibration_payload() -> dict:
    path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/selection_score_calibration.v1.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_selection_calibration_policy_loader_binds_valid_source(tmp_path) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(_calibration_payload(), ensure_ascii=False),
        encoding="utf-8",
    )

    policy = load_selection_calibration_policy(path)

    assert policy.source_path == path.resolve()
    assert policy.source_sha256.startswith("sha256:")
    assert "auto_193450_3573_3665" in policy.anchors
    assert len(policy.ordering_constraints) == 1


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        (
            lambda payload: payload.update(schema_version="stale"),
            "SELECTION_CALIBRATION_POLICY_SCHEMA_INVALID",
        ),
        (
            lambda payload: payload["anchors"].append("not-an-object"),
            "SELECTION_CALIBRATION_ANCHOR_INVALID",
        ),
        (
            lambda payload: payload["anchors"].append(
                dict(payload["anchors"][0])
            ),
            "SELECTION_CALIBRATION_CANDIDATE_ID_INVALID",
        ),
    ],
)
def test_selection_calibration_policy_never_degrades_invalid_asset_to_empty(
    tmp_path, mutation, reason_code
) -> None:
    payload = _calibration_payload()
    mutation(payload)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SelectionCalibrationPolicyError) as raised:
        load_selection_calibration_policy(path)

    assert raised.value.reason_code == reason_code


def test_selection_calibration_policy_missing_and_bad_json_fail_closed(
    tmp_path,
) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(SelectionCalibrationPolicyError) as missing_error:
        load_selection_calibration_policy(missing)
    assert (
        missing_error.value.reason_code
        == "SELECTION_CALIBRATION_POLICY_UNREADABLE"
    )

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(SelectionCalibrationPolicyError) as malformed_error:
        load_selection_calibration_policy(malformed)
    assert (
        malformed_error.value.reason_code
        == "SELECTION_CALIBRATION_POLICY_JSON_INVALID"
    )


def test_selection_calibration_ordering_constraint_is_executable(tmp_path) -> None:
    payload = _calibration_payload()
    constraint = payload["ordering_constraints"][0]
    constraint["higher_candidate_id"], constraint["lower_candidate_id"] = (
        constraint["lower_candidate_id"],
        constraint["higher_candidate_id"],
    )
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SelectionCalibrationPolicyError) as raised:
        load_selection_calibration_policy(path)

    assert (
        raised.value.reason_code
        == "SELECTION_CALIBRATION_ORDERING_CONTRADICTED"
    )
