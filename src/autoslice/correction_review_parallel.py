"""Concurrent correction decisions, with serial mutation and original inputs.

Provider admission belongs exclusively to provider_admission. Results are
speculative reads until the ordinary routed consumer reaches their exact SRT
and finding; a changed input goes through the original adjudicator again.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
import hashlib
from importlib import import_module
from threading import Event
from types import ModuleType
from typing import NotRequired, TypeAlias, TypedDict

from .provider_admission import FinalReviewCall

from .exact_review_parallel import _NATIVE_TURN

from .jingting_chunker import parse_srt_cues


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
Finding: TypeAlias = dict[str, JsonValue]
Decision: TypeAlias = tuple[str, Finding]
Adjudicator: TypeAlias = Callable[[str, Finding], Decision]


class ExecutionReceipt(TypedDict):
    schema_version: str
    provider_controller: str
    status: str
    prefetched: int
    consumed: int
    input_changed: int
    reason_code: NotRequired[str]
    unused_original_failures: NotRequired[list[str]]


def _controller_module() -> ModuleType | None:
    name = __package__ + '.provider_admission'
    try:
        return import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


class PreparedCorrectionReviews:
    """Ephemeral futures cannot authorize a patch against a different input."""

    def __init__(self, srt_text: str, findings: Sequence[Finding], adjudicate: Adjudicator,
                 original_srt_text: str | None, provider_call: object) -> None:
        self.srt_text, self.adjudicate = srt_text, adjudicate
        self.futures: dict[int, tuple[Finding, Future[Decision]]] = {}
        self.pool: ThreadPoolExecutor | None = None
        self.receipt: ExecutionReceipt = {'schema_version': 'correction-review-execution.v1',
            'provider_controller': 'provider_admission.ProviderController',
            'status': 'SERIAL', 'prefetched': 0, 'consumed': 0, 'input_changed': 0}
        controller = _controller_module()
        if controller is None:
            self.receipt['reason_code'] = 'SHARED_PROVIDER_CONTROLLER_NOT_INSTALLED'
            return
        # C owns routing/hedging and checkpoints; both canonical call types
        # now use the same admission owner. Keep C's callable intact so its
        # ruling capture and exact-model dispatch remain visible downstream.
        canonical_call = isinstance(provider_call, FinalReviewCall)
        if not canonical_call:
            from .finding_closure_shell import FinalReviewCall as RoutedFinalReviewCall
            canonical_call = isinstance(provider_call, RoutedFinalReviewCall)
        if not canonical_call:
            self.receipt['reason_code'] = 'CANONICAL_PROVIDER_CALL_REQUIRED'
            return
        original = [cue for cue in parse_srt_cues(original_srt_text or srt_text) if cue.text.strip()]
        live = {(cue.start_ms, cue.end_ms): (index, cue.text)
                for index, cue in enumerate((cue for cue in parse_srt_cues(srt_text) if cue.text.strip()), 1)}
        windows = {index: (cue.start_ms, cue.end_ms) for index, cue in enumerate(original, 1)}
        def window_for(row: Finding) -> tuple[int, int] | None:
            index = row.get('cue_index')
            return windows.get(index) if type(index) is int else None

        counts = Counter(window_for(row) for row in findings)
        # A serial/deferred item is a dependency boundary. Preserve the
        # existing native hard-admission order over the concurrent prefix;
        # later items use the original serial consumer.
        eligible: list[tuple[Finding, Finding]] = []
        for row in findings:
            window = window_for(row)
            current = live.get(window) if window is not None else None
            if current is None or counts[window] != 1 or row.get('routed') == 'deferred_same_cue':
                break
            index, text = current
            suspect = row.get('suspect')
            if (index != row.get('cue_index') or not isinstance(suspect, str) or not suspect or suspect not in text
                    or row.get('base_text_sha256') != hashlib.sha256(text.encode()).hexdigest()):
                break
            eligible.append((row, deepcopy(row)))
        if len(eligible) < 2:
            return
        # One worker per finite independent item. There is no second provider
        # concurrency/rate definition; actual sends use the shared controller.
        self.pool = ThreadPoolExecutor(max_workers=len(eligible), thread_name_prefix='correction-review')
        self.receipt['status'] = 'PREFETCHED_SERIAL_PATCH'
        self.receipt['prefetched'] = len(eligible)
        admitted = [Event() for _ in eligible]

        def run(index: int, snapshot: Finding) -> Decision:
            token = _NATIVE_TURN.set((admitted, index))
            try:
                return adjudicate(srt_text, deepcopy(snapshot))
            finally:
                _NATIVE_TURN.reset(token)
                admitted[index].set()

        try:
            for index, (row, snapshot) in enumerate(eligible):
                context = copy_context()
                future = self.pool.submit(context.run, run, index, snapshot)
                self.futures[id(row)] = snapshot, future
        except BaseException:
            self.close()
            raise

    def __call__(self, live_srt: str, finding: Finding) -> Decision:
        saved = self.futures.get(id(finding))
        if saved is not None:
            snapshot, future = saved
            if live_srt == self.srt_text and finding == snapshot:
                self.receipt['consumed'] += 1
                return future.result()
            self.receipt['input_changed'] += 1
            # Join this original call before the fresh-input consumer. That
            # preserves its receipt/UNKNOWN and never duplicates an in-flight
            # effect through another orchestration path.
            try:
                future.result()
            except Exception as exc:
                self.receipt.setdefault('unused_original_failures', []).append(type(exc).__name__)
                # The original owner's UNKNOWN/error remains retained. This
                # next call belongs to the independently changed input.
        return self.adjudicate(live_srt, finding)

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=True)
            self.pool = None


@contextmanager
def prefetch_correction_reviews(srt_text: str, findings: Sequence[Finding], *,
                                adjudicate: Adjudicator, original_srt_text: str | None = None,
                                provider_call: object = None) -> Iterator[PreparedCorrectionReviews]:
    prepared = PreparedCorrectionReviews(srt_text, findings, adjudicate, original_srt_text, provider_call)
    try:
        yield prepared
    finally:
        prepared.close()
