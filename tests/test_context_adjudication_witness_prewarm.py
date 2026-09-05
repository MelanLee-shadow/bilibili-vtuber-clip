"""Tests for the context-adjudication witness prewarm .

Covers both levels:
* unit tests directly against ``prewarm_context_adjudication_witnesses``
  (budget replication, concurrency cap, per-item failure isolation, stale
  ``base_text_sha256`` skip, never-raises contract);
* integration tests through ``producer_text_pipeline._run_final_review``,
  proving the mechanism (unique underlying-provider spends collapse to the
  prewarm's ``fired_count``; the serial loop adds none for the admitted,
  non-rebased batch) and pipeline output equivalence with/without prewarm.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.context_adjudication_witness_prewarm import (
    CONTEXT_ADJUDICATION_WITNESS_PREWARM_CONCURRENCY,
    prewarm_context_adjudication_witnesses,
)

from tests.test_producer_text_pipeline_final_review import (
    _adapters,
    _memoizing_witness_observer,
    _split_llm,
    _srt,
    _witness_verdict,
)


# ---------------------------------------------------------------------------
# Unit tests: prewarm_context_adjudication_witnesses in isolation
# ---------------------------------------------------------------------------


def _finding(cue_index, current, suspect, suggestion, proposed):
    return {
        "cue_index": cue_index,
        "base_text_sha256": hashlib.sha256(current.encode()).hexdigest(),
        "suspect": suspect,
        "suggestion": suggestion,
        "span_start_codepoint": current.index(suspect) if suspect else 0,
        "span_end_codepoint": (
            current.index(suspect) + len(suspect) if suspect else 0
        ),
        "proposed_full_cue": proposed,
        "repair_class": "phonetic",
    }


def test_prewarm_never_raises_without_entity_verifier():
    receipt = prewarm_context_adjudication_witnesses(
        _srt("坏词留在这里"),
        [_finding(1, "坏词留在这里", "坏词", "好词", "好词留在这里")],
        entity_verifier=None,
        max_adjudications=12,
    )
    assert receipt["status"] == "SKIPPED"
    assert receipt["reason_code"] == "NO_ENTITY_VERIFIER"


def test_prewarm_never_raises_without_findings():
    receipt = prewarm_context_adjudication_witnesses(
        _srt("坏词留在这里"),
        [],
        entity_verifier=lambda request: _witness_verdict(request, "hao ci"),
        max_adjudications=12,
    )
    assert receipt["status"] == "SKIPPED"
    assert receipt["reason_code"] == "NO_FINDINGS"


def test_prewarm_skips_finding_with_stale_base_text_sha256():
    """A finding whose recorded base text no longer matches the live cue must
    not be prewarmed -- the real serial loop would rebase it instead, so a
    request built against the current text is guaranteed to target the wrong
    (already-superseded) audio window."""

    fired = []

    def verify(request):
        fired.append(request)
        return _witness_verdict(request, "hao ci liu zai zhe li")

    srt_text = _srt("坏词留在这里")
    stale = {
        "cue_index": 1,
        # Recorded against a base text the live cue no longer has -- the
        # real serial loop would treat this as a rebase trigger.
        "base_text_sha256": hashlib.sha256("这不是真实文本".encode()).hexdigest(),
        "suspect": "坏词",
        "suggestion": "好词",
        "span_start_codepoint": 0,
        "span_end_codepoint": 2,
        "proposed_full_cue": "好词留在这里",
        "repair_class": "phonetic",
    }

    receipt = prewarm_context_adjudication_witnesses(
        srt_text, [stale], entity_verifier=verify, max_adjudications=12
    )

    assert receipt["status"] == "PASS"
    assert receipt["skipped_base_text_stale"] == 1
    assert receipt["selected"] == 0
    assert fired == []


def test_prewarm_respects_max_adjudications_budget():
    """More admitted windows than the budget allows must not be prewarmed --
    prewarm must never spend AGY/Gemini quota the serial loop would not."""

    findings = [
        _finding(
            index,
            f"坏词{index}留在这里",
            f"坏词{index}",
            f"好词{index}",
            f"好词{index}留在这里",
        )
        for index in range(1, 14)
    ]
    srt_text = _srt(*[f"坏词{index}留在这里" for index in range(1, 14)])
    fired = []

    def verify(request):
        fired.append(request)
        return _witness_verdict(request, "hao ci liu zai zhe li")

    receipt = prewarm_context_adjudication_witnesses(
        srt_text,
        findings,
        entity_verifier=verify,
        max_adjudications=12,
        original_srt_text=srt_text,
    )

    assert receipt["status"] == "PASS"
    assert receipt["selected"] == 12
    assert receipt["skipped_budget"] == 1
    assert receipt["fired_count"] == 12
    assert len(fired) == 12


def test_prewarm_selects_only_first_finding_per_admitted_window():
    """Two findings on the same cue window: only the first is prewarmed --
    the second is always rebased mid-loop against mutated live text, so a
    prewarm request built pre-loop cannot match it."""

    srt_text = _srt("坏甲和坏乙")
    findings = [
        _finding(1, "坏甲和坏乙", "坏甲", "好甲", "好甲和坏乙"),
        _finding(1, "坏甲和坏乙", "坏乙", "好乙", "坏甲和好乙"),
    ]
    fired = []

    def verify(request):
        fired.append(request)
        return _witness_verdict(request, "hao jia he huai yi")

    receipt = prewarm_context_adjudication_witnesses(
        srt_text,
        findings,
        entity_verifier=verify,
        max_adjudications=12,
        original_srt_text=srt_text,
    )

    assert receipt["selected"] == 1
    assert len(fired) == 1
    assert fired[0]["cue_indexes"] == [1]


def test_prewarm_isolates_single_item_failure():
    """One request's provider failure must not affect sibling prewarm calls
    or raise out of the function."""

    findings = [
        _finding(
            index,
            f"坏词{index}留在这里",
            f"坏词{index}",
            f"好词{index}",
            f"好词{index}留在这里",
        )
        for index in range(1, 5)
    ]
    srt_text = _srt(*[f"坏词{index}留在这里" for index in range(1, 5)])
    succeeded = []

    def verify(request):
        if request["cue_indexes"] == [2]:
            raise RuntimeError("boom")
        succeeded.append(request)
        return _witness_verdict(request, "hao ci liu zai zhe li")

    receipt = prewarm_context_adjudication_witnesses(
        srt_text,
        findings,
        entity_verifier=verify,
        max_adjudications=12,
        original_srt_text=srt_text,
    )

    assert receipt["status"] == "PASS"
    assert receipt["fired_count"] == 4
    assert receipt["succeeded_count"] == 3
    assert receipt["failed_count"] == 1
    assert len(succeeded) == 3


def test_prewarm_concurrency_is_bounded():
    findings = [
        _finding(
            index,
            f"坏词{index}留在这里",
            f"坏词{index}",
            f"好词{index}",
            f"好词{index}留在这里",
        )
        for index in range(1, 9)
    ]
    srt_text = _srt(*[f"坏词{index}留在这里" for index in range(1, 9)])
    lock = threading.Lock()
    in_flight = 0
    max_observed = 0

    def verify(request):
        nonlocal in_flight, max_observed
        with lock:
            in_flight += 1
            max_observed = max(max_observed, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return _witness_verdict(request, "hao ci liu zai zhe li")

    receipt = prewarm_context_adjudication_witnesses(
        srt_text,
        findings,
        entity_verifier=verify,
        max_adjudications=12,
        original_srt_text=srt_text,
    )

    assert receipt["fired_count"] == 8
    assert max_observed == CONTEXT_ADJUDICATION_WITNESS_PREWARM_CONCURRENCY


def test_prewarm_selection_error_is_swallowed():
    """A genuinely broken srt_text/finding combination must produce a typed
    receipt, never an exception -- prewarm failure must never break the
    caller."""

    receipt = prewarm_context_adjudication_witnesses(
        "not a valid srt document",
        [{"cue_index": 1, "proposed_full_cue": "x"}],
        entity_verifier=lambda request: _witness_verdict(request, "hao"),
        max_adjudications=12,
    )
    # Either a clean per-item skip (no valid window/target) or a typed
    # selection error -- never a raised exception reaching the caller.
    assert receipt["status"] in {"PASS", "SELECTION_ERROR"}


# ---------------------------------------------------------------------------
# Integration tests: through producer_text_pipeline._run_final_review
# ---------------------------------------------------------------------------


def test_pipeline_prewarm_collapses_serial_witness_spend(monkeypatch):
    """End-to-end mechanism proof: with prewarm wired in, the number of
    distinct underlying-provider spends for the admitted context-adjudication
    batch equals the prewarm's own ``fired_count`` -- the serial loop's own
    per-finding fetches for that batch add zero new provider spends."""

    source_texts = [f"坏词{index}留在这里" for index in range(1, 9)]
    findings = [
        {
            "cue": index,
            "kind": "context",
            "proposed_full_cue": f"好词{index}留在这里",
            "repair_class": "phonetic",
            "why": "上下文明确",
        }
        for index in range(1, 9)
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False), "PROPOSED"
        ),
    )
    observe, requests = _memoizing_witness_observer(
        lambda request: _witness_verdict(
            request, f"hao ci {request['cue_indexes'][0]} liu zai zhe li"
        )
    )

    _output, audit = pipeline._run_final_review(
        srt_text=_srt(*source_texts),
        chat_authority_audit={"applied": []},
        handled_entity_cues=set(),
        verify_confusable_entity=observe,
        adapters=_adapters(),
    )

    prewarm_receipt = audit["context_adjudication_witness_prewarm"]
    assert prewarm_receipt["status"] == "PASS"
    assert prewarm_receipt["fired_count"] == 8
    # All 8 underlying provider spends came from the prewarm; the serial
    # loop's own 8 calls into ``observe`` all replayed from the (simulated)
    # acoustic cache.
    assert len(requests) == prewarm_receipt["fired_count"] == 8


def test_pipeline_output_identical_with_and_without_prewarm(monkeypatch):
    """Disabling only the prewarm call must not change what the (untouched)
    serial loop ultimately decides and writes."""

    source_texts = [f"坏词{index}留在这里" for index in range(1, 6)]
    findings = [
        {
            "cue": index,
            "kind": "context",
            "proposed_full_cue": f"好词{index}留在这里",
            "repair_class": "phonetic",
            "why": "上下文明确",
        }
        for index in range(1, 6)
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False), "PROPOSED"
        ),
    )

    def run(*, disable_prewarm):
        observe, requests = _memoizing_witness_observer(
            lambda request: _witness_verdict(
                request, f"hao ci {request['cue_indexes'][0]} liu zai zhe li"
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            if disable_prewarm:
                mp.setattr(
                    pipeline,
                    "prewarm_context_adjudication_witnesses",
                    lambda *args, **kwargs: {
                        "schema_version": "context-adjudication-witness-prewarm.v1",
                        "status": "SKIPPED",
                        "mutation_authorized": False,
                        "reason_code": "TEST_DISABLED",
                    },
                )
            output, audit = pipeline._run_final_review(
                srt_text=_srt(*source_texts),
                chat_authority_audit={"applied": []},
                handled_entity_cues=set(),
                verify_confusable_entity=observe,
                adapters=_adapters(),
            )
        return output, audit, requests

    output_prewarmed, audit_prewarmed, requests_prewarmed = run(disable_prewarm=False)
    output_baseline, audit_baseline, requests_baseline = run(disable_prewarm=True)

    assert output_prewarmed == output_baseline
    for key in (
        "findings",
        "applied_count",
        "context_adjudication_count",
        "status",
    ):
        assert audit_prewarmed[key] == audit_baseline[key]
    # Baseline (no prewarm) still only spends once per unique window, thanks
    # to the same acoustic-cache-shaped memoization -- both runs must reach
    # exactly the same total.
    assert len(requests_baseline) == len(requests_prewarmed) == 5
