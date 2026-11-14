"""One parallel executor for the lane's independent provider calls (TASK-31).

维护者's rule (/10-22): API calls belong in parallel; a concurrency bound comes from the provider's own
limit (429, quota), a local number is only a guard against one clip's fan-out starving everything else; the call
itself runs outside any lock, and a cache is written to a temporary place and renamed into place.

What this module does:

- ``run(items, call, provider=...)`` calls ``call(item)`` for every item, at most ``limit(provider)`` at a time;
- results come back in **input order**, whatever the completion order (so a record list, a merged answer and the
  PROCESS lines are the same as a serial pass);
- when a call raises, the calls already running finish, the queued ones are cancelled, and the **earliest**
  exception in input order is raised — a serial pass and a parallel pass then fail with the same error;
- ``Stats`` reports calls, peak concurrency and wall clock; ``LEDGER`` keeps every run of the process so a step can
  write what actually happened (``snapshot``).

The bound is shared by everything in the process: clips run side by side as threads (multi_run.py), so the
``limit(provider)`` open calls are counted over all of them, not per ``run`` (``slot`` is that one gate; a lane step that
is heavy on the local machine takes a slot of its own name too).  A call must not itself call ``run`` or ``slot`` for the
provider it holds a slot of: that would wait on itself, so it raises.

What it deliberately does not do: **retry**.  Retry and backoff already live in the adapters, once:
``assets/manual_lane/remote/cpa_text.py`` retries 408/429/5xx and waits out 524/503 in-process,
``listen_windows.py`` retries a window three times and turns a 429 into a QUOTA record, ``witness_windows.py``
caches by excerpt bytes, ``free_asr_paced.py`` paces one call per provider.  A second retry layer here would
re-send on its own timing and hide the provider's own signal.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar, TypedDict, cast

from .lane_rules import ProviderLimit, rules

T = TypeVar("T")
R = TypeVar("R")

DEFAULT_LIMIT = 4   # only when lane_rules has no concurrency row for the provider
LEDGER: list[Stats] = []
_LEDGER_LOCK = threading.Lock()
_GATES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_HELD = threading.local()            # the providers whose slot the current thread holds


class ProviderStats(TypedDict):
    limit: int
    calls: int
    peak: int
    wall_s: float
    runs: int


class StatsSnapshot(TypedDict):
    providers: dict[str, ProviderStats]
    peak: int


def limit(provider: str) -> int:
    """The concurrent bound of one provider from lane_rules `concurrency.<provider>.limit`."""
    try:
        row = cast(ProviderLimit, rules("concurrency").get(provider) or {})
    except KeyError:                 # a rules file without the section (an old experiment copy)
        row = {}
    try:
        value = int(row.get("limit") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else DEFAULT_LIMIT


@dataclass
class Stats:
    """What one `run` did: the provider, the bound it used, the calls, the peak concurrency, the wall clock."""
    provider: str = ""
    limit: int = 0
    calls: int = 0
    peak: int = 0
    wall_s: float = 0.0


class _Meter:
    """In-flight counter behind Stats: peak concurrency is what the executor actually had open."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.calls = 0

    def enter(self) -> None:
        with self._lock:
            self.inflight += 1
            self.calls += 1
            self.peak = max(self.peak, self.inflight)

    def leave(self) -> None:
        with self._lock:
            self.inflight -= 1


def _held() -> set[str]:
    if not hasattr(_HELD, "names"):
        _HELD.names = set()
    return cast("set[str]", _HELD.names)


@contextmanager
def slot(provider: str) -> Iterator[None]:
    """One of the process's ``limit(provider)`` places: waits while all of them are taken, by any clip."""
    names = _held()
    if provider in names:
        raise RuntimeError(f"parallel: a call holding a {provider!r} slot asked for another {provider!r} slot")
    bound = limit(provider)
    with _LEDGER_LOCK:
        gate = _GATES.setdefault((provider, bound), threading.BoundedSemaphore(bound))
    with gate:
        names.add(provider)
        try:
            yield
        finally:
            names.discard(provider)


def run(items: Sequence[T], call: Callable[[T], R], *, provider: str, stats: Stats | None = None) -> list[R]:
    """`call` on every item, at most `limit(provider)` at once, results in input order."""
    jobs = list(items)
    if provider in _held():
        raise RuntimeError(f"parallel.run({provider!r}) inside a call that holds a {provider!r} slot")
    bound = limit(provider)
    record = stats if stats is not None else Stats()
    record.provider, record.limit = provider, bound
    if not jobs:
        _keep(record)
        return []
    started = time.monotonic()
    workers = max(1, min(bound, len(jobs)))
    try:
        if workers == 1:
            results: list[R] = []
            for job in jobs:
                with slot(provider):
                    results.append(call(job))
            record.calls, record.peak = len(jobs), 1
            return results
        meter = _Meter()

        def one(job: T) -> R:
            with slot(provider):
                meter.enter()
                try:
                    return call(job)
                finally:
                    meter.leave()

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"lane-{provider}")
        futures = [pool.submit(one, job) for job in jobs]
        try:
            return [future.result() for future in futures]      # input order, not completion order
        except BaseException:
            for future in futures:                              # queued work does not start after a failure
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True)
            record.calls, record.peak = meter.calls, meter.peak
    finally:
        record.wall_s = round(time.monotonic() - started, 2)
        _keep(record)


def _keep(record: Stats) -> None:
    with _LEDGER_LOCK:
        LEDGER.append(record)


def reset() -> None:
    """Forget this process's runs (a step starts its own account)."""
    with _LEDGER_LOCK:
        LEDGER.clear()


def snapshot() -> StatsSnapshot:
    """{provider: {limit, calls, peak, wall_s}} over this process's runs, plus the largest peak of any one run."""
    with _LEDGER_LOCK:
        rows = list(LEDGER)
    out: dict[str, ProviderStats] = {}
    for row in rows:
        acc = out.setdefault(row.provider, {"limit": row.limit, "calls": 0, "peak": 0, "wall_s": 0.0, "runs": 0})
        acc["calls"] += row.calls
        acc["peak"] = max(acc["peak"], row.peak)
        acc["wall_s"] = round(acc["wall_s"] + row.wall_s, 2)
        acc["runs"] += 1
    return {"providers": out, "peak": max((r["peak"] for r in out.values()), default=0)}
