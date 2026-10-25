"""Stable G6 grouping and bounded execution for exact-review agenda items.

This module owns scheduling only. Callers retain adjudication, shared-state
admission and persistence. Ordered preparation keeps logical admission separate
from concurrent provider work.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
import os
import threading
from typing import Any, TypeVar

from src.autoslice import audio_question_identity

T = TypeVar("T")
PARALLELISM_ENV = "AUTOSLICE_EXACT_REVIEW_PARALLELISM"


def exact_review_parallelism(environ: Mapping[str, str] | None = None) -> int:
    """Read the independent-group worker count; provider slots remain separate."""
    raw = (os.environ if environ is None else environ).get(PARALLELISM_ENV, "8")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{PARALLELISM_ENV} must be an integer in 1..64") from exc
    if not 1 <= value <= 64:
        raise ValueError(f"{PARALLELISM_ENV} must be an integer in 1..64")
    return value


def group_exact_review_agenda(
    findings: Sequence[Mapping[str, Any]],
    *,
    request_for_finding: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
) -> list[list[int]]:
    """Group agenda indexes using only G6's candidate-independent question.

    The ordinary request producer is supplied by the consumer, so scheduling
    never introduces another request or question contract. An unbuildable
    request may be represented by None and keeps its own item for the ordinary
    adjudicator's failure path. No nominations are merged or discarded.
    """
    requests = [request_for_finding(finding) for finding in findings]
    parents = list(range(len(requests)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for later, request in enumerate(requests):
        if request is None:
            continue
        for earlier in range(later):
            old = requests[earlier]
            if old is not None and audio_question_identity.same_audio_question(
                old, request, compare_proposed=False, strict_source=True,
            )[0]:
                parents[root(later)] = root(earlier)
    groups: dict[int, list[int]] = {}
    for index in range(len(requests)):
        groups.setdefault(root(index), []).append(index)
    return sorted(groups.values(), key=lambda group: group[0])


def execute_exact_review_groups(
    groups: Sequence[Sequence[int]],
    process: Callable[[int], T] | None = None,
    *,
    parallelism: int | None = None,
    prepare: Callable[[int], Callable[[], T]] | None = None,
    collect_result: Callable[[int, T], None] | None = None,
) -> list[T]:
    """Run each group serially; collect in agenda order after every group ends.

    An exception stops the remaining items of that group. Other groups finish,
    then the earliest agenda exception is re-raised without a success result.
    This deliberately does not promise a failed run's side effects equal the
    old fail-fast serial loop's files.
    """
    workers = exact_review_parallelism() if parallelism is None else parallelism
    if type(workers) is not int or not 1 <= workers <= 64:
        raise ValueError("parallelism must be an integer in 1..64")
    frozen = [tuple(group) for group in groups]
    indexes = [index for group in frozen for index in group]
    if (any(type(index) is not int for index in indexes)
            or any(not group or list(group) != sorted(group) for group in frozen)
            or sorted(indexes) != list(range(len(indexes)))):
        raise ValueError("groups must partition the agenda indexes in original within-group order")
    if not indexes:
        return []
    if prepare is not None:
        return _execute_prepared(frozen, prepare, workers, collect_result)
    if process is None:
        raise TypeError("process or prepare is required")

    outputs: dict[int, T] = {}
    failures = []
    if workers == 1:
        owners = {index: owner for owner, group in enumerate(frozen) for index in group}
        failed_groups = set()
        for index in range(len(indexes)):
            if owners[index] in failed_groups:
                continue
            try:
                outputs[index] = process(index)
            except Exception as exc:
                failed_groups.add(owners[index])
                failures.append((index, exc, exc.__traceback__))
    else:
        def run_group(group):
            rows = []
            for index in group:
                try:
                    rows.append((index, process(index)))
                except Exception as exc:
                    return rows, (index, exc, exc.__traceback__)
            return rows, None

        with ThreadPoolExecutor(max_workers=min(workers, len(frozen)),
                                thread_name_prefix="exact-review") as pool:
            futures = [pool.submit(copy_context().run, run_group, group) for group in frozen]
            for future in futures:
                rows, failure = future.result()
                outputs.update(rows)
                if failure is not None:
                    failures.append(failure)
    if failures:
        _, exc, traceback = min(failures, key=lambda failure: failure[0])
        raise exc.with_traceback(traceback)
    return [outputs[index] for index in range(len(indexes))]


_NATIVE_TURN: ContextVar[tuple[list[threading.Event], int] | None] = ContextVar(
    "exact_review_native_turn", default=None)


def wait_for_native_turn(*, require_open: bool = False) -> None:
    """Wait for earlier hard request streams before a counter transaction.

    A caller that has declared its last reservation closes admission explicitly;
    dynamic owners close it when no further requests are possible.
    Soft counters never enter this gate; callers wait before taking any lock.
    """
    turn = _NATIVE_TURN.get()
    if turn is not None:
        admitted, index = turn
        for event in admitted[:index]:
            event.wait()
        if require_open and admitted[index].is_set():
            raise RuntimeError("native admission is already finished for this agenda item")


def finish_native_admission() -> None:
    """Declare no further native requests, allowing later hard admissions.

    Dynamic callers may need provider/CPA answers to prove this. Fixed callers
    can declare their last reservation before I/O. Soft budgets never wait here.
    """
    turn = _NATIVE_TURN.get()
    if turn is not None:
        admitted, index = turn
        admitted[index].set()


def _execute_prepared(groups, prepare, workers, collect_result):
    owners = {index: owner for owner, group in enumerate(groups) for index in group}
    finished = [threading.Event() for _ in owners]
    admitted = [threading.Event() for _ in owners]
    futures = {}
    previous = {}
    outputs = {}
    failures = []
    failed_groups = set()
    published = 0
    skipped = set()

    def run(index, callback):
        token = _NATIVE_TURN.set((admitted, index))
        try:
            return callback()
        finally:
            _NATIVE_TURN.reset(token)
            admitted[index].set()
            finished[index].set()

    def collect(index):
        if index not in futures:
            return
        future = futures.pop(index)
        try:
            outputs[index] = future.result()
        except Exception as exc:
            failed_groups.add(owners[index])
            failures.append((index, exc, exc.__traceback__))

    def publish_prefix(stop, *, wait):
        nonlocal published
        while published < stop:
            index = published
            if index in futures:
                if not wait and not futures[index].done():
                    break
                collect(index)
            if index not in outputs and index not in skipped and not finished[index].is_set():
                break
            if index in outputs and collect_result is not None:
                collect_result(index, outputs[index])
            published += 1

    with ThreadPoolExecutor(max_workers=min(workers, len(groups)),
                            thread_name_prefix="exact-review") as pool:
        # Preparation is cheap/cache-only and owns all logical admission. Wait
        # for a group's predecessor before preparing its next item. Submitting
        # in agenda order also prevents bounded-pool turn-gate starvation.
        for index in range(len(owners)):
            owner = owners[index]
            if owner in previous:
                # Only this group's predecessor supplies dependencies. Final
                # agenda publication may still be behind an unrelated group.
                predecessor = previous[owner]
                if predecessor in futures:
                    collect(predecessor)
            if owner in failed_groups:
                finished[index].set()
                admitted[index].set()
                skipped.add(index)
                continue
            try:
                callback = prepare(index)
                futures[index] = pool.submit(copy_context().run, run, index, callback)
                previous[owner] = index
            except Exception as exc:
                finished[index].set()
                admitted[index].set()
                failed_groups.add(owner)
                failures.append((index, exc, exc.__traceback__))
            publish_prefix(index + 1, wait=workers == 1)
        publish_prefix(len(owners), wait=True)
    if failures:
        _, exc, traceback = min(failures, key=lambda failure: failure[0])
        raise exc.with_traceback(traceback)
    return [outputs[index] for index in range(len(owners))]


class OrderedVerifierAccess:
    """Compatibility facade; cache owners publish atomically without call locks."""
    def __init__(self, verifier):
        self._verifier = verifier

    def __call__(self, *args, **kwargs):
        return self._verifier(*args, **kwargs)

    def __getattr__(self, name):
        # Callables own protocol metadata (range providers, assisted routes,
        # cache identity, recorder bindings). Return the original callable so
        # ordinary consumers see the same capabilities and identity as serial.
        return getattr(self._verifier, name)
