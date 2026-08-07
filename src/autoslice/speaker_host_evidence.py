"""Host-evidence policy for the talk speaker binary (Ivan 2026-08-07, x2).

Default label is GUEST (连线).  Labelling a cue as HOST (李豆沙) requires hard
evidence: a confident CAM++ voiceprint margin, or (new) a loudness margin
against the session's host-anchor baseline.  Semantics (the whole-clip LLM
context judge) is a corroborating signal, not an independent one: it may only
tip an acoustically *borderline* cue to HOST, and it may always confirm GUEST.
When acoustic evidence is absent, unavailable, or clearly guest-ward,
semantics alone must never assign HOST (Ivan: 「我没说语义应该完全出局，语义
当然也是一个线索，但是不能作为独立线索而已」；cue43 in the 真善美 clip proved
naive semantic heuristics wrong in both directions).

Calibration source: auto_203735_555_680, 61-cue Ivan-adjudicated truth diff,
2026-08-07 (see docs/pipeline/40-subtitle-text.md and the finalization test
fixtures for the study this module was calibrated against).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER

# Among acoustically-ambiguous (within-band) cues in the calibration clip,
# whole-clip-context HOST votes were correct in only 4/14 cases; all 10 wrong
# votes sat >=0.10 below the CAM++ threshold, while the 2 correct ones sat
# within 0.07 of it.  The corroboration floor below is set at 0.08 below
# threshold: inside the observed gap, so it keeps the 2 correct corroborated
# flips while excluding every observed wrong one.
DEFAULT_HOST_SEMANTIC_CORROBORATION_MARGIN_BELOW_THRESHOLD = 0.08
DEFAULT_HOST_SEMANTIC_MIN_CONFIDENCE = 0.7

# Loudness lane: a same-session mean-volume(dB) study on the calibration clip
# (ffmpeg volumedetect per cue window) found no reliable host/guest
# separation (host median -22.95dB vs guest -22.0dB; full range overlap
# -40.8..-14.9dB on both sides; guest median was in fact louder).  Ivan's
# "closer mic -> louder" heuristic did not hold empirically for this mixed,
# already-normalized stream.  The margin below is deliberately calibrated
# above the observed guest ceiling relative to the host-anchor baseline so
# the lane stays conservative (zero loudness-sourced false-host on the
# calibration fixture) rather than silently degrading the false-host rate.
# It is wired in as declared infrastructure for a future revisit with
# isolated per-speaker audio (e.g. per-track stems), not a currently
# load-bearing signal.
DEFAULT_HOST_LOUDNESS_REQUIRED_MARGIN_DB = 9.0


@dataclass(frozen=True)
class HostEvidenceDecision:
    speaker: str
    decision_source: str
    semantic_eligible: bool


def acoustic_hard_pass(margin: float, threshold: float, band: float) -> str | None:
    """Confident (non-ambiguous) acoustic margin -> speaker, else None."""

    distance = margin - threshold
    if distance >= band:
        return HOST_SPEAKER
    if distance <= -band:
        return GUEST_SPEAKER
    return None


def semantic_corroboration_eligible(
    margin: float, threshold: float, *, corroboration_margin_below_threshold: float
) -> bool:
    """True only inside the narrow band where semantics may corroborate HOST."""

    return margin - threshold >= -corroboration_margin_below_threshold


def loudness_hard_pass(
    cue_loudness_db: float | None,
    host_anchor_baseline_db: float | None,
    *,
    required_margin_db: float,
) -> bool:
    if cue_loudness_db is None or host_anchor_baseline_db is None:
        return False
    return cue_loudness_db - host_anchor_baseline_db >= required_margin_db


def resolve_ambiguous_cue_speaker(
    *,
    margin: float,
    threshold: float,
    band: float,
    policy: Mapping[str, object],
    context_speaker: str | None,
    context_confidence: float | None,
    cue_loudness_db: float | None = None,
    host_anchor_baseline_db: float | None = None,
) -> HostEvidenceDecision:
    """Resolve one acoustically-ambiguous cue under the asymmetric policy.

    Default is GUEST.  HOST requires a hard acoustic/loudness lane, or a
    semantic vote inside the narrow acoustic borderline band with sufficient
    confidence.  The whole-clip LLM path can never assign HOST on its own
    when acoustic evidence is absent or clearly guest-ward.
    """

    corroboration_floor = float(
        policy.get(
            "host_semantic_corroboration_margin_below_threshold",
            DEFAULT_HOST_SEMANTIC_CORROBORATION_MARGIN_BELOW_THRESHOLD,
        )
    )
    min_confidence = float(
        policy.get("host_semantic_min_confidence", DEFAULT_HOST_SEMANTIC_MIN_CONFIDENCE)
    )
    loudness_margin = float(
        policy.get(
            "host_loudness_required_margin_db", DEFAULT_HOST_LOUDNESS_REQUIRED_MARGIN_DB
        )
    )

    if loudness_hard_pass(
        cue_loudness_db, host_anchor_baseline_db, required_margin_db=loudness_margin
    ):
        return HostEvidenceDecision(HOST_SPEAKER, "loudness_hard", True)

    eligible = semantic_corroboration_eligible(
        margin, threshold, corroboration_margin_below_threshold=corroboration_floor
    )
    if (
        eligible
        and context_speaker == HOST_SPEAKER
        and context_confidence is not None
        and context_confidence >= min_confidence
    ):
        return HostEvidenceDecision(HOST_SPEAKER, "campp_semantic_corroborated", True)
    if context_speaker == GUEST_SPEAKER:
        return HostEvidenceDecision(
            GUEST_SPEAKER, "whole_clip_context_guest_confirmed", eligible
        )
    return HostEvidenceDecision(GUEST_SPEAKER, "guest_default_ambiguity", eligible)


def mixed_cue_speaker(window_speakers: list[str]) -> HostEvidenceDecision:
    """Mixed-cue v1: whole cue -> GUEST unless every window supports HOST.

    Reusable combinator seam for a future per-cue audio-window scorer; this
    slice does not add new sub-cue audio windowing (see docs/pipeline
    40-subtitle-text.md note).  Callers that only have one whole-cue window
    (today's campp_audio path) should treat that single window's confident
    label as the sole entry of ``window_speakers``.
    """

    if window_speakers and all(speaker == HOST_SPEAKER for speaker in window_speakers):
        return HostEvidenceDecision(HOST_SPEAKER, "mixed_window_unanimous_host", False)
    return HostEvidenceDecision(
        GUEST_SPEAKER, "mixed_window_disagreement_guest_default", False
    )
