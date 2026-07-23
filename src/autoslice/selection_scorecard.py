"""Deterministic Li Dousha talk-selection scorecards and ranking.

The semantic model may extract evidence and score rubric levels, but it does
not get to invent the arithmetic or collapse Ivan's hard topic tiers into one
opaque confidence number.  This module is the single choke point used both at
recall time and by the unattended runner when it allocates delivery slots.
"""

from __future__ import annotations

import math
from typing import Mapping


SCHEMA_VERSION = "lidousha-selection-scorecard.v1"

# Score levels are 0..4.  The weights intentionally sum to 100 so a scorecard
# remains legible to an operator without another normalization convention.
DIMENSION_WEIGHTS: dict[str, int] = {
    "lidousha_centrality": 25,
    "stance_intensity": 20,
    "audience_salience": 15,
    "relationship_interaction": 15,
    "persona_reversal": 10,
    "comedic_payoff": 10,
    "self_contained": 5,
}

TIER_ONE_BASES = frozenset(
    {
        "relationship_chain",
        "explicit_gl_stance",
        "cp_positioning",
        "audience_driven_performance",
    }
)
TIER_BASES = TIER_ONE_BASES | {"personal_stance", "generic_event"}


def _number(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def normalize_selection_scorecard(
    raw: object,
    *,
    start_cue: int,
    end_cue: int,
) -> dict[str, object] | None:
    """Validate model evidence, enforce Tier admission, and compute scores.

    Invalid or fabricated cue references fail the scorecard rather than being
    silently converted into authority.  Callers may keep the candidate as an
    explicitly unscored reserve for backwards compatibility, but it cannot
    outrank a valid Tier 1/2 scorecard.
    """

    if not isinstance(raw, Mapping):
        return None
    raw_dimensions = raw.get("dimensions")
    if not isinstance(raw_dimensions, Mapping):
        return None
    dimensions: dict[str, int] = {}
    for name in DIMENSION_WEIGHTS:
        value = raw_dimensions.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
            return None
        dimensions[name] = value

    requested_tier = raw.get("tier")
    if isinstance(requested_tier, bool) or requested_tier not in (1, 2, 3):
        return None
    evidence_raw = raw.get("tier_evidence_cues")
    if not isinstance(evidence_raw, list):
        return None
    evidence_cues: list[int] = []
    for value in evidence_raw:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not start_cue <= value <= end_cue
        ):
            return None
        if value not in evidence_cues:
            evidence_cues.append(value)

    uncertainty = _number(raw.get("uncertainty_penalty", 0), minimum=0, maximum=15)
    fatigue = _number(raw.get("fatigue_penalty", 0), minimum=0, maximum=10)
    if uncertainty is None or fatigue is None:
        return None

    tier = int(requested_tier)
    tier_basis = str(raw.get("tier_basis") or "").strip()
    if tier_basis not in TIER_BASES:
        return None
    reason_codes: list[str] = []
    if tier == 1:
        relationship_admitted = (
            tier_basis in {"relationship_chain", "explicit_gl_stance", "cp_positioning"}
            and dimensions["relationship_interaction"] >= 3
            and dimensions["audience_salience"] >= 2
            and len(evidence_cues) >= 2
        )
        performance_admitted = (
            tier_basis == "audience_driven_performance"
            and dimensions["relationship_interaction"] >= 3
            and dimensions["persona_reversal"] >= 3
            and len(evidence_cues) >= 2
        )
        if tier_basis not in TIER_ONE_BASES or not (
            relationship_admitted or performance_admitted
        ):
            tier = 2
            reason_codes.append("TIER1_ADMISSION_DOWNGRADED")

    # A clip whose hook/payoff cannot stand on its own is the metric's Tier 3,
    # even when the model assigned high topical scores elsewhere.
    if dimensions["self_contained"] <= 1 or dimensions["comedic_payoff"] <= 1:
        if tier < 3:
            reason_codes.append("LOW_READABILITY_DOWNGRADED_TIER3")
        tier = 3

    raw_score = sum(
        DIMENSION_WEIGHTS[name] * dimensions[name] / 4
        for name in DIMENSION_WEIGHTS
    )
    effective_score = max(0.0, raw_score - uncertainty - fatigue)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "VALID",
        "tier": tier,
        "requested_tier": int(requested_tier),
        "tier_basis": tier_basis,
        "tier_reason": str(raw.get("tier_reason") or "").strip()[:500],
        "tier_evidence_cues": evidence_cues,
        "dimensions": dimensions,
        "weights": dict(DIMENSION_WEIGHTS),
        "raw_score": round(raw_score, 2),
        "uncertainty_penalty": round(uncertainty, 2),
        "fatigue_penalty": round(fatigue, 2),
        "effective_score": round(effective_score, 2),
        "reason_codes": reason_codes,
    }


def selection_rank_key(item: Mapping[str, object]) -> tuple[float, float, float, str]:
    """Lexicographic hard-Tier rank, then effective score and confidence."""

    confidence_raw = item.get("confidence")
    confidence = (
        float(confidence_raw)
        if isinstance(confidence_raw, (int, float))
        and not isinstance(confidence_raw, bool)
        and math.isfinite(float(confidence_raw))
        else 0.0
    )
    scorecard = item.get("selection_scorecard")
    if selection_scorecard_is_valid(scorecard):
        return (
            float(scorecard["tier"]),
            -float(scorecard["effective_score"]),
            -confidence,
            str(item.get("cid") or item.get("candidate_id") or ""),
        )
    # Legacy/unscored candidates remain visible and runnable, but are explicit
    # Tier 3 and therefore cannot displace a newly scored Tier 1/2 candidate.
    return (
        3.0,
        -(max(0.0, min(1.0, confidence)) * 100.0),
        -confidence,
        str(item.get("cid") or item.get("candidate_id") or ""),
    )


def selection_scorecard_is_valid(raw: object) -> bool:
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("status") != "VALID"
        or raw.get("tier") not in (1, 2, 3)
        or raw.get("requested_tier") not in (1, 2, 3)
        or raw.get("tier_basis") not in TIER_BASES
        or raw.get("weights") != DIMENSION_WEIGHTS
    ):
        return False
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, Mapping) or set(dimensions) != set(DIMENSION_WEIGHTS):
        return False
    if any(
        isinstance(dimensions[name], bool)
        or not isinstance(dimensions[name], int)
        or not 0 <= dimensions[name] <= 4
        for name in DIMENSION_WEIGHTS
    ):
        return False
    evidence = raw.get("tier_evidence_cues")
    if not isinstance(evidence, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in evidence
    ):
        return False
    uncertainty = _number(raw.get("uncertainty_penalty"), minimum=0, maximum=15)
    fatigue = _number(raw.get("fatigue_penalty"), minimum=0, maximum=10)
    raw_score = _number(raw.get("raw_score"), minimum=0, maximum=100)
    effective = _number(raw.get("effective_score"), minimum=0, maximum=100)
    if None in (uncertainty, fatigue, raw_score, effective):
        return False
    expected_raw = sum(
        DIMENSION_WEIGHTS[name] * dimensions[name] / 4 for name in DIMENSION_WEIGHTS
    )
    expected_effective = max(0.0, expected_raw - uncertainty - fatigue)
    if abs(raw_score - round(expected_raw, 2)) > 0.001 or abs(
        effective - round(expected_effective, 2)
    ) > 0.001:
        return False
    normalized = normalize_selection_scorecard(
        {
            "tier": raw.get("requested_tier"),
            "tier_basis": raw.get("tier_basis"),
            "tier_reason": raw.get("tier_reason"),
            "tier_evidence_cues": evidence,
            "dimensions": dimensions,
            "uncertainty_penalty": uncertainty,
            "fatigue_penalty": fatigue,
        },
        start_cue=min(evidence, default=0),
        end_cue=max(evidence, default=0),
    )
    return bool(
        normalized is not None
        and normalized["tier"] == raw.get("tier")
        and normalized["reason_codes"] == raw.get("reason_codes")
        and normalized["effective_score"] == raw.get("effective_score")
    )
