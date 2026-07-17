from src.autoslice.song_lane import _nested_reason_codes


def test_nested_song_provider_failure_is_preserved_for_retry_classification():
    record = {
        "decision_action": "BLOCK",
        "reason_codes": ["SONG_LYRICS_UNPROVEN"],
        "source_context_job": {
            "song_context_subtitle_fallback": {
                "reason_codes": [
                    "AGY_SOURCE_CONTEXT_RUNNER_FAILED",
                    "AGY_AND_GEMINI_API_FAILED",
                ]
            },
            "song_repair_gate": {
                "status": "BLOCKED",
                "reason_codes": ["SONG_LYRICS_UNPROVEN"],
            },
        },
    }

    assert _nested_reason_codes(record) == [
        "SONG_LYRICS_UNPROVEN",
        "AGY_SOURCE_CONTEXT_RUNNER_FAILED",
        "AGY_AND_GEMINI_API_FAILED",
    ]
