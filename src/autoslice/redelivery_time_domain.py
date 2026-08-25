"""Explicit time-domain contract for reviewed subtitle replay.

A reviewed SRT can be local either to the whole bound source interval or to the
already-cropped delivery.  Treating one as the other applies the delivery crop
twice and silently deletes/retimes cues.  Operator full-ownership v3 assets must
therefore declare the domain instead of relying on an ambiguous ``start_ms``.
"""

from __future__ import annotations

from collections.abc import Mapping


TIME_DOMAIN_KEY = "time_domain"
PIECE_LOCAL = "PIECE_LOCAL"
DELIVERY_LOCAL = "DELIVERY_LOCAL"
SUPPORTED_TIME_DOMAINS = frozenset({PIECE_LOCAL, DELIVERY_LOCAL})
OPERATOR_TEXT_PIN_V3 = "operator-reviewed-text-full-ownership-pin.v3"


class RedeliveryTimeDomainError(ValueError):
    """A reviewed baseline has no trustworthy source-to-delivery domain."""


def operator_v3_time_domain(config: Mapping[str, object]) -> str | None:
    """Return the mandatory explicit domain for operator-v3 baselines.

    Older baseline schemas retain their historical behavior.  The v3 operator
    lane is release-authoritative and may bypass fresh text generation, so an
    absent or unknown domain must fail closed.
    """

    ownership = config.get("operator_text_full_ownership")
    if not isinstance(ownership, Mapping) or ownership.get("schema_version") != OPERATOR_TEXT_PIN_V3:
        return None
    value = config.get(TIME_DOMAIN_KEY)
    if value not in SUPPORTED_TIME_DOMAINS:
        raise RedeliveryTimeDomainError(
            "REDELIVERY_BASELINE_TIME_DOMAIN_MISSING_OR_INVALID"
        )
    return str(value)


def require_delivery_local_interval(
    config: Mapping[str, object], *, absolute_start_ms: int, absolute_end_ms: int
) -> None:
    """Bind a DELIVERY_LOCAL baseline to the exact media interval once."""

    domain = operator_v3_time_domain(config)
    if domain != DELIVERY_LOCAL:
        return
    if (
        config.get("absolute_source_start_ms") != absolute_start_ms
        or config.get("absolute_source_end_ms") != absolute_end_ms
    ):
        raise RedeliveryTimeDomainError(
            "REDELIVERY_BASELINE_DELIVERY_INTERVAL_MISMATCH"
        )


def replay_diagnostic_duration_ms(
    config: Mapping[str, object],
    *,
    padded_start_ms: int,
    padded_end_ms: int,
    final_start_ms: int,
    final_end_ms: int,
) -> int:
    """Validate a replay plan's domain and return its local SRT duration."""

    domain = operator_v3_time_domain(config)
    if domain == DELIVERY_LOCAL:
        require_delivery_local_interval(
            config,
            absolute_start_ms=padded_start_ms + final_start_ms,
            absolute_end_ms=padded_start_ms + final_end_ms,
        )
        return final_end_ms - final_start_ms
    if domain == PIECE_LOCAL:
        if (
            config.get("absolute_source_start_ms") != padded_start_ms
            or config.get("absolute_source_end_ms") != padded_end_ms
        ):
            raise RedeliveryTimeDomainError(
                "REDELIVERY_BASELINE_PIECE_INTERVAL_MISMATCH"
            )
    return padded_end_ms - padded_start_ms
