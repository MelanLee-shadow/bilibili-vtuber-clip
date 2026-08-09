"""Source-bound terminal projection for exact reviewed redeliveries.

An exact reviewed interval can end a few milliseconds after the last fresh
closure cue while a differently segmented fresh cue for the next topic crosses
that endpoint.  This module binds the reviewed bytes and actual source
provenance before exposing one typed semantic-review option.  It never treats
reviewed subtitle text as a story-closure verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.piece_roles import single_content_piece_index
from src.autoslice.redelivery_source_binding import (
    RedeliverySourceBindingError,
    resolve_v2_redelivery_source_binding,
)


AUTHORITY_CONFIG_KEY = "reviewed_exact_interval_terminal_projection"
PROJECTION_MODE_CONFIG_KEY = "terminal_projection_mode"
PROJECTION_MODE = "source_bound_reviewed_endpoint"
AUTHORITY_SCHEMA_VERSION = (
    "reviewed-exact-interval-terminal-projection.v1"
)
SCOPE_SCHEMA_VERSION = (
    "reviewed-exact-interval-terminal-projection-scope.v1"
)
MATERIALIZATION_SCHEMA_VERSION = (
    "reviewed-exact-interval-terminal-projection-materialization.v1"
)
MAX_TERMINAL_DRIFT_MS = 250
_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


class RedeliveryBoundaryProjectionError(ValueError):
    """The reviewed terminal projection is absent, forged, or stale."""


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _normalized_sha256(value: object, reason: str) -> str:
    match = _SHA256_RX.fullmatch(str(value or "").strip())
    if match is None:
        raise RedeliveryBoundaryProjectionError(reason)
    return "sha256:" + match.group(1)


def _required_int(value: object, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RedeliveryBoundaryProjectionError(reason)
    return value


def _resolved_baseline_path(
    raw: object, *, spec_parent: Path
) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_BASELINE_PATH_INVALID"
        )
    path = Path(raw)
    if not path.is_absolute():
        path = spec_parent / path
    if not path.is_file() or path.is_symlink():
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_BASELINE_PATH_INVALID"
        )
    return path.resolve()


def build_terminal_projection_authority(
    *,
    spec: Mapping[str, object],
    piece_provenance_rows: Sequence[Mapping[str, object]],
    spec_parent: Path,
) -> dict[str, object] | None:
    """Bind exact reviewed bytes to the one actual content-source interval."""

    config = spec.get("subtitle_redelivery_baseline")
    if not isinstance(config, Mapping) or config.get("schema_version") != (
        "subtitle-redelivery-baseline.v2"
    ):
        return None
    if AUTHORITY_CONFIG_KEY in config:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_CALLER_SUPPLIED_AUTHORITY"
        )
    projection_mode = config.get(PROJECTION_MODE_CONFIG_KEY)
    if projection_mode is None:
        return None
    if projection_mode != PROJECTION_MODE:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_MODE_INVALID"
        )
    replay = config.get("exact_interval_replay", False)
    if not isinstance(replay, bool):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_EXACT_INTERVAL_REPLAY_INVALID"
        )
    if not replay:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_REQUIRES_EXACT_INTERVAL_REPLAY"
        )

    pieces = spec.get("pieces")
    try:
        content_index = single_content_piece_index(pieces)
        content_piece = pieces[content_index]
        piece_start_ms = _required_int(
            content_piece.get("start_ms"),
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_INTERVAL_INVALID",
        )
        absolute_start_ms = _required_int(
            config.get("absolute_source_start_ms"),
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_INTERVAL_INVALID",
        )
        absolute_end_ms = _required_int(
            config.get("absolute_source_end_ms"),
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_INTERVAL_INVALID",
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_INTERVAL_INVALID"
        ) from exc
    local_start_ms = absolute_start_ms - piece_start_ms
    local_end_ms = absolute_end_ms - piece_start_ms
    if local_start_ms < 0 or local_end_ms <= local_start_ms:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_INTERVAL_INVALID"
        )
    try:
        source_binding = resolve_v2_redelivery_source_binding(
            spec=spec,
            piece_provenance_rows=piece_provenance_rows,
            final_start=local_start_ms,
            final_end=local_end_ms,
        )
    except RedeliverySourceBindingError as exc:
        raise RedeliveryBoundaryProjectionError(str(exc)) from exc
    if source_binding is None:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_BINDING_MISSING"
        )

    expected_basename = str(
        config.get("source_recording_basename") or ""
    ).strip()
    if source_binding.source_recording_basename != expected_basename:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_BASENAME_MISMATCH"
        )
    expected_source_sha256 = _normalized_sha256(
        config.get("source_sha256"),
        "REDELIVERY_TERMINAL_PROJECTION_SOURCE_SHA256_INVALID",
    )
    actual_source_sha256 = _normalized_sha256(
        source_binding.source_sha256,
        "REDELIVERY_TERMINAL_PROJECTION_SOURCE_SHA256_INVALID",
    )
    if actual_source_sha256 != expected_source_sha256:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_SHA256_MISMATCH"
        )
    if (
        source_binding.absolute_source_start_ms != absolute_start_ms
        or source_binding.absolute_source_end_ms != absolute_end_ms
    ):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SOURCE_INTERVAL_MISMATCH"
        )

    baseline_path = _resolved_baseline_path(
        config.get("path"), spec_parent=spec_parent
    )
    baseline_bytes = baseline_path.read_bytes()
    baseline_sha256 = _sha256_bytes(baseline_bytes)
    if baseline_sha256 != _normalized_sha256(
        config.get("sha256"),
        "REDELIVERY_TERMINAL_PROJECTION_BASELINE_SHA256_INVALID",
    ):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_BASELINE_SHA256_MISMATCH"
        )
    try:
        baseline_cues = [
            cue
            for cue in parse_srt_cues(
                baseline_bytes.decode("utf-8", errors="strict")
            )
            if cue.text.strip()
        ]
    except (UnicodeError, ValueError) as exc:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_BASELINE_INVALID"
        ) from exc
    interval_duration_ms = absolute_end_ms - absolute_start_ms
    if (
        not baseline_cues
        or baseline_cues[-1].end_ms != interval_duration_ms
        or any(
            cue.start_ms < 0
            or cue.end_ms <= cue.start_ms
            or cue.end_ms > interval_duration_ms
            for cue in baseline_cues
        )
    ):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_BASELINE_TERMINAL_END_MISMATCH"
        )
    authority_name = str(config.get("authority") or "").strip()
    if not authority_name:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_AUTHORITY_MISSING"
        )
    terminal = baseline_cues[-1]
    core: dict[str, object] = {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "status": "BOUND",
        "candidate_id": str(spec.get("candidate_id") or ""),
        "baseline_srt_sha256": baseline_sha256,
        "baseline_terminal_cue_index": len(baseline_cues),
        "baseline_terminal_end_ms": terminal.end_ms,
        "baseline_terminal_text_sha256": _sha256_bytes(
            terminal.text.encode("utf-8")
        ),
        "source_recording_basename": expected_basename,
        "source_sha256": expected_source_sha256,
        "absolute_source_start_ms": absolute_start_ms,
        "absolute_source_end_ms": absolute_end_ms,
        "local_source_start_ms": local_start_ms,
        "local_source_end_ms": local_end_ms,
        "max_terminal_drift_ms": MAX_TERMINAL_DRIFT_MS,
        "authority": authority_name,
    }
    return {**core, "authority_sha256": _canonical_sha256(core)}


def validate_terminal_projection_authority(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_AUTHORITY_INVALID"
        )
    authority = dict(value)
    digest = authority.pop("authority_sha256", None)
    required_strings = (
        "candidate_id",
        "source_recording_basename",
        "authority",
    )
    required_hashes = (
        "baseline_srt_sha256",
        "baseline_terminal_text_sha256",
        "source_sha256",
    )
    required_ints = (
        "baseline_terminal_cue_index",
        "baseline_terminal_end_ms",
        "absolute_source_start_ms",
        "absolute_source_end_ms",
        "local_source_start_ms",
        "local_source_end_ms",
        "max_terminal_drift_ms",
    )
    expected_keys = {
        "schema_version",
        "status",
        *required_strings,
        *required_hashes,
        *required_ints,
    }
    if (
        set(authority) != expected_keys
        or
        authority.get("schema_version") != AUTHORITY_SCHEMA_VERSION
        or authority.get("status") != "BOUND"
        or any(not str(authority.get(key) or "").strip() for key in required_strings)
        or any(
            _SHA256_RX.fullmatch(str(authority.get(key) or "")) is None
            for key in required_hashes
        )
        or any(
            isinstance(authority.get(key), bool)
            or not isinstance(authority.get(key), int)
            or int(authority[key]) < 0
            for key in required_ints
        )
        or authority["baseline_terminal_cue_index"] < 1
        or authority["absolute_source_end_ms"]
        <= authority["absolute_source_start_ms"]
        or authority["local_source_end_ms"] <= authority["local_source_start_ms"]
        or authority["local_source_end_ms"]
        - authority["local_source_start_ms"]
        != authority["absolute_source_end_ms"]
        - authority["absolute_source_start_ms"]
        or Path(str(authority["source_recording_basename"])).name
        != authority["source_recording_basename"]
        or authority["baseline_terminal_end_ms"]
        != authority["absolute_source_end_ms"]
        - authority["absolute_source_start_ms"]
        or authority["max_terminal_drift_ms"] != MAX_TERMINAL_DRIFT_MS
        or _SHA256_RX.fullmatch(str(digest or "")) is None
        or digest != _canonical_sha256(authority)
    ):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_AUTHORITY_INVALID"
        )
    return dict(value)


def projection_scope_binding(authority: object) -> dict[str, object]:
    bound = validate_terminal_projection_authority(authority)
    return {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "authority_sha256": bound["authority_sha256"],
        "reviewed_endpoint_ms": bound["local_source_end_ms"],
        "max_terminal_drift_ms": bound["max_terminal_drift_ms"],
    }


def projection_scope_from_spec(
    spec: Mapping[str, object],
) -> dict[str, object] | None:
    config = spec.get("subtitle_redelivery_baseline")
    if not isinstance(config, Mapping):
        return None
    authority = config.get(AUTHORITY_CONFIG_KEY)
    if authority is None:
        return None
    return projection_scope_binding(authority)


def validate_projection_scope(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SCOPE_INVALID"
        )
    scope = dict(value)
    if (
        set(scope)
        != {
            "schema_version",
            "authority_sha256",
            "reviewed_endpoint_ms",
            "max_terminal_drift_ms",
        }
        or scope.get("schema_version") != SCOPE_SCHEMA_VERSION
        or _SHA256_RX.fullmatch(str(scope.get("authority_sha256") or ""))
        is None
        or isinstance(scope.get("reviewed_endpoint_ms"), bool)
        or not isinstance(scope.get("reviewed_endpoint_ms"), int)
        or scope["reviewed_endpoint_ms"] < 0
        or scope.get("max_terminal_drift_ms") != MAX_TERMINAL_DRIFT_MS
    ):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SCOPE_INVALID"
        )
    return scope


def _text_sha256(value: object) -> str:
    return _sha256_bytes(str(value or "").encode("utf-8"))


def terminal_projection_relaxation(
    rows: Sequence[Mapping[str, object]],
    *,
    projection_scope: object,
    minimum_end_ms: int,
    cap_end_ms: int,
    cue_grid_sha256: str,
) -> dict[str, object] | None:
    """Return the unique pre-cap closure / crossing-next-topic projection."""

    scope = validate_projection_scope(projection_scope)
    endpoint = int(scope["reviewed_endpoint_ms"])
    if minimum_end_ms != endpoint or cap_end_ms != endpoint:
        return None
    normalized: list[dict[str, object]] = []
    for row in rows:
        try:
            index = _required_int(
                row.get("cue_index"),
                "REDELIVERY_TERMINAL_PROJECTION_CUE_GRID_INVALID",
            )
            start = _required_int(
                row.get("start_ms"),
                "REDELIVERY_TERMINAL_PROJECTION_CUE_GRID_INVALID",
            )
            end = _required_int(
                row.get("end_ms"),
                "REDELIVERY_TERMINAL_PROJECTION_CUE_GRID_INVALID",
            )
        except AttributeError as exc:
            raise RedeliveryBoundaryProjectionError(
                "REDELIVERY_TERMINAL_PROJECTION_CUE_GRID_INVALID"
            ) from exc
        if index < 1 or end <= start:
            raise RedeliveryBoundaryProjectionError(
                "REDELIVERY_TERMINAL_PROJECTION_CUE_GRID_INVALID"
            )
        normalized.append(
            {
                "cue_index": index,
                "start_ms": start,
                "end_ms": end,
                "text": str(row.get("text") or ""),
            }
        )
    crossings = [
        position
        for position, row in enumerate(normalized)
        if int(row["start_ms"]) < endpoint < int(row["end_ms"])
    ]
    if len(crossings) != 1 or crossings[0] == 0:
        return None
    crossing_position = crossings[0]
    closure_position = crossing_position - 1
    closure = normalized[closure_position]
    crossing = normalized[crossing_position]
    drift_ms = endpoint - int(closure["end_ms"])
    if (
        not 0 < drift_ms <= int(scope["max_terminal_drift_ms"])
        or int(crossing["start_ms"]) != int(closure["end_ms"])
        or any(
            position != crossing_position
            and int(row["start_ms"]) < endpoint < int(row["end_ms"])
            for position, row in enumerate(normalized)
        )
    ):
        return None
    return {
        "kind": "reviewed_exact_interval_terminal_projection",
        "cue_index": closure["cue_index"],
        "cue_start_ms": closure["start_ms"],
        "cue_end_ms": closure["end_ms"],
        "cue_text_sha256": _text_sha256(closure["text"]),
        "reviewed_endpoint_ms": endpoint,
        "terminal_drift_ms": drift_ms,
        "max_terminal_drift_ms": scope["max_terminal_drift_ms"],
        "crossing_witness_cue_index": crossing["cue_index"],
        "crossing_witness_start_ms": crossing["start_ms"],
        "crossing_witness_end_ms": crossing["end_ms"],
        "crossing_witness_text_sha256": _text_sha256(crossing["text"]),
        "authority_sha256": scope["authority_sha256"],
        "cue_grid_sha256": cue_grid_sha256,
    }


def selected_terminal_projection(
    *,
    review: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    scope: Mapping[str, object],
    authority: object,
) -> dict[str, object] | None:
    projection_scope = scope.get("reviewed_exact_interval_projection")
    if projection_scope is None:
        return None
    if projection_scope_binding(authority) != projection_scope:
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SCOPE_AUTHORITY_MISMATCH"
        )
    receipt = terminal_projection_relaxation(
        rows,
        projection_scope=projection_scope,
        minimum_end_ms=int(scope["minimum_recommended_end_ms"]),
        cap_end_ms=int(scope["max_recommended_end_ms"]),
        cue_grid_sha256=str(review.get("cue_grid_sha256") or ""),
    )
    if receipt is None:
        return None
    if (
        review.get("recommended_end_cue_index") != receipt["cue_index"]
        or review.get("recommended_end_ms")
        != receipt["reviewed_endpoint_ms"]
        or review.get("recommendation_relaxations") != [receipt]
    ):
        return None
    return receipt


def projection_endpoint_binding_is_valid(
    *,
    review: Mapping[str, object],
    cues: Sequence[object],
    closure_index: int | None,
    snapped_end_ms: int,
    final_end_ms: int,
) -> bool:
    """Recompute the inverse-crossing endpoint from the bound fresh grid."""

    scope = review.get("boundary_search_scope")
    if not isinstance(scope, Mapping):
        return False
    projection_scope = scope.get("reviewed_exact_interval_projection")
    if projection_scope is None:
        return False
    rows = [
        {
            "cue_index": index,
            "start_ms": int(getattr(cue, "start_ms")),
            "end_ms": int(getattr(cue, "end_ms")),
            "text": str(getattr(cue, "text") or "").strip(),
        }
        for index, cue in enumerate(cues, start=1)
        if str(getattr(cue, "text", "") or "").strip()
    ]
    grid_sha256 = _canonical_sha256(
        {
            "schema_version": "talk-boundary-cue-grid.v1",
            "cues": rows,
        }
    )
    if review.get("cue_grid_sha256") != grid_sha256:
        return False
    try:
        expected = terminal_projection_relaxation(
            rows,
            projection_scope=projection_scope,
            minimum_end_ms=int(scope["minimum_recommended_end_ms"]),
            cap_end_ms=int(scope["max_recommended_end_ms"]),
            cue_grid_sha256=grid_sha256,
        )
    except (KeyError, TypeError, ValueError, RedeliveryBoundaryProjectionError):
        return False
    binding = review.get("selected_terminal_projection_binding")
    if expected is None or binding != expected:
        return False
    return bool(
        review.get("recommendation_relaxations") == [expected]
        and closure_index == expected["cue_index"]
        and review.get("recommended_end_cue_index") == closure_index
        and review.get("recommended_end_ms")
        == expected["reviewed_endpoint_ms"]
        and snapped_end_ms == expected["cue_end_ms"]
        and final_end_ms == expected["reviewed_endpoint_ms"]
    )


def stored_projection_endpoint_is_valid(
    review: Mapping[str, object], endpoint: Mapping[str, object]
) -> bool:
    """Validate the persisted projection receipt without trusting its label."""

    scope = review.get("boundary_search_scope")
    binding = review.get("selected_terminal_projection_binding")
    relaxations = review.get("recommendation_relaxations")
    if (
        not isinstance(scope, Mapping)
        or not isinstance(binding, Mapping)
        or relaxations != [binding]
        or binding.get("kind")
        != "reviewed_exact_interval_terminal_projection"
    ):
        return False
    try:
        projection_scope = validate_projection_scope(
            scope.get("reviewed_exact_interval_projection")
        )
    except RedeliveryBoundaryProjectionError:
        return False
    cue_end = binding.get("cue_end_ms")
    cue_index = binding.get("cue_index")
    reviewed_end = binding.get("reviewed_endpoint_ms")
    crossing_index = binding.get("crossing_witness_cue_index")
    crossing_start = binding.get("crossing_witness_start_ms")
    crossing_end = binding.get("crossing_witness_end_ms")
    drift = binding.get("terminal_drift_ms")
    values = (
        cue_end,
        cue_index,
        reviewed_end,
        crossing_index,
        crossing_start,
        crossing_end,
        drift,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return False
    evidence = review.get("evidence_cue_indexes")
    return bool(
        binding.get("authority_sha256")
        == projection_scope["authority_sha256"]
        and binding.get("cue_grid_sha256") == review.get("cue_grid_sha256")
        and _SHA256_RX.fullmatch(str(binding.get("cue_text_sha256") or ""))
        is not None
        and _SHA256_RX.fullmatch(
            str(binding.get("crossing_witness_text_sha256") or "")
        )
        is not None
        and cue_index == review.get("recommended_end_cue_index")
        and isinstance(evidence, list)
        and crossing_index in evidence
        and crossing_index > cue_index
        and reviewed_end == review.get("recommended_end_ms")
        and reviewed_end == projection_scope["reviewed_endpoint_ms"]
        and cue_end == endpoint.get("final_snapped_end_ms")
        and endpoint.get("final_end_ms") == reviewed_end
        and endpoint.get("closure_text_sha256")
        == binding.get("cue_text_sha256")
        and crossing_start == cue_end
        and crossing_start < reviewed_end < crossing_end
        and drift == reviewed_end - cue_end
        and 0 < drift <= projection_scope["max_terminal_drift_ms"]
        and binding.get("max_terminal_drift_ms")
        == projection_scope["max_terminal_drift_ms"]
    )


def materialization_spec_for_selected_projection(
    spec: Mapping[str, object],
    boundary_audit: Mapping[str, object],
) -> Mapping[str, object]:
    """Activate exact projection replay only for a validated selected grant."""

    config = spec.get("subtitle_redelivery_baseline")
    authority_raw = (
        config.get(AUTHORITY_CONFIG_KEY)
        if isinstance(config, Mapping)
        else None
    )
    review = boundary_audit.get("boundary_semantic_review")
    selected = (
        review.get("selected_terminal_projection_binding")
        if isinstance(review, Mapping)
        else None
    )
    if authority_raw is None:
        if selected is not None:
            raise RedeliveryBoundaryProjectionError(
                "REDELIVERY_TERMINAL_PROJECTION_AUTHORITY_MISSING"
            )
        return spec
    if selected is None:
        result = dict(spec)
        result_config = dict(config)
        result_config.pop(AUTHORITY_CONFIG_KEY, None)
        result["subtitle_redelivery_baseline"] = result_config
        return result
    authority = validate_terminal_projection_authority(authority_raw)
    endpoint = review.get("final_endpoint_binding")
    scope = review.get("boundary_search_scope")
    if (
        not isinstance(endpoint, Mapping)
        or not isinstance(scope, Mapping)
        or projection_scope_binding(authority)
        != scope.get("reviewed_exact_interval_projection")
        or not stored_projection_endpoint_is_valid(review, endpoint)
    ):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_SELECTION_INVALID"
        )
    return spec


def projection_materialization_receipt(
    *,
    config: Mapping[str, object],
    baseline_cues: Sequence[object],
    current_source_start_ms: int,
    current_source_end_ms: int,
    application_strategy: str,
    video_tail_extension_ms: int,
) -> dict[str, object] | None:
    authority_raw = config.get(AUTHORITY_CONFIG_KEY)
    if authority_raw is None:
        return None
    authority = validate_terminal_projection_authority(authority_raw)
    terminal_end = (
        int(getattr(baseline_cues[-1], "end_ms"))
        if baseline_cues
        else None
    )
    if (
        config.get("exact_interval_replay") is not True
        or _normalized_sha256(
            config.get("sha256"),
            "REDELIVERY_TERMINAL_PROJECTION_AUTHORITY_CONFIG_MISMATCH",
        )
        != authority["baseline_srt_sha256"]
        or str(config.get("source_recording_basename") or "").strip()
        != authority["source_recording_basename"]
        or _normalized_sha256(
            config.get("source_sha256"),
            "REDELIVERY_TERMINAL_PROJECTION_AUTHORITY_CONFIG_MISMATCH",
        )
        != authority["source_sha256"]
        or config.get("absolute_source_start_ms")
        != authority["absolute_source_start_ms"]
        or config.get("absolute_source_end_ms")
        != authority["absolute_source_end_ms"]
        or application_strategy != "exact_reviewed_interval_replay"
        or video_tail_extension_ms != 0
        or current_source_start_ms
        != authority["absolute_source_start_ms"]
        or current_source_end_ms != authority["absolute_source_end_ms"]
        or terminal_end != authority["baseline_terminal_end_ms"]
    ):
        raise RedeliveryBoundaryProjectionError(
            "REDELIVERY_TERMINAL_PROJECTION_REQUIRES_EXACT_INTERVAL"
        )
    return {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "status": "PASS",
        "authority_sha256": authority["authority_sha256"],
        "application_strategy": application_strategy,
        "absolute_source_start_ms": current_source_start_ms,
        "absolute_source_end_ms": current_source_end_ms,
        "video_tail_extension_ms": video_tail_extension_ms,
    }
