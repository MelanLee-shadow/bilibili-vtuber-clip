"""One deterministic translation satisfying every observed lyric start."""

GLOBAL_SHIFT_TOLERANCE_MS = 1_500


def solve_global_shift(residuals):
    if not residuals or any(type(value) is not int for value in residuals):
        raise ValueError("audio global-shift residuals must be nonempty integer milliseconds")
    ordered = sorted(residuals)
    median = ordered[len(ordered) // 2]
    lower = ordered[-1] - GLOBAL_SHIFT_TOLERANCE_MS
    upper = ordered[0] + GLOBAL_SHIFT_TOLERANCE_MS
    if lower > upper:
        raise ValueError("audio observations do not fit one global shift within ±1500ms")
    if lower <= median <= upper:
        offset, strategy = median, "upper_median"
    else:
        offset, strategy = (lower + upper) // 2, "feasible_interval_midpoint"
    return {
        "schema_version": "song-global-shift-proof.v1",
        "strategy": strategy,
        "tolerance_ms": GLOBAL_SHIFT_TOLERANCE_MS,
        "observation_count": len(ordered),
        "minimum_residual_ms": ordered[0],
        "maximum_residual_ms": ordered[-1],
        "median_offset_ms": median,
        "feasible_minimum_ms": lower,
        "feasible_maximum_ms": upper,
        "offset_ms": offset,
        "maximum_error_ms": max(abs(value - offset) for value in ordered),
    }
