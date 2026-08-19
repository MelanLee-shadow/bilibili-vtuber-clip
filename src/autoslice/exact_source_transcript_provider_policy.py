"""Typed provider-route receipt for exact-source transcript observations."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.autoslice import gemini_backup_policy


ROUTE_SCHEMA = "exact-source-transcript-provider-route.v1"
EXACT_TRANSCRIPT_PURPOSE = (
    gemini_backup_policy.CANDIDATE_BLIND_EXACT_TRANSCRIPT_PURPOSE
)
_ROUTE_KEYS = {
    "schema_version", "provider", "model", "purpose",
    "accepted_key_tier", "accepted_key_ordinal", "configured_key_count",
    "paid_backup_policy", "provider_failures", "mutation_authorized",
    "receipt_sha256",
}
_FAILURE_KEYS = {
    "provider", "category", "attempted", "error_type", "http_status",
    "key_tier", "key_ordinal", "attempt_round", "model", "circuit_breaker",
}
_NON_ATTEMPT_CATEGORIES = {
    "AGY_BINARY_ABSENT",
    "AGY_DISABLED_BY_ENV",
    "AGY_STALE_OUTPUT_CLEANUP_FAILED",
    "GEMINI_API_AUDIO_EXTRACTION_FAILED",
}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value.removeprefix("sha256:")) == 64
        and all(char in "0123456789abcdef" for char in value.removeprefix("sha256:"))
    )


def _valid_failure_value(key: str, value: object) -> bool:
    if key in {"provider", "category", "error_type", "key_tier", "model", "circuit_breaker"}:
        return isinstance(value, str) and bool(value) and len(value) <= 256
    if key == "attempted":
        return isinstance(value, bool)
    if key in {"http_status", "key_ordinal", "attempt_round"}:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    return False


def normalize_provider_history(
    rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Canonicalize only bounded routing metadata; never provider text."""

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_HISTORY_INVALID")
        provider = raw.get("provider")
        category = str(raw.get("category") or "")
        if provider not in {"agy", "gemini_api"} or not category:
            raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_HISTORY_INVALID")
        skipped = (
            raw.get("attempted") is False
            or category in _NON_ATTEMPT_CATEGORIES
            or category.startswith("PAID_BACKUP_SKIPPED:")
            or raw.get("circuit_breaker") == "OPEN"
        )
        row = {key: raw[key] for key in _FAILURE_KEYS if key in raw}
        row["provider"] = provider
        row["category"] = category
        row["attempted"] = not skipped
        if not all(_valid_failure_value(key, value) for key, value in row.items()):
            raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_HISTORY_INVALID")
        normalized.append(row)
    return normalized


def _history_order_valid(rows: list[Mapping[str, Any]]) -> bool:
    seen_gemini = False
    for row in rows:
        if row.get("provider") == "gemini_api":
            seen_gemini = True
        elif seen_gemini:
            return False
    return True


def build_provider_route(
    *,
    provider: str,
    model: str,
    audio_clip_sha256: str,
    accepted_key_tier: str | None = None,
    accepted_key_ordinal: int | None = None,
    configured_key_count: int = 0,
    paid_backup_policy: Mapping[str, Any] | None = None,
    provider_failures: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Seal the exact lane's AGY-first/free-chain/paid-gate provenance."""

    if provider_failures is not None and not isinstance(provider_failures, list):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
    history = normalize_provider_history(list(provider_failures or []))
    if not model or not _valid_sha256(audio_clip_sha256) or not _history_order_valid(history):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
    if provider == "agy":
        if (
            accepted_key_tier is not None
            or accepted_key_ordinal is not None
            or not isinstance(configured_key_count, int)
            or isinstance(configured_key_count, bool)
            or configured_key_count != 0
            or paid_backup_policy is not None
            or history
        ):
            raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
    elif provider == "gemini_api":
        if not history or not any(row["provider"] == "agy" for row in history):
            raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
        if accepted_key_tier not in {
            gemini_backup_policy.FREE_KEY_TIER,
            gemini_backup_policy.PAID_KEY_TIER,
        }:
            raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
        error = gemini_backup_policy.validate_key_acceptance_metadata(
            configured_key_count=configured_key_count,
            accepted_key_ordinal=accepted_key_ordinal,
            accepted_key_tier=accepted_key_tier,
            paid_backup_policy=paid_backup_policy,
        )
        if error is not None:
            raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
        if accepted_key_tier == gemini_backup_policy.PAID_KEY_TIER:
            if not gemini_backup_policy.valid_paid_policy_stamp(
                paid_backup_policy,
                expected_purpose=EXACT_TRANSCRIPT_PURPOSE,
                expected_item_key=audio_clip_sha256.removeprefix("sha256:"),
            ):
                raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
    else:
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
    route: dict[str, Any] = {
        "schema_version": ROUTE_SCHEMA,
        "provider": provider,
        "model": model,
        "purpose": EXACT_TRANSCRIPT_PURPOSE,
        "accepted_key_tier": accepted_key_tier,
        "accepted_key_ordinal": accepted_key_ordinal,
        "configured_key_count": configured_key_count,
        "paid_backup_policy": (
            dict(paid_backup_policy) if isinstance(paid_backup_policy, Mapping) else None
        ),
        "provider_failures": history,
        "mutation_authorized": False,
    }
    route["receipt_sha256"] = _canonical_sha256(route)
    return route


def valid_provider_route(
    route: Mapping[str, Any], *, provider: str, model: str, audio_clip_sha256: str
) -> bool:
    if set(route) != _ROUTE_KEYS or route.get("receipt_sha256") != _canonical_sha256(
        {key: value for key, value in route.items() if key != "receipt_sha256"}
    ):
        return False
    try:
        rebuilt = build_provider_route(
            provider=str(route.get("provider") or ""),
            model=str(route.get("model") or ""),
            audio_clip_sha256=audio_clip_sha256,
            accepted_key_tier=route.get("accepted_key_tier"),
            accepted_key_ordinal=route.get("accepted_key_ordinal"),
            configured_key_count=route.get("configured_key_count"),
            paid_backup_policy=route.get("paid_backup_policy"),
            provider_failures=route.get("provider_failures"),
        )
    except (TypeError, ValueError):
        return False
    return rebuilt == dict(route) and provider == route.get("provider") and model == route.get("model")


def rebind_cached_gemini_route(
    route: Mapping[str, Any], *, audio_clip_sha256: str,
    current_provider_failures: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind a cached Gemini result to this run's fresh AGY-first outcome."""

    if not valid_provider_route(
        route,
        provider="gemini_api",
        model=str(route.get("model") or ""),
        audio_clip_sha256=audio_clip_sha256,
    ):
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
    cached_gemini_failures = [
        row for row in route["provider_failures"] if row.get("provider") == "gemini_api"
    ]
    return build_provider_route(
        provider="gemini_api",
        model=str(route["model"]),
        audio_clip_sha256=audio_clip_sha256,
        accepted_key_tier=route.get("accepted_key_tier"),
        accepted_key_ordinal=route.get("accepted_key_ordinal"),
        configured_key_count=route.get("configured_key_count"),
        paid_backup_policy=route.get("paid_backup_policy"),
        provider_failures=list(current_provider_failures) + cached_gemini_failures,
    )
