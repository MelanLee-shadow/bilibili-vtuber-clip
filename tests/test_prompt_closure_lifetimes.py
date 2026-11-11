"""Returned prompt views must not keep their complete input graphs alive."""
from contextlib import contextmanager
import gc
import hashlib
import json
import weakref

import pytest

from src.autoslice import exact_final_convergence, prompt_materials
from src.autoslice.review_errors import ContractRejection


class WeakDict(dict):
    pass


@contextmanager
def no_cyclic_gc():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def _terminal_input():
    return WeakDict({
        "context_sources": {"terms": {"id": "terms:1", "text": "原始词表"}},
        "whole_session_and_session_context": {"speaker": {"id": "speaker:1", "name": "主播"}},
        "stopped_finding_evidence": {"recorded_evidence": [{"id": "evidence:1", "text": "证据"}]},
        "observations": [{"observation_id": "observation:1", "raw_observation": {"id": "ignored:raw"}}],
        "events": [{"id": "event:own", "order": [3, 2, 2, 1]}],
        "audio": [{"id": "ignored:audio"}],
        "named": WeakDict({"id": "named:1", "value": "保留原值"}),
        "equal_alias": {"id": "named:1", "value": "保留原值"},
        "absent": None,
    })


def _receipts_case(kind):
    if kind == "no_shared":
        return (
            WeakDict({"prior": [{"text": "第一条\n原文", "number": -0.0, "approved": False}]}),
            WeakDict({"current": {"text": "第二条原文", "ordered": [3, 2, 2, 1], "missing": None}}),
        )
    if kind == "no_savings":
        # The repeated UTF-8 string exceeds 256 bytes; the shared table is
        # nevertheless longer than the original two small JSON documents.
        return WeakDict({"prior": "界" * 100}), WeakDict({"current": "界" * 100})
    if kind == "savings":
        receipt = {
            "id": "receipt:original",
            "text": "中文原始证据，含重复与标点。" * 256,
            "raw": {"ordered": [3, 2, 2, 1], "rejected": False, "missing": None},
        }
        return (
            WeakDict({"history": [dict(receipt) for _ in range(3)], "$receipt_ref": "original literal"}),
            WeakDict({"evidence": [dict(receipt) for _ in range(5)], "$receipt_ref_": "another literal"}),
        )
    raise AssertionError(kind)


def _output_sha256(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# These hashes were computed from the frozen pre-change source named in the
# handoff manifest. They include every output byte, not only parsed JSON values.
TERMINAL_GOLDEN = {False: "8b1b2a1546da58452846b5501dfd122773e241fc0046cb6d66d243e2bbcc0df4", True: "cc5e538bbf45a9d13c21862430af12f306d95db4ba1f0960d9db4ff9c9141b67"}
RECEIPTS_GOLDEN = {
    "no_shared": "2e4b11f76995310894a6fd2377b855f922f078633c0da7b994d4ed9fd79c4acb",
    "no_savings": "d7970ca192297f681b0ed250bedf4b72eeaf13bdfe9080bdf5ff923de6ab076a",
    "savings": "63f8572668d05d7ce407d466825b04587f4e47e555e2af3e0a715c49bc217b8d",
}


@pytest.mark.parametrize("named_materials", [False, True])
def test_terminal_index_releases_input_without_cyclic_gc(named_materials):
    with no_cyclic_gc():
        data = _terminal_input()
        tracked = (weakref.ref(data), weakref.ref(data["named"]))
        result = prompt_materials.terminal_material_index(data, named_materials=named_materials)
        del data
        assert all(reference() is None for reference in tracked)
        assert result["observation:1"] == ["observations", 0]


@pytest.mark.parametrize("named_materials", [False, True])
def test_terminal_index_preserves_complete_ordered_mapping(named_materials):
    result = prompt_materials.terminal_material_index(_terminal_input(), named_materials=named_materials)
    assert _output_sha256(list(result.items())) == TERMINAL_GOLDEN[named_materials]
    assert "ignored:raw" not in result
    assert "ignored:audio" not in result
    assert (result.get("named:1") == ["named"]) is named_materials


def test_terminal_index_preserves_collision_rejection_and_traceback():
    data = {"first": {"id": "same", "text": "甲"}, "second": {"id": "same", "text": "乙"}}
    with pytest.raises(ContractRejection, match="^TERMINAL_MATERIAL_ID_COLLISION$") as caught:
        prompt_materials.terminal_material_index(data)
    names = []
    trace = caught.value.__traceback__
    while trace is not None:
        names.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert "terminal_material_index" in names
    assert names[-1] == "visit"


def test_terminal_index_propagates_same_nested_exception():
    expected = RuntimeError("same original mapping failure")

    class BrokenDict(dict):
        def items(self):
            raise expected

    with pytest.raises(RuntimeError) as caught:
        prompt_materials.terminal_material_index({"nested": BrokenDict({"text": "原文"})})
    assert caught.value is expected
    assert expected.__traceback__ is not None
    assert expected.__cause__ is None
    assert expected.__context__ is None


@pytest.mark.parametrize("kind", ["no_shared", "no_savings", "savings"])
def test_cycle_receipts_release_inputs_without_cyclic_gc(kind):
    with no_cyclic_gc():
        history, evidence = _receipts_case(kind)
        tracked = (weakref.ref(history), weakref.ref(evidence))
        result = exact_final_convergence._cycle_prompt_receipts(history, evidence)
        del history, evidence
        assert all(reference() is None for reference in tracked)
        assert len(result) == 3
        assert all(isinstance(part, str) for part in result)


@pytest.mark.parametrize("kind", ["no_shared", "no_savings", "savings"])
def test_cycle_receipts_preserve_frozen_output_bytes_and_all_facts(kind):
    history, evidence = _receipts_case(kind)
    result = exact_final_convergence._cycle_prompt_receipts(history, evidence)
    assert _output_sha256(result) == RECEIPTS_GOLDEN[kind]
    history_json, evidence_json, section = result
    assert bool(section) is (kind == "savings")
    if not section:
        assert json.loads(history_json) == history
        assert json.loads(evidence_json) == evidence
        return
    table = json.loads(section.rsplit("\n", 1)[1])

    def expand(value):
        if isinstance(value, dict):
            if list(value) == ["$receipt_ref__"]:
                return expand(table[value["$receipt_ref__"]])
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value

    assert expand(json.loads(history_json)) == history
    assert expand(json.loads(evidence_json)) == evidence


def test_cycle_receipts_keep_existing_hash_callbacks_late_bound(monkeypatch):
    original_text = exact_final_convergence._text_sha256
    original_json = exact_final_convergence._json_sha256
    for label in ("first", "replacement"):
        calls = {"text": 0, "json": 0}

        def text_hash(value):
            calls["text"] += 1
            return original_text(value)

        def json_hash(value):
            calls["json"] += 1
            return original_json(value)

        monkeypatch.setattr(exact_final_convergence, "_text_sha256", text_hash)
        monkeypatch.setattr(exact_final_convergence, "_json_sha256", json_hash)
        result = exact_final_convergence._cycle_prompt_receipts(*_receipts_case("savings"))
        assert all(calls.values()), label
        assert _output_sha256(result) == RECEIPTS_GOLDEN["savings"]


@pytest.mark.parametrize("callback,stage", [("_text_sha256", "collect"), ("_json_sha256", "render")])
def test_cycle_receipts_propagate_original_callback_exception(monkeypatch, callback, stage):
    expected = RuntimeError("same original callback failure")

    def fail(value):
        raise expected

    monkeypatch.setattr(exact_final_convergence, callback, fail)
    with pytest.raises(RuntimeError) as caught:
        exact_final_convergence._cycle_prompt_receipts(*_receipts_case("savings"))
    assert caught.value is expected
    names = []
    trace = expected.__traceback__
    while trace is not None:
        names.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert "_cycle_prompt_receipts" in names
    assert stage in names
    assert names[-1] == "fail"
    assert expected.__cause__ is None
    assert expected.__context__ is None


def test_cycle_receipts_preserve_unsupported_json_error():
    with pytest.raises(TypeError, match="is not JSON serializable"):
        exact_final_convergence._cycle_prompt_receipts({"bad": object()}, {})


def test_cycle_receipts_preserve_recursive_input_error():
    history = {}
    history["self"] = history
    try:
        with pytest.raises(RecursionError, match="maximum recursion depth"):
            exact_final_convergence._cycle_prompt_receipts(history, {})
    finally:
        history.clear()
