from __future__ import annotations

import hashlib
import json
import wave
from array import array
from pathlib import Path

import pytest

from scripts import build_reviewed_voiceprint_enrollment as enrollment
from scripts import run_cue_aligned_speaker_shadow as shadow
from src.autoslice import host_occupancy as ho


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (shadow.STRATEGY_MAX, ho.LABEL_HOST),
        (shadow.STRATEGY_MEDIAN, ho.LABEL_OTHER),
        (shadow.STRATEGY_TWO_VOTE, ho.LABEL_UNKNOWN),
        (shadow.STRATEGY_ALL_VOTE, ho.LABEL_UNKNOWN),
    ],
)
def test_single_high_prototype_does_not_survive_consensus(strategy: str, expected: str) -> None:
    scores = {"p1": 0.72, "p2": 0.22, "p3": 0.25}
    assert shadow.classify_prototype_scores(scores, strategy=strategy) == expected


def test_two_high_prototypes_are_not_all_prototype_consensus() -> None:
    scores = {"p1": 0.72, "p2": 0.64, "p3": 0.40}
    assert (
        shadow.classify_prototype_scores(scores, strategy=shadow.STRATEGY_TWO_VOTE) == ho.LABEL_HOST
    )
    assert (
        shadow.classify_prototype_scores(scores, strategy=shadow.STRATEGY_ALL_VOTE)
        == ho.LABEL_UNKNOWN
    )


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "media_ms", "speech_ratio", "reason"),
    [
        (0, 299, 10_000, 1.0, "CUE_TOO_SHORT_FOR_HARD_LABEL"),
        (0, 1_499, 10_000, 1.0, None),
        (0, 4_001, 10_000, 1.0, None),
        (0, 2_000, 10_000, 0.49, "INSUFFICIENT_ENERGY_ACTIVITY"),
        (-1, 2_000, 10_000, 1.0, "CUE_OUTSIDE_MEDIA"),
        (0, 10_001, 10_000, 1.0, "CUE_OUTSIDE_MEDIA"),
        (0, 2_000, 10_000, 0.50, None),
    ],
)
def test_cue_quality_gate_only_moves_toward_abstain(
    start_ms: int,
    end_ms: int,
    media_ms: int,
    speech_ratio: float,
    reason: str | None,
) -> None:
    assert (
        shadow._abstention_reason(
            start_ms=start_ms,
            end_ms=end_ms,
            media_duration_ms=media_ms,
            speech_ratio=speech_ratio,
        )
        == reason
    )


def test_nonfinite_scores_fail_closed() -> None:
    with pytest.raises(shadow.CueAlignedShadowError):
        shadow.classify_prototype_scores({"p1": float("nan")}, strategy=shadow.STRATEGY_MAX)


def test_five_second_long_cue_uses_two_stable_windows() -> None:
    assert shadow.plan_long_consensus_windows(1_000, 6_000) == [
        (1_000, 3_500),
        (3_500, 6_000),
    ]


def test_session_anchor_consensus_is_three_way() -> None:
    assert shadow.classify_session_anchor_scores({"a": 0.72, "b": 0.70, "c": 0.40}) == ho.LABEL_HOST
    assert (
        shadow.classify_session_anchor_scores({"a": 0.30, "b": 0.42, "c": 0.44}) == ho.LABEL_OTHER
    )
    assert (
        shadow.classify_session_anchor_scores({"a": 0.70, "b": 0.50, "c": 0.40}) == ho.LABEL_UNKNOWN
    )


def _write_wav(path: Path, *, duration_ms: int = 800) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array(
        "h",
        (
            5_000 if index % 2 else -5_000
            for index in range(duration_ms * ho.SAMPLE_RATE_HZ // 1_000)
        ),
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(ho.SAMPLE_RATE_HZ)
        handle.writeframes(samples.tobytes())
    return path


def _hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _development_manifest(tmp_path: Path) -> Path:
    root = tmp_path / "bank"
    media = tmp_path / "source.mp4"
    text_final_srt = tmp_path / "source.srt"
    override = tmp_path / "truth.json"
    media.write_bytes(b"bound-media")
    text_final_srt.write_bytes(b"bound-srt")
    override.write_bytes(b"bound-override")
    clips = []
    for index in range(3):
        clip = _write_wav(root / "clips" / f"clip-{index}.wav")
        clips.append(
            {
                "id": f"source-session:cue-{index}",
                "relative_path": str(clip.relative_to(root)),
                "sha256": _hash(clip),
                "duration_ms": 800,
            }
        )
    payload = {
        "schema_version": enrollment.SCHEMA_VERSION,
        "purpose": enrollment.PURPOSE,
        "source_session_id": "source-session",
        "candidate_id": "source-candidate",
        "bindings": {
            "enrollment_root_path": str(root),
            "source_media_path": str(media),
            "source_media_sha256": _hash(media),
            "text_final_srt_path": str(text_final_srt),
            "text_final_srt_sha256": _hash(text_final_srt),
            "reviewed_override_path": str(override),
            "reviewed_override_sha256": _hash(override),
        },
        "selection": {"policy": enrollment.SELECTION_POLICY},
        "clips": clips,
        "summary": {
            "development_shadow_ready": True,
            "production_enrollment_ready": False,
        },
    }
    payload["deterministic_payload_sha256"] = shadow._canonical_sha256(payload)
    manifest = tmp_path / "reviewed-enrollment.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_reviewed_development_enrollment_is_cross_session_and_hash_bound(
    tmp_path: Path,
) -> None:
    prototypes, receipt = shadow.load_reviewed_development_enrollment(
        _development_manifest(tmp_path),
        target_session_id="target-session",
        target_candidate_id="target-candidate",
        target_media_sha256="sha256:" + "0" * 64,
    )
    assert len(prototypes) == 3
    assert receipt["source_session_id"] == "source-session"
    assert receipt["target_session_id"] == "target-session"
    assert receipt["production_profile_unchanged"] is True
    assert receipt["promotion_authority"] is False


def test_reviewed_development_enrollment_rejects_same_session(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        shadow.CueAlignedShadowError,
        match="must come from a different named session",
    ):
        shadow.load_reviewed_development_enrollment(
            _development_manifest(tmp_path),
            target_session_id="source-session",
            target_candidate_id="target-candidate",
            target_media_sha256="sha256:" + "0" * 64,
        )


def test_reviewed_development_enrollment_rejects_drifted_clip(
    tmp_path: Path,
) -> None:
    manifest = _development_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    root = Path(payload["bindings"]["enrollment_root_path"])
    (root / payload["clips"][0]["relative_path"]).write_bytes(b"drifted")
    with pytest.raises(
        shadow.CueAlignedShadowError,
        match="clip is missing or drifted",
    ):
        shadow.load_reviewed_development_enrollment(
            manifest,
            target_session_id="target-session",
            target_candidate_id="target-candidate",
            target_media_sha256="sha256:" + "0" * 64,
        )
