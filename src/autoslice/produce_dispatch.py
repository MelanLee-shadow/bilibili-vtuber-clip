"""Windowed concurrent slice production with deploy-yield.

Ivan 2026-07-27 部署优先令（「一个切片结束了立刻部署最新版」）：马拉松
tick 整批持锁会让部署排队数小时。派发窗口每次派新活前检查 deploy.guard，
在场即停派（在飞的做完），本 tick 提前收官让位；未派发候选状态未动，
下个 tick（新代码）自然续跑。全部依赖注入，runner 侧同名 wrapper 保持
monkeypatch 面（BASE/MAX_PARALLEL_PRODUCE 仍在 runner 全局）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait as _futures_wait,
)
from pathlib import Path
from typing import Any

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
                **{
                    key: item[key]
                    for key in _FAILED_ITEM_PASSTHROUGH_KEYS
                    if key in item
                },
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
    in_flight: dict[Any, int] = {}
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
                index, item = queue.pop(0)
                in_flight[pool.submit(_one, item)] = index
            if not in_flight:
                break
            done, _pending = _futures_wait(
                in_flight, return_when=FIRST_COMPLETED
            )
            for future in done:
                results_by_index[in_flight.pop(future)] = future.result()
    return [results_by_index[index] for index in sorted(results_by_index)]


__all__ = ["produce_batch_windowed"]
