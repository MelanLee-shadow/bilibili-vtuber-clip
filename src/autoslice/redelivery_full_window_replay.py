"""Exact v2 full-window baseline replay and delivery-local audit projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path

from src.autoslice.recut_materialization import (
    BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS,
    BOUNDARY_CUE_START_TOLERANCE_MS,
    _fresh_srt_to_source_cues,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.redelivery_source_binding import V2RedeliverySourceBinding
from src.autoslice.redelivery_subtitle_baseline import (
    apply_redelivery_subtitle_baseline,
)
from src.autoslice.redelivery_time_domain import (
    DELIVERY_LOCAL,
    PIECE_LOCAL,
    RedeliveryTimeDomainError,
    operator_v3_time_domain,
    require_delivery_local_interval,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_subtitle_truth import source_truth_owner_windows


class FullWindowReplayError(RuntimeError):
    """A full-window replay or its private temporary is unsafe."""


def exact_full_window_replay_enabled(
    config: Mapping[str, object], binding: V2RedeliverySourceBinding | None
) -> bool:
    """Select full-window replay only for an explicitly piece-local v3 SRT."""

    try:
        time_domain = operator_v3_time_domain(config)
    except RedeliveryTimeDomainError as exc:
        raise FullWindowReplayError(str(exc)) from exc
    if time_domain == DELIVERY_LOCAL:
        return False
    return bool(
        binding is not None
        and config.get("exact_interval_replay") is True
        and config.get("absolute_source_start_ms")
        == binding.content_absolute_start_ms
        and config.get("absolute_source_end_ms")
        == binding.content_absolute_end_ms
        and (time_domain is None or time_domain == PIECE_LOCAL)
    )



def _require_v3_time_domain_binding(
    config: Mapping[str, object], binding: V2RedeliverySourceBinding | None
) -> str | None:
    try:
        time_domain = operator_v3_time_domain(config)
        if time_domain is None:
            return None
        if binding is None or config.get("exact_interval_replay") is not True:
            raise RedeliveryTimeDomainError(
                "REDELIVERY_BASELINE_TIME_DOMAIN_SOURCE_BINDING_MISSING"
            )
        if time_domain == DELIVERY_LOCAL:
            require_delivery_local_interval(
                config,
                absolute_start_ms=binding.absolute_source_start_ms,
                absolute_end_ms=binding.absolute_source_end_ms,
            )
        elif (
            config.get("absolute_source_start_ms")
            != binding.content_absolute_start_ms
            or config.get("absolute_source_end_ms")
            != binding.content_absolute_end_ms
        ):
            raise RedeliveryTimeDomainError(
                "REDELIVERY_BASELINE_PIECE_INTERVAL_MISMATCH"
            )
        return time_domain
    except RedeliveryTimeDomainError as exc:
        raise FullWindowReplayError(str(exc)) from exc

def _audit_deferred_exact_replay_reverification(
    *,
    pre_truth_audit: Mapping[str, object],
    baseline_audit: Mapping[str, object],
    post_truth_audit: Mapping[str, object] | None,
) -> dict[str, object]:
    """Prove an early cue-shape deferral reached its promised late authority."""

    strategy = pre_truth_audit.get("deferred_strategy")
    result: dict[str, object] = {
        "schema_version": "deferred-exact-replay-reverification.v1",
        "status": "NOT_REQUIRED",
        "deferred_strategy": strategy,
        "required_truth_ids": [],
        "context_only_truth_ids": [],
        "straddling_truth_ids": [],
        "reverified_truth_ids": [],
        "missing_truth_ids": [],
    }
    supported_strategies = {
        "exact_reviewed_interval_replay_then_reapply_source_truth",
        "reviewed_text_restore_then_reapply_source_truth",
    }
    if strategy not in supported_strategies:
        return result

    current_source_interval = baseline_audit.get("current_source_interval")
    if isinstance(current_source_interval, Mapping):
        final_source_start_ms = current_source_interval.get(
            "absolute_source_start_ms"
        )
        final_source_end_ms = current_source_interval.get(
            "absolute_source_end_ms"
        )
    else:
        final_source_start_ms = final_source_end_ms = None
    final_interval_valid = bool(
        isinstance(final_source_start_ms, int)
        and not isinstance(final_source_start_ms, bool)
        and isinstance(final_source_end_ms, int)
        and not isinstance(final_source_end_ms, bool)
        and final_source_end_ms > final_source_start_ms
    )
    required_ids: list[str] = []
    context_only_ids: list[str] = []
    straddling_ids: list[str] = []
    for row in pre_truth_audit.get("failures") or []:
        if not isinstance(row, Mapping):
            continue
        truth_id = str(row.get("truth_id") or "")
        if not truth_id:
            continue
        source_start_ms = row.get("source_start_ms")
        source_end_ms = row.get("source_end_ms")
        source_interval_valid = bool(
            isinstance(source_start_ms, int)
            and not isinstance(source_start_ms, bool)
            and isinstance(source_end_ms, int)
            and not isinstance(source_end_ms, bool)
            and source_end_ms > source_start_ms
        )
        if not (final_interval_valid and source_interval_valid):
            required_ids.append(truth_id)
            continue
        wholly_outside = bool(
            source_end_ms <= final_source_start_ms
            or source_start_ms >= final_source_end_ms
        )
        wholly_inside = bool(
            final_source_start_ms <= source_start_ms
            and source_end_ms <= final_source_end_ms
        )
        if wholly_outside:
            context_only_ids.append(truth_id)
        elif wholly_inside:
            required_ids.append(truth_id)
        else:
            straddling_ids.append(truth_id)
    required_ids = sorted(set(required_ids))
    straddling_ids = sorted(set(straddling_ids))
    # A duplicated truth id is context-only only if every occurrence is wholly
    # outside. Any inside or straddling occurrence keeps it out of that class.
    context_only_ids = sorted(
        set(context_only_ids) - set(required_ids) - set(straddling_ids)
    )
    result["required_truth_ids"] = required_ids
    result["context_only_truth_ids"] = context_only_ids
    result["straddling_truth_ids"] = straddling_ids
    if straddling_ids:
        result["status"] = "FAILED"
        result["reason_code"] = (
            "DEFERRED_TRUTH_STRADDLES_FINAL_DELIVERY"
        )
        return result
    exact_strategy = (
        strategy
        == "exact_reviewed_interval_replay_then_reapply_source_truth"
    )
    baseline_strategy_ok = (
        baseline_audit.get("application_strategy")
        == "exact_reviewed_interval_replay"
        if exact_strategy
        else baseline_audit.get("status")
        in {"APPLIED", "ALREADY_SATISFIED"}
    )
    post_truth_ok = bool(
        isinstance(post_truth_audit, Mapping)
        and post_truth_audit.get("status")
        in {"APPLIED", "ALREADY_SATISFIED", "NO_RELEVANT_INTERVAL"}
    )
    if not baseline_strategy_ok or not post_truth_ok:
        result["status"] = "FAILED"
        result["reason_code"] = (
            "EXACT_REPLAY_OR_POST_TRUTH_AUTHORITY_MISSING"
        )
        return result
    if not required_ids:
        if context_only_ids:
            result["status"] = "PASS"
            result["reason_code"] = (
                "ALL_DEFERRED_TRUTH_CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
            )
            return result
        result["status"] = "FAILED"
        result["reason_code"] = "DEFERRED_TRUTH_REQUIREMENT_EMPTY"
        return result

    reverified_ids = sorted(
        {
            str(row.get("truth_id"))
            for key in ("applied", "satisfied")
            for row in (post_truth_audit.get(key) or [])
            if isinstance(row, Mapping) and str(row.get("truth_id") or "")
        }
    )
    missing_ids = sorted(set(required_ids) - set(reverified_ids))
    result["reverified_truth_ids"] = reverified_ids
    result["missing_truth_ids"] = missing_ids
    if missing_ids:
        result["status"] = "FAILED"
        result["reason_code"] = "DEFERRED_TRUTH_ID_NOT_REVERIFIED"
    else:
        result["status"] = "PASS"
    return result


def attach_deferred_exact_replay_reverification(
    *,
    baseline_audit: dict[str, object],
    pre_truth_audit: Mapping[str, object],
    post_truth_audit: Mapping[str, object] | None,
    deferred_audit: dict[str, object] | None,
) -> dict[str, object]:
    """Attach the one deferred replay receipt before its baseline is written."""

    audit = deferred_audit or _audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre_truth_audit,
        baseline_audit=baseline_audit,
        post_truth_audit=post_truth_audit,
    )
    baseline_audit["deferred_exact_replay_reverification"] = audit
    return audit



def translate_final_local_protected_windows(
    windows: Sequence[tuple[int, int]], *, final_start_ms: int, padded_content_start_ms: int
) -> list[tuple[int, int]]:
    """Translate final-local source-truth drops to the padded replay grid."""

    offset = final_start_ms - padded_content_start_ms
    return [(start_ms + offset, end_ms + offset) for start_ms, end_ms in windows]


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


_DELIVERY_PROJECTION_SCHEMA = "reviewed-baseline-full-release-delivery-projection.v1"
_DELIVERY_PROJECTION_SCHEMA_V2 = "reviewed-baseline-full-release-delivery-projection.v2"
_DELIVERY_PROJECTION_COORDINATE_SCHEMA = "reviewed-baseline-grid-record-grid.v1"


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _private_create_only_json(path: Path, value: Mapping[str, object]) -> str:
    """Commit one private receipt without making a reusable mutable sidecar."""

    payload = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_RECEIPT_COLLISION") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_RECEIPT_WRITE_FAILED")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_RECEIPT_WRITE_FAILED") from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o600:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_RECEIPT_UNSAFE")
    return _sha256_bytes(payload)


def _projection_hash(value: object, *, code: str) -> str:
    raw = str(value or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", raw):
        raise FullWindowReplayError(code)
    return raw


def _clip_interval(
    start_ms: int, end_ms: int, *, final_start_ms: int, final_end_ms: int
) -> tuple[int, int] | None:
    clipped_start = max(start_ms, final_start_ms)
    clipped_end = min(end_ms, final_end_ms)
    if clipped_end <= clipped_start:
        return None
    if (
        clipped_start - final_start_ms <= BOUNDARY_CUE_START_TOLERANCE_MS
        and clipped_end - clipped_start <= BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS
    ):
        return None
    return clipped_start, clipped_end


def project_full_window_audit_to_final_delivery(
    audit: dict, *, final_start_ms: int, final_end_ms: int
) -> None:
    """Preserve the source-grid audit while exposing final-local owner rows."""

    mappings = audit.get("mappings")
    owned = audit.get("owned_intervals")
    protected = audit.get("protected_intervals")
    if not all(isinstance(value, list) for value in (mappings, owned, protected)):
        raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_AUDIT_INVALID")
    full_source = {
        "schema_version": "redelivery-baseline-full-source-replay.v1",
        "mappings": deepcopy(mappings),
        "owned_intervals": deepcopy(owned),
        "protected_intervals": deepcopy(protected),
    }
    active_mappings: list[dict] = []
    omitted: list[dict] = []
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_MAPPING_INVALID")
        start_ms, end_ms = mapping.get("start_ms"), mapping.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_MAPPING_INVALID")
        clipped = _clip_interval(
            start_ms, end_ms, final_start_ms=final_start_ms, final_end_ms=final_end_ms
        )
        if clipped is None:
            reason = (
                "OUTSIDE_FINAL_DELIVERY"
                if end_ms <= final_start_ms or start_ms >= final_end_ms
                else "BOUNDARY_FLASH_FRAGMENT_OMITTED"
            )
            omitted.append(
                {"baseline_cue_index": mapping.get("baseline_cue_index"),
                 "source_start_ms": start_ms, "source_end_ms": end_ms, "reason": reason}
            )
            continue
        projected = deepcopy(dict(mapping))
        projected.update({"start_ms": clipped[0] - final_start_ms,
                          "end_ms": clipped[1] - final_start_ms,
                          "full_source_start_ms": start_ms,
                          "full_source_end_ms": end_ms})
        active_mappings.append(projected)

    def project_intervals(rows: list, error: str) -> list[dict[str, int]]:
        active: list[dict[str, int]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise FullWindowReplayError(error)
            start_ms, end_ms = row.get("start_ms"), row.get("end_ms")
            if (
                isinstance(start_ms, bool)
                or isinstance(end_ms, bool)
                or not isinstance(start_ms, int)
                or not isinstance(end_ms, int)
                or end_ms <= start_ms
            ):
                raise FullWindowReplayError(error)
            clipped = _clip_interval(
                start_ms, end_ms, final_start_ms=final_start_ms, final_end_ms=final_end_ms
            )
            if clipped is not None:
                active.append({"start_ms": clipped[0] - final_start_ms,
                               "end_ms": clipped[1] - final_start_ms})
        return active

    audit.update(
        {
            "full_source_replay": full_source,
            "full_source_replay_sha256": _canonical_sha256(full_source),
            "mappings": active_mappings,
            "owned_intervals": project_intervals(
                owned, "REDELIVERY_BASELINE_FULL_REPLAY_OWNED_INTERVAL_INVALID"
            ),
            # A protected window is a deliberate omission, not a subtitle
            # cue, so preserve a visible clipped portion even if it is short.
            "protected_intervals": _project_protected_intervals(
                protected, final_start_ms=final_start_ms, final_end_ms=final_end_ms
            ),
            "full_source_mapped_cue_count": len(mappings),
            "mapped_cue_count": len(active_mappings),
            "omitted_context_mapping_count": len(omitted),
            "omitted_context_mappings": omitted,
        }
    )


def _project_protected_intervals(
    rows: list, *, final_start_ms: int, final_end_ms: int
) -> list[dict[str, int]]:
    projected: list[dict[str, int]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_PROTECTED_INTERVAL_INVALID")
        start_ms, end_ms = row.get("start_ms"), row.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_PROTECTED_INTERVAL_INVALID")
        clipped_start, clipped_end = max(start_ms, final_start_ms), min(end_ms, final_end_ms)
        if clipped_start < clipped_end:
            projected.append({"start_ms": clipped_start - final_start_ms,
                              "end_ms": clipped_end - final_start_ms})
    return projected


def _build_full_release_delivery_projection_receipt(
    *,
    candidate_id: str,
    record_sha256: str,
    record_boundary: Mapping[str, object],
    padded_start_ms: int,
    padded_end_ms: int,
    final_start_ms: int,
    final_end_ms: int,
    diagnostic_text: str,
    full_release_text: str,
    delivery_bytes: bytes,
    config: Mapping[str, object],
    staged_media_sha256: str,
    lane_bytes: Mapping[str, bytes],
    c5_start_clamp_proposal_path: Path | None = None,
    c5_start_clamp_acceptance_path: Path | None = None,
    recording_date: str | None = None,
    baseline_source_start_ms: int | None = None,
    baseline_source_end_ms: int | None = None,
    baseline_crop_start_ms: int | None = None,
    baseline_crop_end_ms: int | None = None
) -> dict[str, object] | None:
    """Prove an exact sealed full-release -> final-delivery projection.

    This receipt deliberately carries cue indexes/times and byte bindings but
    never subtitle text or filesystem locations.  It exists only for the
    exceptional case where the final delivery grid is a strict subset of the
    sealed release grid; ordinary equal-grid replay does not need it.
    """

    if not candidate_id or not isinstance(record_boundary, Mapping):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_INPUT_INVALID")
    for value in (padded_start_ms, padded_end_ms, final_start_ms, final_end_ms):
        if isinstance(value, bool) or not isinstance(value, int):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_BOUNDARY_INVALID")
    if not (padded_start_ms < padded_end_ms and 0 <= final_start_ms < final_end_ms <= padded_end_ms - padded_start_ms):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_BOUNDARY_INVALID")
    if (
        record_boundary.get("final_start_ms") != final_start_ms
        or record_boundary.get("final_end_ms") != final_end_ms
    ):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_RECORD_BOUNDARY_DRIFT")
    coordinate_values = (
        baseline_source_start_ms, baseline_source_end_ms,
        baseline_crop_start_ms, baseline_crop_end_ms,
    )
    if any(value is not None for value in coordinate_values) and not all(
        value is not None for value in coordinate_values
    ):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_COORDINATE_INVALID")
    if all(value is not None for value in coordinate_values):
        if any(isinstance(value, bool) or not isinstance(value, int) for value in coordinate_values):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_COORDINATE_INVALID")
        assert isinstance(baseline_source_start_ms, int) and isinstance(baseline_source_end_ms, int)
        assert isinstance(baseline_crop_start_ms, int) and isinstance(baseline_crop_end_ms, int)
        if not (
            padded_start_ms <= baseline_source_start_ms < baseline_source_end_ms <= padded_end_ms
            and 0 <= baseline_crop_start_ms < baseline_crop_end_ms <= baseline_source_end_ms - baseline_source_start_ms
            and baseline_source_start_ms + baseline_crop_start_ms == padded_start_ms + final_start_ms
            and baseline_source_start_ms + baseline_crop_end_ms == padded_start_ms + final_end_ms
        ):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_COORDINATE_DRIFT")
        grid_start_ms, grid_end_ms = baseline_crop_start_ms, baseline_crop_end_ms
    else:
        grid_start_ms, grid_end_ms = final_start_ms, final_end_ms
    lanes = config.get("operator_truth_lanes")
    if not isinstance(lanes, Mapping):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID")
    diagnostic = lanes.get("pipeline_diagnostic")
    ledger = lanes.get("decision_ledger")
    diff = lanes.get("diff_receipt")
    if not all(isinstance(item, Mapping) for item in (diagnostic, ledger, diff)):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID")
    try:
        diagnostic_sha = _projection_hash(
            "sha256:" + str(diagnostic.get("sha256") or "").removeprefix("sha256:"),
            code="REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID",
        )
        ledger_sha = _projection_hash(
            "sha256:" + str(ledger.get("sha256") or "").removeprefix("sha256:"),
            code="REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID",
        )
        diff_sha = _projection_hash(
            "sha256:" + str(diff.get("sha256") or "").removeprefix("sha256:"),
            code="REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID",
        )
        full_release_sha = _projection_hash(
            "sha256:" + str(config.get("sha256") or "").removeprefix("sha256:"),
            code="REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID",
        )
        record_sha256 = _projection_hash(record_sha256, code="REDELIVERY_DELIVERY_PROJECTION_RECORD_INVALID")
        staged_media_sha256 = _projection_hash(staged_media_sha256, code="REDELIVERY_DELIVERY_PROJECTION_MEDIA_INVALID")
    except AttributeError as exc:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID") from exc
    if set(lane_bytes) != {"pipeline_diagnostic", "decision_ledger", "diff_receipt"}:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID")
    for key, expected in (
        ("pipeline_diagnostic", diagnostic_sha),
        ("decision_ledger", ledger_sha),
        ("diff_receipt", diff_sha),
    ):
        captured = lane_bytes.get(key)
        if not isinstance(captured, bytes) or _sha256_bytes(captured) != expected:
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_LANES_INVALID")
    if _sha256_bytes(diagnostic_text.encode("utf-8")) != diagnostic_sha:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_DIAGNOSTIC_DRIFT")
    if _sha256_bytes(full_release_text.encode("utf-8")) != full_release_sha:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_FULL_RELEASE_DRIFT")
    try:
        old_cues = parse_srt_cues(diagnostic_text)
        release_cues = parse_srt_cues(full_release_text)
        delivery_cues = parse_srt_cues(delivery_bytes.decode("utf-8"))
        diff_document = json.loads(lane_bytes["diff_receipt"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_GRID_INVALID") from exc
    if not isinstance(diff_document, Mapping) or not isinstance(diff_document.get("rows"), list):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_GRID_INVALID")
    diff_rows = diff_document["rows"]
    if len(diff_rows) != len(old_cues):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_MAP_INVALID")
    if len(release_cues) == len(delivery_cues):
        # Equal-grid C6-style replay is deliberately not projected.  A crop
        # that did not remove a release cue has no exceptional authority.
        return None
    if len(release_cues) < len(delivery_cues):
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_DELIVERY_GRID_DRIFT")
    rows: list[dict[str, object]] = []
    release_cursor = 0
    delivery_cursor = 0
    for old_ordinal, (old, diff_row) in enumerate(zip(old_cues, diff_rows, strict=True), start=1):
        if not isinstance(diff_row, Mapping) or diff_row.get("cue") != old_ordinal:
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_MAP_INVALID")
        if diff_row.get("disposition") == "OPERATOR_DROP":
            if diff_row.get("release_cue_index") is not None:
                raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_MAP_INVALID")
            rows.append({
                "old_source_index": old_ordinal,
                "release_cue_index": None,
                "delivery_cue_index": None,
                "disposition": "OPERATOR_DROP",
            })
            continue
        if release_cursor >= len(release_cues):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_MAP_INVALID")
        release = release_cues[release_cursor]
        release_index = release_cursor + 1
        if (
            diff_row.get("release_cue_index") != release_index
            or (old.start_ms, old.end_ms) != (release.start_ms, release.end_ms)
            or str(diff_row.get("release_truth_text") or "") != release.text
        ):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_MAP_INVALID")
        release_cursor += 1
        if release.end_ms <= grid_start_ms or release.start_ms >= grid_end_ms:
            rows.append({
                "old_source_index": old_ordinal,
                "release_cue_index": release_index,
                "delivery_cue_index": None,
                "disposition": "OUTSIDE_FINAL_DELIVERY",
            })
            continue
        coordinate_projection = all(
            value is not None
            for value in (
                baseline_source_start_ms,
                baseline_source_end_ms,
                baseline_crop_start_ms,
                baseline_crop_end_ms,
            )
        )
        clamp_start_bound = grid_start_ms if coordinate_projection else final_start_ms
        clamp_end_bound = grid_end_ms if coordinate_projection else final_end_ms
        start_clamp = release.start_ms < clamp_start_bound
        end_clamp = release.end_ms > clamp_end_bound
        c7b_start_geometry = None
        c7b_geometry = None
        if start_clamp:
            from src.autoslice.fastlane_c7b_source_reconciliation import (
                C7bSourceReconciliationError, resolve_c7b_delivery_start_clamp,
            )
            try:
                c7b_start_geometry = resolve_c7b_delivery_start_clamp(
                    repo_root=Path(__file__).resolve().parents[2], candidate_id=candidate_id, recording_date=recording_date, record_sha256=record_sha256, staged_media_sha256=staged_media_sha256, final_start_ms=final_start_ms, final_end_ms=final_end_ms, source_ordinal=old_ordinal, text=release.text, source_start_ms=release.start_ms, source_end_ms=release.end_ms,
                )
            except C7bSourceReconciliationError as exc:
                raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_STRADDLER") from exc
        if end_clamp:
            from src.autoslice.fastlane_c7b_source_reconciliation import (
                C7bSourceReconciliationError, resolve_c7b_delivery_end_clamp,
            )
            try:
                c7b_geometry = resolve_c7b_delivery_end_clamp(
                    repo_root=Path(__file__).resolve().parents[2], candidate_id=candidate_id,
                    recording_date=recording_date, record_sha256=record_sha256,
                    staged_media_sha256=staged_media_sha256, final_start_ms=final_start_ms,
                    final_end_ms=final_end_ms, source_ordinal=old_ordinal,
                    text=release.text, source_start_ms=release.start_ms,
                    source_end_ms=release.end_ms,
                )
            except C7bSourceReconciliationError as exc:
                raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_STRADDLER") from exc
            if c7b_geometry is None:
                raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_STRADDLER")
            expected_start, expected_end = c7b_geometry
            disposition = "RETAINED_FINAL_DELIVERY_END_CLAMP"
        c5_authorized = None not in (
            c5_start_clamp_proposal_path,
            c5_start_clamp_acceptance_path,
            recording_date,
        )
        if (
            (release.start_ms < grid_start_ms or release.end_ms > grid_end_ms)
            and c7b_start_geometry is None
            and c7b_geometry is None
            and not (start_clamp and c5_authorized)
        ):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_STRADDLER")
        if start_clamp and c7b_start_geometry is None and not c5_authorized:
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_STRADDLER")
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_STRADDLER")
        if delivery_cursor >= len(delivery_cues):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_DELIVERY_GRID_DRIFT")
        delivery = delivery_cues[delivery_cursor]
        delivery_index = delivery_cursor + 1
        if c7b_start_geometry is not None:
            expected_start, expected_end = c7b_start_geometry
        elif not end_clamp:
            expected_start, expected_end = (
                release.start_ms - grid_start_ms,
                release.end_ms - grid_start_ms,
            )
        disposition = "RETAINED_FINAL_DELIVERY"
        if c7b_start_geometry is not None:
            disposition = "RETAINED_FINAL_DELIVERY_START_CLAMP"
        if end_clamp:
            disposition = "RETAINED_FINAL_DELIVERY_END_CLAMP"
        if start_clamp and c7b_start_geometry is None:
            # No generic clipping authority exists.  The only exception is the
            # accepted C5 cue-5 geometry, loaded from the runtime-private
            # proposal and acceptance bytes at the first projection boundary.
            from src.autoslice.c5_start_clamp import (
                C5StartClampError,
                C5_ACCEPTANCE_EXPECTATIONS,
                CUE,
                accepted_delivery_geometry,
                load_accepted_authority,
                load_proposal,
            )
            assert c5_start_clamp_proposal_path is not None
            assert c5_start_clamp_acceptance_path is not None
            assert recording_date is not None
            try:
                proposal, proposal_sha = load_proposal(c5_start_clamp_proposal_path)
                acceptance = load_accepted_authority(
                    c5_start_clamp_acceptance_path,
                    proposal_path=c5_start_clamp_proposal_path,
                    expectations=C5_ACCEPTANCE_EXPECTATIONS,
                )
                expected_start, expected_end = accepted_delivery_geometry(
                    proposal_path=c5_start_clamp_proposal_path,
                    proposal=proposal,
                    proposal_file_sha256=proposal_sha,
                    acceptance=acceptance,
                    expectations=C5_ACCEPTANCE_EXPECTATIONS,
                    candidate_id=candidate_id,
                    recording_date=recording_date,
                    final_start_ms=final_start_ms,
                    final_end_ms=final_end_ms,
                    source_index=old_ordinal,
                    text=release.text,
                    speaker_label=str(CUE["speaker_label"]),
                    old_start_ms=release.start_ms,
                    old_end_ms=release.end_ms,
                    media_sha256=staged_media_sha256,
                )
            except C5StartClampError as exc:
                raise FullWindowReplayError(
                    "REDELIVERY_DELIVERY_PROJECTION_STRADDLER"
                ) from exc
            disposition = "RETAINED_FINAL_DELIVERY_START_CLAMP"
        if (
            delivery.index != str(delivery_index)
            or (delivery.start_ms, delivery.end_ms)
            != (expected_start, expected_end)
            or delivery.text != release.text
        ):
            raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_DELIVERY_GRID_DRIFT")
        rows.append({
            "old_source_index": old_ordinal,
            "release_cue_index": release_index,
            "delivery_cue_index": delivery_index,
            "disposition": disposition,
            "release_start_ms": release.start_ms,
            "release_end_ms": release.end_ms,
            "delivery_start_ms": delivery.start_ms,
            "delivery_end_ms": delivery.end_ms,
        })
        delivery_cursor += 1
    if release_cursor != len(release_cues) or delivery_cursor != len(delivery_cues) or not delivery_cues:
        raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_MAP_INVALID")
    receipt: dict[str, object] = {
        "schema_version": (_DELIVERY_PROJECTION_SCHEMA_V2 if coordinate_values[0] is not None else _DELIVERY_PROJECTION_SCHEMA),
        "candidate_id": candidate_id,
        "record_sha256": record_sha256,
        "record_boundary_sha256": _canonical_sha256(dict(record_boundary)),
        "pipeline_diagnostic_sha256": diagnostic_sha,
        "full_release_srt_sha256": full_release_sha,
        "operator_ledger_sha256": ledger_sha,
        "operator_truth_diff_sha256": diff_sha,
        "staged_srt_sha256": _sha256_bytes(delivery_bytes),
        "staged_media_sha256": staged_media_sha256,
        "old_diagnostic_cue_count": len(old_cues),
        "full_release_cue_count": len(release_cues),
        "final_delivery_cue_count": len(delivery_cues),
        "rows": rows,
    }
    if coordinate_values[0] is None:
        receipt["padded_source_interval"] = {"start_ms": padded_start_ms, "end_ms": padded_end_ms}
        receipt["final_delivery_boundary"] = {"start_ms": final_start_ms, "end_ms": final_end_ms}
    else:
        receipt["coordinate_contract"] = {
            "schema_version": _DELIVERY_PROJECTION_COORDINATE_SCHEMA,
            "baseline_source_interval": {"start_ms": baseline_source_start_ms, "end_ms": baseline_source_end_ms},
            "baseline_delivery_crop": {"start_ms": baseline_crop_start_ms, "end_ms": baseline_crop_end_ms},
            "record_padded_source_interval": {"start_ms": padded_start_ms, "end_ms": padded_end_ms},
            "record_final_delivery_boundary": {"start_ms": final_start_ms, "end_ms": final_end_ms},
            "baseline_to_record_padded_offset_ms": baseline_source_start_ms - padded_start_ms,
        }
    return receipt


def replay_full_window_then_crop(
    *,
    recut_dir: Path,
    cid: str,
    sanitized: Sequence[SourceCue],
    binding: V2RedeliverySourceBinding,
    config: Mapping[str, object],
    spec_parent: Path,
    protected_windows: Sequence[tuple[int, int]],
    final_start_ms: int,
    final_end_ms: int,
    subtitle_path: Path,
    write_source_range_srt: Callable[[Sequence[SourceCue], int, int, Path], None],
    candidate_id: str | None = None,
    recording_date: str | None = None,
) -> tuple[str, dict]:
    """Replay the sealed full interval and atomically clean its private temp."""

    full_path = recut_dir / f".{cid}.baseline-full-window.srt"
    try:
        os.lstat(full_path)
    except FileNotFoundError:
        pass
    else:
        raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_TEMP_PATH_EXISTS")
    owns_full_path = True
    try:
        write_source_range_srt(
            sanitized,
            binding.padded_content_start_ms,
            binding.padded_content_end_ms,
            full_path,
        )
        current_text = full_path.read_text(encoding="utf-8")
        output_text, audit = apply_redelivery_subtitle_baseline(
            current_text,
            config=config,
            spec_parent=spec_parent,
            protected_windows=translate_final_local_protected_windows(
                protected_windows,
                final_start_ms=final_start_ms,
                padded_content_start_ms=binding.padded_content_start_ms,
            ),
            current_source_start_ms=binding.content_absolute_start_ms,
            current_source_end_ms=binding.content_absolute_end_ms,
            current_source_recording_basename=binding.source_recording_basename,
            current_source_sha256=binding.source_sha256,
            candidate_id=candidate_id,
            recording_date=recording_date,
        )
        if audit.get("status") != "FAILED":
            full_cues = [
                SourceCue(
                    f"redelivery_full_{index:04d}",
                    binding.padded_content_start_ms + cue.start_ms,
                    binding.padded_content_start_ms + cue.end_ms,
                    cue.text.strip(), "zh", "speech", 1.0,
                )
                for index, cue in enumerate(parse_srt_cues(output_text), start=1)
                if cue.text.strip()
            ]
            write_source_range_srt(full_cues, final_start_ms, final_end_ms, subtitle_path)
            output_text = subtitle_path.read_text(encoding="utf-8")
            audit["final_delivery_projection"] = {
                "absolute_source_start_ms": binding.absolute_source_start_ms,
                "absolute_source_end_ms": binding.absolute_source_end_ms,
            }
            audit["exact_replay_then_final_crop"] = True
            project_full_window_audit_to_final_delivery(
                audit, final_start_ms=final_start_ms, final_end_ms=final_end_ms
            )
        return output_text, audit
    finally:
        if owns_full_path:
            try:
                info = os.lstat(full_path)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_TEMP_PATH_UNSAFE")
                full_path.unlink()


def replay_baseline_for_final_recut(
    *,
    truth_audit: Mapping[str, object],
    recut_dir: Path,
    cid: str,
    sanitized: Sequence[SourceCue],
    binding: V2RedeliverySourceBinding | None,
    config: Mapping[str, object],
    spec_parent: Path,
    final_start_ms: int,
    final_end_ms: int,
    subtitle_path: Path,
    write_source_range_srt: Callable[[Sequence[SourceCue], int, int, Path], None],
    recording_date: str | None = None,
) -> tuple[str, dict]:
    """Apply either final-local or exact full-window baseline authority."""

    protected: list[tuple[int, int]] = []
    for key in ("applied", "satisfied"):
        rows = truth_audit.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or row.get("action") != "drop_cue":
                continue
            for owner_start, owner_end in source_truth_owner_windows(row):
                start_ms = max(0, owner_start - final_start_ms)
                end_ms = min(final_end_ms - final_start_ms, owner_end - final_start_ms)
                if start_ms < end_ms:
                    protected.append((start_ms, end_ms))
    try:
        time_domain = operator_v3_time_domain(config)
    except RedeliveryTimeDomainError as exc:
        raise FullWindowReplayError(str(exc)) from exc
    exact_replay = exact_full_window_replay_enabled(config, binding)
    piece_local_fallback = bool(
        time_domain == PIECE_LOCAL
        and isinstance(binding, V2RedeliverySourceBinding)
        and config.get("exact_interval_replay") is True
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (
                config.get("absolute_source_start_ms"),
                config.get("absolute_source_end_ms"),
                binding.content_absolute_start_ms,
                binding.content_absolute_end_ms,
            )
        )
        and (
            config.get("absolute_source_start_ms")
            != binding.content_absolute_start_ms
            or config.get("absolute_source_end_ms")
            != binding.content_absolute_end_ms
        )
        and binding.content_absolute_start_ms < binding.content_absolute_end_ms
    )
    if exact_replay:
        _require_v3_time_domain_binding(config, binding)
        assert binding is not None
        return replay_full_window_then_crop(
            recut_dir=recut_dir,
            cid=cid,
            sanitized=sanitized,
            binding=binding,
            config=config,
            spec_parent=spec_parent,
            protected_windows=protected,
            final_start_ms=final_start_ms,
            final_end_ms=final_end_ms,
            subtitle_path=subtitle_path,
            write_source_range_srt=write_source_range_srt,
            candidate_id=cid,
            recording_date=recording_date,
        )
    if time_domain == DELIVERY_LOCAL or (
        time_domain == PIECE_LOCAL and not piece_local_fallback
    ):
        _require_v3_time_domain_binding(config, binding)
    current_start = binding.absolute_source_start_ms if binding else None
    current_end = binding.absolute_source_end_ms if binding else None
    return apply_redelivery_subtitle_baseline(
        subtitle_path.read_text(encoding="utf-8"),
        config=config,
        spec_parent=spec_parent,
        protected_windows=protected,
        current_source_start_ms=current_start,
        current_source_end_ms=current_end,
        current_source_recording_basename=(binding.source_recording_basename if binding else None),
        current_source_sha256=(binding.source_sha256 if binding else None),
        candidate_id=cid,
        recording_date=recording_date,
    )


def replay_full_window_text_and_crop(
    *,
    text: str,
    config: Mapping[str, object],
    spec_parent: Path,
    padded_start_ms: int,
    padded_end_ms: int,
    final_start_ms: int,
    final_end_ms: int,
    write_source_range_srt: Callable[[Sequence[SourceCue], int, int, Path], None],
    crop_path: Path,
    read_crop: Callable[[Path], bytes],
    projection_receipt_path: Path | None = None,
    projection_candidate_id: str | None = None,
    projection_record_sha256: str | None = None,
    projection_record_boundary: Mapping[str, object] | None = None,
    projection_staged_media_sha256: str | None = None,
    projection_lane_bytes: Mapping[str, bytes] | None = None,
    c5_start_clamp_proposal_path: Path | None = None,
    c5_start_clamp_acceptance_path: Path | None = None,
    recording_date: str | None = None,
    delivery_projection_padded_start_ms: int | None = None,
    delivery_projection_padded_end_ms: int | None = None,
    delivery_projection_final_start_ms: int | None = None,
    delivery_projection_final_end_ms: int | None = None,
    delivery_projection_baseline_start_ms: int | None = None,
    delivery_projection_baseline_end_ms: int | None = None,
    delivery_projection_baseline_crop_start_ms: int | None = None,
    delivery_projection_baseline_crop_end_ms: int | None = None
) -> tuple[bytes, dict]:
    """Apply an exact padded baseline and return a deterministic final crop."""

    try:
        time_domain = operator_v3_time_domain(config)
    except RedeliveryTimeDomainError as exc:
        raise FullWindowReplayError(str(exc)) from exc
    if time_domain == DELIVERY_LOCAL:
        raise FullWindowReplayError(
            "REDELIVERY_DELIVERY_LOCAL_BASELINE_CANNOT_REPLAY_FULL_WINDOW"
        )
    if time_domain == PIECE_LOCAL and (
        config.get("absolute_source_start_ms") != padded_start_ms
        or config.get("absolute_source_end_ms") != padded_end_ms
    ):
        raise FullWindowReplayError("REDELIVERY_BASELINE_PIECE_INTERVAL_MISMATCH")

    reviewed, audit = apply_redelivery_subtitle_baseline(
        text,
        config=config,
        spec_parent=spec_parent,
        current_source_start_ms=padded_start_ms,
        current_source_end_ms=padded_end_ms,
        current_source_recording_basename=str(config["source_recording_basename"]),
        current_source_sha256=str(config["source_sha256"]),
        candidate_id=projection_candidate_id,
        recording_date=recording_date,
    )
    if audit.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        raise FullWindowReplayError("REPLAY_BASELINE_APPLICATION_FAILED")
    try:
        cues = _fresh_srt_to_source_cues(
            reviewed, window_start_ms=0, duration_ms=padded_end_ms - padded_start_ms
        )
    except ValueError as exc:
        raise FullWindowReplayError("REPLAY_REVIEWED_BASELINE_GEOMETRY_INVALID") from exc
    write_source_range_srt(cues, final_start_ms, final_end_ms, crop_path)
    try:
        cropped = read_crop(crop_path)
        projection_inputs = (
            projection_receipt_path, projection_candidate_id,
            projection_record_sha256, projection_record_boundary,
            projection_staged_media_sha256, projection_lane_bytes,
        )
        if any(value is not None for value in projection_inputs):
            if not all(value is not None for value in projection_inputs):
                raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_INPUT_INVALID")
            projection_bounds = (
                delivery_projection_padded_start_ms,
                delivery_projection_padded_end_ms,
                delivery_projection_final_start_ms,
                delivery_projection_final_end_ms,
            )
            if any(value is not None for value in projection_bounds) and not all(
                value is not None for value in projection_bounds
            ):
                raise FullWindowReplayError("REDELIVERY_DELIVERY_PROJECTION_INPUT_INVALID")
            receipt = _build_full_release_delivery_projection_receipt(
                candidate_id=str(projection_candidate_id),
                record_sha256=str(projection_record_sha256),
                record_boundary=projection_record_boundary,
                padded_start_ms=(delivery_projection_padded_start_ms
                                 if delivery_projection_padded_start_ms is not None else padded_start_ms),
                padded_end_ms=(delivery_projection_padded_end_ms
                               if delivery_projection_padded_end_ms is not None else padded_end_ms),
                final_start_ms=(delivery_projection_final_start_ms
                                if delivery_projection_final_start_ms is not None else final_start_ms),
                final_end_ms=(delivery_projection_final_end_ms
                              if delivery_projection_final_end_ms is not None else final_end_ms),
                diagnostic_text=text, full_release_text=reviewed,
                delivery_bytes=cropped, config=config,
                staged_media_sha256=str(projection_staged_media_sha256),
                lane_bytes=projection_lane_bytes,
                c5_start_clamp_proposal_path=c5_start_clamp_proposal_path,
                c5_start_clamp_acceptance_path=c5_start_clamp_acceptance_path,
                recording_date=recording_date,
                baseline_source_start_ms=delivery_projection_baseline_start_ms,
                baseline_source_end_ms=delivery_projection_baseline_end_ms,
                baseline_crop_start_ms=delivery_projection_baseline_crop_start_ms,
                baseline_crop_end_ms=delivery_projection_baseline_crop_end_ms,
            )
            if receipt is not None:
                assert projection_receipt_path is not None
                receipt_sha = _private_create_only_json(projection_receipt_path, receipt)
                audit["full_release_delivery_projection"] = {
                    "schema_version": receipt["schema_version"],
                    "receipt_sha256": receipt_sha,
                    "full_release_cue_count": receipt["full_release_cue_count"],
                    "final_delivery_cue_count": receipt["final_delivery_cue_count"],
                }
        return cropped, audit
    finally:
        crop_path.unlink(missing_ok=True)
