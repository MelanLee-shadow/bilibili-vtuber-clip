"""Real router/checkpoint regressions using the existing recorded transport."""
from __future__ import annotations

from dataclasses import asdict
import gc
import json
from pathlib import Path
import weakref

from src.autoslice import finding_closure_shell as shell
from src.autoslice.cpa_model_ladder import CPA_MODEL_LADDER
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice.llm_client import CpaCompletion
from tests.test_finding_closure_wiring import (
    check,
    prepared,
    proposal_inputs,
    router as recorded_router,
)


RAW = '{"status":"NO_GAIN"}'
STAGE = "proposal-rebuild"
PROMPT = "recorded router lifetime question"


def _retain_original(_raw):
    return True


def _recorded_answer(model, _prompt):
    return CpaCompletion(RAW, model)


def _invoke_and_drop_materials(call):
    # Use the existing real typed-material fixture. No ruling_capture remains
    # alive to legitimately retain a ConsumedRuling.inputs after this returns.
    inputs = RetainedObject.from_value(proposal_inputs(prepared(check())))
    invocation = shell.RulingInvocation(
        STAGE, PROMPT, inputs, Path(__file__), None, "router_lifetime_probe"
    )
    probes = {"invocation": weakref.ref(invocation), "inputs": weakref.ref(inputs)}
    result = call.run_ruling(invocation, call, _retain_original)
    assert result is not None
    answer, hit, checkpoint = result
    assert str(answer) == RAW and not hit
    assert answer.answering_model == CPA_MODEL_LADDER[0]
    probes["checkpoint"] = weakref.ref(checkpoint)
    # Paths and weakrefs have no back-reference to the request/checkpoint.
    return probes, checkpoint.directory


def test_completed_record_callback_releases_materials_while_router_survives(tmp_path, monkeypatch):
    call, calls = recorded_router(tmp_path, monkeypatch, _recorded_answer)
    try:
        probes, _directory = _invoke_and_drop_materials(call)
    finally:
        # A still-running hedge is entitled to keep its request until it ends.
        call.drain()
    assert len(calls) == 1 and len(call.executors) == 1
    executor = call.executors[0]
    assert executor.receipts and executor.outcomes
    assert all(not thread.is_alive() for thread in executor.threads)
    gc.collect()
    assert {name: ref() is None for name, ref in probes.items()} == {
        "invocation": True,
        "inputs": True,
        "checkpoint": True,
    }, "completed executor.record kept the finished request/checkpoint graph alive"
    assert call.executors[0] is executor


def test_record_callback_preserves_receipt_wire_timing_and_cached_original(tmp_path, monkeypatch):
    call, calls = recorded_router(tmp_path, monkeypatch, _recorded_answer)
    try:
        _probes, checkpoint_directory = _invoke_and_drop_materials(call)
    finally:
        call.drain()
    assert len(calls) == 1 and len(call.executors) == 1
    receipt, = call.executors[0].receipts
    expected = RetainedObject.from_value({**asdict(receipt), "requested_model": receipt.route.model})
    routing = tmp_path / "canonical" / "finding-closure-routing"
    winner, = routing.glob("*/winner.json")

    assert winner.read_bytes() == (expected.json_text + "\n").encode()
    receipt_path = winner.parent / (expected.sha256 + ".json")
    assert receipt_path.read_bytes() == json.dumps(
        expected.value(), ensure_ascii=False, sort_keys=True, indent=2
    ).encode() + b"\n"
    assert receipt.status == "REPLIED" and receipt.raw_completion == RAW
    assert receipt.raw_sha256 == shell.core.sha(RAW)
    assert receipt.answering_model == CPA_MODEL_LADDER[0] and not receipt.cache_hit

    expected_span = RetainedObject.from_value({
        "schema_version": "run-time-span.v1",
        "span_id": receipt.effect_key,
        "kind": "wait",
        "name": "ruling." + STAGE,
        "started_utc": receipt.started_utc,
        "ended_utc": receipt.ended_utc,
        "wait_reason": "CANONICAL_PROVIDER_OWNER",
        "status": receipt.status,
        "model": receipt.answering_model,
        "stage": STAGE,
        "cache_hit": receipt.cache_hit,
    })
    timing = routing / "time-spans.jsonl"
    assert timing.read_bytes() == (expected_span.json_text + "\n").encode()
    originals = {p: p.read_bytes() for p in (tmp_path / "canonical").rglob("*.json")}
    before_timing = timing.read_bytes()

    inputs = RetainedObject.from_value(proposal_inputs(prepared(check())))
    invocation = shell.RulingInvocation(
        STAGE, PROMPT, inputs, Path(__file__), None, "router_lifetime_probe"
    )
    replay = call.run_ruling(invocation, call, _retain_original)
    assert replay is not None
    raw, hit, checkpoint = replay
    assert hit and str(raw) == RAW and checkpoint.directory == checkpoint_directory
    call.drain()
    assert len(calls) == 1 and len(call.executors) == 1
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert {p for p in (tmp_path / "canonical").rglob("*.json")} == set(originals)
    assert timing.read_bytes() == before_timing
