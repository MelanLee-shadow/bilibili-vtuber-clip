"""Offline contracts for the single Argon representation successor."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import argon_escalation_storage as storage
from src.autoslice.argon_escalation_contract import AudioSpec, ContractError, Finding, KeyPolicy
from src.autoslice.argon_escalation_witness import ArgonEscalationWitness
from src.autoslice.argon_format_recovery import (
    make_authorization,
    predecessor_proof,
    recover,
    validate_successor_envelope,
)
from tests.test_argon_escalation_witness import FakeProvider, case as witness_case, finish
from test_argon_consumer_integration import scene


def _malformed_once(provider):
    changed = {"value": False}

    def mutate(operation):
        output = operation.get("response") or operation.get("metadata", {}).get("output")
        if output and not changed["value"]:
            changed["value"] = True
            payload = output["inlinedResponses"]["inlinedResponses"][0]["response"]
            heard = __import__("json").loads(
                payload["candidates"][0]["content"]["parts"][0]["text"]
            )
            heard["uncertainties"] = [-1]
            payload["candidates"][0]["content"]["parts"][0]["text"] = __import__(
                "json"
            ).dumps(heard)
        return operation

    provider.mutate = mutate


def _authorize(engine, spec, rejected):
    proof = predecessor_proof(engine, rejected, expected=spec)
    return proof, make_authorization(
        proof,
        receipt={"authority": "test-only", "source_message_sha256": "a" * 64},
    )


def test_authorized_format_successor_preserves_predecessor_and_is_cold_idempotent(
    witness_case,
):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    _malformed_once(provider)
    rejected = finish(witness_case, provider)
    assert rejected["status"] == "RESPONSE_REJECTED"
    task = engine.root / handle["task_id"]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in task.rglob("*") if path.is_file()}
    proof, authorization = _authorize(engine, spec, rejected)
    recovered = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
        now=lambda: 100,
    )
    assert recovered["result"]["status"] == "OBSERVED"
    assert recovered["dispatches_this_advance"] == 1
    assert recovered["new_usable_observations_this_recovery"] == 1
    assert len(provider.posts) == 2
    assert provider.posts[0]["body"] == provider.posts[1]["body"]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in task.rglob("*") if path.is_file()} == before
    meta = recovered["format_recovery"]
    child = ArgonEscalationWitness(Path(meta["successor_engine_root"]), read_only=True)
    child_result = child.validate_evidence(meta["successor_task_id"], recovered["result"], expected=spec)
    validate_successor_envelope(meta, result=child_result, expected=spec)
    again = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
        now=lambda: 101,
    )
    assert len(provider.posts) == 2
    assert again["format_recovery"] == meta
    assert again["result"]["result_sha256"] == recovered["result"]["result_sha256"]
    assert again["new_usable_observations_this_recovery"] == 0
    assert proof["ledger_sha256"] == predecessor_proof(engine, rejected, expected=spec)["ledger_sha256"]


def test_second_malformed_successor_is_terminal_and_does_not_rotate(witness_case):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    provider.mutate = lambda operation: _always_malformed(operation)
    rejected = finish(witness_case, provider)
    assert rejected["status"] == "RESPONSE_REJECTED"
    _, authorization = _authorize(engine, spec, rejected)
    recovered = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
    )
    assert recovered["result"]["status"] == "RESPONSE_REJECTED"
    assert len(provider.posts) == 2
    recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
    )
    assert len(provider.posts) == 2


def test_pending_successor_cold_resume_updates_wrapper_without_new_post(witness_case):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    _malformed_once(provider)
    rejected = finish(witness_case, provider)
    _, authorization = _authorize(engine, spec, rejected)
    provider.state = "BATCH_STATE_RUNNING"
    pending = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
    )
    assert pending["result"]["status"] == "PENDING"
    assert pending["new_usable_observations_this_recovery"] == 0
    assert len(provider.posts) == 2
    wrapper_root = engine.root / "format-recovery" / "ledger"
    wrapper_files = list(wrapper_root.glob("*.json"))
    assert len(wrapper_files) == 1
    assert __import__("json").loads(wrapper_files[0].read_bytes())["state"] == "PROCESSING"
    provider.state = "BATCH_STATE_SUCCEEDED"
    resumed = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
    )
    assert resumed["result"]["status"] == "OBSERVED"
    assert resumed["dispatches_this_advance"] == 0
    assert resumed["new_usable_observations_this_recovery"] == 1
    assert len(provider.posts) == 2
    assert __import__("json").loads(wrapper_files[0].read_bytes())["state"] == "COMPLETED"
    replayed = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
    )
    assert replayed["result"]["status"] == "OBSERVED"
    assert replayed["new_usable_observations_this_recovery"] == 0
    assert len(provider.posts) == 2


def _always_malformed(operation):
    if operation.get("response"):
        payload = operation["response"]["inlinedResponses"]["inlinedResponses"][0]["response"]
        heard = __import__("json").loads(payload["candidates"][0]["content"]["parts"][0]["text"])
        heard["uncertainties"] = [-1]
        payload["candidates"][0]["content"]["parts"][0]["text"] = __import__("json").dumps(heard)
    return operation


def _malformed_shape(payload, shape):
    value = deepcopy(payload)
    if shape == "missing_candidates":
        value.pop("candidates", None)
    elif shape == "nonobject_part":
        value["candidates"][0]["content"]["parts"] = [{"text": {}}]
    else:
        raise AssertionError(f"unknown synthetic malformed shape: {shape}")
    return value


def _malformed_shape_once(provider, shape):
    changed = {"value": False}

    def mutate(operation):
        output = operation.get("response") or operation.get("metadata", {}).get("output")
        if output and not changed["value"]:
            changed["value"] = True
            payload = output["inlinedResponses"]["inlinedResponses"][0]["response"]
            output["inlinedResponses"]["inlinedResponses"][0]["response"] = _malformed_shape(
                payload, shape
            )
        return operation

    provider.mutate = mutate


@pytest.mark.parametrize("mode", ["realtime"])
def test_realtime_successor_keeps_original_mode(witness_case, mode):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    malformed = deepcopy(provider.payload)
    heard = __import__("json").loads(
        malformed["candidates"][0]["content"]["parts"][0]["text"]
    )
    heard["uncertainties"] = [-1]
    malformed["candidates"][0]["content"]["parts"][0]["text"] = __import__(
        "json"
    ).dumps(heard)
    provider.payload = malformed
    rejected = engine.advance(
        handle["task_id"], expected=spec, provider=provider,
        key_policy=KeyPolicy(1), mode=mode,
    )
    assert rejected["status"] == "RESPONSE_REJECTED" and rejected["mode"] == mode
    proof, authorization = _authorize(engine, spec, rejected)
    provider.payload = provider.hearing_payload()
    recovered = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
    )
    assert recovered["result"]["status"] == "OBSERVED"
    assert recovered["result"]["mode"] == mode
    assert proof["mode"] == mode
    assert [call["path"] for call in provider.calls if call["method"] == "POST"] == [
        "models/gemini-4-argon:generateContent",
        "models/gemini-4-argon:generateContent",
    ]


@pytest.mark.parametrize(
    ("shape", "reason"),
    [("missing_candidates", "RESPONSE_CANDIDATES_INCOMPLETE"),
     ("nonobject_part", "RESPONSE_PART_INVALID")],
)
@pytest.mark.parametrize("mode", ["batch", "realtime"])
def test_format_successor_rejects_new_malformed_shapes_and_keeps_original_mode(
    witness_case, shape, reason, mode
):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    valid_payload = deepcopy(provider.payload)
    if mode == "batch":
        _malformed_shape_once(provider, shape)
        rejected = finish(witness_case, provider)
    else:
        provider.payload = _malformed_shape(valid_payload, shape)
        rejected = engine.advance(
            handle["task_id"], expected=spec, provider=provider,
            key_policy=KeyPolicy(1), mode="realtime",
        )
    assert rejected["status"] == "RESPONSE_REJECTED"
    assert rejected["reason"] == reason
    proof, authorization = _authorize(engine, spec, rejected)
    assert proof["wire_schema_sha256"] == proof["schema_sha256"]
    provider.payload = valid_payload
    recovered = recover(
        parent_root=engine.root,
        original_engine=engine,
        original_result=rejected,
        expected=spec,
        authorization=authorization,
        provider=provider,
        request_timeout_s=1,
        max_recovery_pages=1,
    )
    assert recovered["result"]["status"] == "OBSERVED"
    assert recovered["result"]["mode"] == mode
    assert len(provider.posts) == 2
    assert [call["path"] for call in provider.posts] == [
        "models/gemini-4-argon:" + ("batchGenerateContent" if mode == "batch" else "generateContent"),
    ] * 2


@pytest.mark.parametrize("reason", ["HEARING_BINDING_MISMATCH", "REMOTE_RESULT_NOT_COMPLETE"])
def test_non_format_or_incomplete_original_cannot_create_successor(witness_case, reason):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    rejected = finish(witness_case, provider)
    rejected = {**rejected, "reason": reason}
    proof = None
    with pytest.raises(ContractError):
        proof = predecessor_proof(engine, rejected, expected=spec)
    assert proof is None
    assert not (engine.root / "format-recovery").exists()
    assert len(provider.posts) == 1


def test_unauthorized_never_writes_or_posts(witness_case):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    _malformed_once(provider)
    rejected = finish(witness_case, provider)
    proof = predecessor_proof(engine, rejected, expected=spec)
    bad = make_authorization(proof, receipt={"authority": "test"})
    bad["authorized"] = False
    with pytest.raises(ContractError):
        recover(
            parent_root=engine.root,
            original_engine=engine,
            original_result=rejected,
            expected=spec,
            authorization=bad,
            provider=provider,
        )
    assert not (engine.root / "format-recovery").exists()
    assert len(provider.posts) == 1


def test_observed_predecessor_never_enters_format_recovery(witness_case):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    observed = finish(witness_case, provider)
    assert observed["status"] == "OBSERVED"
    with pytest.raises(ContractError):
        predecessor_proof(engine, observed, expected=spec)
    assert not (engine.root / "format-recovery").exists()
    assert len(provider.posts) == 1


def test_tampered_predecessor_never_enters_format_recovery(witness_case):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    _malformed_once(provider)
    rejected = finish(witness_case, provider)
    tampered = {**rejected, "result_sha256": "0" * 64}
    with pytest.raises(ContractError):
        predecessor_proof(engine, tampered, expected=spec)
    assert not (engine.root / "format-recovery").exists()
    assert len(provider.posts) == 1


@pytest.mark.parametrize("kind", ["transport_unknown", "pending"])
def test_unknown_or_pending_predecessor_never_enters_format_recovery(witness_case, kind):
    engine, spec, handle, _ = witness_case
    provider = FakeProvider(handle)
    if kind == "transport_unknown":
        provider.post_error = TimeoutError("synthetic unknown")
    result = engine.advance(handle["task_id"], expected=spec, provider=provider, key_policy=KeyPolicy(1))
    assert result["status"] == ("SUBMIT_UNKNOWN" if kind == "transport_unknown" else "PENDING")
    with pytest.raises(ContractError):
        predecessor_proof(engine, result, expected=spec)
    assert not (engine.root / "format-recovery").exists()
    assert len(provider.posts) == 1


def test_ordinary_runtime_reaches_format_successor_and_consumer(scene, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_CPA_GEMINI_ENABLED", "0")
    changed = {"value": False}

    def mutate(operation):
        output = operation.get("response") or operation.get("metadata", {}).get("output")
        if output and not changed["value"]:
            changed["value"] = True
            payload = output["inlinedResponses"]["inlinedResponses"][0]["response"]
            heard = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
            heard["uncertainties"] = [-1]
            payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(heard)
        return operation

    scene.provider.mutate = mutate
    _, pending = scene.run()
    assert pending["argon_escalation"]["status"] == "PENDING"
    _, rejected = scene.run(scene.build())
    assert rejected["argon_escalation"]["status"] == "RESPONSE_REJECTED"
    original_audit = rejected["argon_escalation"]
    original_engine = ArgonEscalationWitness(Path(original_audit["engine_root"]))
    original_task = original_engine.root / original_audit["task_id"]
    original_manifest = storage.get_json(original_task / "manifest.json")
    binding = original_manifest["binding"]
    expected = AudioSpec(
        scene.source,
        Finding(binding["source_media_sha256"], tuple(binding["target_source_ms"]), binding["finding_id"]),
        tuple(binding["context_source_ms"]),
    )
    original_result = {
        **json.loads(storage.get_blob(original_task, original_audit["result_sha256"])),
        "result_sha256": original_audit["result_sha256"],
        "accounting": original_audit["accounting"],
    }
    proof = predecessor_proof(original_engine, original_result, expected=expected)
    authorization = make_authorization(
        proof, receipt={"authority": "test-only", "source_message_sha256": "b" * 64}
    )
    scene.provider.mutate = lambda operation: operation
    output, recovered = scene.run(scene.build(format_recovery_authorization=authorization))
    assert recovered["repaired"]
    assert recovered["argon_escalation"]["new_usable_observations_this_advance"] == 1
    assert recovered["argon_escalation"]["status"] == "OBSERVED"
    assert recovered["request"]["argon_escalation_evidence"]
    assert len([call for call in scene.provider.calls if call["method"] == "POST"]) == 2
    posts = [call for call in scene.provider.calls if call["method"] == "POST"]
    assert posts[0]["body"] == posts[1]["body"]
    assert "一百五十首" in output
    output2, cold = scene.run(scene.build(format_recovery_authorization=authorization))
    assert output2 == output and cold["repaired"]
    assert cold["argon_escalation"]["new_usable_observations_this_advance"] == 0
    assert len([call for call in scene.provider.calls if call["method"] == "POST"]) == 2


def test_ordinary_runtime_consumes_malformed_successor_raw_replay(scene, monkeypatch):
    """A malformed authorized child stays uncertain but reaches the consumer."""
    monkeypatch.setenv("AUTOSLICE_CPA_GEMINI_ENABLED", "0")
    from src.autoslice import argon_escalation_witness as witness_module

    real_create_raw_replay = witness_module.create_raw_replay

    def successor_only_replay(engine, *args, **kwargs):
        # Model the pre-replay predecessor retained by production.  The
        # authorized child is the only task allowed to publish the independent
        # raw-replay sidecar.
        if "format-recovery" in str(engine.root):
            return real_create_raw_replay(engine, *args, **kwargs)
        return None

    monkeypatch.setattr(witness_module, "create_raw_replay", successor_only_replay)

    def malformed(operation):
        output = operation.get("response") or operation.get("metadata", {}).get("output")
        if output:
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

    scene.provider.mutate = malformed
    _, pending = scene.run()
    assert pending["argon_escalation"]["status"] == "PENDING"
    _, rejected = scene.run(scene.build())
    assert rejected["argon_escalation"]["status"] == "RESPONSE_REJECTED"
    assert rejected["argon_escalation"]["raw_replay"] is None
    original_audit = rejected["argon_escalation"]
    original_engine = ArgonEscalationWitness(Path(original_audit["engine_root"]))
    original_task = original_engine.root / original_audit["task_id"]
    original_manifest = storage.get_json(original_task / "manifest.json")
    binding = original_manifest["binding"]
    expected = AudioSpec(
        scene.source,
        Finding(binding["source_media_sha256"], tuple(binding["target_source_ms"]), binding["finding_id"]),
        tuple(binding["context_source_ms"]),
    )
    original_result = {
        **json.loads(storage.get_blob(original_task, original_audit["result_sha256"])),
        "result_sha256": original_audit["result_sha256"],
        "accounting": original_audit["accounting"],
    }
    proof = predecessor_proof(original_engine, original_result, expected=expected)
    authorization = make_authorization(
        proof, receipt={"authority": "test-only", "source_message_sha256": "d" * 64}
    )
    original_files = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in original_task.rglob("*") if path.is_file()
    }

    output, recovered = scene.run(
        scene.build(format_recovery_authorization=authorization)
    )
    assert output == scene.srt
    assert not recovered["repaired"]
    audit = recovered["argon_escalation"]
    assert audit["status"] == "RESPONSE_REJECTED"
    assert audit["advance_operation"] == "RAW_REPLAY"
    assert audit["format_recovery"] is not None
    assert audit["raw_replay"]["status"] == "UNCERTAIN"
    assert audit["raw_replay"]["observation_status"] == "OBSERVED"
    assert audit["raw_replay"]["provider_calls"] == 0
    assert audit["format_recovery"]["status"] == "RESPONSE_REJECTED"
    envelope = recovered["request"]["argon_escalation_evidence"]

    from src.autoslice.argon_escalation_integration import validate_consumer_evidence

    consumer = validate_consumer_evidence(recovered["request"], envelope)
    assert consumer["status"] == "RESPONSE_REJECTED"
    assert consumer["observation_status"] == "OBSERVED"
    assert consumer["hearing"]["status"] == "UNCERTAIN"
    assert len(consumer["hearing"]["uncertainties"]) == 2
    assert consumer["raw_replay"]["provider_calls"] == 0
    assert len([call for call in scene.provider.calls if call["method"] == "POST"]) == 2
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in original_task.rglob("*") if path.is_file()
    } == original_files

    before_posts = len([call for call in scene.provider.calls if call["method"] == "POST"])
    output2, cold = scene.run(scene.build(format_recovery_authorization=authorization))
    assert output2 == output and not cold["repaired"]
    assert cold["argon_escalation"]["advance_operation"] == "RAW_REPLAY"
    assert cold["argon_escalation"]["format_recovery"] is not None
    assert cold["argon_escalation"]["new_usable_observations_this_advance"] == 0
    assert len([call for call in scene.provider.calls if call["method"] == "POST"]) == before_posts == 2

    # The envelope gate must not accept a copied observation flag when its
    # independently sealed sidecar is absent.  This mutates only the pytest
    # temporary successor, never the retained predecessor or production data.
    meta = audit["format_recovery"]
    successor_engine = ArgonEscalationWitness(
        Path(meta["successor_engine_root"]), read_only=True
    )
    successor_task = successor_engine.root / meta["successor_task_id"]
    _, successor_manifest, successor_inner = successor_engine._load(
        meta["successor_task_id"], expected
    )
    successor_result = {
        **json.loads(
            storage.get_blob(successor_task, meta["successor_result_sha256"])
        ),
        "result_sha256": meta["successor_result_sha256"],
        "accounting": successor_engine._accounting(
            successor_task,
            successor_manifest,
            successor_inner,
        ),
    }
    receipt_path = (
        successor_engine.root / meta["successor_task_id"] / "raw-replay" / "receipt.json"
    )
    receipt_path.unlink()
    with pytest.raises(ContractError, match="FORMAT_ENVELOPE_RAW_REPLAY_INVALID"):
        validate_successor_envelope(
            meta,
            result={**successor_result, "raw_replay": audit["raw_replay"]},
            expected=expected,
        )
    with pytest.raises(ContractError, match="FORMAT_ENVELOPE_RECEIPT_METADATA_DRIFT"):
        validate_successor_envelope(
            {**meta, "reason": "OTHER_REJECTION"},
            result=successor_result,
            expected=expected,
        )


def test_targeted_relisten_exhaustion_consumes_authorized_successor_without_reset(
    tmp_path, monkeypatch
):
    """The successor must enter through the ordinary targeted-relisten owner."""
    from src.autoslice import targeted_relisten as policy
    from test_argon_targeted_relisten import attach_argon, run_public
    from test_targeted_relisten_public_contract import (
        build_offline_rig, public_judge, three_observations,
    )

    rig = build_offline_rig(tmp_path, monkeypatch)
    witness, check, finding, _ = three_observations(rig)
    projection = policy.evidence_projection(check, witness=witness)
    assert projection["effective_distinct_observations"] == 3
    material = policy.target_material_exhaustion(check, projection)
    assert material["exhausted"]
    assert material["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"]

    # Keep CPA's normal default enabled.  The injected provider remains an
    # offline transport and AUTOSLICE_BASE already points at this temp scope.
    monkeypatch.delenv("AUTOSLICE_CPA_GEMINI_ENABLED", raising=False)
    monkeypatch.setenv(
        "AUTOSLICE_CPA_GEMINI_LEDGER",
        str(rig.tmp / "base" / "state" / "cpa-gemini" / "requests.jsonl"),
    )
    provider, integration = attach_argon(rig, monkeypatch)
    _malformed_once(provider)
    judge = public_judge(rig)
    # Actual targeted_relisten entry: exhausted observations create the
    # trigger, then the retained Argon batch is submitted.
    _, pending = run_public(rig, finding, judge)
    assert pending["argon_escalation"]["status"] == "PENDING", pending
    assert len([call for call in provider.calls if call["method"] == "POST"]) == 1

    # Ordinary resume reads the original complete response and records the
    # local representation failure without changing the predecessor.
    _, rejected = run_public(rig, finding, judge)
    assert rejected["argon_escalation"]["status"] == "RESPONSE_REJECTED"
    original_audit = rejected["argon_escalation"]
    original_engine = ArgonEscalationWitness(Path(original_audit["engine_root"]))
    original_task = original_engine.root / original_audit["task_id"]
    original_manifest = storage.get_json(original_task / "manifest.json")
    binding = original_manifest["binding"]
    expected = AudioSpec(
        rig.source,
        Finding(binding["source_media_sha256"], tuple(binding["target_source_ms"]), binding["finding_id"]),
        tuple(binding["context_source_ms"]),
    )
    original_result = {
        **json.loads(storage.get_blob(original_task, original_audit["result_sha256"])),
        "result_sha256": original_audit["result_sha256"],
        "accounting": original_audit["accounting"],
    }
    proof = predecessor_proof(original_engine, original_result, expected=expected)
    authorization = make_authorization(
        proof, receipt={"authority": "test-only", "source_message_sha256": "c" * 64}
    )
    original_files = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in original_task.rglob("*") if path.is_file()
    }
    original_accounting = deepcopy(original_audit["accounting"])

    # Same public current input/config; no direct runtime.advance invocation.
    integration.runtime.config["format_recovery_authorization"] = authorization
    provider.mutate = lambda operation: operation
    output, recovered = run_public(rig, finding, judge)
    assert recovered["repaired"] and "我听到了" in output
    assert recovered["argon_escalation"]["new_usable_observations_this_advance"] == 1
    assert recovered["argon_escalation"]["status"] == "OBSERVED"
    assert recovered["argon_escalation"]["format_recovery"]["status"] == "OBSERVED"
    assert len([call for call in provider.calls if call["method"] == "POST"]) == 2
    _, after_manifest, after_inner = original_engine._load(original_task.name, expected)
    assert original_engine._accounting(original_task, after_manifest, after_inner) == original_accounting
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in original_task.rglob("*") if path.is_file()
    } == original_files
    assert policy.target_material_exhaustion(
        check, policy.evidence_projection(check, witness=witness)
    )["exhausted"]

    # Cold public/session read consumes the durable child, never a second POST.
    before_posts = len([call for call in provider.calls if call["method"] == "POST"])
    output2, cold = run_public(rig, finding, judge)
    assert output2 == output and cold["repaired"]
    assert cold["argon_escalation"]["new_usable_observations_this_advance"] == 0
    assert len([call for call in provider.calls if call["method"] == "POST"]) == before_posts == 2


def test_shared_config_preserves_other_rejected_finding_without_slot(scene, monkeypatch):
    from src.autoslice import argon_escalation_runtime as runtime

    monkeypatch.setenv("AUTOSLICE_CPA_GEMINI_ENABLED", "0")
    _malformed_once(scene.provider)
    scene.run()
    _, rejected = scene.run(scene.build())
    audit = rejected["argon_escalation"]
    assert audit["status"] == "RESPONSE_REJECTED"
    engine = ArgonEscalationWitness(Path(audit["engine_root"]))
    manifest = storage.get_json(engine.root / audit["task_id"] / "manifest.json")
    binding = manifest["binding"]
    spec = AudioSpec(scene.source, Finding(binding["source_media_sha256"],
        tuple(binding["target_source_ms"]), binding["finding_id"]), tuple(binding["context_source_ms"]))
    result = {**json.loads(storage.get_blob(engine.root/audit["task_id"], audit["result_sha256"])),
              "result_sha256": audit["result_sha256"], "accounting": audit["accounting"]}
    proof = predecessor_proof(engine, result, expected=spec)
    auth = make_authorization(proof, receipt={"authority": "test-only"})
    # Generate a second original through the same ordinary producer path,
    # then reuse its real authorization in this clip's shared config.
    other_finding = {"cue_index": 2, "suspect": "两", "suggestion": "三",
                     "proposed_full_cue": "还差三个", "repair_class": "phonetic",
                     "why": "another source-bound doubt"}
    _malformed_once(scene.provider)
    scene.run(scene.build(), finding=other_finding)
    _, other_rejected = scene.run(scene.build(), finding=other_finding)
    other_audit = other_rejected["argon_escalation"]
    assert other_audit["status"] == "RESPONSE_REJECTED"
    other_engine = ArgonEscalationWitness(Path(other_audit["engine_root"]))
    other_task = other_engine.root / other_audit["task_id"]
    other_binding = storage.get_json(other_task/"manifest.json")["binding"]
    other_spec = AudioSpec(scene.source, Finding(other_binding["source_media_sha256"],
        tuple(other_binding["target_source_ms"]), other_binding["finding_id"]),
        tuple(other_binding["context_source_ms"]))
    other_result = {**json.loads(storage.get_blob(other_task, other_audit["result_sha256"])),
                   "result_sha256": other_audit["result_sha256"], "accounting": other_audit["accounting"]}
    unrelated = make_authorization(predecessor_proof(other_engine, other_result, expected=other_spec),
                                   receipt={"authority": "test-only other finding"})
    before_calls = len(scene.provider.calls)
    with monkeypatch.context() as isolated:
        def no_slot(**kwargs):
            raise AssertionError("unrelated finding must not enter a recovery slot")
        isolated.setattr(runtime, "runtime_provider_slot", no_slot)
        _, unchanged = scene.run(scene.build(format_recovery_authorization=unrelated))
    assert unchanged["argon_escalation"]["status"] == "RESPONSE_REJECTED"
    assert not unchanged["argon_escalation"].get("format_recovery")
    assert len(scene.provider.calls) == before_calls
    malformed = deepcopy(auth)
    malformed["original"].pop("task_id")
    _, refused = scene.run(scene.build(format_recovery_authorization=malformed))
    assert refused["reason_code"] == "ARGON_INTEGRATION_REJECTED"
    assert refused["detail"] == "FORMAT_AUTH_TARGET_INVALID"
    assert len(scene.provider.calls) == before_calls
    scene.provider.mutate = lambda operation: operation
    _, recovered = scene.run(scene.build(format_recovery_authorization=auth))
    assert recovered["argon_escalation"]["status"] == "OBSERVED"
    assert len([call for call in scene.provider.calls if call["method"] == "POST"]) == 3
