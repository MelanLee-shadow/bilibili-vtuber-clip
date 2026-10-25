"""Windowed concurrent slice production with deploy-yield.

公开规则部署优先令（「一个切片结束了立刻部署最新版」）：马拉松
tick 整批持锁会让部署排队数小时。派发窗口每次派新活前检查 deploy.guard，
在场即停派（在飞的做完），本 tick 提前收官让位；未派发候选状态未动，
下个 tick（新代码）自然续跑。全部依赖注入，runner 侧同名 wrapper 保持
monkeypatch 面（BASE/MAX_PARALLEL_PRODUCE 仍在 runner 全局）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait as _futures_wait,
)
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.final_review_provider_budget_retry import (
    carry_validated_active_provider_budget_ledger,
)
from src.autoslice.final_review_carryover import (
    carryover_path,
    load_replayable_final_review_carryover,
)
from src.autoslice.semantic_scorecard_refresh_receipt import copied_refresh_receipt
from src.autoslice.selected_source_fact_recovery import (
    RECOVERY_RECEIPT_FIELD as SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD,
)
from src.autoslice.selected_final_review_recovery import (
    RECOVERY_RECEIPT_FIELD as SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD,
)
from src.autoslice.selected_final_review_terminal_regrant import (
    RECOVERY_RECEIPT_FIELD as FINAL_REVIEW_TERMINAL_REGRANT_RECEIPT_FIELD,
)
from src.autoslice import runtime_storage
from src.autoslice.producer_batch_projection import prepared_batch_entry
from src.autoslice.producer_batch_transaction import (
    ProducerBatchTransactionError,
    bind_prepared_result_inputs,
    checkpoint_prepared_result,
    load_prepared_result_checkpoint,
)
from src.autoslice.producer_delivery_transaction import ProducerDeliveryTransactionError

_FAILED_ITEM_PASSTHROUGH_KEYS = (
    "talk_dependency_snapshot",
    "song_dependency_fingerprint",
    "hook",
    "confidence",
    "selection_scorecard",
    "session_relation_authority",
    "danmaku",
    "preview",
    "segment_path",
    "seg_dur_ms",
    "start_ms",
    "end_ms",
    "lane",
    "title_hint",
    "visual_song_evidence",
    "selected_repair",
    "talk_repair_retry_count",
    "talk_transient_retry_count",
    "final_review_carryover_consumed_fingerprints",
    "retry_reason",
    "anchor_start_ms",
    "anchor_end_ms",
    "transient_retry_count",
    "session_id",
    "cover_diversity_slot",
    "cover_route_regeneration_fingerprint",
    "cover_route_regeneration_attempts",
    "recovery_source_record_sha256",
)


def _source_key(value: object) -> str | None:
    """Identify a source lexically without touching a possibly-fragile FUSE."""
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.fspath(value).strip()
    if not raw:
        return None
    # Basename intentionally aliases legacy ``segment`` and absolute paths.
    # A false collision only costs parallelism; a false split can break FUSE.
    return os.path.basename(os.path.normpath(raw)) or None


def _source_affinity(item: Mapping[str, object]) -> tuple[frozenset[str], bool]:
    """Extract every declared source segment and whether identity is unknown.

    Current runner queues use one ``segment_path``.  Recovery/spec-shaped work
    may additionally declare multiple source pieces, so all such paths form a
    conflict set.  A malformed *declared* source identity is fail-closed and
    must run alone; a genuinely source-less generic work item remains
    independent so it does not turn the whole dispatcher into a serial lane.
    """

    path_values: list[object] = []
    declared = False
    malformed = False
    for field in (
        "segment_path",
        "segment",
        "source_path",
        "source_segment_path",
        "source_media_path",
        "remote_media",
    ):
        if field not in item:
            continue
        declared = True
        path_values.append(item[field])
    for field in (
        "segment_paths",
        "source_paths",
        "source_segments",
        "source_piece_segments",
    ):
        if field not in item:
            continue
        declared = True
        values = item[field]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            malformed = True
        else:
            path_values.extend(values)
    for field in ("pieces", "source_pieces"):
        if field not in item:
            continue
        declared = True
        pieces = item[field]
        if not isinstance(pieces, Sequence) or isinstance(pieces, (str, bytes, bytearray)):
            malformed = True
            continue
        for piece in pieces:
            if isinstance(piece, (str, os.PathLike)):
                path_values.append(piece)
                continue
            if not isinstance(piece, Mapping):
                malformed = True
                continue
            piece_paths = [
                piece[path_field]
                for path_field in (
                    "remote_media",
                    "segment_path",
                    "segment",
                    "source_path",
                    "source_segment_path",
                    "source_media_path",
                    "media_path",
                    "path",
                )
                if path_field in piece
            ]
            # A timing-only piece inherits the top-level segment path.  If no
            # top-level source exists, however, its source identity is unknown.
            if not piece_paths and not path_values:
                malformed = True
            path_values.extend(piece_paths)

    keys = {_source_key(value) for value in path_values}
    if None in keys:
        keys.remove(None)
        malformed = True
    return frozenset(keys), bool(declared and (malformed or not keys))


def _capacity_item(
    item: Mapping[str, object],
    *,
    talk_window_pre_ms: int,
    talk_window_post_ms: int,
    song_window_pre_ms: int,
    song_window_post_ms: int,
) -> dict[str, object]:
    """Project the bounded media window that admission will actually process.

    Queue rows keep the source recording path for affinity and the semantic
    anchor for state.  They do not represent the entire recording's allocation
    peak.  The producer's known context padding is therefore materialized as a
    private capacity field before the conservative per-second estimate.
    """

    projected = dict(item)

    def integer(name: str) -> int | None:
        value = item.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    anchor_start, anchor_end = integer("anchor_start_ms"), integer("anchor_end_ms")
    if anchor_start is not None and anchor_end is not None and anchor_end >= anchor_start:
        start = max(0, anchor_start - song_window_pre_ms)
        end = anchor_end + song_window_post_ms
        segment_duration = integer("seg_dur_ms")
        if segment_duration is not None:
            end = min(segment_duration, end)
        projected["capacity_duration_ms"] = max(0, end - start)
        return projected

    start, end = integer("start_ms"), integer("end_ms")
    if start is not None and end is not None and end >= start:
        start = max(0, start - talk_window_pre_ms)
        end += talk_window_post_ms
        given_end = integer("given_end_ms")
        if given_end is not None:
            end += max(0, given_end - integer("end_ms"))
        segment_duration = integer("seg_dur_ms")
        if segment_duration is not None:
            end = min(segment_duration, end)
        projected["capacity_duration_ms"] = max(0, end - start)
    return projected


def _timeout_final_review_carryover(
    *, base: Path, date: str, item: Mapping[str, object]
) -> tuple[str, dict[str, object]] | None:
    candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
    if not candidate_id:
        return None
    out_root = base / "out" / date
    candidate_dir = out_root / candidate_id
    carryover = carryover_path(candidate_dir, candidate_id)
    try:
        if not candidate_dir.is_dir():
            return None
        rows = load_replayable_final_review_carryover(carryover)
    except (OSError, TypeError, ValueError):
        return None
    if not rows:
        return None
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return fingerprint, {
        "schema_version": "talk-final-review-timeout-evidence.v1",
        "candidate_id": candidate_id,
        "carryover": {
            "file": carryover.name,
            "observed_count": len(rows),
            "rows_sha256": fingerprint,
            "retry_ready": True,
        },
    }


def _prepared_result_input_binding(
    *, base: Path, date: str, lane: str, item: Mapping[str, object], fingerprint: str,
) -> dict[str, object]:
    """Bind an interrupted completion to its unchanged queue and source files.

    Input text/sidecars are hashed. Media keeps device/inode/size/mtime/ctime
    identity so resuming a result does not reread multi-GiB recordings. The
    prepared transaction still verifies every actual artifact hash at commit.
    """

    paths: set[Path] = set()
    media: set[Path] = set()
    media_keys = {
        "segment_path", "segment", "source_path", "source_segment_path",
        "source_media_path", "remote_media", "media_path",
    }

    def collect(value: object, key: str = "") -> None:
        if isinstance(value, Mapping):
            for name, child in value.items():
                collect(child, str(name))
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child, key)
        elif isinstance(value, (str, os.PathLike)):
            path = Path(value)
            if path.is_absolute() and (
                key in media_keys or key in {"xml", "chat_jsonl", "chat_jsonl_local", "danmaku_xml_local", "bcut_srt_local"}
                or key.endswith(("_path", "_paths"))
            ):
                paths.add(path)
                if key in media_keys or path.suffix.lower() in {".mp4", ".flv", ".mkv", ".mov", ".wav", ".m4a"}:
                    media.add(path)

    collect(item)
    for path in tuple(media):
        # These are the same implicit source sidecars/cache used by discovery
        # and Talk construction; absence is also part of the identity.
        paths.add(base / "cache" / date / f"{path.stem}.bcut.srt")
        paths.update(path.with_suffix(suffix) for suffix in (".xml", ".json", ".jsonl", ".chat.json", ".danmaku.json"))
    try:
        snapshots = bind_prepared_result_inputs({path: path in media for path in paths})
    except OSError as exc:
        raise ProducerBatchTransactionError("producer checkpoint inputs are unavailable") from exc
    return {
        "date": date, "lane": lane, "item": copy.deepcopy(dict(item)),
        "pipeline_fingerprint": fingerprint, "source_files": snapshots,
    }


def _completed_source_dependencies(*, base: Path, date: str, candidate_id: str) -> dict:
    """Retain the final spec and inputs added during boundary construction.

    The request key is captured before dispatch; the final spec can be created
    or widened by the producer. Its source pieces and text authorities must
    also stay unchanged before a completed result may bypass production.
    """

    spec_path = base / "out" / date / f"spec_{candidate_id}.json"
    if not spec_path.exists():
        return {}
    try:
        spec = json.loads(spec_path.read_bytes())
        if not isinstance(spec, dict):
            raise ValueError("spec is not an object")
        # Reuse the request snapshot projection to cover actual piece sources,
        # and bind additional text-authority fields that lack a _path suffix.
        for name in ("subtitle_text_overrides", "subtitle_regression", "speaker_overrides"):
            if name in spec:
                spec[name + "_path"] = spec[name]
        dependencies = _prepared_result_input_binding(
            base=base, date=date, lane="completion", item=spec, fingerprint="",
        )["source_files"]
        dependencies.update(bind_prepared_result_inputs({spec_path: False}))
        return dependencies
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ProducerBatchTransactionError("producer final spec cannot be bound") from exc


def produce_batch_windowed(
    date: str,
    items: Sequence[dict],
    produce_fn: Callable[..., dict],
    *,
    produce_talk_fn: Callable[..., dict],
    produce_song_fn: Callable[..., dict],
    base: Path,
    max_parallel: int,
    min_free_bytes: int = 0,
    free_bytes_fn: Callable[[Path], int] | None = None,
    operational_reserve_bytes: int | None = None,
    peak_bytes_per_second: int | None = None,
    log: Callable[[str], None],
    talk_pipeline_fingerprint: Callable[[str], str],
    pipeline_fingerprint: Callable[[], str],
    song_pipeline_fingerprint: Callable[[], str],
    song_window_pre_ms: int,
    song_window_post_ms: int,
    talk_window_pre_ms: int = 0,
    talk_window_post_ms: int = 0,
    live_hold_active_fn: Callable[[], bool] | None = None,
    stop_requested_fn: Callable[[], bool] | None = None,
    prepared_result_dependency_fingerprint: Callable[[str, dict, str], str] | None = None,
    capture_talk_dependency_snapshot: Callable[[str, dict], dict] | None = None,
    capture_song_dependency_fingerprint: Callable[[str, dict], str] | None = None,
    prepare_only: bool = False,
    on_result: Callable[[int, dict, dict], bool | None] | None = None,
    execution_backend: Any = None,
) -> list[dict]:
    """Produce ``items`` concurrently, preserving input order.

    Each slice is an independent subprocess, so threads just wait on those; a
    crash in one becomes a failed result and never kills the batch.  Targeted
    talk repairs may opt into the talk lane's ``reuse_cover`` path through
    their persisted queue item.  When ``on_result`` is supplied, each
    contiguous completed input-prefix result is delivered to it before the
    dispatcher schedules another item.  Returning ``False`` yields the rest
    of the queue while still draining already in-flight work.
    """

    producer_calls: list[str] = []
    recovered_prepares: list[str] = []
    started_at = time.monotonic()

    def prepared_fingerprint(item: dict, lane: str) -> str:
        if prepared_result_dependency_fingerprint is not None:
            return prepared_result_dependency_fingerprint(date, item, lane)
        return talk_pipeline_fingerprint(str(item.get("cid") or item.get("candidate_id") or "")) if lane == "talk" else song_pipeline_fingerprint()

    def retained_result(item: dict) -> tuple[dict | None, dict | None, str | None, str | None]:
        lane = "talk" if produce_fn is produce_talk_fn else "song" if produce_fn is produce_song_fn else None
        if not prepare_only or lane is None:
            return None, None, None, None
        candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
        fingerprint = prepared_fingerprint(item, lane)
        binding = _prepared_result_input_binding(
            base=base, date=date, lane=lane, item=item, fingerprint=fingerprint,
        )
        retained = load_prepared_result_checkpoint(
            runtime_root=base, lane=lane, candidate_id=candidate_id, binding=binding,
        )
        return retained, binding, lane, fingerprint

    def _one(item: dict, scratch_dir: Path | None = None, execution_lease: Any = None) -> dict:
        # The scratch path is allocated and owned by the dispatcher.  Pass a
        # shallow copy so the queue/state item itself never acquires a
        # lifecycle-only field.
        work_item = dict(item)
        if scratch_dir is not None:
            work_item["runtime_scratch_dir"] = str(scratch_dir)
        try:
            produce_kwargs = {"prepare_only": True} if prepare_only else {}
            retained, binding, lane, fingerprint = retained_result(item)
            candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
            if retained is not None:
                fresh_binding = _prepared_result_input_binding(
                    base=base, date=date, lane=lane, item=item,
                    fingerprint=prepared_fingerprint(item, lane),
                )
                if fresh_binding != binding:
                    raise ProducerBatchTransactionError("producer checkpoint input changed during recovery")
                recovered_prepares.append(candidate_id)
                log(f"{candidate_id}: recovered completed prepare before state checkpoint; producer calls=0")
                return retained
            dependency_fields = {}
            if produce_fn is produce_talk_fn and capture_talk_dependency_snapshot is not None:
                dependency_fields["talk_dependency_snapshot"] = capture_talk_dependency_snapshot(date, item)
            elif produce_fn is produce_song_fn and capture_song_dependency_fingerprint is not None:
                dependency_fields["song_dependency_fingerprint"] = capture_song_dependency_fingerprint(date, item)
            work_item.update(copy.deepcopy(dependency_fields))
            if produce_fn is produce_talk_fn and work_item.get("published_cover_carry_required") is True:
                carry = work_item.get("published_cover_carry")
                from src.autoslice.published_cover_carry import validate_materialized_marker
                if not validate_materialized_marker(
                    carry, base=base, date=date, candidate_id=str(work_item.get("cid") or "")
                ):
                    raise ValueError("PUBLISHED_COVER_CARRY_MARKER_INVALID")
                produce_kwargs["reuse_cover"] = True
            elif produce_fn is produce_talk_fn and work_item.get("reuse_cover"):
                # Historical bare reuse remains supported only when it never
                # asserted this stricter, typed published-carry policy.
                produce_kwargs["reuse_cover"] = True
            producer_calls.append(candidate_id)
            result = (
                execution_backend.execute(execution_lease, produce_fn, date, work_item, produce_kwargs)
                if execution_backend is not None
                else produce_fn(date, work_item, **produce_kwargs)
            )
            if not isinstance(result, dict):
                raise ValueError("producer returned no result object")
            result.update(copy.deepcopy(dependency_fields))
            if work_item.get("session_id"):
                result.setdefault("session_id", work_item["session_id"])
            if binding is not None and result.get("status") == "delivery_prepared_no_target":
                fresh_binding = _prepared_result_input_binding(
                    base=base, date=date, lane=lane, item=item,
                    fingerprint=prepared_fingerprint(item, lane),
                )
                if fresh_binding != binding:
                    raise ProducerBatchTransactionError("producer checkpoint source changed during preparation")
                try:
                    entry = prepared_batch_entry(
                        runtime_root=base, lane=lane, candidate_id=candidate_id, result=result,
                    )
                except ValueError as exc:
                    raise ProducerBatchTransactionError("producer prepared handle is invalid") from exc
                if entry is None:
                    raise ProducerBatchTransactionError("producer checkpoint prepared handle is missing")
                checkpoint_prepared_result(
                    entry=entry, binding=binding, result=result,
                    source_dependencies=_completed_source_dependencies(
                        base=base, date=date, candidate_id=candidate_id,
                    ),
                )
            return result
        except (ProducerBatchTransactionError, ProducerDeliveryTransactionError):
            # A corrupt/missing completion receipt is a persistence failure,
            # never a content failure that can safely advance the lane.
            raise
        except (Exception, SystemExit) as exc:  # one bad slice must not kill the batch
            log(f"produce crashed for {work_item.get('cid')}: {exc}")
            timeout_carryover = (
                _timeout_final_review_carryover(base=base, date=date, item=work_item)
                if produce_fn is produce_talk_fn
                and isinstance(exc, subprocess.TimeoutExpired)
                else None
            )
            reason_code = (
                "PRODUCE_EXIT_WITHOUT_RESULT"
                if isinstance(exc, SystemExit)
                else "PRODUCE_TIMEOUT_FINAL_REVIEW_CARRYOVER"
                if timeout_carryover is not None
                else "PRODUCE_TIMEOUT"
                if produce_fn is produce_talk_fn
                and isinstance(exc, subprocess.TimeoutExpired)
                else "PRODUCE_UNEXPECTED_EXCEPTION"
            )
            result = {
                "candidate_id": work_item.get("cid"),
                "rc": -1,
                "status": "failed",
                "error": str(exc),
                "reason_codes": [reason_code],
                **(
                    {"failure_recoverable": True}
                    if produce_fn is produce_talk_fn
                    else {}
                ),
                **(
                    {
                        "failure_kind": "subtitle_authority",
                        "failure_stage": "final_review_carryover",
                        "failure_fingerprint": timeout_carryover[0],
                        "failure_evidence": timeout_carryover[1],
                    }
                    if timeout_carryover is not None
                    else {}
                ),
                "pipeline_fingerprint": (
                    talk_pipeline_fingerprint(str(item.get("cid") or ""))
                    if produce_fn is produce_talk_fn
                    else pipeline_fingerprint()
                ),
                **(
                    {"song_pipeline_fingerprint": song_pipeline_fingerprint()}
                    if produce_fn is produce_song_fn
                    else {}
                ),
                **{key: work_item[key] for key in _FAILED_ITEM_PASSTHROUGH_KEYS if key in work_item},
                **copied_refresh_receipt(work_item),
            }
            carry_validated_active_provider_budget_ledger(
                work_item,
                result,
                candidate_id=str(work_item.get("cid") or work_item.get("candidate_id") or ""),
            )
            if work_item.get("segment_path"):
                result["segment"] = Path(str(work_item["segment_path"])).name
            anchor_start = work_item.get("anchor_start_ms")
            anchor_end = work_item.get("anchor_end_ms")
            if isinstance(anchor_start, int) and isinstance(anchor_end, int):
                result["start_ms"] = max(0, anchor_start - song_window_pre_ms)
                result["end_ms"] = anchor_end + song_window_post_ms
            source_fact_receipt = work_item.get(
                SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD
            )
            if isinstance(source_fact_receipt, Mapping):
                # Runner-side backfill/bundle projection has not happened yet.
                # Carry an independent queue receipt; the historical recovery
                # commit seam advances it against the exact final pick before
                # any state write.
                result[SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD] = (
                    copy.deepcopy(dict(source_fact_receipt))
                )
            final_review_receipt = work_item.get(
                SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD
            )
            if isinstance(final_review_receipt, Mapping):
                result[SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD] = (
                    copy.deepcopy(dict(final_review_receipt))
                )
            terminal_regrant = work_item.get(FINAL_REVIEW_TERMINAL_REGRANT_RECEIPT_FIELD)
            if isinstance(terminal_regrant, Mapping):
                result[FINAL_REVIEW_TERMINAL_REGRANT_RECEIPT_FIELD] = copy.deepcopy(
                    dict(terminal_regrant)
                )
            return result

    if not items:
        return []
    if isinstance(min_free_bytes, bool) or not isinstance(min_free_bytes, int) or min_free_bytes < 0:
        raise ValueError("min_free_bytes must be a non-negative integer")
    if operational_reserve_bytes is None:
        operational_reserve_bytes = runtime_storage.parse_operational_reserve_bytes()
    if peak_bytes_per_second is None:
        peak_bytes_per_second = runtime_storage.parse_peak_bytes_per_second()
    if operational_reserve_bytes is not None and (
        isinstance(operational_reserve_bytes, bool)
        or not isinstance(operational_reserve_bytes, int)
        or operational_reserve_bytes < 0
    ):
        raise ValueError("operational_reserve_bytes must be a non-negative integer")
    if peak_bytes_per_second is not None and (
        isinstance(peak_bytes_per_second, bool)
        or not isinstance(peak_bytes_per_second, int)
        or peak_bytes_per_second < 0
    ):
        raise ValueError("peak_bytes_per_second must be a non-negative integer")
    for name, value in (
        ("talk_window_pre_ms", talk_window_pre_ms),
        ("talk_window_post_ms", talk_window_post_ms),
        ("song_window_pre_ms", song_window_pre_ms),
        ("song_window_post_ms", song_window_post_ms),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    workers = min(max_parallel, len(items))
    log(f"producing {len(items)} slice(s), up to {workers} in parallel")
    deploy_guard = base / "deploy.guard"
    free_bytes = free_bytes_fn or (lambda path: shutil.disk_usage(path).free)
    results_by_index: dict[int, dict] = {}
    ready_by_index: dict[int, dict] = {}
    completed_leases: dict[int, runtime_storage.ScratchLease] = {}
    result_prefix: list[dict] = []
    next_callback_index = 0
    queue = list(enumerate(items))
    in_flight: dict[Any, tuple[int, frozenset[str], bool, runtime_storage.Reservation | None, runtime_storage.ScratchLease]] = {}
    active_source_keys: set[str] = set()
    exclusive_source_active = False
    callback_gap_hold = False
    deploy_yield = False
    yield_reason: str | None = None
    capacity_controller = runtime_storage.CapacityAdmission(
        runtime_base=base,
        free_bytes_fn=free_bytes,
        operational_reserve_bytes=(
            operational_reserve_bytes
        ),
        pressure_threshold_bytes=min_free_bytes,
        peak_bytes_per_second=(
            peak_bytes_per_second
        ),
        log=log,
        date=date,
    )

    # Clean only leases left by this runtime's own completed consumers.  The
    # registry contains no source, queue, package, or caller-nominated paths.
    try:
        runtime_storage.collect_ephemeral(base, reason="stage-start")
    except runtime_storage.RuntimeStorageError as exc:
        log(f"runtime ephemeral GC deferred at stage start: {exc}")

    def _lease_failed(result: Mapping[str, object], *, callback_error: bool = False) -> bool:
        if callback_error:
            return True
        if result.get("failure_recoverable") is True or result.get("resumable") is True:
            return True
        if result.get("rc") not in (None, 0):
            return True
        return str(result.get("status") or "") in {
            "failed",
            "candidate_rejected",
            "boundary_unrepairable",
            "blocked",
            "delivery_quarantined",
            "quarantine",
            "retry",
            "retryable",
            "resumable",
        }

    def _release_completed_lease(
        index: int,
        lease: runtime_storage.ScratchLease,
        result: Mapping[str, object],
        *,
        callback_error: bool = False,
    ) -> None:
        unreleased_leases.pop(index, None)
        try:
            # A failed or resumable result retains its workspace as evidence;
            # successful consumers release it for the next deterministic GC.
            lease.release(failed=_lease_failed(result, callback_error=callback_error))
        except runtime_storage.RuntimeStorageError as exc:
            log(f"runtime ephemeral release deferred for {lease.path}: {exc}")
        try:
            runtime_storage.collect_ephemeral(base, reason="between-completed-items")
        except runtime_storage.RuntimeStorageError as exc:
            log(f"runtime ephemeral GC deferred between items: {exc}")

    unreleased_leases: dict[int, runtime_storage.ScratchLease] = {}
    dispatched_indices: set[int] = set()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while queue or in_flight:
                while (
                    queue
                    and len(in_flight) < workers
                    and not deploy_yield
                    and not callback_gap_hold
                ):
                    if stop_requested_fn is not None and stop_requested_fn():
                        deploy_yield = True
                        yield_reason = "OPERATOR_STOP"
                        log(
                            "operator stop present — yielding after "
                            f"{len(in_flight)} in-flight item(s), "
                            f"{len(queue)} deferred; pending work retained"
                        )
                        break
                    if deploy_guard.exists():
                        deploy_yield = True
                        yield_reason = "DEPLOY_GUARD"
                        log(
                            "deploy guard present — yielding tick after "
                            f"{len(in_flight)} in-flight item(s), "
                            f"{len(queue)} deferred to next tick"
                        )
                        break
                    # Same yield contract for a broadcast that starts mid-batch
                    # (a tick begun before the stream kept producing
                    # 37 minutes into it).  Undispatched items keep their state and
                    # the next tick re-picks them; in-flight items finish.
                    if live_hold_active_fn is not None and live_hold_active_fn():
                        deploy_yield = True
                        yield_reason = "LIVE_HOLD"
                        log(
                            "room went LIVE — yielding tick after "
                            f"{len(in_flight)} in-flight item(s), "
                            f"{len(queue)} deferred to next tick"
                        )
                        break
                    index, item = queue[0]
                    source_keys, source_unknown = _source_affinity(item)
                    if (
                        exclusive_source_active
                        or (source_unknown and bool(in_flight))
                        or bool(active_source_keys.intersection(source_keys))
                    ):
                        # Never skip a blocked head item.  The caller persists a
                        # deploy/live yield as an input-prefix result; dispatching
                        # a later item here could create an unrepresentable gap.
                        break
                    reservation: runtime_storage.Reservation | None = None
                    admission_details: dict[str, object] = {}
                    retained, _binding, _lane, _fingerprint = retained_result(item)
                    if capacity_controller is not None and retained is None:
                        try:
                            admission_item = _capacity_item(
                                item,
                                talk_window_pre_ms=talk_window_pre_ms,
                                talk_window_post_ms=talk_window_post_ms,
                                song_window_pre_ms=song_window_pre_ms,
                                song_window_post_ms=song_window_post_ms,
                            )
                            if execution_backend is None:
                                reservation, admission_details = capacity_controller.reserve_for_item(
                                    admission_item,
                                    candidate_id=str(item.get("cid") or item.get("candidate_id") or "") or None,
                                    date=date,
                                )
                            else:
                                reservation, admission_details = execution_backend.reserve(
                                    capacity_controller, date=date, item=item,
                                    admission_item=admission_item,
                                    lane="talk" if produce_fn is produce_talk_fn else "song",
                                    prepare_only=prepare_only,
                                )
                        except (runtime_storage.CapacityBudgetError, runtime_storage.CapacityProbeError) as exc:
                            deploy_yield = True
                            yield_reason = "CAPACITY_PROBE_FAILED"
                            log(
                                "disk capacity admission failed — yielding tick after "
                                f"{len(in_flight)} in-flight item(s), {len(queue)} deferred: {exc}"
                            )
                            break
                        if reservation is None:
                            deploy_yield = True
                            yield_reason = "CAPACITY_WAIT"
                            log(
                                "disk peak capacity unavailable — yielding tick after "
                                f"{len(in_flight)} in-flight item(s), {len(queue)} deferred "
                                f"(available={admission_details.get('available_bytes')} "
                                f"required={admission_details.get('required_bytes')})"
                            )
                            break
                    owner = str(item.get("cid") or item.get("candidate_id") or f"queue-{index}")
                    try:
                        lease = runtime_storage.create_ephemeral_workspace(base, owner=owner)
                    except runtime_storage.RuntimeStorageError as exc:
                        if reservation is not None:
                            reservation.release()
                        deploy_yield = True
                        yield_reason = "WORKSPACE_UNAVAILABLE"
                        log(f"runtime ephemeral workspace failed — yielding tick: {exc}")
                        break
                    unreleased_leases[index] = lease
                    queue.pop(0)
                    dispatched_indices.add(index)
                    if retained is not None:
                        recovered_prepares.append(owner)
                        log(f"{owner}: recovered completed prepare without new producer peak reservation; producer calls=0")
                        future = pool.submit(lambda value=retained: value)
                    else:
                        future = pool.submit(_one, item, lease.path, reservation)
                    in_flight[future] = (
                        index,
                        source_keys,
                        source_unknown,
                        reservation,
                        lease,
                    )
                    active_source_keys.update(source_keys)
                    if source_unknown:
                        exclusive_source_active = True
                if not in_flight:
                    break
                done, _pending = _futures_wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    index, source_keys, source_unknown, reservation, lease = in_flight.pop(future)
                    active_source_keys.difference_update(source_keys)
                    if source_unknown:
                        exclusive_source_active = False
                    try:
                        result = future.result()
                    finally:
                        if reservation is not None:
                            reservation.release()
                    results_by_index[index] = result
                    if on_result is not None:
                        ready_by_index[index] = result
                        completed_leases[index] = lease
                    else:
                        _release_completed_lease(index, lease, result)
                if on_result is not None:
                    while next_callback_index in ready_by_index:
                        result = ready_by_index.pop(next_callback_index)
                        lease = completed_leases.pop(next_callback_index)
                        callback_error = False
                        try:
                            should_continue = on_result(
                                next_callback_index, items[next_callback_index], result,
                            )
                        except BaseException:
                            callback_error = True
                            raise
                        finally:
                            _release_completed_lease(
                                next_callback_index, lease, result, callback_error=callback_error
                            )
                        result_prefix.append(result)
                        next_callback_index += 1
                        if should_continue is False:
                            deploy_yield = True
                            yield_reason = "CALLBACK_YIELD"
                    if ready_by_index and next_callback_index not in ready_by_index:
                        # A later future may finish while the input-prefix head is
                        # still running.  Do not launch another candidate past
                        # that durable checkpoint gap; wait for the head first.
                        callback_gap_hold = True
                    else:
                        callback_gap_hold = False
    except BaseException as exc:
        log(json.dumps({
            "schema_version": "producer-dispatch-outcome.v1", "date": date,
            "status": "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "CHECKPOINT_BLOCKED",
            "reason_code": type(exc).__name__, "completed_count": len(results_by_index),
            "checkpointed_count": len(result_prefix),
            "uncheckpointed_candidate_ids": [
                str(items[index].get("cid") or items[index].get("candidate_id") or "")
                for index in sorted(dispatched_indices - set(range(next_callback_index)))
            ],
            "deferred_candidate_ids": [str(item.get("cid") or item.get("candidate_id") or "") for _, item in queue],
            "producer_calls": len(producer_calls), "recovered_prepares": len(recovered_prepares),
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }, ensure_ascii=False, sort_keys=True))
        raise
    finally:
        # Pool shutdown drains every worker first. If a callback/persistence
        # failure interrupts this loop, preserve all unconsumed workspaces and
        # release their reservations; completed private receipts remain replayable.
        for _future, (index, _keys, _unknown, reservation, lease) in in_flight.items():
            if reservation is not None:
                reservation.release()
        for index, lease in list(unreleased_leases.items()):
            _release_completed_lease(index, lease, {}, callback_error=True)
    log(json.dumps({
        "schema_version": "producer-dispatch-outcome.v1", "date": date,
        "status": "NO_PROGRESS" if not results_by_index else "DEFERRED" if queue else "COMPLETED",
        "reason_code": yield_reason, "completed_count": len(results_by_index),
        "deferred_candidate_ids": [str(item.get("cid") or item.get("candidate_id") or "") for _, item in queue],
        "producer_calls": len(producer_calls), "recovered_prepares": len(recovered_prepares),
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
    }, ensure_ascii=False, sort_keys=True))
    if on_result is not None:
        return result_prefix
    return [results_by_index[index] for index in sorted(results_by_index)]


__all__ = ["produce_batch_windowed"]
