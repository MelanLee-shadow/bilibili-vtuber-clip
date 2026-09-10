"""A native timestamp rounding tail is evidence, never permission to clamp."""

import pytest

from src.autoslice.mai_transcription import MaiTranscriptionError, _parse_evidence_response


def payload(overrun):
    return {
        "durationMilliseconds": 1000,
        "combinedPhrases": [{"text": "甲。"}],
        "phrases": [
            {
                "text": "甲。",
                "speaker": 0,
                "offsetMilliseconds": 800,
                "durationMilliseconds": 200 + overrun,
                "words": [
                    {"text": "甲", "offsetMilliseconds": 800, "durationMilliseconds": 100},
                    {"text": "。", "offsetMilliseconds": 1000, "durationMilliseconds": overrun},
                ],
            }
        ],
    }


@pytest.mark.parametrize("overrun", [20, 500])
def test_encoder_tail_is_preserved_with_diagnostic_not_retimed(overrun):
    rows, _, diagnostics = _parse_evidence_response(payload(overrun), duration_ms=1000)
    assert rows[0]["end_ms"] == 1000 + overrun
    assert any(
        d["reason_code"] == "MAI_NATIVE_ENCODER_TAIL_OVERHANG" and d["overhang_ms"] == overrun
        for d in diagnostics
    )
    assert rows[0]["words"][-1]["end_ms"] == 1000 + overrun


def test_larger_overrun_remains_blocked():
    with pytest.raises(MaiTranscriptionError):
        _parse_evidence_response(payload(501), duration_ms=1000)


def test_missing_physical_duration_cannot_grant_new_tolerance():
    with pytest.raises(MaiTranscriptionError):
        _parse_evidence_response(payload(20), duration_ms=None)
