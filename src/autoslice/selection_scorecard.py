"""Deterministic Li Dousha talk-selection scorecards and ranking.

The semantic model may extract evidence and score rubric levels, but it does
not get to invent the arithmetic or collapse Ivan's hard topic tiers into one
opaque confidence number.  This module is the single choke point used both at
recall time and by the unattended runner when it allocates delivery slots.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.channel_profile import ChannelProfile, load_channel_profile


SCHEMA_VERSION = "lidousha-selection-scorecard.v1"
CALIBRATION_SCHEMA_VERSION = "lidousha-selection-score-calibration.v1"

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
_CHANNEL_PROFILE = load_channel_profile(
    Path(__file__).resolve().parents[2]
)


class SelectionCalibrationPolicyError(ValueError):
    """The reviewed selection-calibration authority is unusable."""

    def __init__(self, reason_code: str, path: Path, detail: str = "") -> None:
        self.reason_code = reason_code
        self.path = path
        self.detail = detail
        message = f"{reason_code}: {path}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class SelectionCalibrationPolicy:
    source_path: Path
    source_sha256: str
    anchors: Mapping[str, Mapping[str, object]]
    ordering_constraints: tuple[Mapping[str, object], ...]


def _number(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _calibration_error(path: Path, code: str, detail: str = "") -> None:
    raise SelectionCalibrationPolicyError(code, path, detail)


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    *,
    path: Path,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        _calibration_error(
            path,
            "SELECTION_CALIBRATION_KEYS_INVALID",
            f"{label}: missing={sorted(expected - actual)} extra={sorted(actual - expected)}",
        )


def _calibrated_effective_score(anchor: Mapping[str, object]) -> float:
    dimensions = anchor["dimensions"]
    assert isinstance(dimensions, Mapping)
    raw_score = sum(
        DIMENSION_WEIGHTS[name] * int(dimensions[name]) / 4
        for name in DIMENSION_WEIGHTS
    )
    return round(
        max(
            0.0,
            raw_score
            - float(anchor["uncertainty_penalty"])
            - float(anchor["fatigue_penalty"]),
        ),
        2,
    )


def load_selection_calibration_policy(path: Path) -> SelectionCalibrationPolicy:
    """Load and fully validate the reviewed calibration authority.

    Missing or malformed authority is a production blocker.  Returning an
    empty anchor table would make a bad policy indistinguishable from a valid
    policy that intentionally names no candidate.
    """

    path = path.resolve()
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        _calibration_error(
            path, "SELECTION_CALIBRATION_POLICY_UNREADABLE", type(exc).__name__
        )
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _calibration_error(
            path, "SELECTION_CALIBRATION_POLICY_JSON_INVALID", type(exc).__name__
        )
    if not isinstance(payload, Mapping):
        _calibration_error(path, "SELECTION_CALIBRATION_POLICY_ROOT_INVALID")
    _require_exact_keys(
        payload,
        {"schema_version", "authority", "anchors", "ordering_constraints"},
        path=path,
        label="root",
    )
    if payload.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        _calibration_error(path, "SELECTION_CALIBRATION_POLICY_SCHEMA_INVALID")
    if not str(payload.get("authority") or "").strip():
        _calibration_error(path, "SELECTION_CALIBRATION_AUTHORITY_MISSING")
    raw_anchors = payload.get("anchors")
    raw_constraints = payload.get("ordering_constraints")
    if not isinstance(raw_anchors, list) or not isinstance(raw_constraints, list):
        _calibration_error(path, "SELECTION_CALIBRATION_COLLECTION_INVALID")

    anchors: dict[str, Mapping[str, object]] = {}
    anchor_scores: dict[str, float] = {}
    anchor_keys = {
        "candidate_id",
        "label",
        "expected_tier",
        "effective_score_min",
        "effective_score_max",
        "dimensions",
        "uncertainty_penalty",
        "fatigue_penalty",
    }
    for index, raw_anchor in enumerate(raw_anchors):
        if not isinstance(raw_anchor, Mapping):
            _calibration_error(
                path, "SELECTION_CALIBRATION_ANCHOR_INVALID", f"anchors[{index}]"
            )
        _require_exact_keys(
            raw_anchor,
            anchor_keys,
            path=path,
            label=f"anchors[{index}]",
        )
        candidate_id = str(raw_anchor.get("candidate_id") or "")
        if not candidate_id or candidate_id in anchors:
            _calibration_error(
                path,
                "SELECTION_CALIBRATION_CANDIDATE_ID_INVALID",
                f"anchors[{index}]={candidate_id!r}",
            )
        if not str(raw_anchor.get("label") or "").strip():
            _calibration_error(
                path, "SELECTION_CALIBRATION_LABEL_MISSING", candidate_id
            )
        expected_tier = raw_anchor.get("expected_tier")
        if isinstance(expected_tier, bool) or expected_tier not in (1, 2, 3):
            _calibration_error(
                path, "SELECTION_CALIBRATION_TIER_INVALID", candidate_id
            )
        dimensions = raw_anchor.get("dimensions")
        if not isinstance(dimensions, Mapping) or set(dimensions) != set(
            DIMENSION_WEIGHTS
        ):
            _calibration_error(
                path, "SELECTION_CALIBRATION_DIMENSIONS_INVALID", candidate_id
            )
        if any(
            isinstance(dimensions[name], bool)
            or not isinstance(dimensions[name], int)
            or not 0 <= dimensions[name] <= 4
            for name in DIMENSION_WEIGHTS
        ):
            _calibration_error(
                path, "SELECTION_CALIBRATION_DIMENSIONS_INVALID", candidate_id
            )
        minimum = _number(
            raw_anchor.get("effective_score_min"), minimum=0, maximum=100
        )
        maximum = _number(
            raw_anchor.get("effective_score_max"), minimum=0, maximum=100
        )
        uncertainty = _number(
            raw_anchor.get("uncertainty_penalty"), minimum=0, maximum=15
        )
        fatigue = _number(
            raw_anchor.get("fatigue_penalty"), minimum=0, maximum=10
        )
        if (
            minimum is None
            or maximum is None
            or minimum > maximum
            or uncertainty is None
            or fatigue is None
        ):
            _calibration_error(
                path, "SELECTION_CALIBRATION_NUMERIC_RANGE_INVALID", candidate_id
            )
        anchor = dict(raw_anchor)
        effective = _calibrated_effective_score(anchor)
        if not minimum <= effective <= maximum:
            _calibration_error(
                path,
                "SELECTION_CALIBRATION_FIXED_SCORE_OUT_OF_RANGE",
                f"{candidate_id}={effective}",
            )
        anchors[candidate_id] = anchor
        anchor_scores[candidate_id] = effective

    constraints: list[Mapping[str, object]] = []
    seen_constraints: set[tuple[str, str]] = set()
    constraint_keys = {
        "higher_candidate_id",
        "lower_candidate_id",
        "reason",
    }
    for index, raw_constraint in enumerate(raw_constraints):
        if not isinstance(raw_constraint, Mapping):
            _calibration_error(
                path,
                "SELECTION_CALIBRATION_ORDERING_INVALID",
                f"ordering_constraints[{index}]",
            )
        _require_exact_keys(
            raw_constraint,
            constraint_keys,
            path=path,
            label=f"ordering_constraints[{index}]",
        )
        higher = str(raw_constraint.get("higher_candidate_id") or "")
        lower = str(raw_constraint.get("lower_candidate_id") or "")
        pair = (higher, lower)
        if (
            not higher
            or not lower
            or higher == lower
            or higher not in anchors
            or lower not in anchors
            or pair in seen_constraints
            or not str(raw_constraint.get("reason") or "").strip()
        ):
            _calibration_error(
                path,
                "SELECTION_CALIBRATION_ORDERING_INVALID",
                f"{higher!r}>{lower!r}",
            )
        higher_rank = (
            int(anchors[higher]["expected_tier"]),
            -anchor_scores[higher],
        )
        lower_rank = (
            int(anchors[lower]["expected_tier"]),
            -anchor_scores[lower],
        )
        if not higher_rank < lower_rank:
            _calibration_error(
                path,
                "SELECTION_CALIBRATION_ORDERING_CONTRADICTED",
                f"{higher}>{lower}",
            )
        seen_constraints.add(pair)
        constraints.append(dict(raw_constraint))

    return SelectionCalibrationPolicy(
        source_path=path,
        source_sha256="sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
        anchors=anchors,
        ordering_constraints=tuple(constraints),
    )


def load_selected_selection_calibration_policy(
    profile: ChannelProfile | None = None,
) -> SelectionCalibrationPolicy:
    selected = profile or _CHANNEL_PROFILE
    try:
        path = selected.asset_file("selection_score_calibration")
    except (KeyError, ValueError) as exc:
        fallback = selected.manifest_path
        _calibration_error(
            fallback,
            "SELECTION_CALIBRATION_PROFILE_BINDING_INVALID",
            type(exc).__name__,
        )
    return load_selection_calibration_policy(path)


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


def selection_calibration_violations(
    candidate_id: str,
    scorecard: object,
    *,
    policy: SelectionCalibrationPolicy | None = None,
) -> list[str]:
    """Enforce reviewed score ranges for named regression anchors."""

    selected_policy = policy or load_selected_selection_calibration_policy()
    anchor = selected_policy.anchors.get(str(candidate_id or ""))
    if anchor is None:
        return []
    if not selection_scorecard_is_valid(scorecard):
        return ["SELECTION_CALIBRATION_SCORECARD_INVALID"]
    assert isinstance(scorecard, Mapping)
    violations: list[str] = []
    if scorecard.get("tier") != anchor.get("expected_tier"):
        violations.append("SELECTION_CALIBRATION_TIER_MISMATCH")
    effective = float(scorecard["effective_score"])
    minimum = _number(
        anchor.get("effective_score_min"), minimum=0, maximum=100
    )
    maximum = _number(
        anchor.get("effective_score_max"), minimum=0, maximum=100
    )
    if (
        minimum is None
        or maximum is None
        or minimum > maximum
        or not minimum <= effective <= maximum
    ):
        violations.append("SELECTION_CALIBRATION_SCORE_OUT_OF_RANGE")
    return violations


def apply_reviewed_selection_calibration(
    candidate_id: str,
    scorecard: object,
    *,
    policy: SelectionCalibrationPolicy | None = None,
) -> dict[str, object] | None:
    """Apply a reviewed anchor while preserving the candidate's cue evidence."""

    selected_policy = policy or load_selected_selection_calibration_policy()
    if not selection_scorecard_is_valid(scorecard):
        return dict(scorecard) if isinstance(scorecard, dict) else None
    assert isinstance(scorecard, Mapping)
    anchor = selected_policy.anchors.get(str(candidate_id or ""))
    if anchor is None:
        return dict(scorecard)
    dimensions = anchor.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise SelectionCalibrationPolicyError(
            "SELECTION_CALIBRATION_DIMENSIONS_INVALID",
            selected_policy.source_path,
            str(candidate_id or ""),
        )
    evidence = list(scorecard.get("tier_evidence_cues") or [])
    normalized = normalize_selection_scorecard(
        {
            "tier": anchor.get("expected_tier"),
            "tier_basis": scorecard.get("tier_basis"),
            "tier_reason": scorecard.get("tier_reason"),
            "tier_evidence_cues": evidence,
            "dimensions": dict(dimensions),
            "uncertainty_penalty": anchor.get("uncertainty_penalty"),
            "fatigue_penalty": anchor.get("fatigue_penalty"),
        },
        start_cue=min(evidence, default=0),
        end_cue=max(evidence, default=0),
    )
    if normalized is None or selection_calibration_violations(
        candidate_id, normalized, policy=selected_policy
    ):
        raise SelectionCalibrationPolicyError(
            "SELECTION_CALIBRATION_APPLICATION_INVALID",
            selected_policy.source_path,
            str(candidate_id or ""),
        )
    return normalized


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
