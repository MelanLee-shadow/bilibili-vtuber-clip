from src.autoslice import song_lane
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


def test_early_song_infra_failure_is_typed_and_backed_off(monkeypatch):
    monkeypatch.setattr(song_lane.time, "time", lambda: 10_000)

    result = song_lane._early_song_infra_failure(
        {"transient_retry_count": 2},
        reason_code="SONG_WINDOW_CUT_FAILED",
        error="window cut failed: source not ready",
    )

    assert result["status"] == "failed"
    assert result["reason_codes"] == ["SONG_WINDOW_CUT_FAILED"]
    assert result["transient_failure_code"] == "SONG_WINDOW_CUT_FAILED"
    assert result["retry_after_seconds"] == 60 * 60
    assert result["next_retry_at_epoch"] == 13_600
