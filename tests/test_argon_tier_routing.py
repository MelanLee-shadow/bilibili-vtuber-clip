"""Argon key-tier routing and definitive zero-inference refusals (COST-01, SUB-04).

Rules under test (no truth injected; the assertions are the rules):
* 公开规则：先调用AGY订阅，然后用免费key，最后用付费key「付费key不能加冷却期」;
 免费key用完后必须接付费key「这是要求」.
* Google pricing (read): "Gemini 4 Argon is not available on the Free
 tier." models.get visibility on a free key is not an inference entitlement.
* A complete zero-inference HTTP 400 FAILED_PRECONDITION means the route cannot
 serve; INVALID_ARGUMENT / 401 / 403 never walk the key chain; ambiguous
 outcomes are never replayed.
The only provider bytes reused are the real OCI3 refusal (fixture); every
inference answer is a synthetic protocol stub; there is no network.
"""
import json
from pathlib import Path

import pytest

from src.autoslice import argon_escalation_storage as storage
from src.autoslice import argon_model_capability as capability
from src.autoslice import gemini_backup_policy as paid
from src.autoslice.argon_escalation_contract import (
    AMBIGUOUS, NEVER_ADVANCE, ROUTE_UNSERVABLE, classify_provider_rejection, strict_json,
)
from src.autoslice.argon_escalation_defaults import (
    PRODUCTION_ARGON_ESCALATION, apply_production_argon_default,
)
from src.autoslice.argon_escalation_witness import ArgonEscalationWitness, HttpResponse
from tests.argon_admission_support import admission  # noqa: F401  (fixture)
from test_argon_consumer_integration import offline  # noqa: F401  (autouse fixture)
from test_argon_escalation_witness import case  # noqa: F401  (fixture)

REAL_TIER_TABLE = dict(capability.TIER_INFERENCE)
REFUSAL = json.loads((Path(__file__).parent / "fixtures/argon_free_batch_refusal_20261021.json").read_text())
OCI3_400 = REFUSAL["body"].encode()


def real_tiers(monkeypatch):
    monkeypatch.setattr(capability, "TIER_INFERENCE", REAL_TIER_TABLE)


def refuse(state, tier, method, status, body):
    original = state.provider.request

    def request(http_method, path, body_bytes, *, route, timeout_s):
        result = original(http_method, path, body_bytes, route=route, timeout_s=timeout_s)
        if (http_method == "POST" and route["accepted_key_tier"] == tier
                and path.endswith(":" + method)):
            return HttpResponse(status, body)
        return result
    state.provider.request = request


def posts(state):
    return [(c["route"]["accepted_key_tier"], c["path"].rsplit(":", 1)[-1])
            for c in state.provider.calls if c["method"] == "POST"]


def lane_task(state, lane):
    handle = storage.get_json(state.root / f"attempt-{lane}/handle.json")
    engine = ArgonEscalationWitness(state.root / f"attempt-{lane}/engine")
    task, manifest, inner = engine._load(handle["task_id"], state.spec)
    return engine, task, manifest, inner


def test_shared_classifier_reads_the_retained_refusal_and_keeps_the_hard_guards():
    assert REFUSAL["raw_sha256"] == "47b64e5fa6fa3d963f6a2034dcadab8958b60afa6126cb594e7927904c6a515d"
    oci3 = classify_provider_rejection(400, OCI3_400, response_complete=True)
    assert (oci3["class"], oci3["basis"], oci3["provider_status"]) == (
        ROUTE_UNSERVABLE, "ZERO_INFERENCE_FAILED_PRECONDITION", "FAILED_PRECONDITION")
    assert oci3["detail_reasons"] == []  # The provider gave no details[].reason.
    invalid = b'{"error":{"code":400,"message":"bad","status":"INVALID_ARGUMENT"}}'
    assert classify_provider_rejection(400, invalid, response_complete=True)["class"] == NEVER_ADVANCE
    for status, name in ((401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")):
        raw = json.dumps({"error": {"code": status, "status": name}}).encode()
        assert classify_provider_rejection(status, raw, response_complete=True)["class"] == NEVER_ADVANCE
    # Not provably zero-inference: extra payload beside the error object.
    mixed = b'{"error":{"code":400,"status":"FAILED_PRECONDITION"},"usageMetadata":{"totalTokenCount":9}}'
    assert classify_provider_rejection(400, mixed, response_complete=True)["class"] == NEVER_ADVANCE
    assert classify_provider_rejection(400, OCI3_400, response_complete=False)["class"] == AMBIGUOUS
    assert classify_provider_rejection(503, b'{"error":{"code":503}}', response_complete=True)["class"] == AMBIGUOUS
    assert classify_provider_rejection(429, b"", response_complete=True)["class"] == ROUTE_UNSERVABLE


def test_production_argon_skips_the_free_tier_and_admits_paid_with_its_ledger(admission, monkeypatch):
    s = admission
    real_tiers(monkeypatch)
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "0")
    first = s.advance()
    assert first["result"]["status"] == "PENDING" and first["dispatches_this_advance"] == 1
    assert posts(s) == [("paid_backup", "batchGenerateContent")]
    assert {t["tier"] for t in s.provider.transports} == {"paid_backup"}
    stamp = s.provider.calls[-1]["route"]["paid_backup_policy"]
    assert stamp["mode"] == paid.FREE_CHAIN_UNSERVABLE_MODE and stamp["daily_cap"] is None
    assert paid.paid_calls_today() == 1
    _, task, _, _ = lane_task(s, 1)
    assert paid.free_chain_strikes(task.name) == 0  # No free round was run or invented.
    receipt = strict_json(storage.get_blob(task, storage.get_json(task / "capability-api-batch.json")["receipt_sha256"]))
    assert receipt["binding"]["schema_version"] == capability.API_SCHEMA
    assert receipt["binding"]["tier_inference"] == {
        "tier": "paid_backup", "status": "AVAILABLE", "basis": capability.TIER_INFERENCE_BASIS}
    evidence = storage.get_json(s.root / "attempt-1/paid-policy-batch.intent.json")
    assert evidence["basis"] == "FREE_TIER_UNAVAILABLE_FOR_MODEL"
    second = s.advance()
    assert second["result"]["status"] == "OBSERVED" and second["dispatches_this_advance"] == 0
    engine, task, _, _ = lane_task(s, 1)
    assert engine.validate_evidence(task.name, second["result"], expected=s.spec)["status"] == "OBSERVED"


def test_metadata_on_a_free_key_never_admits_free_inference(admission, monkeypatch):
    s = admission
    real_tiers(monkeypatch)
    route = {"configured_key_count": 2, "accepted_key_ordinal": 1, "accepted_key_tier": "free"}
    binding = {"schema_version": capability.API_SCHEMA, "kind": "api", "required_method": "batchGenerateContent",
               "route": route, "tier_inference": capability.tier_inference("free")}
    full = json.dumps({"name": "models/gemini-4-argon",
                       "supportedGenerationMethods": ["generateContent", "batchGenerateContent"]}).encode()
    assert capability.classify(binding, {"complete": True, "http_status": 200}, full) == (
        "UNSUPPORTED", "ARGON_API_TIER_UNAVAILABLE")


def test_retained_free_lane_refusal_moves_to_paid_in_the_same_call(admission, monkeypatch):
    s = admission
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "0")
    # The lane the deployed 69eb9ae6 version wrote: free key 1, batch submit
    # refused with the real OCI3 bytes, and no route marker at all.
    refuse(s, "free", "batchGenerateContent", 400, OCI3_400)
    # The deployed version wrote metadata-only v1 capability receipts.
    import functools
    current_binding = capability.api_binding
    monkeypatch.setattr(capability, "api_binding",
                        functools.partial(current_binding, schema_version=capability.SCHEMA))
    first = s.advance()
    assert first["result"]["status"] == "ROUTE_UNSERVABLE_NEXT_ROUTE_PENDING"
    assert first["dispatches_this_advance"] == 1
    _, lane1, _, _ = lane_task(s, 1)
    v1 = strict_json(storage.get_blob(lane1, storage.get_json(lane1 / "capability-api-batch.json")["receipt_sha256"]))
    assert v1["binding"]["schema_version"] == capability.SCHEMA and "tier_inference" not in v1["binding"]
    monkeypatch.setattr(capability, "api_binding", current_binding)
    (s.root / "attempt-1/route-unservable.json").unlink()
    real_tiers(monkeypatch)
    s.config["allow_realtime_fallback"] = True
    second = s.advance()
    assert posts(s) == [("free", "batchGenerateContent"), ("paid_backup", "batchGenerateContent")]
    assert second["dispatches_this_advance"] == 1 and second["result"]["status"] == "PENDING"
    marker = storage.get_json(s.root / "attempt-1/route-unservable.json")
    assert marker["response_sha256"] == REFUSAL["raw_sha256"]
    assert marker["classification"]["basis"] == "ZERO_INFERENCE_FAILED_PRECONDITION"
    assert [a["classification"]["class"] for a in second["prior_key_attempts"]] == [ROUTE_UNSERVABLE]
    assert s.provider.calls[-1]["route"]["paid_backup_policy"]["mode"] == paid.FREE_CHAIN_UNSERVABLE_MODE
    assert paid.paid_calls_today() == 1
    third = s.advance()
    assert third["result"]["status"] == "OBSERVED"
    assert len(posts(s)) == 2  # No free realtime, no replay of the refused request.
    # The retained v1 lane still replays under its own metadata-only contract.
    engine1, lane1, _, _ = lane_task(s, 1)
    meta = engine1._dispatch_metadata(engine1._ledger(lane1, *lane_task(s, 1)[2:3],
        "batch", engine1._body(lane1.name, lane_task(s, 1)[3], "batch")).snapshot())
    assert capability.validated_api_receipt(lane1, meta["request_sha256"], "batch", meta["route"],
        expected_sha=meta["capability_receipt_sha256"])["status"] == "SUPPORTED"


@pytest.mark.parametrize("status,body", [
    (400, b'{"error":{"code":400,"message":"bad body","status":"INVALID_ARGUMENT"}}'),
    (401, b'{"error":{"code":401,"status":"UNAUTHENTICATED"}}'),
    (403, b'{"error":{"code":403,"status":"PERMISSION_DENIED"}}'),
])
def test_request_or_credential_refusal_never_walks_the_key_chain(admission, monkeypatch, status, body):
    s = admission  # A modelled free-tier route, so the free ladder itself is exercised.
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1")
    refuse(s, "free", "batchGenerateContent", status, body)
    first, second = s.advance(), s.advance()
    assert first["result"]["status"] == second["result"]["status"] == "FAILED"
    assert posts(s) == [("free", "batchGenerateContent")]
    assert not list(s.root.rglob("route-unservable.json")) and not list(s.root.rglob("quota-rejected.json"))
    assert paid.paid_calls_today() == 0


def test_paid_batch_precondition_tries_the_same_paid_key_realtime_once(admission, monkeypatch):
    s = admission
    real_tiers(monkeypatch)
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "0")
    s.config["allow_realtime_fallback"] = True
    refuse(s, "paid_backup", "batchGenerateContent", 400, OCI3_400)
    first = s.advance()
    assert first["result"]["status"] == "ROUTE_UNSERVABLE_SAME_ROUTE_REALTIME_PENDING"
    second = s.advance()
    assert posts(s) == [("paid_backup", "batchGenerateContent"), ("paid_backup", "generateContent")]
    assert second["result"]["status"] == "OBSERVED"
    stamps = [c["route"]["paid_backup_policy"] for c in s.provider.calls if c["method"] == "POST"]
    assert stamps[0] != stamps[1] and paid.paid_calls_today() == 2  # Each physical paid request ledgered.
    third = s.advance()
    assert third["result"]["status"] == "OBSERVED" and len(posts(s)) == 2


def test_production_default_carries_cost01_and_keeps_explicit_values():
    assert PRODUCTION_ARGON_ESCALATION == {"enabled": True, "allow_paid": True, "allow_realtime_fallback": True}
    bare = {"argon_escalation": {"enabled": True}}
    assert apply_production_argon_default(bare)
    assert bare["argon_escalation"] == dict(PRODUCTION_ARGON_ESCALATION)
    explicit = {"argon_escalation": {"enabled": True, "allow_paid": False}}
    apply_production_argon_default(explicit)
    assert explicit["argon_escalation"]["allow_paid"] is False
    off = {"argon_escalation": {"enabled": False}}
    assert not apply_production_argon_default(off) and off["argon_escalation"] == {"enabled": False}
    comparison = {"argon_escalation": {"enabled": True, "comparison_plan": "/p"}}
    assert not apply_production_argon_default(comparison)
    assert comparison["argon_escalation"] == {"enabled": True, "comparison_plan": "/p"}


def consumer_replay(state, outcome, engine_root=None):
    """The ordinary consumer's read-only replay at its source-bound batch root."""
    engine = ArgonEscalationWitness(Path(engine_root or outcome["engine_root"]), read_only=True)
    return engine.validate_evidence(outcome["result"]["task_id"], outcome["result"],
                                    expected=state.spec, acquisition_root=state.root)


@pytest.mark.parametrize("geometry", ["free_tier_closed", "free_lane_refused"])
def test_consumer_locates_the_paid_lane_by_the_route_chain_not_the_key_ordinal(
        admission, monkeypatch, tmp_path, geometry):
    """41 Argon (COST-01, SUB-04): a retained paid hearing is the escalation's
    terminal evidence. The runtime opens a new lane only after a create-only
    route refusal, so the paid key ordinal (free count + 1) is not a lane
    index; the consumer must replay the lane the dispatch actually lives in
    (OCI3 e9c50824: paid ordinal 4 in attempt-2 -> FileNotFoundError attempt-4).
    """
    import shutil
    s = admission
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "0")
    if geometry == "free_tier_closed":
        real_tiers(monkeypatch)
        expected_lane = 1
    else:
        refuse(s, "free", "batchGenerateContent", 400, OCI3_400)
        assert s.advance()["result"]["status"] == "ROUTE_UNSERVABLE_NEXT_ROUTE_PENDING"
        real_tiers(monkeypatch)
        expected_lane = 2
    s.advance()
    done = s.advance()
    assert done["result"]["status"] == "OBSERVED"
    paid_route = [c["route"] for c in s.provider.calls if c["method"] == "POST"][-1]
    assert paid_route["accepted_key_tier"] == "paid_backup"
    assert paid_route["accepted_key_ordinal"] == paid_route["configured_key_count"] + 1
    assert paid_route["accepted_key_ordinal"] != expected_lane  # The geometry under test.
    assert Path(done["engine_root"]) == s.root / f"attempt-{expected_lane}" / "engine"
    assert storage.serving_lane(s.root) == expected_lane
    calls = len(s.provider.calls)
    replayed = consumer_replay(s, done)
    assert replayed["status"] == "OBSERVED" and replayed["hearing"] == done["result"]["hearing"]
    # A legally retained relocated copy still binds to that original lane.
    copy = tmp_path / "retained-copy" / "engine"
    shutil.copytree(done["engine_root"], copy)
    assert consumer_replay(s, done, engine_root=copy)["status"] == "OBSERVED"
    assert len(s.provider.calls) == calls  # Read-only replay: no provider request.
