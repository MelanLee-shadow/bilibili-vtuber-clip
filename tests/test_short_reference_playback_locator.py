from __future__ import annotations

import hashlib
import math
import struct
import wave
from pathlib import Path

import pytest

from src.autoslice.short_reference_playback_locator import (
    CONTEXT_ALGORITHM_VERSION,
    LEGACY_ALGORITHM_VERSION,
    ShortReferencePlaybackLocatorError,
    _locate_short_reference,
    locate_short_reference,
    validate_locator,
)
from src.autoslice.nested_media_acoustic_observer import bound_observer_prompt


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pcm(path: Path, samples: list[float], sample_rate: int = 8000) -> str:
    encoded = b"".join(
        struct.pack("<h", max(-32768, min(32767, round(value * 32767))))
        for value in samples
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(encoded)
    return _sha(path)


def _fixture(tmp_path: Path, *, repeat: bool = False) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    reference_samples = [
        0.65 * math.sin(index * 0.37) + 0.2 * math.sin(index * 0.11)
        for index in range(320)
    ]
    target_samples = [0.0] * 8000
    offsets = [2400, 5200] if repeat else [2800]
    for offset in offsets:
        target_samples[offset : offset + len(reference_samples)] = reference_samples
    source_media = tmp_path / "official-source.mp4"
    source_media.write_bytes(b"immutable official source bytes")
    reference_audio = tmp_path / "official-reference.wav"
    target_audio = tmp_path / "current-target.wav"
    reference_sha = _write_pcm(reference_audio, reference_samples)
    target_sha = _write_pcm(target_audio, target_samples)
    return {
        "reference_audio": reference_audio,
        "reference_sha": reference_sha,
        "target_audio": target_audio,
        "target_sha": target_sha,
        "source_media": source_media,
        "source_sha": _sha(source_media),
        "source_uri": "https://official.example/gentiana-short.mp4",
        "window": [0, 1000],
        "interval": [200, 900],
        "context": {
            "candidate_id": "cue-33",
            "observation_id": "obs-33",
            "outer_media": {"path": "/outer/current.mp4", "sha256": "sha256:" + "1" * 64},
            "final_srt": {"path": "/outer/current.srt", "sha256": "sha256:" + "2" * 64},
            "caption_frame": {
                "path": "/outer/frame.jpg", "sha256": "sha256:" + "3" * 64,
                "frame_ms": 75390,
            },
            "target_interval_ms": [200, 900],
            "target_cue_indexes": [33],
        },
    }


def _locate(fixture: dict[str, object]) -> dict[str, object]:
    return locate_short_reference(
        reference_audio_path=fixture["reference_audio"],
        reference_audio_sha256=fixture["reference_sha"],
        target_audio_path=fixture["target_audio"],
        target_audio_sha256=fixture["target_sha"],
        target_audio_window_ms=fixture["window"],
        target_interval_ms=fixture["interval"],
        reference_source_path=fixture["source_media"],
        reference_source_sha256=fixture["source_sha"],
        source_uri=fixture["source_uri"],
        binding_context=fixture["context"],
    )


def test_locates_unique_complete_reference_and_revalidates_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    locator = _locate(fixture)

    assert locator["status"] == "LOCATED"
    assert locator["authority"] == "EVIDENCE_ONLY"
    assert locator["time_alignment_performed"] is False
    assert locator["source_role_authority"] is False
    assert locator["matched_target_interval_ms"] == [350, 390]
    assert locator["matched_target_offset_ms"] == [350, 390]
    assert locator["abs_ncc"] >= 0.999
    prompt = bound_observer_prompt(
        observer_prompt="base observer",
        interval_ms=[0, 1000],
        target_interval_ms=fixture["interval"],
        target_cue_indexes=[33],
        target_audio_window_ms=fixture["window"],
        playback_locator=locator,
    )
    assert "PLAYBACK_LOCATOR_HINT_JSON" in prompt
    assert "not a linear mapping" in prompt
    assert validate_locator(
        locator,
        reference_audio_path=fixture["reference_audio"],
        reference_audio_sha256=fixture["reference_sha"],
        target_audio_path=fixture["target_audio"],
        target_audio_sha256=fixture["target_sha"],
        target_audio_window_ms=fixture["window"],
        target_interval_ms=fixture["interval"],
        reference_source_path=fixture["source_media"],
        reference_source_sha256=fixture["source_sha"],
        source_uri=fixture["source_uri"],
        binding_context=fixture["context"],
    ) == locator


def test_complete_reference_crosses_caption_edges_but_overlaps_current_cue(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture['interval'] = [360, 380]
    fixture['context']['target_interval_ms'] = [360, 380]
    locator = _locate(fixture)
    assert locator['matched_target_interval_ms'] == [350, 390]
    assert locator['target_overlap_interval_ms'] == [360, 380]
    assert locator['source_role_authority'] is False
    assert locator['mutation_authorized'] is False


def test_match_only_in_neighbouring_context_does_not_supply_current_cue_hint(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture['interval'] = [500, 700]
    fixture['context']['target_interval_ms'] = [500, 700]
    with pytest.raises(ShortReferencePlaybackLocatorError, match='LOCATOR_MATCH_DOES_NOT_OVERLAP_TARGET'):
        _locate(fixture)


def test_neighbouring_replay_does_not_make_current_cue_match_ambiguous(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, repeat=True)
    fixture['interval'] = [310, 330]
    fixture['context']['target_interval_ms'] = [310, 330]
    locator = _locate(fixture)
    assert locator['matched_target_interval_ms'] == [300, 340]
    assert locator['target_overlap_interval_ms'] == [310, 330]
    assert locator['source_role_authority'] is False


@pytest.mark.parametrize('version', [LEGACY_ALGORITHM_VERSION, CONTEXT_ALGORITHM_VERSION])
def test_historical_locator_is_recomputed_with_original_algorithm(tmp_path: Path, version: str) -> None:
    fixture = _fixture(tmp_path)
    args = dict(reference_audio_path=fixture['reference_audio'], reference_audio_sha256=fixture['reference_sha'],
                target_audio_path=fixture['target_audio'], target_audio_sha256=fixture['target_sha'],
                target_audio_window_ms=fixture['window'], target_interval_ms=fixture['interval'],
                reference_source_path=fixture['source_media'], reference_source_sha256=fixture['source_sha'],
                source_uri=fixture['source_uri'], binding_context=fixture['context'])
    locator = _locate_short_reference(**args, algorithm_version=version)
    assert ('target_overlap_interval_ms' in locator) is (version != LEGACY_ALGORITHM_VERSION)
    assert validate_locator(locator, **args) == locator


def test_rejects_wrong_reference_and_silence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    wrong = tmp_path / "wrong-reference.wav"
    wrong_sha = _write_pcm(wrong, [0.4 * math.sin(index * 0.73) for index in range(320)])
    with pytest.raises(ShortReferencePlaybackLocatorError, match="LOCATOR_WEAK_MATCH"):
        locate_short_reference(
            reference_audio_path=wrong,
            reference_audio_sha256=wrong_sha,
            target_audio_path=fixture["target_audio"],
            target_audio_sha256=fixture["target_sha"],
            target_audio_window_ms=fixture["window"],
            target_interval_ms=fixture["interval"],
            reference_source_path=fixture["source_media"],
            reference_source_sha256=fixture["source_sha"],
            source_uri=fixture["source_uri"],
        )

    silent = tmp_path / "silent-reference.wav"
    silent_sha = _write_pcm(silent, [0.0] * 320)
    with pytest.raises(ShortReferencePlaybackLocatorError, match="LOCATOR_SILENT_AUDIO"):
        locate_short_reference(
            reference_audio_path=silent,
            reference_audio_sha256=silent_sha,
            target_audio_path=fixture["target_audio"],
            target_audio_sha256=fixture["target_sha"],
            target_audio_window_ms=fixture["window"],
            target_interval_ms=fixture["interval"],
            reference_source_path=fixture["source_media"],
            reference_source_sha256=fixture["source_sha"],
            source_uri=fixture["source_uri"],
        )


def test_rejects_repeated_reference_and_pcm_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, repeat=True)
    with pytest.raises(
        ShortReferencePlaybackLocatorError,
        match="LOCATOR_REPEATED_OR_AMBIGUOUS_MATCH",
    ):
        _locate(fixture)

    valid_fixture = _fixture(tmp_path / "valid")
    locator = _locate(valid_fixture)
    valid_fixture["target_audio"].write_bytes(valid_fixture["target_audio"].read_bytes() + b"x")
    with pytest.raises(ShortReferencePlaybackLocatorError, match="TARGET_AUDIO_HASH_MISMATCH"):
        validate_locator(
            locator,
            reference_audio_path=valid_fixture["reference_audio"],
            reference_audio_sha256=valid_fixture["reference_sha"],
            target_audio_path=valid_fixture["target_audio"],
            target_audio_sha256=valid_fixture["target_sha"],
            target_audio_window_ms=valid_fixture["window"],
            target_interval_ms=valid_fixture["interval"],
            reference_source_path=valid_fixture["source_media"],
            reference_source_sha256=valid_fixture["source_sha"],
            source_uri=valid_fixture["source_uri"],
            binding_context=valid_fixture["context"],
        )


@pytest.mark.parametrize("drift", ["media", "srt", "frame", "uri"])
def test_cached_locator_cannot_cross_current_binding_context(tmp_path: Path, drift: str) -> None:
    fixture = _fixture(tmp_path)
    locator = _locate(fixture)
    context = dict(fixture["context"])
    if drift == "media":
        context["outer_media"] = {"path": "/outer/other.mp4", "sha256": "sha256:" + "4" * 64}
    elif drift == "srt":
        context["final_srt"] = {"path": "/outer/other.srt", "sha256": "sha256:" + "5" * 64}
    elif drift == "frame":
        context["caption_frame"] = {
            "path": "/outer/other.jpg", "sha256": "sha256:" + "6" * 64, "frame_ms": 75391,
        }
    else:
        source_uri = "https://official.example/other.mp4"
    with pytest.raises(ShortReferencePlaybackLocatorError, match="LOCATOR_RESULT_DRIFT"):
        validate_locator(
            locator,
            reference_audio_path=fixture["reference_audio"],
            reference_audio_sha256=fixture["reference_sha"],
            target_audio_path=fixture["target_audio"],
            target_audio_sha256=fixture["target_sha"],
            target_audio_window_ms=fixture["window"],
            target_interval_ms=fixture["interval"],
            reference_source_path=fixture["source_media"],
            reference_source_sha256=fixture["source_sha"],
            source_uri=source_uri if drift == "uri" else fixture["source_uri"],
            binding_context=context,
        )
