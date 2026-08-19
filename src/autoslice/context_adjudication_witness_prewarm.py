"""Read-only, best-effort parallel prewarm of the context-adjudication witness cache.

``adjudicate_routed_findings`` (``deferred_same_cue_resolution.py``) and its
per-finding worker ``adjudicate_context_finding`` (``final_review_auditor.py``)
run serially and, in the common (non-deferred, non-rebased) case, each spend
one AGY dictation-witness call built from ``build_context_adjudication_request``
+ ``build_witness_request``.  That witness call is candidate-blind (only cue
audio geometry survives into the request — see ``build_witness_request``) and
its result is written to a content-addressed, hash-keyed disk cache
(``entity_verdicts/<hash>/verdict.manifest.json`` /
``acoustic_cache/<identity>.json``).  A cache lookup by exact
``request_sha256``/``audio_clip_sha256`` identity can never serve the wrong
answer to a mismatched request — at worst it misses and the caller falls back
to the ordinary provider call.  That makes it safe to *warm* this cache ahead
of the serial loop by firing the same, byte-identical first-window requests
concurrently, then letting the untouched serial loop replay them from cache.

This module deliberately reads only ``srt_text``/``findings`` and the same
constants (``max_adjudications``) the real loop already enforces; it never
mutates ``srt_text``, never touches subtitle state, and never authorizes any
mutation.  It replicates, read-only, exactly the admission bookkeeping
``adjudicate_routed_findings`` performs before it would call the provider for
each finding, so that:

* only the *first* finding of each admitted (start_ms, end_ms) window is
  prewarmed — later same-window findings are always rebased mid-loop
  (``rebase_deferred_finding``) before their real request is built, so a
  prewarm request built against the pre-loop text is guaranteed to miss for
  them regardless (see the module docstring and ``adjudicate_routed_findings``
  in ``deferred_same_cue_resolution.py``);
* findings that would fail either of the loop's own cheap prechecks
  (``base_text_sha256`` no longer matches the live cue text, or
  ``suspect`` is no longer a substring of the live cue text) are skipped,
  matching ``STALE_FINDING_SKIPPED``/rebase in the real loop;
* windows beyond ``max_adjudications`` (``group_sizes``/``reserved_calls``,
  the same ``ContextAdjudicationBudget``-shaped cap the real loop enforces)
  are never prewarmed, so prewarm cannot spend AGY/Gemini quota the serial
  loop would never have spent.

Any failure building or firing an individual request (invalid finding,
provider error, timeout, malformed response) is swallowed per item; the
function itself never raises.  It writes nothing except through the ordinary
``entity_verifier`` cache-write path already exercised by the serial loop.


提速理由（Ivan 2026-08-19 裁定）：串行主循环逐 finding 打一次 AGY 听写证人，
是这条腿墙钟时间的主因。证人请求是纯函数——内容由音频几何决定，结果按
request_sha256/audio_clip_sha256 内容寻址落盘——所以同一份请求提前并发打一遍
写进既有缓存，随后串行循环原样命中即可。预热**只读**地复演
``adjudicate_routed_findings`` 自己的准入算术（每窗口只取第一条 finding、
budget 上限一致、陈旧 base_text_sha256 或已不在文本中的 suspect 一律跳过），
不越权授权任何改写、不改串行循环一个字节、任何失败静默吞掉——correctness
完全不依赖预热是否成功。
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Sequence

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.deferred_same_cue_resolution import (
    _live_cue_for_window,
    _text_sha256,
)
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.jingting_chunker import parse_srt_cues

SCHEMA_VERSION = "context-adjudication-witness-prewarm.v1"

# Matches the ``produce_dispatch.py``/``microcue_acoustic_discovery.py``
# bounded-ThreadPoolExecutor convention: a plain module constant, no new env
# knob, so a pathological finding batch cannot open unbounded concurrent
# AGY/Gemini sessions.
CONTEXT_ADJUDICATION_WITNESS_PREWARM_CONCURRENCY = 4


def _select_prewarm_requests(
    srt_text: str,
    findings: Sequence[Mapping[str, Any]],
    *,
    original_srt_text: str | None,
    max_adjudications: int,
    clip_context: Mapping[str, object] | None,
    source_media_timeline_offset_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read-only replay of ``adjudicate_routed_findings``'s admission math.

    Returns the deduplicated (by ``request_sha256``) witness requests worth
    prewarming, plus a small counter breakdown for the receipt.  Never
    mutates ``srt_text``/``findings``/any input.
    """

    counters = {
        "candidate_count": len(findings),
        "skipped_no_window": 0,
        "skipped_duplicate_window": 0,
        "skipped_no_live_cue": 0,
        "skipped_base_text_stale": 0,
        "skipped_budget": 0,
        "skipped_suspect_stale": 0,
        "skipped_request_build_failed": 0,
        "selected": 0,
    }
    initial_cues = [
        cue
        for cue in parse_srt_cues(original_srt_text or srt_text)
        if cue.text.strip()
    ]
    targets = {
        index: ((cue.start_ms, cue.end_ms), cue.text)
        for index, cue in enumerate(initial_cues, start=1)
    }
    group_sizes = Counter(
        targets.get(int(row.get("cue_index") or 0), ((0, 0), ""))[0]
        for row in findings
    )
    admitted_windows: set[tuple[int, int]] = set()
    rejected_windows: set[tuple[int, int]] = set()
    selected_windows: set[tuple[int, int]] = set()
    reserved_calls = 0
    by_request_sha: dict[str, dict[str, Any]] = {}
    for row in findings:
        if not isinstance(row, Mapping):
            continue
        original_index = int(row.get("cue_index") or 0)
        target = targets.get(original_index)
        if target is None:
            counters["skipped_no_window"] += 1
            continue
        window, _original_text = target
        if window in selected_windows:
            counters["skipped_duplicate_window"] += 1
            continue
        finding_cue, live_text = _live_cue_for_window(srt_text, window)
        if live_text is None:
            counters["skipped_no_live_cue"] += 1
            continue
        live_sha = _text_sha256(live_text)
        if row.get("base_text_sha256") != live_sha:
            # Matches the loop's rebase trigger: a prewarm request built
            # against this (pre-loop) text would not match what the real,
            # rebased request eventually looks like.  Skip — guaranteed miss.
            counters["skipped_base_text_stale"] += 1
            continue
        if window not in admitted_windows and window not in rejected_windows:
            required = group_sizes.get(window, 1)
            if reserved_calls + required <= max_adjudications:
                admitted_windows.add(window)
                reserved_calls += required
            else:
                rejected_windows.add(window)
        if window in rejected_windows:
            counters["skipped_budget"] += 1
            continue
        suspect = str(row.get("suspect") or "")
        if suspect not in live_text:
            counters["skipped_suspect_stale"] += 1
            continue
        selected_windows.add(window)
        finding_for_request = dict(row)
        if finding_cue is not None:
            finding_for_request["cue_index"] = finding_cue
        try:
            check_request = build_context_adjudication_request(
                srt_text,
                finding_for_request,
                clip_context=clip_context,
                source_media_timeline_offset_ms=source_media_timeline_offset_ms,
            )
            witness_request = build_witness_request(check_request)
        except (TypeError, ValueError, KeyError):
            counters["skipped_request_build_failed"] += 1
            continue
        request_sha = str(witness_request.get("request_sha256") or "")
        if not request_sha:
            counters["skipped_request_build_failed"] += 1
            continue
        by_request_sha.setdefault(request_sha, witness_request)
    counters["selected"] = len(by_request_sha)
    return list(by_request_sha.values()), counters


def prewarm_context_adjudication_witnesses(
    srt_text: str,
    findings: Sequence[Mapping[str, Any]],
    *,
    entity_verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    max_adjudications: int,
    original_srt_text: str | None = None,
    clip_context: Mapping[str, object] | None = None,
    source_media_timeline_offset_ms: int = 0,
    concurrency: int = CONTEXT_ADJUDICATION_WITNESS_PREWARM_CONCURRENCY,
) -> dict[str, Any]:
    """Fire the first-per-admitted-window context-adjudication witness calls
    concurrently, ahead of the untouched serial ``adjudicate_routed_findings``
    loop, so it replays cache hits instead of waiting on AGY one cue at a
    time.

    Never raises, never mutates ``srt_text``/``findings``, never authorizes
    any mutation, and never bypasses ``max_adjudications`` or any circuit
    breaker owned by ``entity_verifier`` (concurrency only fans out calls the
    serial loop would already make; it never adds new ones).  Any individual
    request failure (build error, provider error, timeout, bad schema) is
    swallowed — the serial loop still runs unmodified afterward and simply
    misses the cache for that item, exactly as if this function had not run.
    """

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "SKIPPED",
        "mutation_authorized": False,
    }
    if entity_verifier is None or not findings:
        receipt["reason_code"] = (
            "NO_ENTITY_VERIFIER" if entity_verifier is None else "NO_FINDINGS"
        )
        return receipt
    try:
        requests, counters = _select_prewarm_requests(
            srt_text,
            findings,
            original_srt_text=original_srt_text,
            max_adjudications=max_adjudications,
            clip_context=clip_context,
            source_media_timeline_offset_ms=source_media_timeline_offset_ms,
        )
    except Exception as exc:  # selection itself must never break the caller
        receipt["status"] = "SELECTION_ERROR"
        receipt["error_type"] = type(exc).__name__
        return receipt
    receipt.update(counters)
    if not requests:
        receipt["status"] = "PASS"
        receipt["reason_code"] = "NO_PREWARMABLE_REQUESTS"
        return receipt

    succeeded = 0
    failed = 0

    def _fire(request: Mapping[str, Any]) -> bool:
        try:
            entity_verifier(request)
            return True
        except Exception:
            # Best-effort: the serial loop makes its own call (and its own
            # error handling) if this one failed. Never propagate.
            return False

    workers = max(1, min(int(concurrency), len(requests)))
    if workers <= 1:
        for request in requests:
            if _fire(request):
                succeeded += 1
            else:
                failed += 1
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [pool.submit(_fire, request) for request in requests]
            for future in futures:
                try:
                    if future.result():
                        succeeded += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
        finally:
            pool.shutdown(wait=True)

    receipt["status"] = "PASS"
    receipt["fired_count"] = len(requests)
    receipt["succeeded_count"] = succeeded
    receipt["failed_count"] = failed
    return receipt
