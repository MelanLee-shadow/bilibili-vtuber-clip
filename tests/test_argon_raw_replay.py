"""Zero-dispatch replay of one retained Argon representation failure."""

from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice import argon_escalation_storage as storage
from src.autoslice.argon_escalation_contract import ContractError, canonical, sha
from src.autoslice.argon_escalation_witness import ArgonEscalationWitness
from src.autoslice.argon_raw_replay import project_hearing_document
from test_argon_escalation_witness import FakeProvider, advance, case, finish
from test_argon_consumer_integration import scene


def _flat_payload(provider):
    payload = deepcopy(provider.payload)
    hearing = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
    start, end = hearing["target_window_ms"]
    hearing.update(
        status="UNCERTAIN",
        transcript=None,
        reason="synthetic retained uncertainty",
        uncertainties=[
            "start_ms", start, "end_ms", start + 200, "reason", "first span",
            "start_ms", end - 200, "end_ms", end, "reason", "second span",
        ],
    )
    payload["candidates"][0]["content"]["parts"][0]["text"] = canonical(hearing).decode()
    return payload, hearing


def test_exact_raw_replay_creates_independent_result_without_dispatch(case):
    engine, spec, handle, _ = case
    provider = FakeProvider(handle)
    provider.payload, original_hearing = _flat_payload(provider)
    rejected = finish(case, provider)
    assert rejected["status"] == "RESPONSE_REJECTED"
    assert rejected["reason"] == "HEARING_UNCERTAINTIES_INVALID"
    assert rejected["hearing"] is None
    task = engine.root / handle["task_id"]
    manifest = storage.get_json(task / "manifest.json")
    ledger = engine._ledger(task, manifest, "batch", engine._body(task.name,
        engine._load(task.name, spec)[2], "batch"))
    original_files = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in task.rglob("*") if path.is_file()
    }
    original_ledger_sha = sha(storage.read(ledger.path))
    original_result_raw = storage.get_blob(task, rejected["result_sha256"])
    original_response_raw = storage.get_blob(task, rejected["response_sha256"])
    posts = len(provider.posts)

    resumed = advance(case, provider)
    assert resumed["status"] == "RESPONSE_REJECTED"
    assert resumed["hearing"] is None
    assert resumed["result_sha256"] == rejected["result_sha256"]
    assert resumed["raw_replay"]["status"] == "UNCERTAIN"
    assert resumed["raw_replay"]["observation_status"] == "OBSERVED"
    assert resumed["raw_replay"]["hearing"] == {
        **{key: value for key, value in original_hearing.items() if key != "uncertainties"},
        "uncertainties": [
            {"start_ms": original_hearing["uncertainties"][1],
             "end_ms": original_hearing["uncertainties"][3],
             "reason": original_hearing["uncertainties"][5]},
            {"start_ms": original_hearing["uncertainties"][7],
             "end_ms": original_hearing["uncertainties"][9],
             "reason": original_hearing["uncertainties"][11]},
        ],
    }
    replay = resumed["raw_replay"]
    assert replay["result_sha256"] != rejected["result_sha256"]
    derived_raw = storage.get_blob(task, replay["result_sha256"])
    assert sha(derived_raw) == replay["result_sha256"]
    receipt_raw = storage.read(task / "raw-replay" / "receipt.json")
    assert replay["receipt_sha256"] == sha(receipt_raw)
    assert len(provider.posts) == posts == 1
    assert storage.get_blob(task, rejected["result_sha256"]) == original_result_raw
    assert storage.get_blob(task, rejected["response_sha256"]) == original_response_raw
    assert sha(storage.read(ledger.path)) == original_ledger_sha
    for path, digest in original_files.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    cold = ArgonEscalationWitness(engine.root, read_only=True).read_status(
        handle["task_id"], expected=spec
    )
    assert cold["result"]["status"] == "RESPONSE_REJECTED"
    assert cold["result"]["raw_replay"] == replay
    revalidated = ArgonEscalationWitness(engine.root, read_only=True).validate_evidence(
        handle["task_id"], resumed, expected=spec
    )
    assert revalidated["status"] == "RESPONSE_REJECTED"
    assert revalidated["raw_replay"] == replay
    after_read = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in task.rglob("*") if path.is_file()
    }
    assert after_read[task / "raw-replay" / "receipt.json"] == sha(receipt_raw)


@pytest.mark.parametrize("mutation", [
    "key_order", "float", "bool", "extra", "outside", "binding", "clock", "status",
])
def test_projection_keeps_strict_controls(case, mutation):
    provider = FakeProvider(case[2])
    _, hearing = _flat_payload(provider)
    binding = deepcopy(case[2]["binding"])
    changed = deepcopy(hearing)
    if mutation == "key_order":
        changed["uncertainties"][0] = "end_ms"
    elif mutation == "float":
        changed["uncertainties"][1] = float(changed["uncertainties"][1])
    elif mutation == "bool":
        changed["uncertainties"][1] = True
    elif mutation == "extra":
        changed["uncertainties"].append("extra")
    elif mutation == "outside":
        changed["uncertainties"][1] -= 1
    elif mutation == "binding":
        changed["binding_sha256"] = "0" * 64
    elif mutation == "clock":
        changed["target_window_ms"] = [changed["target_window_ms"][0] + 1,
                                        changed["target_window_ms"][1]]
    else:
        changed["status"] = "OBSERVED"
    with pytest.raises(ContractError):
        project_hearing_document(changed, binding)


def test_other_strict_failure_does_not_create_raw_replay(case):
    engine, spec, handle, _ = case
    provider = FakeProvider(handle)

    def mutate(operation):
        output = operation.get("response") or operation.get("metadata", {}).get("output")
        if output:
            payload = output["inlinedResponses"]["inlinedResponses"][0]["response"]
            payload["candidates"] = []
        return operation

    provider.mutate = mutate
    rejected = finish(case, provider)
    assert rejected["status"] == "RESPONSE_REJECTED"
    assert rejected["reason"] == "RESPONSE_CANDIDATES_INCOMPLETE"
    resumed = advance(case, provider)
    assert resumed["status"] == "RESPONSE_REJECTED"
    assert "raw_replay" not in resumed
    assert len(provider.posts) == 1


def test_readonly_predecessor_without_sidecar_stays_readonly(case, monkeypatch):
    from src.autoslice import argon_escalation_witness as witness_module
    engine, spec, handle, _ = case
    provider = FakeProvider(handle)
    provider.payload, _ = _flat_payload(provider)
    # The ordinary pre-repair writer retained this failed result without a
    # sidecar. A cold consumer cannot grant itself permission to create one.
    with monkeypatch.context() as patch:
        patch.setattr(witness_module, 'create_raw_replay', lambda *a, **k: None)
        original = finish(case, provider)
    task = engine.root / handle['task_id']
    before = {p: p.read_bytes() for p in task.rglob('*') if p.is_file()}
    cold = ArgonEscalationWitness(engine.root, read_only=True).read_status(
        handle['task_id'], expected=spec)
    assert cold['result']['status'] == original['status'] == 'RESPONSE_REJECTED'
    assert 'raw_replay' not in cold['result']
    assert {p: p.read_bytes() for p in task.rglob('*') if p.is_file()} == before


def test_ordinary_runtime_consumes_replay_without_new_provider_request(scene):
    _, pending = scene.run()
    assert pending["argon_escalation"]["status"] == "PENDING"
    changed = {"value": False}

    def mutate(operation):
        output = operation.get("response") or operation.get("metadata", {}).get("output")
        if output and not changed["value"]:
            changed["value"] = True
            payload = output["inlinedResponses"]["inlinedResponses"][0]["response"]
            hearing = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
            start, end = hearing["target_window_ms"]
            hearing.update(
                status="UNCERTAIN",
                transcript=None,
                reason="synthetic retained uncertainty",
                uncertainties=[
                    "start_ms", start, "end_ms", start + 100, "reason", "one",
                    "start_ms", end - 100, "end_ms", end, "reason", "two",
                ],
            )
            payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(hearing)
        return operation

    scene.provider.mutate = mutate
    output, audit = scene.run(scene.build())
    assert output == scene.srt
    assert not audit["repaired"]
    assert audit["argon_escalation"]["status"] == "RESPONSE_REJECTED"
    assert audit["argon_escalation"]["advance_operation"] == "RAW_REPLAY"
    assert audit["argon_escalation"]["raw_replay"]["status"] == "UNCERTAIN"
    assert audit["argon_escalation"]["raw_replay"]["provider_calls"] == 0
    assert [call["method"] for call in scene.provider.calls] == ["POST", "GET"]


def test_raw_replay_passes_real_consumer_validation_as_observed_uncertain(scene):
    scene.run()
    changed = {"value": False}

    def mutate(operation):
        output = operation.get("response") or operation.get("metadata", {}).get("output")
        if output and not changed["value"]:
            changed["value"] = True
            payload = output["inlinedResponses"]["inlinedResponses"][0]["response"]
            hearing = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
            start, end = hearing["target_window_ms"]
            hearing.update(
                status="UNCERTAIN",
                transcript=None,
                reason="synthetic retained uncertainty",
                uncertainties=[
                    "start_ms", start, "end_ms", start + 100, "reason", "one",
                    "start_ms", end - 100, "end_ms", end, "reason", "two",
                ],
            )
            payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(hearing)
        return operation

    scene.provider.mutate = mutate
    _, audit = scene.run(scene.build())
    request = audit["request"]
    envelope = request["argon_escalation_evidence"]
    from src.autoslice.acoustic_witness_adjudication import word_choice_contract
    from src.autoslice.argon_escalation_integration import (
        transcript_witness,
        validate_consumer_evidence,
    )

    consumer = validate_consumer_evidence(request, envelope)
    assert consumer["status"] == "RESPONSE_REJECTED"
    assert consumer["observation_status"] == "OBSERVED"
    assert consumer["hearing"]["status"] == "UNCERTAIN"
    assert len(consumer["hearing"]["uncertainties"]) == 2
    assert consumer["raw_replay"]["provider_calls"] == 0
    witness = transcript_witness(request, envelope)
    assert witness["status"] == "UNCERTAIN"
    contract = word_choice_contract(
        llm_call=lambda _prompt: "",
        check_request=request,
        witness=witness,
    )
    assert "## 新增 Argon 独立盲听证据" in contract.prompt
    assert '"status": "UNCERTAIN"' in contract.prompt
    assert [call["method"] for call in scene.provider.calls] == ["POST", "GET"]
