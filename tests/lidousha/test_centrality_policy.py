from __future__ import annotations

import pytest

from src.autoslice import centrality_policy as cp


def _evidence(label: str, cue: int) -> dict[str, object]:
    return {
        "cue_id": cue,
        "start_ms": cue * 1_000,
        "end_ms": cue * 1_000 + 900,
        "speaker_label": label,
        "claim": f"key contribution {cue}",
    }


def _scored(level: int, *, band: str = "mixed") -> dict[str, object]:
    return {
        "status": cp.ASSESSMENT_SCORED,
        "speaker_status": cp.SPEAKER_VERIFIED,
        "level": level,
        "host_speech_share_band": band,
        "evidence": [
            _evidence(cp.HOST_SPEAKER_LABEL, 1),
            _evidence(cp.OTHER_SPEAKER_LABEL, 2),
        ],
        "counterfactual": "Removing the host leaves only the setup.",
        "reason": "The host supplies the decisive reaction.",
    }


@pytest.mark.parametrize(
    ("level", "disposition", "auto", "manual"),
    [
        (0, cp.DISPOSITION_AUTO_INELIGIBLE, False, False),
        (1, cp.DISPOSITION_AUTO_INELIGIBLE, False, False),
        (2, cp.DISPOSITION_MANUAL_SELECTION_ONLY, False, True),
        (3, cp.DISPOSITION_AUTO_ELIGIBLE, True, False),
        (4, cp.DISPOSITION_AUTO_ELIGIBLE, True, False),
    ],
)
def test_approved_level_table(level: int, disposition: str, auto: bool, manual: bool) -> None:
    projected = cp.project_centrality_disposition(
        assessment_status=cp.ASSESSMENT_SCORED,
        speaker_status=cp.SPEAKER_VERIFIED,
        level=level,
    )
    assert projected.disposition == disposition
    assert projected.automatic_eligible is auto
    assert projected.manual_selection_required is manual
    assert projected.human_review_required is False


def test_not_run_is_not_silently_queued_for_human_review() -> None:
    projected = cp.project_centrality_disposition(
        assessment_status=cp.ASSESSMENT_NOT_RUN,
        speaker_status=cp.SPEAKER_NOT_RUN,
        level=None,
    )
    assert projected.disposition == cp.DISPOSITION_NOT_EVALUATED
    assert projected.human_review_required is False
    assert projected.automatic_eligible is False


def test_verified_speaker_may_wait_for_centrality_without_becoming_human_work() -> None:
    projected = cp.project_centrality_disposition(
        assessment_status=cp.ASSESSMENT_NOT_RUN,
        speaker_status=cp.SPEAKER_VERIFIED,
        level=None,
    )
    assert projected.disposition == cp.DISPOSITION_NOT_EVALUATED


def test_assessed_null_routes_to_human_review() -> None:
    projected = cp.project_centrality_disposition(
        assessment_status=cp.ASSESSMENT_NEEDS_HUMAN_REVIEW,
        speaker_status=cp.SPEAKER_NEEDS_HUMAN_REVIEW,
        level=None,
    )
    assert projected.disposition == cp.DISPOSITION_HUMAN_REVIEW_REQUIRED
    assert projected.human_review_required is True


@pytest.mark.parametrize("bad_level", [True, False, -1, 5, 2.0, "3"])
def test_invalid_levels_fail_closed(bad_level: object) -> None:
    with pytest.raises(cp.CentralityPolicyError):
        cp.project_centrality_disposition(
            assessment_status=cp.ASSESSMENT_SCORED,
            speaker_status=cp.SPEAKER_VERIFIED,
            level=bad_level,  # type: ignore[arg-type]
        )


def test_scored_level_requires_verified_speaker() -> None:
    with pytest.raises(
        cp.CentralityPolicyError,
        match="CENTRALITY_SCORE_REQUIRES_VERIFIED_SPEAKER_ATTRIBUTION",
    ):
        cp.project_centrality_disposition(
            assessment_status=cp.ASSESSMENT_SCORED,
            speaker_status=cp.SPEAKER_NEEDS_HUMAN_REVIEW,
            level=3,
        )


def test_unresolved_speaker_cannot_hide_as_not_run() -> None:
    with pytest.raises(
        cp.CentralityPolicyError,
        match="CENTRALITY_UNRESOLVED_SPEAKER_MUST_ENTER_HUMAN_REVIEW",
    ):
        cp.project_centrality_disposition(
            assessment_status=cp.ASSESSMENT_NOT_RUN,
            speaker_status=cp.SPEAKER_NEEDS_HUMAN_REVIEW,
            level=None,
        )


def test_high_level_requires_host_key_evidence() -> None:
    raw = _scored(4)
    raw["evidence"] = [
        _evidence(cp.OTHER_SPEAKER_LABEL, 1),
        _evidence(cp.OTHER_SPEAKER_LABEL, 2),
    ]
    with pytest.raises(
        cp.CentralityPolicyError,
        match="CENTRALITY_HIGH_LEVEL_LACKS_HOST_EVIDENCE",
    ):
        cp.normalize_centrality_assessment(raw)


def test_all_other_key_evidence_cannot_score_level_two() -> None:
    raw = _scored(2)
    raw["evidence"] = [
        _evidence(cp.OTHER_SPEAKER_LABEL, 1),
        _evidence(cp.OTHER_SPEAKER_LABEL, 2),
    ]
    with pytest.raises(
        cp.CentralityPolicyError,
        match="CENTRALITY_OTHER_ONLY_LEVEL_TOO_HIGH",
    ):
        cp.normalize_centrality_assessment(raw)


def test_uncertain_key_evidence_cannot_be_scored() -> None:
    raw = _scored(3)
    raw["evidence"] = [
        _evidence(cp.HOST_SPEAKER_LABEL, 1),
        _evidence(cp.UNCERTAIN_SPEAKER_LABEL, 2),
    ]
    with pytest.raises(
        cp.CentralityPolicyError,
        match="CENTRALITY_SCORED_EVIDENCE_HAS_UNCERTAIN_SPEAKER",
    ):
        cp.normalize_centrality_assessment(raw)


def test_human_review_accepts_uncertain_key_evidence() -> None:
    normalized = cp.normalize_centrality_assessment(
        {
            "status": cp.ASSESSMENT_NEEDS_HUMAN_REVIEW,
            "speaker_status": cp.SPEAKER_NEEDS_HUMAN_REVIEW,
            "level": None,
            "host_speech_share_band": "unavailable",
            "evidence": [_evidence(cp.UNCERTAIN_SPEAKER_LABEL, 2)],
            "counterfactual": "",
            "reason": "",
        }
    )
    assert normalized["gate"]["disposition"] == cp.DISPOSITION_HUMAN_REVIEW_REQUIRED


def test_host_share_band_never_selects_centrality_level() -> None:
    low = cp.normalize_centrality_assessment(_scored(4, band="low"))
    dominant = cp.normalize_centrality_assessment(_scored(4, band="dominant"))
    assert low["gate"] == dominant["gate"]


def test_levels_three_and_four_have_identical_rank_component() -> None:
    three = cp.normalize_centrality_assessment(_scored(3))
    four = cp.normalize_centrality_assessment(_scored(4))
    assert cp.centrality_rank_component(three) == ()
    assert cp.centrality_rank_component(four) == ()
    assert cp.centrality_rank_component(three) == cp.centrality_rank_component(four)


def test_not_run_must_not_carry_decision_evidence() -> None:
    with pytest.raises(
        cp.CentralityPolicyError,
        match="CENTRALITY_NOT_RUN_MUST_NOT_CARRY_DECISION_EVIDENCE",
    ):
        cp.normalize_centrality_assessment(
            {
                "status": cp.ASSESSMENT_NOT_RUN,
                "speaker_status": cp.SPEAKER_NOT_RUN,
                "level": None,
                "host_speech_share_band": "unavailable",
                "evidence": [_evidence(cp.HOST_SPEAKER_LABEL, 1)],
                "counterfactual": "",
                "reason": "",
            }
        )
