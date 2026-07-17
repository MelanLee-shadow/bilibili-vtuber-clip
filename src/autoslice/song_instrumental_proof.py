"""Deterministic proof for long instrumental bridges and outros."""

from __future__ import annotations

from typing import Mapping, Sequence

from src.autoslice.song_common import (
    HOST_LYRIC_SUBJECT,
    LIVE_PERFORMANCE_READY_MODE,
    MAX_LIVE_ARRANGEMENT_OUTRO_MS,
    MAX_PROVEN_LIVE_INSTRUMENTAL_GAP_MS,
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_host_sung(row: object) -> bool:
    return (
        isinstance(row, Mapping)
        and row.get("heard") is True
        and row.get("lyric_vocal_subject") == HOST_LYRIC_SUBJECT
        and row.get("lidousha_role") == "SINGING_THIS_LYRIC"
        and row.get("same_live_vocal_source_as_lidousha") is True
        and row.get("other_singer_or_harmony_audible") is False
        and row.get("recorded_or_playback_vocal_audible") is False
    )


def prove_long_instrumental_spans(
    *,
    observations: Sequence[object],
    heard_rows: Sequence[tuple[int, Mapping[str, object], int, int]],
    live_arrangement: Mapping[str, object],
    live_performance: object,
    spot_checks: object,
    interline_gap_bounds: Sequence[tuple[int, int, int, int]],
    last_index: int,
    last_end_ms: int,
    transition_ms: int,
) -> dict[str, object] | None:
    """Accept >45s silence-from-lyrics only with closed direct-audio evidence."""

    performance_evidence = (
        live_performance.get("evidence")
        if isinstance(live_performance, Mapping)
        else None
    )
    evidence_times = [
        evidence.get("time_ms")
        for evidence in performance_evidence
        if isinstance(evidence, Mapping) and _is_int(evidence.get("time_ms"))
    ] if isinstance(performance_evidence, Sequence) and not isinstance(
        performance_evidence, (str, bytes, bytearray)
    ) else []

    def evidence_binds_host_singing(time_ms: object) -> bool:
        return _is_int(time_ms) and any(
            _is_host_sung(row) and start_ms <= int(time_ms) < end_ms
            for _index, row, start_ms, end_ms in heard_rows
        )

    long_spans: list[dict[str, object]] = []
    for before_index, after_index, gap_start_ms, gap_end_ms in interline_gap_bounds:
        duration_ms = gap_end_ms - gap_start_ms
        if (
            duration_ms > MAX_PROVEN_LIVE_INSTRUMENTAL_GAP_MS
            or not _is_host_sung(observations[before_index])
            or not _is_host_sung(observations[after_index])
            or not any(
                evidence_binds_host_singing(time_ms) and int(time_ms) < gap_start_ms
                for time_ms in evidence_times
            )
            or not any(
                evidence_binds_host_singing(time_ms) and int(time_ms) >= gap_end_ms
                for time_ms in evidence_times
            )
        ):
            raise ValueError(
                f"live arrangement contains an unexplained {duration_ms}ms interruption"
            )
        long_spans.append(
            {
                "kind": "INTERLINE",
                "start_ms": gap_start_ms,
                "end_ms": gap_end_ms,
                "duration_ms": duration_ms,
                "before_lrc_index": before_index,
                "after_lrc_index": after_index,
            }
        )

    outro_gap_ms = transition_ms - last_end_ms
    if outro_gap_ms > MAX_LIVE_ARRANGEMENT_OUTRO_MS:
        if (
            outro_gap_ms > MAX_PROVEN_LIVE_INSTRUMENTAL_GAP_MS
            or not _is_host_sung(heard_rows[-1][1])
            or live_arrangement.get("observed_live_song_ending") is not True
            or not any(
                evidence_binds_host_singing(time_ms) and int(time_ms) < last_end_ms
                for time_ms in evidence_times
            )
        ):
            raise ValueError(
                "live arrangement post-song transition is outside the actual ending boundary"
            )
        long_spans.append(
            {
                "kind": "OUTRO",
                "start_ms": last_end_ms,
                "end_ms": transition_ms,
                "duration_ms": outro_gap_ms,
                "before_lrc_index": last_index,
                "after_lrc_index": None,
            }
        )
    if not long_spans:
        return None

    instrumental_spots = [
        spot
        for spot in spot_checks
        if isinstance(spot, Mapping) and spot.get("name") == "longest_instrumental_gap"
    ] if isinstance(spot_checks, Sequence) and not isinstance(
        spot_checks, (str, bytes, bytearray)
    ) else []
    instrumental_spot = instrumental_spots[0] if len(instrumental_spots) == 1 else None
    spot_ms = (
        instrumental_spot.get("live_time_ms")
        if isinstance(instrumental_spot, Mapping)
        else None
    )
    longest_duration_ms = max(int(span["duration_ms"]) for span in long_spans)
    co_longest_spans = [
        span
        for span in long_spans
        if (
            longest_duration_ms - int(span["duration_ms"]) <= 5_000
            and int(span["duration_ms"]) >= longest_duration_ms * 0.90
        )
    ]
    bound_span = next(
        (
            span
            for span in co_longest_spans
            if _is_int(spot_ms)
            and int(span["start_ms"]) <= int(spot_ms) < int(span["end_ms"])
        ),
        None,
    )
    performance_ready = (
        isinstance(live_performance, Mapping)
        and live_performance.get("mode") == LIVE_PERFORMANCE_READY_MODE
        and live_performance.get("continuous_live_song_performance") is True
        and live_performance.get("same_lidousha_live_performer_across_all_lyrics") is True
        and live_performance.get("other_singer_or_harmony_present") is False
        and live_performance.get("recorded_or_playback_vocal_present") is False
    )
    if (
        not performance_ready
        or not isinstance(instrumental_spot, Mapping)
        or instrumental_spot.get("result") != "OK"
        or not isinstance(instrumental_spot.get("notes"), str)
        or not str(instrumental_spot.get("notes")).strip()
        or bound_span is None
    ):
        largest_gap_ms = max(
            (int(span["duration_ms"]) for span in long_spans),
            default=0,
        )
        raise ValueError(
            f"live arrangement contains an unexplained {largest_gap_ms}ms interruption"
        )
    return {
        "spot_check_ms": int(spot_ms),
        "bound_span_kind": bound_span["kind"],
        "long_spans": long_spans,
    }
