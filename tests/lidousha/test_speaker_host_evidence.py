"""Ivan 2026-08-07: talk speaker binary defaults to GUEST; HOST requires hard
evidence (CAM++ margin or loudness) or acoustic-corroborated semantics.

This replays the new decision layer (src/autoslice/speaker_host_evidence.py +
speaker_context.resolve_ambiguous_labels) against Ivan's 61-cue adjudicated
truth diff for auto_203735_555_680, using an extract of that run's real
CAM++ margins/threshold (fixtures/auto_203735_555_680_machine_decisions_
20260807.json) so the replay does not need CAM++/CPA at test time.

Historical whole_clip_context votes did not persist per-vote confidence
(require_confidence was not enabled for the main ambiguous flow at
collection time, see speaker_finalizer.py's 2026-08-07 change); the replay
treats a historical HOST vote as confidence=1.0 for corroboration-eligibility
purposes only.  This is a documented approximation of the offline replay,
not a claim about the live confidence gate, which is unit-tested directly
below.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER
from src.autoslice.speaker_context import resolve_ambiguous_labels
from src.autoslice.speaker_host_evidence import (
    acoustic_hard_pass,
    loudness_hard_pass,
    mixed_cue_speaker,
    resolve_ambiguous_cue_speaker,
    semantic_corroboration_eligible,
)

FIXTURES = Path(__file__).parent / "fixtures"
TRUTH_PATH = FIXTURES / "ivan_truth_diff_20260807.json"
DECISIONS_PATH = FIXTURES / "auto_203735_555_680_machine_decisions_20260807.json"
LOUDNESS_PATH = FIXTURES / "auto_203735_555_680_loudness_study_20260807.json"

POLICY = {
    "host_semantic_corroboration_margin_below_threshold": 0.08,
    "host_semantic_min_confidence": 0.7,
    "host_loudness_required_margin_db": 9.0,
}


def _load_truth() -> dict[int, dict]:
    document = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    return {row["cue"]: row for row in document["cues"]}


def _load_decisions() -> tuple[dict[int, dict], float, float]:
    document = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    by_cue = {row["source_index"]: row for row in document["decisions"]}
    return by_cue, float(document["threshold"]), float(document["ambiguity_band"])


def _replay_cue(*, margin: float, threshold: float, band: float, historical_row: dict) -> tuple[str, str]:
    """Replay one cue through acoustic_hard_pass + the corroboration gate.

    A historical HOST vote is only present at all when the original run's
    decision_source was whole_clip_context (a real context judge call
    happened); anything else means no semantic vote is available to replay.
    """

    hard = acoustic_hard_pass(margin, threshold, band)
    if hard is not None:
        return hard, "campp_audio"
    context_speaker = (
        historical_row["speaker"]
        if historical_row.get("decision_source") == "whole_clip_context"
        else None
    )
    context_confidence = 1.0 if context_speaker is not None else None
    decision = resolve_ambiguous_cue_speaker(
        margin=margin,
        threshold=threshold,
        band=band,
        policy=POLICY,
        context_speaker=context_speaker,
        context_confidence=context_confidence,
    )
    return decision.speaker, decision.decision_source


def test_replay_false_host_is_zero_on_non_mixed_cues_and_reports_false_guest() -> None:
    truth = _load_truth()
    decisions, threshold, band = _load_decisions()

    false_host = []
    false_guest = []
    correct = 0
    non_mixed_total = 0
    for cue, row in decisions.items():
        truth_row = truth[cue]
        if truth_row["mixed"]:
            continue
        margin = row["margin"]
        if margin is None:
            continue
        non_mixed_total += 1
        speaker, _source = _replay_cue(
            margin=margin, threshold=threshold, band=band, historical_row=row
        )
        truth_label = truth_row["truth_segments"][0]["label"]
        if speaker == truth_label:
            correct += 1
        elif speaker == HOST_SPEAKER and truth_label == GUEST_SPEAKER:
            false_host.append(cue)
        elif speaker == GUEST_SPEAKER and truth_label == HOST_SPEAKER:
            false_guest.append(cue)

    # The whole point of the 2026-08-07 ruling: never guess HOST without hard
    # evidence.  Zero false-host is the hard requirement.
    assert false_host == [], f"false-host cues under the new policy: {false_host}"
    # Some false-guest is the accepted direction (Ivan: no evidence needed to
    # call guest).  Cues 46/48/49 were false-host under the old policy (via a
    # wrong semantic vote) but their CAM++ margins are actually well past the
    # hard acoustic threshold, so acoustic_hard_pass alone recovers them
    # correctly here -- only cue 40 remains: its margin sits inside the
    # corroboration band but the only available (historical) semantic vote
    # said GUEST, so the policy correctly does not invent a HOST override on
    # weaker grounds than the vote it actually has.
    assert false_guest == [40], (
        f"unexpected false-guest set: {false_guest} (expected a small, named "
        "acceptable-direction set; investigate if this changes)"
    )
    assert non_mixed_total == 55
    assert correct == non_mixed_total - len(false_guest)


def test_mixed_cues_default_guest_under_v1_rule() -> None:
    """Mixed-cue v1: whole cue -> GUEST unless every window supports HOST.

    No live sub-cue audio windowing is wired in this slice (see docs/
    pipeline/40-subtitle-text.md); with zero real windows, "every window
    supports HOST" is unattainable, so mixed_cue_speaker() is conservative by
    construction.  For auto_203735_555_680 the six truth-mixed cues (10, 30,
    31, 41, 44, 59) are landed via the hash-bound override document instead
    of relying on live auto-detection.
    """

    assert mixed_cue_speaker([]).speaker == GUEST_SPEAKER
    assert mixed_cue_speaker([GUEST_SPEAKER]).speaker == GUEST_SPEAKER
    assert mixed_cue_speaker([HOST_SPEAKER, GUEST_SPEAKER]).speaker == GUEST_SPEAKER
    assert mixed_cue_speaker([HOST_SPEAKER]).speaker == HOST_SPEAKER
    assert mixed_cue_speaker([HOST_SPEAKER, HOST_SPEAKER]).speaker == HOST_SPEAKER

    truth = _load_truth()
    mixed_cues = {cue for cue, row in truth.items() if row["mixed"]}
    assert mixed_cues == {10, 30, 31, 41, 44, 59}
    for cue in mixed_cues:
        # v1 has no sub-cue windows to prove unanimity, so the automatic
        # decision layer's degenerate case is always GUEST for the whole cue.
        assert mixed_cue_speaker([]).speaker == GUEST_SPEAKER


def test_semantic_corroboration_band_matches_the_calibration_gap() -> None:
    """Corroboration floor (0.08 below threshold) sits inside the observed gap:
    the 2 correct historical semantic-HOST votes sat within 0.0675-0.0673 of
    threshold; the 10 wrong ones all sat >=0.1009 below it."""

    threshold = 0.13210677927927927
    assert semantic_corroboration_eligible(
        threshold - 0.0675, threshold, corroboration_margin_below_threshold=0.08
    )
    assert not semantic_corroboration_eligible(
        threshold - 0.1009, threshold, corroboration_margin_below_threshold=0.08
    )


def test_semantics_alone_never_assigns_host_outside_the_corroboration_band() -> None:
    """A high-confidence semantic HOST vote is ignored when acoustics are
    clearly guest-ward -- semantics only ever corroborates, never assigns."""

    decision = resolve_ambiguous_cue_speaker(
        margin=-0.5,
        threshold=0.0,
        band=0.6,
        policy=POLICY,
        context_speaker=HOST_SPEAKER,
        context_confidence=0.99,
    )
    assert decision.speaker == GUEST_SPEAKER
    assert decision.decision_source == "guest_default_ambiguity"


def test_semantics_can_always_confirm_guest() -> None:
    decision = resolve_ambiguous_cue_speaker(
        margin=-0.02,
        threshold=0.0,
        band=0.1,
        policy=POLICY,
        context_speaker=GUEST_SPEAKER,
        context_confidence=0.4,
    )
    assert decision.speaker == GUEST_SPEAKER
    assert decision.decision_source == "whole_clip_context_guest_confirmed"


def test_low_confidence_semantic_host_vote_inside_band_still_defaults_guest() -> None:
    decision = resolve_ambiguous_cue_speaker(
        margin=-0.02,
        threshold=0.0,
        band=0.1,
        policy=POLICY,
        context_speaker=HOST_SPEAKER,
        context_confidence=0.5,
    )
    assert decision.speaker == GUEST_SPEAKER
    assert decision.decision_source == "guest_default_ambiguity"


def test_loudness_gate_does_not_fire_on_the_calibration_fixture() -> None:
    """Ivan's "closer mic -> louder" heuristic did not hold empirically on
    this clip (see fixtures/auto_203735_555_680_loudness_study_20260807.json):
    host median -22.95dB vs guest median -22.0dB, full range overlap. The
    margin is calibrated above the observed guest ceiling so it stays
    conservative (never contributes a loudness false-host) rather than
    silently degrading the false-host rate."""

    study = json.loads(LOUDNESS_PATH.read_text(encoding="utf-8"))
    summary = study["summary"]
    assert summary["host_median_db"] < summary["guest_median_db"] + 1
    host_anchor_baseline_db = -23.35  # mean of campp host-anchor cues 1, 36
    required_margin_db = POLICY["host_loudness_required_margin_db"]
    fired = [
        row["cue"]
        for row in study["rows"]
        if row["mean_volume_db"] is not None
        and loudness_hard_pass(
            row["mean_volume_db"],
            host_anchor_baseline_db,
            required_margin_db=required_margin_db,
        )
    ]
    assert fired == [], f"loudness gate unexpectedly fired on cues: {fired}"


def test_loudness_gate_fires_when_margin_is_cleared() -> None:
    assert loudness_hard_pass(-10.0, -23.35, required_margin_db=9.0)
    assert not loudness_hard_pass(-15.0, -23.35, required_margin_db=9.0)
    assert not loudness_hard_pass(None, -23.35, required_margin_db=9.0)
    assert not loudness_hard_pass(-10.0, None, required_margin_db=9.0)


def test_resolve_ambiguous_labels_integration_matches_module_contract() -> None:
    """speaker_finalizer's call site threads band/policy/confidences through
    resolve_ambiguous_labels into speaker_host_evidence -- exercise it end to
    end at the resolve_ambiguous_labels seam."""

    labels, sources = resolve_ambiguous_labels(
        [GUEST_SPEAKER, None, None],
        [-0.3, -0.02, -0.5],
        0.0,
        {1: HOST_SPEAKER},
        band=0.1,
        policy=POLICY,
        context_confidences={1: 0.9},
    )
    assert labels == [GUEST_SPEAKER, HOST_SPEAKER, GUEST_SPEAKER]
    assert sources == [
        "campp_audio",
        "campp_semantic_corroborated",
        "guest_default_ambiguity",
    ]
