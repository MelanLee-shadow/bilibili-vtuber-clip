"""Effective-duration gate for the producer's resolved delivery interval."""


def enforce_minimum_effective_duration(
    spec: dict,
    *,
    final_start_ms: int,
    final_end_ms: int,
) -> None:
    """Keep the original strict floor, independently of the ending proof."""
    minimum_effective_duration_ms = spec.get("minimum_effective_duration_ms")
    if minimum_effective_duration_ms is None:
        return
    if (
        isinstance(minimum_effective_duration_ms, bool)
        or not isinstance(minimum_effective_duration_ms, int)
        or minimum_effective_duration_ms < 0
    ):
        raise ValueError("minimum_effective_duration_ms must be a non-negative integer")
    if final_end_ms - final_start_ms <= minimum_effective_duration_ms:
        raise SystemExit(
            "TALK_EFFECTIVE_DURATION_NOT_OVER_60S_AFTER_BOUNDARY: "
            f"effective={final_end_ms - final_start_ms}ms "
            f"minimum_exclusive={minimum_effective_duration_ms}ms"
        )
