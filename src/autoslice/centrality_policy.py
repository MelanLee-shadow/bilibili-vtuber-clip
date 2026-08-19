"""Deterministic centrality eligibility policy for talk candidates.

Centrality is an identity-dependent gate, not a compensable score.  Speaker
attribution and narrative centrality are deliberately separate inputs:
acoustic host share can support the semantic judgement but can never select a
level by itself.

The policy approved by 维护者 is:

* unscored work is ``NOT_EVALUATED``;
* unresolved speaker identity is ``HUMAN_REVIEW_REQUIRED``;
* levels 0 and 1 are automatically ineligible;
* level 2 is manual-selection-only;
* levels 3 and 4 are equally auto-eligible and must not be rank tie-breakers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.channel_profile import load_channel_profile


_PROFILE = load_channel_profile(Path(__file__).resolve().parents[2])
DECISION_POLICY_VERSION = "centrality-eligibility-2026-08-11.v1"
RUBRIC_VERSION = f"{_PROFILE.profile_id}-centrality-rubric.v2"

ASSESSMENT_NOT_RUN = "NOT_RUN"
ASSESSMENT_NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
ASSESSMENT_SCORED = "SCORED"
ASSESSMENT_STATUSES = frozenset(
    {
        ASSESSMENT_NOT_RUN,
        ASSESSMENT_NEEDS_HUMAN_REVIEW,
        ASSESSMENT_SCORED,
    }
)

SPEAKER_NOT_RUN = "NOT_RUN"
SPEAKER_NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
SPEAKER_VERIFIED = "VERIFIED"
SPEAKER_STATUSES = frozenset({SPEAKER_NOT_RUN, SPEAKER_NEEDS_HUMAN_REVIEW, SPEAKER_VERIFIED})

DISPOSITION_NOT_EVALUATED = "NOT_EVALUATED"
DISPOSITION_HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
DISPOSITION_AUTO_INELIGIBLE = "AUTO_INELIGIBLE"
DISPOSITION_MANUAL_SELECTION_ONLY = "MANUAL_SELECTION_ONLY"
DISPOSITION_AUTO_ELIGIBLE = "AUTO_ELIGIBLE"
DISPOSITIONS = frozenset(
    {
        DISPOSITION_NOT_EVALUATED,
        DISPOSITION_HUMAN_REVIEW_REQUIRED,
        DISPOSITION_AUTO_INELIGIBLE,
        DISPOSITION_MANUAL_SELECTION_ONLY,
        DISPOSITION_AUTO_ELIGIBLE,
    }
)

HOST_SPEAKER_LABEL = f"[{_PROFILE.host_speaker_label}]"
OTHER_SPEAKER_LABEL = "[其他]"
UNCERTAIN_SPEAKER_LABEL = "[存疑]"
SPEAKER_LABELS = frozenset({HOST_SPEAKER_LABEL, OTHER_SPEAKER_LABEL, UNCERTAIN_SPEAKER_LABEL})
HOST_SHARE_BANDS = frozenset({"trace", "low", "mixed", "dominant", "unavailable"})


class CentralityPolicyError(ValueError):
    """A centrality assessment violates the approved policy contract."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class CentralityProjection:
    assessment_status: str
    speaker_status: str
    level: int | None
    disposition: str
    automatic_eligible: bool
    manual_selection_required: bool
    human_review_required: bool
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _valid_level(level: object) -> bool:
    return isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 4


def project_centrality_disposition(
    *,
    assessment_status: str,
    speaker_status: str,
    level: int | None,
) -> CentralityProjection:
    """Project the approved 0--4 policy without reading any ranking field.

    ``NOT_RUN`` is intentionally distinct from an assessed ``null``.  The
    former blocks unattended production while remaining outside the human
    queue; the latter means decision-relevant attribution could not be
    resolved and therefore requires review.
    """

    if assessment_status not in ASSESSMENT_STATUSES:
        raise CentralityPolicyError("CENTRALITY_ASSESSMENT_STATUS_INVALID")
    if speaker_status not in SPEAKER_STATUSES:
        raise CentralityPolicyError("CENTRALITY_SPEAKER_STATUS_INVALID")

    if assessment_status == ASSESSMENT_NOT_RUN:
        if level is not None:
            raise CentralityPolicyError("CENTRALITY_NOT_RUN_LEVEL_MUST_BE_NULL")
        if speaker_status == SPEAKER_NEEDS_HUMAN_REVIEW:
            raise CentralityPolicyError("CENTRALITY_UNRESOLVED_SPEAKER_MUST_ENTER_HUMAN_REVIEW")
        return CentralityProjection(
            assessment_status=assessment_status,
            speaker_status=speaker_status,
            level=None,
            disposition=DISPOSITION_NOT_EVALUATED,
            automatic_eligible=False,
            manual_selection_required=False,
            human_review_required=False,
            reason_codes=("CENTRALITY_NOT_EVALUATED",),
        )

    if assessment_status == ASSESSMENT_NEEDS_HUMAN_REVIEW:
        if level is not None:
            raise CentralityPolicyError("CENTRALITY_HUMAN_REVIEW_LEVEL_MUST_BE_NULL")
        if speaker_status != SPEAKER_NEEDS_HUMAN_REVIEW:
            raise CentralityPolicyError("CENTRALITY_HUMAN_REVIEW_REQUIRES_UNRESOLVED_SPEAKER")
        return CentralityProjection(
            assessment_status=assessment_status,
            speaker_status=speaker_status,
            level=None,
            disposition=DISPOSITION_HUMAN_REVIEW_REQUIRED,
            automatic_eligible=False,
            manual_selection_required=True,
            human_review_required=True,
            reason_codes=("CENTRALITY_SPEAKER_ATTRIBUTION_UNRESOLVED",),
        )

    if speaker_status != SPEAKER_VERIFIED:
        raise CentralityPolicyError("CENTRALITY_SCORE_REQUIRES_VERIFIED_SPEAKER_ATTRIBUTION")
    if not _valid_level(level):
        raise CentralityPolicyError("CENTRALITY_LEVEL_INVALID")
    assert isinstance(level, int) and not isinstance(level, bool)
    if level <= 1:
        disposition = DISPOSITION_AUTO_INELIGIBLE
        reasons = ("CENTRALITY_LEVEL_AUTO_INELIGIBLE",)
    elif level == 2:
        disposition = DISPOSITION_MANUAL_SELECTION_ONLY
        reasons = ("CENTRALITY_LEVEL_REQUIRES_MANUAL_SELECTION",)
    else:
        disposition = DISPOSITION_AUTO_ELIGIBLE
        reasons = ("CENTRALITY_LEVEL_AUTO_ELIGIBLE",)
    return CentralityProjection(
        assessment_status=assessment_status,
        speaker_status=speaker_status,
        level=level,
        disposition=disposition,
        automatic_eligible=disposition == DISPOSITION_AUTO_ELIGIBLE,
        manual_selection_required=disposition == DISPOSITION_MANUAL_SELECTION_ONLY,
        human_review_required=False,
        reason_codes=reasons,
    )


def _normalize_evidence(raw_evidence: object) -> list[dict[str, object]]:
    if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, (str, bytes, bytearray)):
        raise CentralityPolicyError("CENTRALITY_EVIDENCE_INVALID")
    evidence: list[dict[str, object]] = []
    seen: set[tuple[object, int, int]] = set()
    for raw in raw_evidence:
        if not isinstance(raw, Mapping):
            raise CentralityPolicyError("CENTRALITY_EVIDENCE_ROW_INVALID")
        cue_id = raw.get("cue_id")
        start_ms = raw.get("start_ms")
        end_ms = raw.get("end_ms")
        speaker_label = raw.get("speaker_label")
        claim = str(raw.get("claim") or "").strip()
        if (
            cue_id is None
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
            or speaker_label not in SPEAKER_LABELS
            or not claim
        ):
            raise CentralityPolicyError("CENTRALITY_EVIDENCE_ROW_INVALID")
        identity = (cue_id, start_ms, end_ms)
        if identity in seen:
            raise CentralityPolicyError("CENTRALITY_EVIDENCE_DUPLICATED")
        seen.add(identity)
        evidence.append(
            {
                "cue_id": cue_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_label": speaker_label,
                "claim": claim[:500],
            }
        )
    return evidence


def normalize_centrality_assessment(raw: object) -> dict[str, object]:
    """Validate the semantic rubric output and attach deterministic policy.

    This function never infers identity from words, first-person pronouns, or
    acoustic occupancy.  It consumes explicit speaker labels only.
    """

    if not isinstance(raw, Mapping):
        raise CentralityPolicyError("CENTRALITY_ASSESSMENT_INVALID")
    assessment_status = str(raw.get("status") or "")
    speaker_status = str(raw.get("speaker_status") or "")
    level = raw.get("level")
    projection = project_centrality_disposition(
        assessment_status=assessment_status,
        speaker_status=speaker_status,
        level=level if level is None or isinstance(level, int) else level,  # type: ignore[arg-type]
    )
    band = str(raw.get("host_speech_share_band") or "")
    if band not in HOST_SHARE_BANDS:
        raise CentralityPolicyError("CENTRALITY_HOST_SHARE_BAND_INVALID")
    evidence = _normalize_evidence(raw.get("evidence", []))
    counterfactual = str(raw.get("counterfactual") or "").strip()
    reason = str(raw.get("reason") or "").strip()

    if assessment_status == ASSESSMENT_SCORED:
        if not 2 <= len(evidence) <= 4:
            raise CentralityPolicyError("CENTRALITY_SCORED_EVIDENCE_COUNT_INVALID")
        if not counterfactual:
            raise CentralityPolicyError("CENTRALITY_COUNTERFACTUAL_MISSING")
        if not reason:
            raise CentralityPolicyError("CENTRALITY_REASON_MISSING")
        if any(row["speaker_label"] == UNCERTAIN_SPEAKER_LABEL for row in evidence):
            raise CentralityPolicyError("CENTRALITY_SCORED_EVIDENCE_HAS_UNCERTAIN_SPEAKER")
        labels = {str(row["speaker_label"]) for row in evidence}
        assert isinstance(level, int) and not isinstance(level, bool)
        if level >= 3 and HOST_SPEAKER_LABEL not in labels:
            raise CentralityPolicyError("CENTRALITY_HIGH_LEVEL_LACKS_HOST_EVIDENCE")
        if labels == {OTHER_SPEAKER_LABEL} and level > 1:
            raise CentralityPolicyError("CENTRALITY_OTHER_ONLY_LEVEL_TOO_HIGH")
    elif assessment_status == ASSESSMENT_NEEDS_HUMAN_REVIEW:
        if evidence and not any(
            row["speaker_label"] == UNCERTAIN_SPEAKER_LABEL for row in evidence
        ):
            raise CentralityPolicyError("CENTRALITY_HUMAN_REVIEW_LACKS_UNCERTAIN_EVIDENCE")
    elif evidence or counterfactual or reason:
        raise CentralityPolicyError("CENTRALITY_NOT_RUN_MUST_NOT_CARRY_DECISION_EVIDENCE")

    return {
        "rubric_version": RUBRIC_VERSION,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "status": assessment_status,
        "speaker_status": speaker_status,
        "level": level,
        "host_speech_share_band": band,
        "evidence": evidence,
        "counterfactual": counterfactual[:1000],
        "reason": reason[:1500],
        "gate": projection.to_dict(),
    }


def centrality_rank_component(assessment: Mapping[str, object]) -> tuple[()]:
    """Return no rank component: levels 3 and 4 are eligibility-equivalent."""

    gate = assessment.get("gate")
    if not isinstance(gate, Mapping) or gate.get("disposition") != DISPOSITION_AUTO_ELIGIBLE:
        raise CentralityPolicyError("CENTRALITY_RANK_REQUIRES_AUTO_ELIGIBLE")
    return ()
