"""Windowed concurrent slice production with deploy-yield.

Ivan 2026-07-27 部署优先令（「一个切片结束了立刻部署最新版」）：马拉松
tick 整批持锁会让部署排队数小时。派发窗口每次派新活前检查 deploy.guard，
在场即停派（在飞的做完），本 tick 提前收官让位；未派发候选状态未动，
下个 tick（新代码）自然续跑。全部依赖注入，runner 侧同名 wrapper 保持
monkeypatch 面（BASE/MAX_PARALLEL_PRODUCE 仍在 runner 全局）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait as _futures_wait,
)
from pathlib import Path
from typing import Any, Mapping

_FAILED_ITEM_PASSTHROUGH_KEYS = (
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
    "retry_reason",
    "anchor_start_ms",
    "anchor_end_ms",
    "transient_retry_count",
    "session_id",
    "cover_diversity_slot",
    "cover_route_regeneration_fingerprint",
    "cover_route_regeneration_attempts",
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


def produce_batch_windowed(
    date: str,
    items: Sequence[dict],
    produce_fn: Callable[..., dict],
    *,
    produce_talk_fn: Callable[..., dict],
    produce_song_fn: Callable[..., dict],
    base: Path,
    max_parallel: int,
    log: Callable[[str], None],
    talk_pipeline_fingerprint: Callable[[str], str],
    pipeline_fingerprint: Callable[[], str],
    song_pipeline_fingerprint: Callable[[], str],
    song_window_pre_ms: int,
    song_window_post_ms: int,
    live_hold_active_fn: Callable[[], bool] | None = None,
) -> list[dict]:
    """Produce ``items`` concurrently, preserving input order.

    Each slice is an independent subprocess, so threads just wait on those; a
    crash in one becomes a failed result and never kills the batch.  Targeted
    talk repairs may opt into the talk lane's ``reuse_cover`` path through
    their persisted queue item.
    """

    def _one(item: dict) -> dict:
        try:
            produce_kwargs = (
                {"reuse_cover": True}
                if produce_fn is produce_talk_fn and item.get("reuse_cover")
                else {}
            )
            result = produce_fn(date, item, **produce_kwargs)
            if item.get("session_id"):
                result.setdefault("session_id", item["session_id"])
            return result
        except Exception as exc:  # noqa: BLE001 — one bad slice must not kill the batch
            log(f"produce crashed for {item.get('cid')}: {exc}")
            result = {
                "candidate_id": item.get("cid"),
                "rc": -1,
                "status": "failed",
                "error": str(exc),
                "reason_codes": ["PRODUCE_UNEXPECTED_EXCEPTION"],
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
                **{key: item[key] for key in _FAILED_ITEM_PASSTHROUGH_KEYS if key in item},
            }
            if item.get("segment_path"):
                result["segment"] = Path(str(item["segment_path"])).name
            anchor_start = item.get("anchor_start_ms")
            anchor_end = item.get("anchor_end_ms")
            if isinstance(anchor_start, int) and isinstance(anchor_end, int):
                result["start_ms"] = max(0, anchor_start - song_window_pre_ms)
                result["end_ms"] = anchor_end + song_window_post_ms
            return result

    if not items:
        return []
    workers = min(max_parallel, len(items))
    log(f"producing {len(items)} slice(s), up to {workers} in parallel")
    deploy_guard = base / "deploy.guard"
    results_by_index: dict[int, dict] = {}
    queue = list(enumerate(items))
    in_flight: dict[Any, tuple[int, frozenset[str], bool]] = {}
    active_source_keys: set[str] = set()
    exclusive_source_active = False
    deploy_yield = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while queue or in_flight:
            while queue and len(in_flight) < workers and not deploy_yield:
                if deploy_guard.exists():
                    deploy_yield = True
                    log(
                        "deploy guard present — yielding tick after "
                        f"{len(in_flight)} in-flight item(s), "
                        f"{len(queue)} deferred to next tick"
                    )
                    break
                # Same yield contract for a broadcast that starts mid-batch
                # (2026-08-09: a tick begun before the stream kept producing
                # 37 minutes into it).  Undispatched items keep their state and
                # the next tick re-picks them; in-flight items finish.
                if live_hold_active_fn is not None and live_hold_active_fn():
                    deploy_yield = True
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
                queue.pop(0)
                future = pool.submit(_one, item)
                in_flight[future] = (index, source_keys, source_unknown)
                active_source_keys.update(source_keys)
                if source_unknown:
                    exclusive_source_active = True
            if not in_flight:
                break
            done, _pending = _futures_wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                index, source_keys, source_unknown = in_flight.pop(future)
                active_source_keys.difference_update(source_keys)
                if source_unknown:
                    exclusive_source_active = False
                results_by_index[index] = future.result()
    return [results_by_index[index] for index in sorted(results_by_index)]


__all__ = ["produce_batch_windowed"]
