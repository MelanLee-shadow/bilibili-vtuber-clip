"""Deterministic Li Dousha talk-selection scorecards and ranking.

The semantic model may extract evidence and score rubric levels, but it does
not get to invent the arithmetic or collapse 维护者's hard topic tiers into one
opaque confidence number.  This module is the single choke point used both at
recall time and by the unattended runner when it allocates delivery slots.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Mapping

from src.autoslice.channel_profile import load_channel_profile


# schema 按 profile 派生（默认 profile 渲染与历史字面量逐字相同）；定义见 _CHANNEL_PROFILE 加载后。

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
        "cute_performance",
    }
)
TIER_BASES = TIER_ONE_BASES | {"personal_stance", "generic_event"}
ADMISSION_POLICY_FIELD = "admission_policy"
# Candidate quality is separate from the seven arithmetic dimensions; a valid
# scorecard alone does not establish candidate production eligibility.
CANDIDATE_QUALITY_FIELD = "candidate_quality"
CANDIDATE_QUALITY_SCHEMA = "candidate-selection-quality.v1"
QUALITY_WATCHABILITY_VALUES = frozenset({"STRONG", "ORDINARY", "THIN"})
QUALITY_DIFFERENTIATION_VALUES = frozenset({"DISTINCTIVE", "ORDINARY", "THIN"})
QUALITY_DISPOSITION_VALUES = frozenset({"PRODUCTION", "RESERVE", "HOLD"})
# Historical v1 cards have no policy field; v2/v3 retain their original rules
# because card bytes and Tier facts may be receipt-bound.
LEGACY_ADMISSION_POLICY = "selection-scorecard-admission.v2"
PREVIOUS_ADMISSION_POLICY = "selection-scorecard-admission.v3"
CURRENT_ADMISSION_POLICY = "selection-scorecard-admission.v4"
_CHANNEL_PROFILE = load_channel_profile(
    Path(__file__).resolve().parents[2]
)
SCHEMA_VERSION = f"{_CHANNEL_PROFILE.profile_id}-selection-scorecard.v1"


def _number(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _performance_tier_one_admitted(
    dimensions: Mapping[str, int],
    evidence_cues: list[int],
    *,
    admission_policy: str | None,
) -> bool:
    """Check performance admission without changing receipt-bound old policies."""

    if admission_policy == CURRENT_ADMISSION_POLICY:
        return len(evidence_cues) >= 1
    admitted = (
        dimensions["relationship_interaction"] >= 3
        and dimensions["persona_reversal"] >= 3
        and len(evidence_cues) >= 2
    )
    if admission_policy in {LEGACY_ADMISSION_POLICY, PREVIOUS_ADMISSION_POLICY}:
        admitted = admitted and (
            dimensions["audience_salience"] >= 2
            and dimensions["self_contained"] >= 3
            and dimensions["comedic_payoff"] == 4
        )
    return admitted


def _v3_quality_gate(quality: Mapping[str, object] | None) -> bool:
    """Return whether a candidate has the common v3 Tier 1 content proof."""

    if not isinstance(quality, Mapping):
        return False
    return (
        quality.get("watchability") == "STRONG"
        and quality.get("differentiation") == "DISTINCTIVE"
        and quality.get("production_disposition") == "PRODUCTION"
        and bool(str(quality.get("reason") or "").strip())
        and isinstance(quality.get("evidence_cues"), list)
        and bool(quality.get("evidence_cues"))
    )


def _v3_common_dimensions_gate(dimensions: Mapping[str, int]) -> bool:
    """Return whether the v3 common content dimensions are substantive."""

    return (
        dimensions["audience_salience"] >= 2
        and dimensions["self_contained"] >= 3
        and dimensions["comedic_payoff"] >= 3
    )


def _normalize_candidate_quality(
    raw: object,
    *,
    start_cue: int,
    end_cue: int,
) -> dict[str, object] | None:
    """Normalize the non-arithmetic, candidate-scoped production judgment.

    This is intentionally categorical rather than another score.  The model
    must state what a viewer gets from the window, how distinctive it is, and
    which in-window cues support that statement.  The production disposition
    remains an explicit editorial output; deterministic code only prevents a
    thin or unsupported claim from entering a production slot.
    """

    if not isinstance(raw, Mapping):
        return None
    watchability = str(raw.get("watchability") or "").strip().upper()
    differentiation = str(raw.get("differentiation") or "").strip().upper()
    disposition = str(raw.get("production_disposition") or "").strip().upper()
    reason = str(raw.get("reason") or "").strip()[:500]
    evidence_raw = raw.get("evidence_cues")
    if (
        watchability not in QUALITY_WATCHABILITY_VALUES
        or differentiation not in QUALITY_DIFFERENTIATION_VALUES
        or disposition not in QUALITY_DISPOSITION_VALUES
        or not reason
        or not isinstance(evidence_raw, list)
        or not evidence_raw
    ):
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
    return {
        "schema_version": CANDIDATE_QUALITY_SCHEMA,
        "watchability": watchability,
        "differentiation": differentiation,
        "production_disposition": disposition,
        "reason": reason,
        "evidence_cues": evidence_cues,
    }


def _normalize_selection_scorecard(
    raw: object,
    *,
    start_cue: int,
    end_cue: int,
    admission_policy: str | None,
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
    if tier_basis == "cute_performance" and admission_policy != CURRENT_ADMISSION_POLICY:
        return None
    quality: dict[str, object] | None = None
    if CANDIDATE_QUALITY_FIELD in raw:
        quality = _normalize_candidate_quality(
            raw.get(CANDIDATE_QUALITY_FIELD),
            start_cue=start_cue,
            end_cue=end_cue,
        )
        if quality is None:
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
            (tier_basis == "audience_driven_performance" or (
                tier_basis == "cute_performance" and admission_policy == CURRENT_ADMISSION_POLICY
            ))
            and _performance_tier_one_admitted(
                dimensions,
                evidence_cues,
                admission_policy=admission_policy,
            )
        )
        v3_quality_admitted = _v3_quality_gate(quality)
        v3_personal_or_event_admitted = (
            tier_basis in {"personal_stance", "generic_event"}
            and (dimensions["lidousha_centrality"] >= 3 or dimensions["stance_intensity"] >= 3)
            and len(evidence_cues) >= 1
        )
        if admission_policy in {PREVIOUS_ADMISSION_POLICY, CURRENT_ADMISSION_POLICY}:
            admitted = _v3_common_dimensions_gate(dimensions) and v3_quality_admitted and (
                relationship_admitted
                or performance_admitted
                or v3_personal_or_event_admitted
            )
        else:
            admitted = tier_basis in TIER_ONE_BASES and (
                relationship_admitted or performance_admitted
            )
        if not admitted:
            tier = 2
            reason_codes.append("TIER1_ADMISSION_DOWNGRADED")
            if admission_policy in {PREVIOUS_ADMISSION_POLICY, CURRENT_ADMISSION_POLICY} and not v3_quality_admitted:
                reason_codes.append("TIER1_COMMON_QUALITY_GATE_FAILED")
            if admission_policy in {PREVIOUS_ADMISSION_POLICY, CURRENT_ADMISSION_POLICY} and not _v3_common_dimensions_gate(dimensions):
                reason_codes.append("TIER1_COMMON_DIMENSION_GATE_FAILED")

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
    normalized: dict[str, object] = {
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
    if quality is not None:
        normalized[CANDIDATE_QUALITY_FIELD] = quality
    if admission_policy in {PREVIOUS_ADMISSION_POLICY, CURRENT_ADMISSION_POLICY}:
        normalized[ADMISSION_POLICY_FIELD] = admission_policy
    return normalized


def normalize_selection_scorecard(
    raw: object,
    *,
    start_cue: int,
    end_cue: int,
) -> dict[str, object] | None:
    """Normalize a new v4 card; validate historical cards under their own policy."""

    if not isinstance(raw, Mapping):
        return None
    if (
        ADMISSION_POLICY_FIELD in raw
        and raw.get(ADMISSION_POLICY_FIELD) != CURRENT_ADMISSION_POLICY
    ):
        return None
    return _normalize_selection_scorecard(
        raw,
        start_cue=start_cue,
        end_cue=end_cue,
        admission_policy=CURRENT_ADMISSION_POLICY,
    )


def _ranking_tier(scorecard: Mapping[str, object]) -> float:
    """Rank historical performance without changing receipt-bound card bytes."""

    tier = float(scorecard["tier"])
    if (
        ADMISSION_POLICY_FIELD not in scorecard
        and tier == 1.0
        and scorecard.get("tier_basis") == "audience_driven_performance"
    ):
        dimensions = scorecard.get("dimensions")
        evidence = scorecard.get("tier_evidence_cues")
        if isinstance(dimensions, Mapping) and isinstance(evidence, list):
            if not _performance_tier_one_admitted(
                dimensions,
                evidence,
                admission_policy=LEGACY_ADMISSION_POLICY,
            ):
                return 2.0
    return tier


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
        assert isinstance(scorecard, Mapping)
        return (
            _ranking_tier(scorecard),
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


def selection_scorecard_production_admission(raw: object) -> dict[str, object]:
    """Return the candidate-scoped production disposition.

    Historical cards have no ``admission_policy`` and retain their original
    admission fact.  v2/v3 cards retain their original quality gate.  New
    cards use the current rubric before a normal semantic candidate can take
    a fresh production seat.
    This gate never changes Tier or either seven-dimension score.
    """

    if not selection_scorecard_is_valid(raw):
        return {
            "status": "HOLD",
            "reason_code": "SELECTION_SCORECARD_INVALID",
        }
    assert isinstance(raw, Mapping)
    policy = raw.get(ADMISSION_POLICY_FIELD)
    if policy not in {LEGACY_ADMISSION_POLICY, PREVIOUS_ADMISSION_POLICY, CURRENT_ADMISSION_POLICY}:
        return {
            "status": "ADMIT",
            "reason_code": "HISTORICAL_SCORECARD_COMPATIBILITY",
        }
    quality = raw.get(CANDIDATE_QUALITY_FIELD)
    if not isinstance(quality, Mapping):
        review_context: dict[str, object] = {}
        dimensions = raw.get("dimensions")
        if isinstance(dimensions, Mapping):
            review_context = {
                "tier_basis": str(raw.get("tier_basis") or ""),
                "dimensions": dict(dimensions),
                "tier_reason": str(raw.get("tier_reason") or "").strip()[:500],
            }
        return {
            "status": "HOLD",
            "reason_code": "CANDIDATE_QUALITY_REVIEW_REQUIRED",
            "review_context": review_context,
        }
    watchability = str(quality.get("watchability") or "")
    differentiation = str(quality.get("differentiation") or "")
    disposition = str(quality.get("production_disposition") or "")
    reason = str(quality.get("reason") or "").strip()
    if disposition != "PRODUCTION":
        return {
            "status": "HOLD",
            "reason_code": f"CANDIDATE_QUALITY_{disposition or 'INVALID'}",
            "reason": reason,
        }
    if watchability == "THIN" or differentiation == "THIN":
        return {
            "status": "HOLD",
            "reason_code": "CANDIDATE_QUALITY_THIN_CANNOT_PRODUCE",
            "reason": reason,
        }
    if watchability == "ORDINARY" and differentiation == "ORDINARY":
        return {
            "status": "HOLD",
            "reason_code": "CANDIDATE_QUALITY_ORDINARY_RESERVE",
            "reason": reason,
        }
    return {
        "status": "ADMIT",
        "reason_code": "CANDIDATE_QUALITY_PRODUCTION",
        "reason": reason,
    }


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
    admission_policy: str | None
    if ADMISSION_POLICY_FIELD in raw:
        if raw.get(ADMISSION_POLICY_FIELD) not in {
            LEGACY_ADMISSION_POLICY,
            PREVIOUS_ADMISSION_POLICY,
            CURRENT_ADMISSION_POLICY,
        }:
            return False
        admission_policy = str(raw[ADMISSION_POLICY_FIELD])
    else:
        # No identifier is the compatibility marker for historical v1 cards.
        admission_policy = None

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
    validation_cues = list(evidence)
    raw_quality = raw.get(CANDIDATE_QUALITY_FIELD)
    if isinstance(raw_quality, Mapping) and isinstance(
        raw_quality.get("evidence_cues"), list
    ):
        validation_cues.extend(
            value
            for value in raw_quality["evidence_cues"]
            if isinstance(value, int) and not isinstance(value, bool)
        )
    normalized = _normalize_selection_scorecard(
        {
            "tier": raw.get("requested_tier"),
            "tier_basis": raw.get("tier_basis"),
            "tier_reason": raw.get("tier_reason"),
            "tier_evidence_cues": evidence,
            "dimensions": dimensions,
            "uncertainty_penalty": uncertainty,
            "fatigue_penalty": fatigue,
            **(
                {CANDIDATE_QUALITY_FIELD: raw.get(CANDIDATE_QUALITY_FIELD)}
                if CANDIDATE_QUALITY_FIELD in raw
                else {}
            ),
        },
        start_cue=min(validation_cues, default=0),
        end_cue=max(validation_cues, default=0),
        admission_policy=admission_policy,
    )
    return bool(
        normalized is not None
        and normalized["tier"] == raw.get("tier")
        and normalized["tier_evidence_cues"] == evidence
        and normalized["reason_codes"] == raw.get("reason_codes")
        and normalized["effective_score"] == raw.get("effective_score")
        and normalized.get(CANDIDATE_QUALITY_FIELD)
        == raw.get(CANDIDATE_QUALITY_FIELD)
    )
