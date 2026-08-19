from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from src.autoslice import speaker_scmc_shadow as scmc


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _embedding(primary: int, secondary: int, amount: float) -> tuple[float, ...]:
    values = [0.0] * 192
    values[primary] = 1.0
    values[secondary] = amount
    return tuple(values)


def _clip(
    *,
    clip_id: str,
    speaker: str,
    session: str,
    duration_ms: int,
    start_ms: int,
    embedding: tuple[float, ...],
    source_media: str | None = None,
) -> scmc.EvidenceClip:
    return scmc.EvidenceClip(
        clip_id=clip_id,
        speaker=speaker,
        session_group_id=session,
        split_role="DEVELOPMENT_BANK_REVIEWED",
        source_media_sha256=_sha(source_media or f"media:{session}"),
        canonical_session_pcm_sha256=_sha(f"session-pcm:{session}"),
        canonical_pcm_sha256=_sha(f"clip-pcm:{clip_id}"),
        embedding_binding_sha256=_sha(f"embedding:{clip_id}"),
        start_sample=start_ms * 16,
        end_sample=(start_ms + duration_ms) * 16,
        duration_ms=duration_ms,
        embedding=embedding,
    )


def _ready_clips() -> list[scmc.EvidenceClip]:
    clips: list[scmc.EvidenceClip] = []
    for session_index in range(3):
        session = f"host-session-{session_index}"
        for stratum, duration_ms, base_start in (
            ("short", 800, 0),
            ("long", 2_000, 10_000),
        ):
            for clip_index in range(3):
                clips.append(
                    _clip(
                        clip_id=f"{session}:{stratum}:h{clip_index}",
                        speaker=scmc.LABEL_HOST,
                        session=session,
                        duration_ms=duration_ms,
                        start_ms=base_start + clip_index * 3_000,
                        embedding=_embedding(0, 2 + session_index, 0.02 * clip_index),
                    )
                )
    for stratum, duration_ms, base_start in (
        ("short", 800, 30_000),
        ("long", 2_000, 50_000),
    ):
        for clip_index in range(12):
            session = f"host-session-{clip_index % 3}"
            clips.append(
                _clip(
                    clip_id=f"{session}:{stratum}:o{clip_index}",
                    speaker=scmc.LABEL_OTHER,
                    session=session,
                    duration_ms=duration_ms,
                    start_ms=base_start + clip_index * 3_000,
                    embedding=_embedding(1, 8 + clip_index, 0.02),
                )
            )
    return clips


def _view(
    *,
    view_id: str,
    duration_ms: int,
    embedding: tuple[float, ...],
    session: str = "target-session",
    source_media: str = "target-media",
    start_ms: int = 0,
) -> scmc.ViewEvidence:
    return scmc.ViewEvidence(
        view_id=view_id,
        session_group_id=session,
        source_media_sha256=_sha(source_media),
        canonical_pcm_sha256=_sha(f"view-pcm:{view_id}"),
        start_sample=start_ms * 16,
        end_sample=(start_ms + duration_ms) * 16,
        duration_ms=duration_ms,
        embedding=embedding,
    )


def test_ready_bank_is_session_stratified_and_deterministic() -> None:
    clips = _ready_clips()
    forward = scmc.build_bank(clips)
    reverse = scmc.build_bank(list(reversed(clips)))

    assert forward.manifest() == reverse.manifest()
    assert forward.strata[scmc.STRATUM_SHORT].status == "READY"
    assert forward.strata[scmc.STRATUM_LONG].status == "READY"
    assert len(forward.strata[scmc.STRATUM_SHORT].host_sessions) == 3
    assert len(forward.strata[scmc.STRATUM_SHORT].other_medoids) == 8
    assert forward.manifest()["policy"]["threshold_state"] == "NULL"
    assert forward.manifest()["policy"]["hard_labels_emitted"] is False


def test_null_threshold_scores_but_never_hard_labels() -> None:
    bank = scmc.build_bank(_ready_clips())
    result = scmc.score_view(
        _view(view_id="host-like", duration_ms=800, embedding=_embedding(0, 5, 0.01)),
        bank,
    )

    assert result["status"] == "SCORED_THRESHOLD_NULL"
    assert result["threshold_state"] == "NULL"
    assert result["host_evidence"] > 0.99
    assert result["decision"] == scmc.LABEL_UNKNOWN
    assert result["promotion_authority"] is False


def test_synthetic_thresholds_cover_host_other_and_unknown_branches() -> None:
    bank = scmc.build_bank(_ready_clips())
    thresholds = scmc.Thresholds(tau_other=0.3, tau_host=0.8, delta_host=0.2)

    host = scmc.score_view(
        _view(view_id="host", duration_ms=800, embedding=_embedding(0, 6, 0.01)),
        bank,
        thresholds=thresholds,
    )
    other = scmc.score_view(
        _view(view_id="other", duration_ms=800, embedding=_embedding(1, 7, 0.01)),
        bank,
        thresholds=thresholds,
    )
    ambiguous = scmc.score_view(
        _view(view_id="ambiguous", duration_ms=800, embedding=_embedding(0, 1, 1.0)),
        bank,
        thresholds=thresholds,
    )

    assert host["decision"] == scmc.LABEL_HOST
    assert other["decision"] == scmc.LABEL_OTHER
    assert ambiguous["decision"] == scmc.LABEL_UNKNOWN
    assert host["required_session_votes"] == 2


def test_insufficient_session_bank_fails_to_unknown() -> None:
    clips = [clip for clip in _ready_clips() if clip.session_group_id != "host-session-2"]
    bank = scmc.build_bank(clips)
    result = scmc.score_view(
        _view(view_id="blocked", duration_ms=800, embedding=_embedding(0, 5, 0.01)),
        bank,
    )

    assert bank.strata[scmc.STRATUM_SHORT].status == "INSUFFICIENT_BANK_SUPPORT"
    assert "INSUFFICIENT_INDEPENDENT_HOST_SESSIONS" in result["blockers"]
    assert result["decision"] == scmc.LABEL_UNKNOWN


def test_pseudo_independent_reencoded_session_is_rejected() -> None:
    clips = _ready_clips()
    original = clips[0]
    clips.append(
        replace(
            original,
            clip_id="renamed-session-copy",
            session_group_id="fake-independent-session",
            source_media_sha256=_sha("renamed-source"),
            canonical_pcm_sha256=_sha("renamed-clip"),
            embedding_binding_sha256=_sha("renamed-embedding"),
        )
    )
    with pytest.raises(scmc.ScmcShadowError, match="pseudo-independent"):
        scmc.build_bank(clips)


def test_duplicate_or_reencoded_clip_audio_is_rejected() -> None:
    clips = _ready_clips()
    original = clips[0]
    clips.append(
        replace(
            original,
            clip_id="duplicate-audio",
            start_sample=original.end_sample + 16_000,
            end_sample=original.end_sample + 16_000 + original.duration_ms * 16,
            embedding_binding_sha256=_sha("duplicate-audio-embedding"),
        )
    )
    with pytest.raises(scmc.ScmcShadowError, match="duplicated or re-encoded"):
        scmc.build_bank(clips)


def test_holdout_material_cannot_be_constructed_as_bank_evidence() -> None:
    with pytest.raises(scmc.ScmcShadowError, match="holdout or unresolved"):
        replace(_ready_clips()[0], split_role="HOLDOUT_A_LOCKED_UNLABELED")


def test_same_session_requires_blocked_crossfit_and_rejects_overlap() -> None:
    clips = _ready_clips()
    bank = scmc.build_bank(clips)
    reference = next(
        clip
        for clip in clips
        if clip.session_group_id == "host-session-0" and clip.stratum == scmc.STRATUM_SHORT
    )
    far_view = _view(
        view_id="same-session-far",
        duration_ms=800,
        embedding=_embedding(0, 5, 0.01),
        session=reference.session_group_id,
        source_media="media:host-session-0",
        start_ms=100_000,
    )
    with pytest.raises(scmc.ScmcShadowError, match="blocked cross-fitting"):
        scmc.score_view(far_view, bank)
    assert (
        scmc.score_view(far_view, bank, allow_same_session_crossfit=True)["decision"]
        == scmc.LABEL_UNKNOWN
    )

    overlapping = scmc.ViewEvidence(
        view_id="one-sample-overlap",
        session_group_id=reference.session_group_id,
        source_media_sha256=reference.source_media_sha256,
        canonical_pcm_sha256=_sha("overlap-view"),
        start_sample=reference.end_sample - 1,
        end_sample=reference.end_sample - 1 + 800 * 16,
        duration_ms=800,
        embedding=_embedding(0, 5, 0.01),
    )
    with pytest.raises(scmc.ScmcShadowError, match="intersects the bank"):
        scmc.score_view(overlapping, bank, allow_same_session_crossfit=True)


def test_under_300ms_mixed_and_long_conflict_all_abstain() -> None:
    bank = scmc.build_bank(_ready_clips())
    short = scmc.score_view(
        _view(view_id="tiny", duration_ms=299, embedding=_embedding(0, 4, 0.01)),
        bank,
    )
    assert short["decision"] == scmc.LABEL_UNKNOWN
    assert short["status"] == "CUE_TOO_SHORT"

    mixed = scmc.combine_cue_decisions(
        duration_ms=2_000,
        mixed=True,
        whole_view={"decision": scmc.LABEL_HOST},
        speech_cells=[{"decision": scmc.LABEL_HOST}],
    )
    conflict = scmc.combine_cue_decisions(
        duration_ms=2_000,
        mixed=False,
        whole_view={"decision": scmc.LABEL_HOST},
        speech_cells=[
            {"decision": scmc.LABEL_HOST},
            {"decision": scmc.LABEL_OTHER},
        ],
    )
    assert mixed == {
        "decision": scmc.LABEL_UNKNOWN,
        "reason": "MIXED_CUE",
        "promotion_authority": False,
    }
    assert conflict["decision"] == scmc.LABEL_UNKNOWN
    assert conflict["reason"] == "WHOLE_OR_CELL_CONFLICT"


def test_tiny_views_still_fail_if_their_audio_leaks_from_the_bank() -> None:
    clips = _ready_clips()
    bank = scmc.build_bank(clips)
    reference = clips[0]
    leaked = scmc.ViewEvidence(
        view_id="tiny-leak",
        session_group_id="different-name",
        source_media_sha256=_sha("different-container"),
        canonical_pcm_sha256=reference.canonical_pcm_sha256,
        start_sample=0,
        end_sample=299 * 16,
        duration_ms=299,
        embedding=_embedding(0, 4, 0.01),
    )
    with pytest.raises(scmc.ScmcShadowError, match="present in the speaker bank"):
        scmc.score_view(leaked, bank)


def test_long_cue_requires_majority_cells_and_zero_opposite_votes() -> None:
    host = scmc.combine_cue_decisions(
        duration_ms=2_000,
        mixed=False,
        whole_view={"decision": scmc.LABEL_HOST},
        speech_cells=[
            {"decision": scmc.LABEL_HOST},
            {"decision": scmc.LABEL_HOST},
            {"decision": scmc.LABEL_UNKNOWN},
        ],
    )
    no_cells = scmc.combine_cue_decisions(
        duration_ms=2_000,
        mixed=False,
        whole_view={"decision": scmc.LABEL_HOST},
        speech_cells=[{"decision": scmc.LABEL_UNKNOWN}],
    )
    assert host["decision"] == scmc.LABEL_HOST
    assert no_cells["decision"] == scmc.LABEL_UNKNOWN
    assert no_cells["reason"] == "NO_VALID_SPEECH_CELL"


def test_invalid_persisted_view_decision_is_not_treated_as_a_label() -> None:
    with pytest.raises(scmc.ScmcShadowError, match="whole-view decision"):
        scmc.combine_cue_decisions(
            duration_ms=800,
            mixed=False,
            whole_view={"decision": "HOSTISH"},
            speech_cells=[],
        )
    with pytest.raises(scmc.ScmcShadowError, match="speech-cell decision"):
        scmc.combine_cue_decisions(
            duration_ms=2_000,
            mixed=False,
            whole_view={"decision": scmc.LABEL_HOST},
            speech_cells=[{"decision": "HOSTISH"}],
        )


@pytest.mark.parametrize(
    "thresholds",
    [
        (0.5, 0.5, 0.1),
        (0.8, 0.5, 0.1),
        (0.2, 0.8, 0.0),
        (0.2, 0.8, float("nan")),
    ],
)
def test_invalid_thresholds_fail_closed(thresholds: tuple[float, float, float]) -> None:
    with pytest.raises(scmc.ScmcShadowError):
        scmc.Thresholds(*thresholds)
