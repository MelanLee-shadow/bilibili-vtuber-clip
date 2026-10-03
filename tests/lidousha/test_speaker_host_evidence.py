"""Synthetic controls for conservative speaker attribution."""
from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER
from src.autoslice.speaker_context import resolve_ambiguous_labels
from src.autoslice.speaker_host_evidence import loudness_hard_pass, mixed_cue_speaker, resolve_ambiguous_cue_speaker, semantic_corroboration_eligible

POLICY = {"host_semantic_min_confidence": 0.7, "host_loudness_required_margin_db": 9.0}

def test_mixed_synthetic_windows_require_unanimous_host_evidence():
    for labels in ([], [GUEST_SPEAKER], [HOST_SPEAKER, GUEST_SPEAKER]):
        assert mixed_cue_speaker(labels).speaker == GUEST_SPEAKER
    assert mixed_cue_speaker([HOST_SPEAKER, HOST_SPEAKER]).speaker == HOST_SPEAKER


def test_semantic_corroboration_requires_hostward_borderline_acoustics() -> None:
    """F4 supersedes the old below-threshold floor with a margin-state gate."""

    threshold = 0.13210677927927927
    assert semantic_corroboration_eligible(
        threshold + 0.0673, threshold, band=0.1
    )
    assert not semantic_corroboration_eligible(
        threshold - 0.0001, threshold, band=0.1
    )
    assert not semantic_corroboration_eligible(threshold + 0.1, threshold, band=0.1)
    assert not semantic_corroboration_eligible(None, threshold, band=0.1)

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

def test_f4_guestward_borderline_margin_cannot_be_reversed_to_host() -> None:
    """Wave 8 F4: semantics corroborates hostward acoustics; it cannot reverse
    a guestward CAM++ margin even when that margin remains inside the ambiguity
    band.  The numbers are the cue23 forensic counterexample.
    """

    decision = resolve_ambiguous_cue_speaker(
        margin=0.1167,
        threshold=0.15825,
        band=0.1,
        policy=POLICY,
        context_speaker=HOST_SPEAKER,
        context_confidence=0.99,
    )
    assert decision.speaker == GUEST_SPEAKER
    assert decision.decision_source == "guest_default_ambiguity"
    assert decision.semantic_eligible is False

def test_f4_missing_acoustics_cannot_be_replaced_by_semantic_host() -> None:
    decision = resolve_ambiguous_cue_speaker(
        margin=None,
        threshold=0.15825,
        band=0.1,
        policy=POLICY,
        context_speaker=HOST_SPEAKER,
        context_confidence=0.99,
    )
    assert decision.speaker == GUEST_SPEAKER
    assert decision.decision_source == "guest_default_ambiguity"
    assert decision.semantic_eligible is False

def test_f4_hard_acoustic_margin_outranks_semantic_source() -> None:
    decision = resolve_ambiguous_cue_speaker(
        margin=0.391,
        threshold=0.15825,
        band=0.1,
        policy=POLICY,
        context_speaker=GUEST_SPEAKER,
        context_confidence=0.99,
    )
    assert decision.speaker == HOST_SPEAKER
    assert decision.decision_source == "campp_audio"
    assert decision.semantic_eligible is False

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
        [-0.3, 0.02, -0.5],
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
        "campp_audio",
    ]
