import hashlib

import pytest

from src.autoslice.local_asr_target_evidence import exact_target_evidence


def metadata(**updates):
    value = {
        "provider": "mai",
        "model": "MAI-Transcribe-2",
        "input_audio_sha256": hashlib.sha256(b"audio").hexdigest(),
        "response_sha256": "b" * 64,
        "native_segments": [{"start_ms": 100, "end_ms": 800, "text": "保留原话", "speaker": "S00"}],
    }
    value.update(updates)
    return value


def project(value):
    return exact_target_evidence(
        value, audio=b"audio", source_sha256="a" * 64, start_ms=1000, end_ms=2000
    )


def test_transcript_never_masquerades_as_independent_pinyin():
    result = project(metadata())
    assert result["transcript"] == "保留原话"
    assert result["mutation_authorized"] is False
    assert result["candidate_exposure"] == "none"
    assert not {"confidence", "heard_pinyin", "syllable_count", "witness_protocol"} & result.keys()
    assert result["source_segments"][0]["source_start_ms"] == 1100
    assert result["native_segments"][0]["start_ms"] == 100


@pytest.mark.parametrize(
    "updates",
    [
        {"model": "other-model"},
        {"provider": "auto"},
        {"response_sha256": "bad"},
        {"input_audio_sha256": "a" * 64},
        {"native_segments": None},
        {"native_segments": [{"start_ms": True, "end_ms": 400, "text": "字"}]},
        {"native_segments": [{"start_ms": 0, "end_ms": 1501, "text": "越界"}]},
    ],
)
def test_wrong_binding_or_geometry_is_rejected(updates):
    with pytest.raises(ValueError):
        project(metadata(**updates))


def test_no_speech_does_not_authorize_drop():
    result = project(metadata(native_segments=[]))
    assert result["status"] == "NO_SPEECH_REPORTED"
    assert result["mutation_authorized"] is False
    assert "target_audible" not in result


def test_overlap_and_order_are_preserved_not_sorted_or_retimed():
    rows = [
        {"start_ms": 300, "end_ms": 900, "text": "甲"},
        {"start_ms": 100, "end_ms": 700, "text": "乙"},
    ]
    result = project(metadata(native_segments=rows))
    assert result["native_segments"] == rows
    assert result["transcript"] == "甲 乙"
    assert result["mutation_authorized"] is False
