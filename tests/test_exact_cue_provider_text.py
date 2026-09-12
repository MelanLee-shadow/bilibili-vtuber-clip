"""Full-crop text can survive bad diarization without inventing a timeline."""

import hashlib

import pytest

from src.autoslice import diarized_transcription as client
from src.autoslice import native_foreign_witness as native
from src.autoslice import subtitle_audio_evidence as secondary
from src.autoslice.local_asr_target_evidence import exact_target_evidence


def metadata(audio=b"audio"):
    return {
        "provider": "moss", "model": "moss-transcribe-diarize-pro",
        "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
        "response_sha256": "c" * 64,
        "raw_response": {"text": "完整原话", "segments": [
            {"start": 0.5, "end": 0.5, "text": "坏时间"},
        ]},
    }


def test_invalid_timing_text_is_exact_cue_only_and_reuses_cache(tmp_path, monkeypatch):
    calls = []

    def fail(audio, **kwargs):
        calls.append(kwargs)
        raise client.DiarizedTranscriptionError("MOSS_SEGMENT_INVALID", "bad range", metadata(audio))

    monkeypatch.setattr(client, "transcribe_evidence", fail)
    args = dict(media_path=tmp_path / "input.mp3", provider="moss", duration_ms=1000)
    with pytest.raises(client.DiarizedTranscriptionError):
        secondary.observe_secondary(b"audio", **args)
    evidence = secondary.observe_secondary(b"audio", **args, exact_cue=True)
    assert evidence["native_segments"] == []
    assert evidence["raw_response"] == metadata()["raw_response"]
    assert evidence["diagnostics"][0]["reason_code"] == "MOSS_SEGMENT_INVALID"
    assert evidence["one_track_srt_eligible"] is False
    warm = secondary.observe_secondary(b"audio", **args, exact_cue=True)
    assert warm["served_from_cache"] is True and len(calls) == 2
    # A full-clip consumer must not read the cue-only cache as a timed transcript.
    with pytest.raises(client.DiarizedTranscriptionError):
        secondary.observe_secondary(b"audio", **args)
    assert len(calls) == 3
    result = exact_target_evidence(evidence, audio=b"audio", source_sha256="a" * 64,
                                  start_ms=1000, end_ms=2000, prefer_provider_text=True)
    assert result["transcript"] == "完整原话"
    assert result["source_segments"] == []
    assert result["transcript_timeline_available"] is False
    assert result["mutation_authorized"] is False


@pytest.mark.parametrize("change", ["http", "empty", "binding"])
def test_full_text_does_not_swallow_transport_empty_or_wrong_source(tmp_path, monkeypatch, change):
    data = metadata()
    reason = "MOSS_SEGMENT_INVALID"
    if change == "http":
        reason = "MOSS_HTTP_ERROR"
    elif change == "empty":
        data["raw_response"]["text"] = ""
    else:
        data["input_audio_sha256"] = "e" * 64

    def fail(*_, **__):
        raise client.DiarizedTranscriptionError(reason, "fixture", data)

    monkeypatch.setattr(client, "transcribe_evidence", fail)
    with pytest.raises((client.DiarizedTranscriptionError, ValueError)):
        secondary.observe_secondary(b"audio", media_path=tmp_path / "input.mp3",
                                    provider="moss", duration_ms=1000, exact_cue=True)


def test_overlap_uses_provider_full_text_without_sorting_or_joining_rows(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    monkeypatch.setattr(native, "_extract_exact_mp3",
                        lambda source, output, start_ms, end_ms: output.write_bytes(b"audio"))
    data = metadata()
    rows = [{"start_ms": 100, "end_ms": 700, "text": "不可"},
            {"start_ms": 600, "end_ms": 900, "text": "拼接"}]
    data.update(native_segments=rows, native_timeline={"has_overlap": True})
    monkeypatch.setattr(native, "observe_secondary", lambda *_, **__: data)
    observe = native.build_native_foreign_witness(source_media=source,
                                                  output_dir=tmp_path / "out", provider="moss")
    result = observe(start_ms=1000, end_ms=2000)
    assert result["transcript"] == "完整原话"
    assert result["native_segments"] == rows
    assert result["transcript_basis"] == "provider_full_crop_text"
    assert result["native_timeline"]["has_overlap"] is True
    assert not result["speaker_identity_observed"]
    assert not result["language_observation_available"]
